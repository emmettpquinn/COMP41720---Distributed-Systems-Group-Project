import asyncio
import time
import httpx
import pytest
from discovery.app.embeddings import Ollama, Unavailable


async def test_timeout_opens_breaker_and_half_open_recovers():
    failing = True
    calls = 0
    clock = [0.0]

    async def handler(request):
        nonlocal calls
        calls += 1
        if failing:
            await asyncio.sleep(1)
        return httpx.Response(200, json={'embeddings': [[1, 0]], 'prompt_eval_count': 3})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url='http://test')
    provider = Ollama(client=client, timeout=0.02, threshold=2, cooldown=30, clock=lambda: clock[0])
    for _ in range(2):
        before = time.monotonic()
        with pytest.raises(Unavailable):
            await provider.embed('music', 'test', 2)
        assert time.monotonic() - before < 0.5
    with pytest.raises(Unavailable, match='circuit_open'):
        await provider.embed('music', 'test', 2)
    assert calls == 2
    clock[0] = 31
    failing = False
    assert await provider.embed('music', 'test', 2) == [1, 0]
    assert provider.opened_at is None
    await provider.close()


async def test_bulkhead_rejects_fifth_without_waiting():
    release = asyncio.Event()
    entered = asyncio.Event()
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        if calls == 4:
            entered.set()
        await release.wait()
        return httpx.Response(200, json={'embeddings': [[1, 0]], 'prompt_eval_count': 1})

    provider = Ollama(client=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url='http://test'))
    tasks = [asyncio.create_task(provider.embed('music', 'test', 2)) for _ in range(4)]
    await asyncio.wait_for(entered.wait(), 2)
    with pytest.raises(Unavailable, match='bulkhead_full'):
        await provider.embed('music', 'test', 2)
    assert calls == 4
    release.set()
    await asyncio.gather(*tasks)
    assert provider.inflight == 0
    await provider.close()


@pytest.mark.parametrize('body', [{'embeddings': [[0, 0]]}, {'embeddings': [[1]]}, {'broken': True},
                                {'embeddings': [['0', '0']]}, {'embeddings': [[True, False]]}])
async def test_invalid_model_response_degrades_instead_of_corrupting_index(body):
    provider = Ollama(client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)), base_url='http://test'))
    with pytest.raises(Unavailable):
        await provider.embed('music', 'test', 2)
    await provider.close()


async def test_changed_model_digest_does_not_mix_embedding_spaces():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={'models': [{'name': 'model:tag', 'digest': 'new-digest'}]})

    provider = Ollama(client=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url='http://test'))
    with pytest.raises(Unavailable):
        await provider.embed('music', 'model:tag', 2, expected_revision='old-digest')
    assert calls == ['/api/tags']
    assert await provider.revision('model:tag') == 'new-digest'
    await provider.close()
