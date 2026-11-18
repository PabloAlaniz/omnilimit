"""Compositions of check, peek and reset with a fixed meaning. See "Recipes" in the spec."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from omnilimit._limiter import AsyncLimiter, Limiter
from omnilimit._types import Decision


@dataclass(frozen=True, slots=True)
class CombinedDecision:
    """The outcome of `check_all`.

    `decision` is the one to answer with: when denied, the denial with the longest wait; when
    allowed, the one with the fewest units left. `decisions` holds every decision taken, in
    order, and is shorter than the input when a step denied while counting.
    """

    allowed: bool
    decision: Decision
    decisions: tuple[Decision, ...]


def _longest_wait(decisions: Sequence[Decision]) -> Decision:
    governing = decisions[0]
    for decision in decisions:
        if (decision.retry_after_ms or 0) > (governing.retry_after_ms or 0):
            governing = decision
    return governing


def _fewest_left(decisions: Sequence[Decision]) -> Decision:
    governing = decisions[0]
    for decision in decisions:
        if decision.remaining is None:
            continue
        if governing.remaining is None or decision.remaining < governing.remaining:
            governing = decision
    return governing


def _refusal(peeks: list[Decision]) -> CombinedDecision | None:
    refused = [decision for decision in peeks if not decision.allowed]
    if not refused:
        return None
    return CombinedDecision(allowed=False, decision=_longest_wait(refused), decisions=tuple(peeks))


def _needs_entries(entries: Sequence[object]) -> None:
    if len(entries) == 0:
        raise ValueError("omnilimit: check_all needs at least one limit")


def check_all(entries: Sequence[tuple[Limiter, str]]) -> CombinedDecision:
    """Apply several limits to one request, such as per IP and per account, so that a request
    one of them denies does not use up the others.

    It peeks every `(limiter, id)` first and counts only when all of them would allow. The peek
    is advisory: two requests racing for the last unit can both pass it, and then the second is
    denied while counting, with the limits before it already counted. Needs stores that can peek.
    """
    _needs_entries(entries)
    refusal = _refusal([limiter.peek(id_value) for limiter, id_value in entries])
    if refusal is not None:
        return refusal
    decisions: list[Decision] = []
    for limiter, id_value in entries:
        decision = limiter.check(id_value)
        decisions.append(decision)
        if not decision.allowed:
            return CombinedDecision(allowed=False, decision=decision, decisions=tuple(decisions))
    return CombinedDecision(
        allowed=True, decision=_fewest_left(decisions), decisions=tuple(decisions)
    )


async def check_all_async(entries: Sequence[tuple[AsyncLimiter, str]]) -> CombinedDecision:
    """`check_all` for `AsyncLimiter`."""
    _needs_entries(entries)
    refusal = _refusal([await limiter.peek(id_value) for limiter, id_value in entries])
    if refusal is not None:
        return refusal
    decisions: list[Decision] = []
    for limiter, id_value in entries:
        decision = await limiter.check(id_value)
        decisions.append(decision)
        if not decision.allowed:
            return CombinedDecision(allowed=False, decision=decision, decisions=tuple(decisions))
    return CombinedDecision(
        allowed=True, decision=_fewest_left(decisions), decisions=tuple(decisions)
    )


@dataclass(frozen=True, slots=True)
class Attempt:
    """What a lockout answers.

    `allowed` is false when there were too many failures: the verification did not run,
    `verified` is `None`, and `decision` is the denial to answer with. Otherwise `verified`
    says how it went, and after a failure `decision` is the one that counted it.
    """

    allowed: bool
    verified: bool | None
    decision: Decision


class Lockout:
    """A limit that counts only failed attempts, as a login does.

    While the budget of failures is spent, even a correct attempt is refused without being
    checked, which is what makes guessing useless. The limiter's policy is how many failures
    are tolerated and how fast they come back.
    """

    def __init__(self, limiter: Limiter, *, reset_on_success: bool = True) -> None:
        self._limiter = limiter
        self._reset_on_success = reset_on_success

    def attempt(self, id_value: str, verify: Callable[[], bool]) -> Attempt:
        """Run `verify` unless `id_value` has failed too often.

        A false result counts as a failure and a true one clears the count. If `verify` raises,
        nothing is counted and the error is yours.
        """
        gate = self._limiter.peek(id_value)
        if not gate.allowed:
            return Attempt(allowed=False, verified=None, decision=gate)
        if verify():
            if self._reset_on_success:
                self._limiter.reset(id_value)
            return Attempt(allowed=True, verified=True, decision=gate)
        return Attempt(allowed=True, verified=False, decision=self._limiter.check(id_value))


class AsyncLockout:
    """`Lockout` for `AsyncLimiter`. `verify` may be a plain function or a coroutine function."""

    def __init__(self, limiter: AsyncLimiter, *, reset_on_success: bool = True) -> None:
        self._limiter = limiter
        self._reset_on_success = reset_on_success

    async def attempt(self, id_value: str, verify: Callable[[], Awaitable[bool] | bool]) -> Attempt:
        gate = await self._limiter.peek(id_value)
        if not gate.allowed:
            return Attempt(allowed=False, verified=None, decision=gate)
        result = verify()
        if await result if inspect.isawaitable(result) else result:
            if self._reset_on_success:
                await self._limiter.reset(id_value)
            return Attempt(allowed=True, verified=True, decision=gate)
        return Attempt(allowed=True, verified=False, decision=await self._limiter.check(id_value))
