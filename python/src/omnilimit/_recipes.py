"""Compositions of check, peek, reset, reserve and refund with a fixed meaning.

See "Recipes" in the spec.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast

from omnilimit._limiter import AsyncLimiter, Limiter, _store_method
from omnilimit._types import Decision, Reservation

Mode = Literal["peek", "reserve"]


@dataclass(frozen=True, slots=True)
class CombinedDecision:
    """The outcome of `check_all`, or of committing a pending check.

    `decision` is the one to answer with: when denied, the denial with the longest wait; when
    allowed, the one with the fewest units left. `decisions` holds every decision taken, in
    order, and is shorter than the input when a step denied while counting.
    """

    allowed: bool
    decision: Decision
    decisions: tuple[Decision, ...]


@dataclass(frozen=True, slots=True)
class Cancelled:
    """What a pending check gave back, one value per decision.

    `True` when that entry's reservation was refunded, `False` when its refund failed and it
    stays counted, `None` when nothing of it was counted.
    """

    refunded: tuple[bool | None, ...]


class SettledError(RuntimeError):
    """A pending check was already settled the other way: `cancel` after `commit`, or `commit`
    after `cancel`. Calling the same one again is not an error: it returns the first result."""


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


def _peeked(peeks: list[Decision]) -> CombinedDecision:
    refused = [decision for decision in peeks if not decision.allowed]
    if refused:
        return CombinedDecision(
            allowed=False, decision=_longest_wait(refused), decisions=tuple(peeks)
        )
    return CombinedDecision(allowed=True, decision=_fewest_left(peeks), decisions=tuple(peeks))


def _counted(decisions: list[Decision]) -> CombinedDecision:
    """Decisions taken in order until the first denial, which then governs."""
    last = decisions[-1]
    if not last.allowed:
        return CombinedDecision(allowed=False, decision=last, decisions=tuple(decisions))
    return CombinedDecision(
        allowed=True, decision=_fewest_left(decisions), decisions=tuple(decisions)
    )


def _cost(entry: tuple[object, ...]) -> int:
    return entry[2] if len(entry) > 2 else 1  # type: ignore[return-value]


def _validate(recipe: str, entries: Sequence[tuple[Any, ...]], mode: Mode = "peek") -> None:
    """Everything a recipe can refuse, checked before its first store call. See the spec."""
    if len(entries) == 0:
        raise ValueError(f"omnilimit: {recipe} needs at least one limit")
    for entry in entries:
        limiter: Limiter | AsyncLimiter = entry[0]
        if limiter.tier == "approximate":
            does = "peeks" if mode == "peek" else "refunds"
            raise TypeError(f"omnilimit: {recipe} {does}, and a store of tier approximate cannot")
        if mode == "reserve":
            _store_method(limiter._store, "refund")
        limiter._core.weighed(_cost(entry))


class _Settlement:
    """What both kinds of pending share: the result of opening, and how it was settled."""

    __slots__ = ("_by", "_own", "_refunded", "mode")

    def __init__(self, mode: Mode, own: CombinedDecision, refunded: list[bool | None]) -> None:
        self.mode: Mode = mode
        self._own = own
        self._refunded = tuple(refunded)
        self._by: str | None = None

    @property
    def allowed(self) -> bool:
        return self._own.allowed

    @property
    def decision(self) -> Decision:
        """The governing decision, as in `CombinedDecision`."""
        return self._own.decision

    @property
    def decisions(self) -> tuple[Decision, ...]:
        return self._own.decisions

    @property
    def refunded(self) -> tuple[bool | None, ...]:
        """What opening gave back, as in `Cancelled`: only a denied `reserve` has anything here."""
        return self._refunded

    @property
    def settled(self) -> bool:
        """True once `commit` or `cancel` was called."""
        return self._by is not None

    def _claim(self, op: str) -> bool:
        """Rule 8: True for the first settlement, False to repeat it, else a `SettledError`."""
        if self._by is None:
            self._by = op
            return True
        if self._by != op:
            raise SettledError(f"omnilimit: this pending check was already settled by {self._by}")
        return False


class Pending(_Settlement):
    """The first phase of a combined check, from `peek_all` or `reserve_all`.

    Settle it once: `commit()` when the work happened, `cancel()` when it did not. Calling the
    same one again returns the first result, also from another thread while the first runs;
    calling the other one raises `SettledError`. See "Combined check" in the spec.
    """

    __slots__ = ("_cancel", "_commit", "_lock", "_result")

    def __init__(
        self,
        mode: Mode,
        own: CombinedDecision,
        refunded: list[bool | None],
        commit: Callable[[], CombinedDecision],
        cancel: Callable[[], Cancelled],
    ) -> None:
        super().__init__(mode, own, refunded)
        self._commit, self._cancel = commit, cancel
        self._lock = threading.Lock()
        self._result: Any = None

    def _settle(self, op: str, work: Callable[[], Any]) -> Any:
        with self._lock:
            if self._claim(op):
                self._result = work()
            return self._result

    def commit(self) -> CombinedDecision:
        """The work happened.

        In `peek` mode this counts every entry, in order, and stops at the first denial: the
        entries before it stay counted. In `reserve` mode, and on a denied pending, it makes no
        store call and returns the pending's own result.
        """
        result: CombinedDecision = self._settle("commit", self._commit)
        return result

    def cancel(self) -> Cancelled:
        """The work did not happen.

        In `reserve` mode this refunds every reservation, in order; otherwise it makes no store
        call. It never raises for a store failure: a failed refund is `False`.
        """
        result: Cancelled = self._settle("cancel", self._cancel)
        return result


class AsyncPending(_Settlement):
    """`Pending` for `AsyncLimiter`. Concurrent settlements share one task."""

    __slots__ = ("_cancel", "_commit", "_task")

    def __init__(
        self,
        mode: Mode,
        own: CombinedDecision,
        refunded: list[bool | None],
        commit: Callable[[], Awaitable[CombinedDecision]],
        cancel: Callable[[], Awaitable[Cancelled]],
    ) -> None:
        super().__init__(mode, own, refunded)
        self._commit, self._cancel = commit, cancel
        self._task: asyncio.Future[Any] | None = None

    async def _settle(self, op: str, work: Callable[[], Awaitable[Any]]) -> Any:
        if self._claim(op):
            self._task = asyncio.ensure_future(work())
        task = cast("asyncio.Future[Any]", self._task)
        # Shielded: a caller that gives up does not cancel what the others wait for.
        return await asyncio.shield(task)

    async def commit(self) -> CombinedDecision:
        """The work happened. See `Pending.commit`."""
        result: CombinedDecision = await self._settle("commit", self._commit)
        return result

    async def cancel(self) -> Cancelled:
        """The work did not happen. See `Pending.cancel`."""
        result: Cancelled = await self._settle("cancel", self._cancel)
        return result


Entries = Sequence[tuple[Limiter, str] | tuple[Limiter, str, int]]
AsyncEntries = Sequence[tuple[AsyncLimiter, str] | tuple[AsyncLimiter, str, int]]


def _count_all(entries: Entries) -> CombinedDecision:
    decisions: list[Decision] = []
    for entry in entries:
        decisions.append(entry[0].check(entry[1], cost=_cost(entry)))
        if not decisions[-1].allowed:
            break
    return _counted(decisions)


async def _count_all_async(entries: AsyncEntries) -> CombinedDecision:
    decisions: list[Decision] = []
    for entry in entries:
        decisions.append(await entry[0].check(entry[1], cost=_cost(entry)))
        if not decisions[-1].allowed:
            break
    return _counted(decisions)


def _refund_each(entries: Entries, held: list[Reservation | None]) -> list[bool | None]:
    return [
        None if reservation is None else entry[0].refund(reservation)
        for entry, reservation in zip(entries, held, strict=False)
    ]


async def _refund_each_async(
    entries: AsyncEntries, held: list[Reservation | None]
) -> list[bool | None]:
    return [
        None if reservation is None else await entry[0].refund(reservation)
        for entry, reservation in zip(entries, held, strict=False)
    ]


def _open_peek(recipe: str, entries: Entries) -> Pending:
    _validate(recipe, entries)
    own = _peeked([entry[0].peek(entry[1], cost=_cost(entry)) for entry in entries])
    nothing: list[bool | None] = [None] * len(own.decisions)
    return Pending(
        "peek",
        own,
        nothing,
        lambda: _count_all(entries) if own.allowed else own,
        lambda: Cancelled(tuple(nothing)),
    )


async def _open_peek_async(recipe: str, entries: AsyncEntries) -> AsyncPending:
    _validate(recipe, entries)
    own = _peeked([await entry[0].peek(entry[1], cost=_cost(entry)) for entry in entries])
    nothing: list[bool | None] = [None] * len(own.decisions)

    async def commit() -> CombinedDecision:
        return await _count_all_async(entries) if own.allowed else own

    async def cancel() -> Cancelled:
        return Cancelled(tuple(nothing))

    return AsyncPending("peek", own, nothing, commit, cancel)


def peek_all(entries: Entries) -> Pending:
    """Open a combined check in `peek` mode: peek every `(limiter, id)` and count nothing.

    `commit()` then counts every limit and `cancel()` leaves them alone. Advisory: two requests
    racing for the last unit can both pass the peek, and then the second is denied while
    committing, with the limits before it already counted. A pending never settled counts
    nothing. Before any store call, raises `ValueError` for an empty list or a cost a limit does
    not accept, and `TypeError` for a limiter of tier approximate.
    """
    return _open_peek("peek_all", entries)


async def peek_all_async(entries: AsyncEntries) -> AsyncPending:
    """`peek_all` for `AsyncLimiter`."""
    return await _open_peek_async("peek_all", entries)


def reserve_all(entries: Entries) -> Pending:
    """Open a combined check in `reserve` mode: count every `(limiter, id)`, in order.

    It stops at the first denial and refunds the limits counted before it. When allowed,
    `commit()` keeps the counts and `cancel()` refunds them all. Unlike `peek_all` it never lets
    more through than the limits allow, at the price of holding the earlier limits for a round
    trip when a later one denies. A pending never settled stays counted. Before any store call,
    raises `ValueError` for an empty list or a cost a limit does not accept, and `TypeError` for a
    limiter whose store cannot refund, such as tier approximate.
    """
    _validate("reserve_all", entries, "reserve")
    decisions: list[Decision] = []
    held: list[Reservation | None] = []
    for entry in entries:
        reserved = entry[0].reserve(entry[1], cost=_cost(entry))
        decisions.append(reserved.decision)
        held.append(reserved.reservation)
        if not reserved.decision.allowed:
            break
    own = _counted(decisions)
    if not own.allowed:
        # Give back what was counted before the denial, in entry order. The denial holds nothing.
        refunded = _refund_each(entries, held)
        return Pending("reserve", own, refunded, lambda: own, lambda: Cancelled(tuple(refunded)))
    return Pending(
        "reserve",
        own,
        [None] * len(decisions),
        lambda: own,
        lambda: Cancelled(tuple(_refund_each(entries, held))),
    )


async def reserve_all_async(entries: AsyncEntries) -> AsyncPending:
    """`reserve_all` for `AsyncLimiter`."""
    _validate("reserve_all", entries, "reserve")
    decisions: list[Decision] = []
    held: list[Reservation | None] = []
    for entry in entries:
        reserved = await entry[0].reserve(entry[1], cost=_cost(entry))
        decisions.append(reserved.decision)
        held.append(reserved.reservation)
        if not reserved.decision.allowed:
            break
    own = _counted(decisions)
    refunded: list[bool | None] = [None] * len(decisions)
    if not own.allowed:
        # Give back what was counted before the denial, in entry order. The denial holds nothing.
        refunded = await _refund_each_async(entries, held)

    async def commit() -> CombinedDecision:
        return own

    async def cancel() -> Cancelled:
        if not own.allowed:
            return Cancelled(tuple(refunded))
        return Cancelled(tuple(await _refund_each_async(entries, held)))

    return AsyncPending("reserve", own, refunded, commit, cancel)


def check_all(entries: Entries) -> CombinedDecision:
    """Apply several limits to one request, such as per IP and per account, so that a request
    one of them denies does not use up the others: `peek_all` followed at once by `commit`.

    It peeks every `(limiter, id)` first and counts only when all of them would allow. The peek
    is advisory: two requests racing for the last unit can both pass it, and then the second is
    denied while counting, with the limits before it already counted. Needs stores that can peek.

    An entry may carry a third value, the cost of the request under that limit. Before any store
    call, raises `ValueError` for an empty list or a cost a limit does not accept, and
    `TypeError` for a limiter of tier approximate.
    """
    return _open_peek("check_all", entries).commit()


async def check_all_async(entries: AsyncEntries) -> CombinedDecision:
    """`check_all` for `AsyncLimiter`."""
    return await (await _open_peek_async("check_all", entries)).commit()


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
        nothing is counted and the error is yours. Raises `TypeError`, before any store call, if
        the limiter is of tier approximate.
        """
        _validate("a lockout", [(self._limiter, id_value)])
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
        _validate("a lockout", [(self._limiter, id_value)])
        gate = await self._limiter.peek(id_value)
        if not gate.allowed:
            return Attempt(allowed=False, verified=None, decision=gate)
        result = verify()
        if await result if inspect.isawaitable(result) else result:
            if self._reset_on_success:
                await self._limiter.reset(id_value)
            return Attempt(allowed=True, verified=True, decision=gate)
        return Attempt(allowed=True, verified=False, decision=await self._limiter.check(id_value))
