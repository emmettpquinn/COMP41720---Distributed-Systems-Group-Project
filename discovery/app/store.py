"""Owned projection, deduplication records and versioned embeddings.

The state row serializes writes across PostgreSQL processes. The local lock also
makes the SQLite learning demo safe within its single process. Model calls happen
outside transactions; save_embedding rechecks the event version afterwards.
"""
import asyncio
from datetime import datetime, timezone
import re
from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, String, Text, delete, select, text, func, true, or_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, defer
from pgvector.sqlalchemy import Vector
from . import metrics


def words(text_value):
    return set(re.findall(r'[a-z0-9]+', text_value.lower()))


class Base(DeclarativeBase):
    pass


class State(Base):
    __tablename__ = 'discovery_state'
    id: Mapped[int] = mapped_column(primary_key=True)
    active: Mapped[str] = mapped_column(String(100))


class Index(Base):
    __tablename__ = 'discovery_indexes'
    version: Mapped[str] = mapped_column(String(100), primary_key=True)
    model: Mapped[str] = mapped_column(String(200))
    model_version: Mapped[str] = mapped_column(String(200))
    dimensions: Mapped[int] = mapped_column(Integer)


class Event(Base):
    __tablename__ = 'discovery_events'
    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    version: Mapped[int] = mapped_column(Integer)
    cancelled: Mapped[bool] = mapped_column(Boolean)
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)
    tags: Mapped[list] = mapped_column(JSON)
    starts_at: Mapped[float] = mapped_column(Float, index=True)
    occurred_at: Mapped[float] = mapped_column(Float)

    def document(self):
        return ' '.join([self.title, self.description, *self.tags])

    def public(self):
        return {'event_id': self.id, 'event_version': self.version, 'title': self.title,
                'description': self.description, 'tags': self.tags,
                'starts_at': datetime.fromtimestamp(self.starts_at, timezone.utc).isoformat()}


class Keyword(Base):
    __tablename__ = 'discovery_keywords'
    word: Mapped[str] = mapped_column(String(200), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey('discovery_events.id'), primary_key=True)


class Entry(Base):
    __tablename__ = 'discovery_embeddings'
    index_version: Mapped[str] = mapped_column(ForeignKey('discovery_indexes.version'), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey('discovery_events.id'), primary_key=True)
    event_version: Mapped[int] = mapped_column(Integer)
    embedding: Mapped[list] = mapped_column(Vector().with_variant(JSON(), 'sqlite'))
    indexed_at: Mapped[float] = mapped_column(Float)


class Processed(Base):
    __tablename__ = 'discovery_processed_messages'
    message_id: Mapped[str] = mapped_column(String(200), primary_key=True)


