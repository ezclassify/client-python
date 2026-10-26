"""Response-cache lifetime, concurrency, and failure behavior."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event, Timer

import pytest

from ezclassify_rest_client import _response_cache


def test_pending_call_stays_shared_even_after_ttl_elapses(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(_response_cache, "monotonic", lambda: clock[0])
    cache = _response_cache.ResponseCache()
    started = Event()
    release = Event()
    calls = [0]

    def load():
        calls[0] += 1
        started.set()
        assert release.wait(2)
        return "answer"

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(cache.get, "same", 0.1, load)
        assert started.wait(2)
        clock[0] += 1.0
        second = executor.submit(cache.get, "same", 0.1, load)
        timer = Timer(0.05, release.set)
        timer.start()
        assert first.result(timeout=2) == "answer"
        assert second.result(timeout=2) == "answer"
        timer.join()
    assert calls[0] == 1
    clock[0] += 0.05
    assert cache.get("same", 0.1, load) == "answer"
    assert calls[0] == 1


def test_expiry_starts_after_success(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(_response_cache, "monotonic", lambda: clock[0])
    cache = _response_cache.ResponseCache()
    calls = [0]

    def load():
        calls[0] += 1
        clock[0] += 20.0
        return calls[0]

    assert cache.get("same", 10.0, load) == 1
    clock[0] += 9.0
    assert cache.get("same", 10.0, load) == 1
    clock[0] += 2.0
    assert cache.get("same", 10.0, load) == 2
    assert calls[0] == 2


def test_failure_is_not_cached():
    cache = _response_cache.ResponseCache()
    calls = [0]

    def load():
        calls[0] += 1
        if calls[0] == 1:
            raise ValueError("bad response")
        return "recovered"

    with pytest.raises(ValueError, match="bad response"):
        cache.get("same", 60.0, load)
    assert cache.get("same", 60.0, load) == "recovered"
    assert calls[0] == 2


def test_lru_bounds_distinct_typeahead_keys():
    cache = _response_cache.ResponseCache(max_entries=2)
    calls = [0]

    def load():
        calls[0] += 1
        return calls[0]

    assert cache.get("a", float("inf"), load) == 1
    assert cache.get("b", float("inf"), load) == 2
    assert cache.get("a", float("inf"), load) == 1
    assert cache.get("c", float("inf"), load) == 3
    assert cache.get("b", float("inf"), load) == 4


def test_waiting_counts_callers_joined_to_a_pending_entry():
    cache = _response_cache.ResponseCache()
    started = Event()
    release = Event()

    def load():
        started.set()
        assert release.wait(2)
        return "answer"

    with ThreadPoolExecutor(max_workers=3) as executor:
        owner = executor.submit(cache.get, "same", 60.0, load)
        assert started.wait(2)
        assert cache.waiting() == 0
        joined = [executor.submit(cache.get, "same", 60.0, load) for _ in range(2)]
        deadline = Timer(2, release.set)
        deadline.start()
        while cache.waiting() < 2 and not release.is_set():
            pass
        assert cache.waiting() == 2
        release.set()
        deadline.cancel()
        assert [f.result(timeout=2) for f in [owner, *joined]] == ["answer"] * 3
    assert cache.waiting() == 0
    # A hit on a settled entry never waits, so it is not counted either.
    assert cache.get("same", 60.0, load) == "answer"
    assert cache.waiting() == 0
