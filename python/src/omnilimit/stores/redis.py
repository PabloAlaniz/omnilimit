"""Redis and Valkey stores: atomic, shared across instances and languages, on the server clock.

They take a client from `redis` or `valkey`, or anything with the same `evalsha` and `eval`.
Set `socket_timeout` and `socket_connect_timeout` on it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

from omnilimit._generated import REDIS_HIT_LUA, REDIS_HIT_SHA1, REDIS_PEEK_LUA, REDIS_PEEK_SHA1
from omnilimit._types import HitParams, RawHit


def _is_no_script(error: Exception) -> bool:
    # redis-py strips the NOSCRIPT prefix from the message and raises NoScriptError.
    return (
        type(error).__name__ == "NoScriptError"
        or "NOSCRIPT" in str(error)
        or "No matching script" in str(error)
    )


def _args(params: HitParams, now_ms: int | None) -> list[str]:
    return [
        str(params.limit),
        str(params.window_ms),
        str(params.capacity),
        str(params.drain_ms),
        "" if now_ms is None else str(now_ms),
    ]


def _parse(reply: Any) -> RawHit:
    if not isinstance(reply, Sequence) or isinstance(reply, str | bytes) or len(reply) != 3:
        raise ValueError("omnilimit: unexpected reply from the Redis script")
    # Values arrive as bytes or str, depending on the client's decode_responses.
    return RawHit(allowed=int(reply[0]) == 1, level=int(reply[1]), ts=int(reply[2]))


class RedisStore:
    """For blocking clients. Use it with `Limiter`."""

    tier: Literal["exact"] = "exact"

    def __init__(self, client: Any) -> None:
        self._client = client

    def _run(self, sha1: str, lua: str, key: str, args: list[str]) -> RawHit:
        try:
            reply = self._client.evalsha(sha1, 1, key, *args)
        except Exception as error:
            if not _is_no_script(error):
                raise
            reply = self._client.eval(lua, 1, key, *args)
        return _parse(reply)

    def hit(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
        return self._run(REDIS_HIT_SHA1, REDIS_HIT_LUA, key, _args(params, now_ms))

    def peek(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
        """What `hit` would answer now, read only."""
        return self._run(REDIS_PEEK_SHA1, REDIS_PEEK_LUA, key, _args(params, now_ms))

    def reset(self, key: str) -> None:
        self._client.delete(key)


class AsyncRedisStore:
    """For `redis.asyncio` and `valkey.asyncio` clients. Use it with `AsyncLimiter`."""

    tier: Literal["exact"] = "exact"

    def __init__(self, client: Any) -> None:
        self._client = client

    async def _run(self, sha1: str, lua: str, key: str, args: list[str]) -> RawHit:
        try:
            reply = await self._client.evalsha(sha1, 1, key, *args)
        except Exception as error:
            if not _is_no_script(error):
                raise
            reply = await self._client.eval(lua, 1, key, *args)
        return _parse(reply)

    async def hit(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
        return await self._run(REDIS_HIT_SHA1, REDIS_HIT_LUA, key, _args(params, now_ms))

    async def peek(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
        """What `hit` would answer now, read only."""
        return await self._run(REDIS_PEEK_SHA1, REDIS_PEEK_LUA, key, _args(params, now_ms))

    async def reset(self, key: str) -> None:
        await self._client.delete(key)
