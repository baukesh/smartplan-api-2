from __future__ import annotations

import asyncio
import copy
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass
class _CacheEntry(Generic[T]):
    expires_at: float
    value: T


class AsyncTTLCache(Generic[T]):
    def __init__(self, *, ttl_seconds: float = 45.0, maxsize: int = 128, copy_values: bool = True) -> None:
        self.ttl_seconds = ttl_seconds
        self.maxsize = maxsize
        self.copy_values = copy_values
        self._entries: OrderedDict[Hashable, _CacheEntry[T]] = OrderedDict()
        self._locks: dict[Hashable, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()

    async def get_or_set(self, key: Hashable, factory: Callable[[], Awaitable[T]]) -> T:
        now = time.monotonic()
        async with self._global_lock:
            entry = self._entries.get(key)
            if entry and entry.expires_at > now:
                self._entries.move_to_end(key)
                return self._copy(entry.value)
            lock = self._locks.setdefault(key, asyncio.Lock())

        async with lock:
            now = time.monotonic()
            async with self._global_lock:
                entry = self._entries.get(key)
                if entry and entry.expires_at > now:
                    self._entries.move_to_end(key)
                    return self._copy(entry.value)

            value = await factory()
            async with self._global_lock:
                self._entries[key] = _CacheEntry(
                    expires_at=time.monotonic() + self.ttl_seconds,
                    value=self._copy(value),
                )
                self._entries.move_to_end(key)
                self._trim_locked()
            return self._copy(value)

    async def get(self, key: Hashable) -> T | None:
        now = time.monotonic()
        async with self._global_lock:
            entry = self._entries.get(key)
            if not entry:
                return None
            if entry.expires_at <= now:
                self._entries.pop(key, None)
                self._locks.pop(key, None)
                return None
            self._entries.move_to_end(key)
            return self._copy(entry.value)

    async def set(self, key: Hashable, value: T) -> None:
        async with self._global_lock:
            self._entries[key] = _CacheEntry(
                expires_at=time.monotonic() + self.ttl_seconds,
                value=self._copy(value),
            )
            self._entries.move_to_end(key)
            self._trim_locked()

    async def clear(self) -> None:
        async with self._global_lock:
            self._entries.clear()
            self._locks.clear()

    def _trim_locked(self) -> None:
        while len(self._entries) > self.maxsize:
            key, _ = self._entries.popitem(last=False)
            self._locks.pop(key, None)

    def _copy(self, value: T) -> T:
        if not self.copy_values:
            return value
        return copy.deepcopy(value)


async def clear_caches(*caches: AsyncTTLCache[object]) -> None:
    await asyncio.gather(*(cache.clear() for cache in caches))
