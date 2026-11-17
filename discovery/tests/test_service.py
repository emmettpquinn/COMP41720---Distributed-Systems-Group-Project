from dataclasses import replace
from datetime import datetime, timezone
import pytest
from discovery.app.seed import demo_messages
from discovery.app.store import Entry
from sqlalchemy import select

NOW = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)


async def seed(service):
    messages = demo_messages(NOW)
    for message in messages:
        await service.handle(message)
    return messages


async def test_semantic_and_fallback_enforce_the_same_dates(service):
    await seed(service)
    await service.index_pending()
    result = await service.search('calm music on Friday night', now=NOW)
    assert result['mode'] == 'semantic'
    assert {r['event_id'] for r in result['results']} == {'demo-jazz', 'demo-rock'}
    assert result['results'][0]['event_id'] == 'demo-jazz'
    service.ollama.failing = True
    fallback = await service.search('calm music on Friday night', now=NOW)
    assert fallback['mode'] == 'keyword_fallback'
    assert fallback['label'] == 'Showing keyword results'
    assert {r['event_id'] for r in fallback['results']} == {'demo-jazz', 'demo-rock'}


async def test_keywords_available_before_embedding_and_recover(service):
    service.ollama.failing = True
    await seed(service)
    assert await service.index_pending() == 0
    result = await service.search('music on Friday night', now=NOW)
    assert len(result['results']) == 2
    service.ollama.failing = False
    assert await service.index_pending() == 4
    assert (await service.search('music Friday night', now=NOW))['mode'] == 'semantic'


async def test_duplicates_stale_messages_and_cancellation_tombstone(service):
    message = (await seed(service))[0]
    assert await service.handle(message) == 'duplicate'
    cancel = replace(message, message_id='cancel', payload={'event_id': 'demo-jazz', 'event_version': 3, 'action': 'cancelled'})
    assert await service.handle(cancel) == 'applied'
    assert await service.handle(replace(message, message_id='old-delivery')) == 'stale'
    assert (await service.store.event('demo-jazz')).cancelled
    result = await service.search('music Friday night', now=NOW)
    assert 'demo-jazz' not in {r['event_id'] for r in result['results']}


async def test_event_update_while_model_is_running_cannot_store_stale_embedding(service):
    messages = await seed(service)
    index, event = next((i, e) for i, e in await service.store.pending() if e.id == 'demo-jazz')
    update = replace(messages[0], message_id='newer', payload={**messages[0].payload, 'event_version': 2, 'title': 'Changed title'})
    await service.handle(update)
    assert not await service.store.save_embedding(index, event, [1.0] * 64)


async def test_version_switch_blocks_incomplete_and_supports_rollback(service):
    messages = await seed(service)
    await service.index_pending()
    await service.store.add_index('v2', 'different-simulator', 'revision-2', 32)
    with pytest.raises(ValueError, match='incomplete'):
        await service.store.activate('v2')
    assert (await service.store.active()).version == 'v1'
    await service.index_pending()
    await service.store.activate('v2')
    assert (await service.search('music Friday night', now=NOW))['index_version'] == 'v2'
    await service.handle(replace(messages[0], message_id='v2-change', payload={**messages[0].payload, 'event_version': 2}))
    with pytest.raises(ValueError, match='incomplete'):
        await service.store.activate('v1')
    await service.index_pending()
    await service.store.activate('v1')
    assert (await service.store.active()).version == 'v1'
    async with service.store.sessions() as session:
        entries = (await session.scalars(select(Entry))).all()
        assert {e.index_version for e in entries} == {'v1', 'v2'}


async def test_same_version_cannot_be_overwritten(service):
    message = (await seed(service))[0]
    changed = replace(message, message_id='same-version-new-id', payload={**message.payload, 'title': 'Should not win'})
    assert await service.handle(changed) == 'stale'
    assert (await service.store.event('demo-jazz')).title == message.payload['title']


async def test_bad_envelope_does_not_enter_projection(service):
    message = demo_messages(NOW)[0]
    with pytest.raises(ValueError):
        await service.handle(replace(message, occurred_at='2026-11-02T12:00:00'))
    assert await service.store.event('demo-jazz') is None


async def test_similar_does_not_parse_event_title_as_date_instruction(service):
    message = demo_messages()[0]
    message = replace(message, payload={**message.payload, 'title': 'Next Friday music'})
    await service.handle(message)
    for other in demo_messages()[1:]:
        await service.handle(other)
    await service.index_pending()
    result = await service.similar(await service.store.event('demo-jazz'))
    assert all(row['event_id'] != 'demo-jazz' for row in result['results'])
    assert result['filter']['until'] is None
