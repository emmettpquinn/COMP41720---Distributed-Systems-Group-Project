"""Compatibility with Catalogue C01 examples at commit 7b9524c."""
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from messaging.envelope import Envelope
from discovery.app.service import EventChange

FIXTURES = Path(__file__).parent / 'fixtures' / 'catalogue'


def message(name):
    return Envelope.from_json((FIXTURES / f'event-{name}.json').read_bytes())


async def test_catalogue_create_update_cancel_and_stale_delivery(service):
    created, updated, cancelled = [message(name) for name in ('created', 'updated', 'cancelled')]
    now = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)
    for envelope in (created, updated):
        assert await service.handle(envelope) == 'applied'
        await service.index_pending()
        result = await service.search('music', now=now)
        assert result['mode'] == 'semantic'
        assert result['results'][0]['event_id'] == envelope.payload['event_id']
        assert result['results'][0]['title'] == envelope.payload['title']
    assert await service.handle(updated) == 'duplicate'
    assert await service.handle(cancelled) == 'applied'
    assert await service.handle(replace(created, message_id='late-created')) == 'stale'
    assert (await service.search('music', now=now))['results'] == []
    assert (await service.store.event(created.payload['event_id'])).cancelled


@pytest.mark.parametrize('extra', [
    {'action': 'cancelled'},
    {'status': 'cancelled'},
    {'change': 'deleted'},
])
def test_inconsistent_catalogue_payload_is_rejected(extra):
    with pytest.raises(ValueError):
        EventChange.model_validate({**message('created').payload, **extra})
