"""Bounded response cache with one in-flight request per Context operation key."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from threading import Event, RLock
from time import monotonic
from typing import Callable, Generic, TypeVar, cast

T = TypeVar("T")


@dataclass
class _Entry(Generic[T]):
    ready: Event = field(default_factory=Event)
    value: T | None = None
    error: BaseException | None = None
    expires_at: float = float("inf")
    # Callers blocked on this entry while it is pending; read by tests only.
    waiters: int = 0


class ResponseCache:
    """Share pending reads and retain successful responses until their TTL expires.

    A failed read is removed immediately. Eviction never cancels a request that
    another caller is already awaiting, and the TTL starts only after success.
    """

    def __init__(self, max_entries: int = 500) -> None:
        self._entries: OrderedDict[str, _Entry[object]] = OrderedDict()
        self._lock = RLock()
        self._max_entries = max_entries

    def get(self, key: str, ttl_seconds: float, load: Callable[[], T]) -> T:
        """Return one validated result for every concurrent call with this key."""
        with self._lock:
            existing = self._entries.get(key)
            if existing is not None and (
                not existing.ready.is_set() or existing.expires_at > monotonic()
            ):
                self._entries.move_to_end(key)
                entry = existing
                owner = False
                counted = not entry.ready.is_set()
                if counted:
                    entry.waiters += 1
            else:
                counted = False
                entry = _Entry[object]()
                self._entries[key] = entry
                self._entries.move_to_end(key)
                while len(self._entries) > self._max_entries:
                    self._entries.popitem(last=False)
                owner = True

        if owner:
            try:
                value = load()
            except BaseException as exc:
                with self._lock:
                    entry.error = exc
                    entry.ready.set()
                    if self._entries.get(key) is entry:
                        del self._entries[key]
                raise
            with self._lock:
                entry.value = value
                entry.expires_at = monotonic() + ttl_seconds
                entry.ready.set()
            return value

        try:
            entry.ready.wait()
        finally:
            if counted:
                with self._lock:
                    entry.waiters -= 1
        if entry.error is not None:
            raise entry.error
        return cast(T, entry.value)

    def waiting(self) -> int:
        """Count the callers blocked on a pending entry.

        A test hook: the conformance suite uses it to know every concurrent
        caller has joined an in-flight request before it releases the answer.
        """
        with self._lock:
            return sum(entry.waiters for entry in self._entries.values())

    def clear(self, keep_key: str | None = None) -> None:
        """Forget old results while optionally retaining the response in flight."""
        with self._lock:
            keep = self._entries.get(keep_key) if keep_key is not None else None
            self._entries.clear()
            if keep is not None and keep_key is not None:
                self._entries[keep_key] = keep
