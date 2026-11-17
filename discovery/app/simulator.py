"""Explicit test/demo double. This is NOT a real semantic embedding model."""
import asyncio
import hashlib
import re
from .embeddings import Unavailable


class SimulatedOllama:
    def __init__(self):
        self.failing = False

    async def embed(self, text, model, dimensions, purpose='search', expected_revision=None):
        if self.failing:
            raise Unavailable('simulated_outage')
        vector = [0.0] * dimensions
        for word in re.findall(r'[a-z]+', text.lower()):
            slot = int.from_bytes(hashlib.sha256(word.encode()).digest()[:4], 'big') % dimensions
            vector[slot] += 1
        vector[0] += 0.001
        await asyncio.sleep(0)
        return vector

    async def close(self):
        pass
