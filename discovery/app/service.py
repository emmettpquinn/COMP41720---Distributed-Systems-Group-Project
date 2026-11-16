from datetime import datetime, timezone
import math
from typing import Literal
from pydantic import BaseModel, Field, model_validator
from .dates import DateFilter, parse_query
from .embeddings import Unavailable
from . import metrics


class EventChange(BaseModel):
    event_id: str = Field(min_length=1, max_length=200)
    event_version: int = Field(ge=1)
    action: Literal['created', 'updated', 'cancelled']
    title: str = Field(default='', max_length=500)
    description: str = Field(default='', max_length=8000)
    tags: list[str] = Field(default_factory=list, max_length=50)
    starts_at: datetime | None = None

    @model_validator(mode='before')
    @classmethod
    def accept_catalogue_change(cls, value):
        # Catalogue C01 uses change; retain action for existing demo messages.
        if not isinstance(value, dict):
            return value
        value = dict(value)
        if 'change' in value:
            if 'action' in value and value['action'] != value['change']:
                raise ValueError('change and action disagree')
            value['action'] = value['change']
        if 'status' in value:
            expected = 'cancelled' if value.get('action') == 'cancelled' else 'scheduled'
            if value['status'] != expected:
                raise ValueError('Event status disagrees with change')
        return value

    @model_validator(mode='after')
    def validate_event(self):
        if any(len(tag) > 200 for tag in self.tags):
            raise ValueError('Tag exceeds 200 characters')
        if self.action != 'cancelled' and (not self.title or not self.starts_at or self.starts_at.tzinfo is None):
            raise ValueError('An active event needs title and timezone-aware starts_at')
        return self


def cosine(a, b):
    if len(a) != len(b):
        raise ValueError('Cannot compare vectors from different dimensions')
    denominator = math.sqrt(sum(float(x)**2 for x in a) * sum(float(y)**2 for y in b))
    return sum(float(x)*float(y) for x, y in zip(a, b)) / denominator if denominator else 0.0


class Discovery:
    def __init__(self, store, ollama, timezone_name='Europe/Dublin'):
        self.store, self.ollama, self.timezone = store, ollama, timezone_name

    async def handle(self, envelope):
        if envelope.type != 'event.changed' or envelope.version != 1:
            raise ValueError('Expected event.changed with envelope schema version 1')
        if not envelope.message_id or len(envelope.message_id) > 200:
            raise ValueError('Invalid message ID')
        occurred = datetime.fromisoformat(envelope.occurred_at.replace('Z', '+00:00'))
        if occurred.tzinfo is None:
            raise ValueError('occurred_at must include timezone')
        return await self.store.apply(envelope, EventChange.model_validate(envelope.payload))

    async def index_pending(self):
        count = 0
        for index, event in await self.store.pending():
            try:
                if index.model_version == 'unverified-local-tag':
                    revision = await self.ollama.revision(index.model)
                    index = await self.store.bind_revision(index.version, revision)
                vector = await self.ollama.embed(event.document(), index.model, index.dimensions, 'indexing', expected_revision=index.model_version)
                count += bool(await self.store.save_embedding(index, event, vector))
            except Unavailable:
                # Durable missing entry is the retry job; broker message is already safe.
                # Continue so an unavailable candidate model cannot starve other indexes.
                continue

        return count

    async def search(self, query, limit=10, now=None, exclude=None):
        date_filter = parse_query(query, now or datetime.now(timezone.utc), self.timezone)
        return await self._rank(date_filter, limit, exclude)

    async def similar(self, event, limit=10):
        date_filter = DateFilter(event.document(), datetime.now(timezone.utc), None, None, False, self.timezone)
        return await self._rank(date_filter, limit, event.id)

    async def _rank(self, date_filter, limit, exclude):
        index, events, entries, hits, terms = await self.store.snapshot(date_filter)
        events = [e for e in events if e.id != exclude]
        mode, reason, vector = 'keyword_fallback', 'no_semantic_text', None
        if date_filter.semantic_text:
            try:
                if index.model_version == 'unverified-local-tag':
                    raise Unavailable('model_not_initialized')
                vector = await self.ollama.embed(date_filter.semantic_text, index.model, index.dimensions, expected_revision=index.model_version)
                if all(e.id in entries and entries[e.id].event_version == e.version for e in events):
                    mode, reason = 'semantic', None
                else:
                    reason = 'index_catching_up'
            except Unavailable as exc:
                reason = str(exc)
        ranked = []
        scores = {}
        if mode == 'semantic':
            scores = await self.store.semantic_scores(index, vector, events, entries)
            if len(scores) != len(events):
                mode, reason = 'keyword_fallback', 'index_changed_during_search'
        for event in events:
            entry = entries.get(event.id)
            if mode == 'semantic':
                score = scores[event.id]
            else:
                score = hits.get(event.id, 0)
                if terms and not score:
                    continue
            ranked.append((score, event, entry))
        ranked.sort(key=lambda row: (-row[0], row[1].starts_at, row[1].id))
        metrics.SEARCHES.labels(mode).inc()
        return {'mode': mode, 'label': 'Showing keyword results' if mode == 'keyword_fallback' else 'Showing semantic results',
                'fallback_reason': reason, 'filter': date_filter.as_dict(),
                'index_version': index.version, 'model': index.model, 'model_version': index.model_version,
                'results': [{**event.public(), 'score': score,
                             'indexed_at': datetime.fromtimestamp(entry.indexed_at, timezone.utc).isoformat() if entry else None}
                            for score, event, entry in ranked[:limit]]}