class Store:
    def __init__(self, url):
        self.engine = create_async_engine(url)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.lock = asyncio.Lock()
        self.job_offset = 0

    async def initialize(self, version='v1', model='nomic-embed-text:latest', model_version='unverified-local-tag', dimensions=768):
        async with self.engine.begin() as connection:
            if self.engine.dialect.name == 'postgresql':
                # Serializes schema/bootstrap only; no model call holds this lock.
                await connection.execute(text('SELECT pg_advisory_xact_lock(41720003)'))
                await connection.execute(text('CREATE EXTENSION IF NOT EXISTS vector'))
            await connection.run_sync(Base.metadata.create_all)
            async with AsyncSession(bind=connection) as session:
                if not await session.get(State, 1):
                    session.add(Index(version=version, model=model, model_version=model_version, dimensions=dimensions))
                    session.add(State(id=1, active=version))
                    await session.flush()
        metrics.ACTIVE.labels((await self.active()).version).set(1)

    async def _state(self, session):
        return (await session.execute(select(State).where(State.id == 1).with_for_update())).scalar_one()

    async def apply(self, envelope, payload):
        async with self.lock, self.sessions.begin() as session:
            await self._state(session)
            if await session.get(Processed, envelope.message_id):
                metrics.MESSAGES.labels('duplicate').inc()
                return 'duplicate'
            previous = await session.get(Event, payload.event_id)
            session.add(Processed(message_id=envelope.message_id))
            if previous and previous.version >= payload.event_version:
                metrics.MESSAGES.labels('stale').inc()
                return 'stale'
            event = previous or Event(id=payload.event_id)
            event.version, event.cancelled = payload.event_version, payload.action == 'cancelled'
            event.title, event.description, event.tags = payload.title, payload.description, payload.tags
            event.starts_at = payload.starts_at.timestamp() if payload.starts_at else 0
            event.occurred_at = datetime.fromisoformat(envelope.occurred_at.replace('Z', '+00:00')).timestamp()
            session.add(event)
            await session.flush()
            await session.execute(delete(Keyword).where(Keyword.event_id == event.id))
            if not event.cancelled:
                session.add_all(Keyword(word=w, event_id=event.id) for w in words(event.document()) if len(w) <= 200)
            # Keep cancellation tombstones so an older update cannot resurrect an event.
            await session.execute(delete(Entry).where(Entry.event_id == event.id))
        metrics.MESSAGES.labels('applied').inc()
        return 'applied'

    async def active(self):
        async with self.sessions() as session:
            state = await session.get(State, 1)
            return await session.get(Index, state.active)

    async def snapshot(self, date_filter):
        async with self.sessions() as session:
            index = await session.get(Index, (await session.get(State, 1)).active)
            query = select(Event).where(Event.cancelled.is_(False), Event.starts_at >= date_filter.start.timestamp())
            if date_filter.end:
                query = query.where(Event.starts_at < date_filter.end.timestamp())
            events = [e for e in (await session.scalars(query)).all()
                      if date_filter.matches(datetime.fromtimestamp(e.starts_at, timezone.utc))]
            entry_query = select(Entry).where(Entry.index_version == index.version,
                                              Entry.event_id.in_([e.id for e in events]))
            if self.engine.dialect.name == 'postgresql':
                entry_query = entry_query.options(defer(Entry.embedding))
            entries = {e.event_id: e for e in (await session.scalars(entry_query)).all()}
            terms = words(date_filter.semantic_text) - {'and', 'or', 'of', 'for', 'to'}
            hits = {}
            if terms:
                for word, event_id in (await session.execute(select(Keyword.word, Keyword.event_id).where(Keyword.word.in_(terms)))).all():
                    hits[event_id] = hits.get(event_id, 0) + 1
            return index, events, entries, hits, terms

    async def semantic_scores(self, index, vector, events, entries):
        if self.engine.dialect.name != 'postgresql':
            from .service import cosine
            return {e.id: cosine(vector, entries[e.id].embedding) for e in events}
        # PostgreSQL computes exact cosine distance; vectors never leave the DB.
        async with self.sessions() as session:
            rows = (await session.execute(select(Entry.event_id, Entry.event_version,
                (1 - Entry.embedding.cosine_distance(vector)).label('score')).where(
                    Entry.index_version == index.version, Entry.event_id.in_([e.id for e in events])))).all()
            versions = {e.id: e.version for e in events}
            return {event_id: float(score) for event_id, version, score in rows if versions.get(event_id) == version}

    async def event(self, event_id):
        async with self.sessions() as session:
            return await session.get(Event, event_id)

    async def pending(self, limit=16):
        # Includes retained indexes: rollback remains current after catalogue edits.
        async with self.sessions() as session:
            query = select(Index, Event).select_from(Index).join(Event, true()).outerjoin(Entry,
                (Entry.index_version == Index.version) & (Entry.event_id == Event.id)
            ).where(Event.cancelled.is_(False), or_(Entry.event_id.is_(None), Entry.event_version != Event.version))
            count = await session.scalar(select(func.count()).select_from(query.subquery()))
            metrics.PENDING.set(count)
            if not count:
                return []
            offset = self.job_offset % count
            self.job_offset += limit
            return (await session.execute(query.order_by(Index.version, Event.occurred_at, Event.id).offset(offset).limit(limit))).all()

    async def save_embedding(self, index, event, vector):
        async with self.lock, self.sessions.begin() as session:
            await self._state(session)
            current = await session.get(Event, event.id)
            if not current or current.cancelled or current.version != event.version:
                return False
            await session.merge(Entry(index_version=index.version, event_id=event.id,
                                      event_version=event.version, embedding=vector,
                                      indexed_at=datetime.now(timezone.utc).timestamp()))
        metrics.LAG.observe(max(0, datetime.now(timezone.utc).timestamp() - event.occurred_at))
        return True

    async def add_index(self, version, model, model_version, dimensions):
        async with self.lock, self.sessions.begin() as session:
            await self._state(session)
            if await session.get(Index, version):
                raise ValueError('Index version already exists; versions are immutable.')
            session.add(Index(version=version, model=model, model_version=model_version, dimensions=dimensions))

    async def bind_revision(self, version, revision):
        async with self.lock, self.sessions.begin() as session:
            await self._state(session)
            index = await session.get(Index, version)
            if index.model_version == 'unverified-local-tag':
                index.model_version = revision
            return index

    async def activate(self, version):
        async with self.lock, self.sessions.begin() as session:
            state = await self._state(session)
            if not await session.get(Index, version):
                raise ValueError('Unknown index version')
            missing = await session.scalar(select(Event.id).outerjoin(Entry,
                (Entry.event_id == Event.id) & (Entry.index_version == version) & (Entry.event_version == Event.version)
            ).where(Event.cancelled.is_(False), Entry.event_id.is_(None)).limit(1))
            if missing:
                raise ValueError('Index is incomplete; wait for pending embeddings before switching.')
            old = state.active
            state.active = version
        metrics.ACTIVE.labels(old).set(0)
        metrics.ACTIVE.labels(version).set(1)

    async def close(self):
        await self.engine.dispose()

    async def index_status(self):
        async with self.sessions() as session:
            state = await session.get(State, 1)
            events = {e.id: e.version for e in (await session.scalars(select(Event).where(Event.cancelled.is_(False)))).all()}
            entries = (await session.execute(select(Entry.index_version, Entry.event_id, Entry.event_version))).all()
            indexes = (await session.scalars(select(Index))).all()
            return [{'index_version': i.version, 'model': i.model, 'model_version': i.model_version,
                     'dimensions': i.dimensions, 'active': state.active == i.version,
                     'pending': len(events) - sum(e.index_version == i.version and events.get(e.event_id) == e.event_version for e in entries)}
                    for i in indexes]
