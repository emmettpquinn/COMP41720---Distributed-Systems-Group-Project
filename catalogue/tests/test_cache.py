from app.cache import DetailCache


def test_expiry_and_bounded_lru_eviction():
    now = [0.0]
    cache = DetailCache(ttl_seconds=1, max_entries=2, clock=lambda: now[0])
    cache.put("a", {"version": 1}, cache.generation)
    cache.put("b", {"version": 2}, cache.generation)
    assert cache.get("a") == {"version": 1}
    cache.put("c", {"version": 3}, cache.generation)
    assert cache.get("b") is None
    now[0] = 1.0
    assert cache.get("a") is None and cache.get("c") is None


def test_invalidation_prevents_an_older_inflight_read_filling_cache():
    cache = DetailCache()
    generation = cache.generation
    cache.invalidate("event-1")
    assert not cache.put("event-1", {"event_version": 1}, generation)
    assert cache.get("event-1") is None


def test_cached_objects_are_not_shared_with_callers():
    cache = DetailCache()
    value = {"seats": ["A1"]}
    cache.put("event-1", value, cache.generation)
    value["seats"].clear()
    result = cache.get("event-1")
    result["seats"].clear()
    assert cache.get("event-1") == {"seats": ["A1"]}


def test_cache_can_be_disabled():
    cache = DetailCache(ttl_seconds=0)
    assert not cache.put("event-1", {}, cache.generation)
    assert cache.get("event-1") is None
