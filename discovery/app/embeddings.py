"""Timeout + fail-fast bulkhead + circuit breaker around one Ollama call."""
import asyncio
import math
import time
import httpx
from . import metrics


class Unavailable(Exception):
    pass


class Ollama:
    def __init__(self, url='http://localhost:11434', timeout=1.5, threshold=5, cooldown=30,
                 capacity=4, price=0.0, client=None, clock=time.monotonic):
        if timeout <= 0 or cooldown < 0 or threshold < 1 or not 1 <= capacity <= 4 or price < 0:
            raise ValueError('Invalid timeout, cooldown, threshold, bulkhead capacity or notional price')
        self.client = client or httpx.AsyncClient(base_url=url, timeout=timeout)
        self.timeout, self.threshold, self.cooldown = timeout, threshold, cooldown
        self.capacity, self.price, self.clock = capacity, price, clock
        self.inflight = self.failures = 0
        self.opened_at = None
        self.probing = False
        self.generation = 0

    async def revision(self, model):
        return await self.embed(None, model, 0, 'metadata')

    async def embed(self, text, model, dimensions, purpose='search', expected_revision=None):
        # No await before admission: atomic within this process's asyncio event loop.
        probe = self.opened_at is not None
        if probe and (self.clock() - self.opened_at < self.cooldown or self.probing):
            raise Unavailable('circuit_open')
        if self.inflight >= self.capacity:
            raise Unavailable('bulkhead_full')
        self.probing = probe
        self.inflight += 1
        generation = self.generation
        metrics.INFLIGHT.set(self.inflight)
        try:
            async with asyncio.timeout(self.timeout):
                if text is None or expected_revision is not None:
                    response = await self.client.get('/api/tags')
                    response.raise_for_status()
                    models = response.json()['models']
                    if not isinstance(models, list) or not all(isinstance(item, dict) for item in models):
                        raise ValueError('Invalid model metadata response')
                    digest = next((item['digest'] for item in models if item.get('name') == model or item.get('model') == model), None)
                    if not digest or (expected_revision is not None and digest != expected_revision):
                        raise ValueError('Model is missing or its digest differs from the index; build a new index version')
                if text is None:
                    vector, tokens = digest, 0
                else:
                    response = await self.client.post('/api/embed', json={
                        'model': model, 'input': text, 'truncate': False, 'keep_alive': '10m'})
                    response.raise_for_status()
                    data = response.json()
                    vector = data['embeddings'][0]
                    if (not isinstance(vector, list) or len(vector) != dimensions
                            or not all(type(v) in (int, float) and math.isfinite(v) for v in vector)
                            or not any(vector)):
                        raise ValueError('Embedding dimension/values do not match index configuration')
                    tokens = int(data.get('prompt_eval_count', 0))
                    if tokens < 0:
                        raise ValueError('Negative token count')
            metrics.TOKENS.labels(purpose).inc(tokens)
            metrics.COST.labels(purpose).inc(tokens * self.price)
            # An older in-flight success must not close a newly opened breaker.
            if generation == self.generation:
                self.failures, self.opened_at = 0, None
                metrics.BREAKER.set(0)
            return vector
        except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError, IndexError) as exc:
            if generation == self.generation:
                self.failures += 1
                if probe or self.failures >= self.threshold:
                    self.opened_at = self.clock()
                    self.generation += 1
                    metrics.BREAKER.set(1)
            raise Unavailable(type(exc).__name__) from exc
        finally:
            self.inflight -= 1
            if probe:
                self.probing = False
            metrics.INFLIGHT.set(self.inflight)

    async def close(self):
        await self.client.aclose()
