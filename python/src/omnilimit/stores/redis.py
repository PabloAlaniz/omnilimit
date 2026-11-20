"""Redis and Valkey stores: atomic, shared across instances and languages, on the server clock.

They take a client from `redis` or `valkey`, or anything with the same `evalsha` and `eval`.
Set `socket_timeout` and `socket_connect_timeout` on it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

from omnilimit._generated import (
    REDIS_HIT_LUA,
    REDIS_HIT_SHA1,
    REDIS_PEEK_LUA,
    REDIS_PEEK_SHA1,
    REDIS_REFUND_LUA,
    REDIS_REFUND_SHA1,
)
from omnilimit._types import HitParams, RawHit, RawRefund


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


def _checked(reply: Any, length: int) -> Sequence[Any]:
    if not isinstance(reply, Sequence) or isinstance(reply, str | bytes) or len(reply) != length:
        raise ValueError("omnilimit: unexpected reply from the Redis script")
    return reply


def _parse(reply: Any) -> RawHit:
    reply = _checked(reply, 3)
    # Values arrive as bytes or str, depending on the client's decode_responses.
    return RawHit(allowed=int(reply[0]) == 1, level=int(reply[1]), ts=int(reply[2]))


def _parse_refund(reply: Any) -> RawRefund:
    reply = _checked(reply, 2)
    return RawRefund(level=int(reply[0]), ts=int(reply[1]))


def _refund_args(params: HitParams, since: int, now_ms: int | None) -> list[str]:
    return [*_args(params, now_ms), str(since)]


class RedisStore:
    """For blocking clients. Use it with `Limiter`."""

    tier: Literal["exact"] = "exact"

    def __init__(self, client: Any) -> None:
        self._client = client

    def _eval(self, sha1: str, lua: str, key: str, args: list[str]) -> Any:
        try:
            return self._client.evalsha(sha1, 1, key, *args)
        except Exception as error:
            if not _is_no_script(error):
                raise
            return self._client.eval(lua, 1, key, *args)

    def _run(self, sha1: str, lua: str, key: str, args: list[str]) -> RawHit:
        return _parse(self._eval(sha1, lua, key, args))

    def hit(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
        return self._run(REDIS_HIT_SHA1, REDIS_HIT_LUA, key, _args(params, now_ms))

    def peek(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
        """What `hit` would answer now, read only."""
        return self._run(REDIS_PEEK_SHA1, REDIS_PEEK_LUA, key, _args(params, now_ms))

    def reset(self, key: str) -> None:
        self._client.delete(key)

    def refund(
        self, key: str, params: HitParams, since: int, now_ms: int | None = None
    ) -> RawRefund:
        """Give back a hit counted at `since`, as far as it has not drained."""
        args = _refund_args(params, since, now_ms)
        return _parse_refund(self._eval(REDIS_REFUND_SHA1, REDIS_REFUND_LUA, key, args))


class AsyncRedisStore:
    """For `redis.asyncio` and `valkey.asyncio` clients. Use it with `AsyncLimiter`."""

    tier: Literal["exact"] = "exact"

    def __init__(self, client: Any) -> None:
        self._client = client

    async def _eval(self, sha1: str, lua: str, key: str, args: list[str]) -> Any:
        try:
            return await self._client.evalsha(sha1, 1, key, *args)
        except Exception as error:
            if not _is_no_script(error):
                raise
            return await self._client.eval(lua, 1, key, *args)

    async def _run(self, sha1: str, lua: str, key: str, args: list[str]) -> RawHit:
        return _parse(await self._eval(sha1, lua, key, args))

    async def hit(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
        return await self._run(REDIS_HIT_SHA1, REDIS_HIT_LUA, key, _args(params, now_ms))

    async def peek(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
        """What `hit` would answer now, read only."""
        return await self._run(REDIS_PEEK_SHA1, REDIS_PEEK_LUA, key, _args(params, now_ms))

    async def reset(self, key: str) -> None:
        await self._client.delete(key)

    async def refund(
        self, key: str, params: HitParams, since: int, now_ms: int | None = None
    ) -> RawRefund:
        """Give back a hit counted at `since`, as far as it has not drained."""
        args = _refund_args(params, since, now_ms)
        return _parse_refund(await self._eval(REDIS_REFUND_SHA1, REDIS_REFUND_LUA, key, args))
