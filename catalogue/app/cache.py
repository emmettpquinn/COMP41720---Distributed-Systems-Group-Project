"""Bounded per-process detail cache with a generation guard against stale fills."""

import copy
import time
from collections import OrderedDict


class DetailCache:
    def __init__(self, ttl_seconds=0.5, max_entries=128, clock=time.monotonic):
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self.clock = clock
        self.generation = 0
        self.entries = OrderedDict()

    def get(self, key):
        cached = self.entries.get(key)
        if cached is None:
            return None
        deadline, value = cached
        if deadline <= self.clock():
            self.entries.pop(key)
            return None
        self.entries.move_to_end(key)
        return copy.deepcopy(value)

    def put(self, key, value, generation):
        if generation != self.generation or self.ttl_seconds <= 0:
            return False
        self.entries[key] = (self.clock() + self.ttl_seconds, copy.deepcopy(value))
        self.entries.move_to_end(key)
        while len(self.entries) > self.max_entries:
            self.entries.popitem(last=False)
        return True

    def invalidate(self, key):
        self.generation += 1
        self.entries.pop(key, None)
