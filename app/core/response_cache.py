from __future__ import annotations

from dataclasses import dataclass

from app.core.ttl_cache import AsyncTTLCache


@dataclass
class CachedResponse:
    status_code: int
    media_type: str | None
    headers: dict[str, str]
    body: bytes


response_cache: AsyncTTLCache[CachedResponse] = AsyncTTLCache(ttl_seconds=45.0, maxsize=160)


async def clear_response_cache() -> None:
    await response_cache.clear()
