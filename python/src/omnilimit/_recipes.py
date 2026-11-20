"""Compositions of check, peek, reset, reserve and refund with a fixed meaning.

See "Recipes" in the spec.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Generic, Literal, TypeVar, cast, overload

from omnilimit._limiter import AsyncLimiter, Limiter, _store_method
from omnilimit._types import Decision, Reservation

Mode = Literal["peek", "reserve"]
LockoutMode = Literal["advisory", "reserve"]
S = TypeVar("S")


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


@dataclass(frozen=True, slots=True)
class _Opened:
    """What opening a combined check found: its own result, what it gave back, what it holds."""

    own: CombinedDecision
    refunded: list[bool | None]
    held: list[Reservation | None]


class _Settlement:
    """What pendings and gates share: the result of opening, and how it was settled."""

    __slots__ = ("_by", "_own", "_refunded", "_what", "mode")

    def __init__(self, mode: str, opened: _Opened, what: str) -> None:
        self.mode = mode
        self._own = opened.own
        self._refunded = tuple(opened.refunded)
        self._by: str | None = None
        self._what = what

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
        """True once it was settled."""
        return self._by is not None

    def _claim(self, op: str) -> bool:
        """Rule 8: True for the first settlement, False to repeat it, else a `SettledError`."""
        if self._by is None:
            self._by = op
            return True
        if self._by != op:
            raise SettledError(f"omnilimit: this {self._what} was already settled by {self._by}")
        return False


class _Sync(_Settlement):
    """Settlement for blocking code: a lock, so a second thread waits for the first result."""

    __slots__ = ("_lock", "_result")

    def __init__(self, mode: str, opened: _Opened, what: str) -> None:
        super().__init__(mode, opened, what)
        self._lock = threading.Lock()
        self._result: Any = None

    def _settle(self, op: str, work: Callable[[], Any]) -> Any:
        with self._lock:
            if self._claim(op):
                self._result = work()
            return self._result


class _Async(_Settlement):
    """Settlement for event loops: one task, which every caller awaits."""

    __slots__ = ("_task",)

    def __init__(self, mode: str, opened: _Opened, what: str) -> None:
        super().__init__(mode, opened, what)
        self._task: asyncio.Future[Any] | None = None

    async def _settle(self, op: str, work: Callable[[], Awaitable[Any]]) -> Any:
        if self._claim(op):
            self._task = asyncio.ensure_future(work())
        task = cast("asyncio.Future[Any]", self._task)
        # Shielded: a caller that gives up does not cancel what the others wait for.
        return await asyncio.shield(task)


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


def _peek_each(recipe: str, entries: Entries) -> _Opened:
    _validate(recipe, entries)
    own = _peeked([entry[0].peek(entry[1], cost=_cost(entry)) for entry in entries])
    return _Opened(own, [None] * len(entries), [None] * len(entries))


async def _peek_each_async(recipe: str, entries: AsyncEntries) -> _Opened:
    _validate(recipe, entries)
    own = _peeked([await entry[0].peek(entry[1], cost=_cost(entry)) for entry in entries])
    return _Opened(own, [None] * len(entries), [None] * len(entries))


def _reserve_each(recipe: str, entries: Entries) -> _Opened:
    _validate(recipe, entries, "reserve")
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
        return _Opened(own, refunded, [None] * len(decisions))
    return _Opened(own, [None] * len(decisions), held)


async def _reserve_each_async(recipe: str, entries: AsyncEntries) -> _Opened:
    _validate(recipe, entries, "reserve")
    decisions: list[Decision] = []
    held: list[Reservation | None] = []
    for entry in entries:
        reserved = await entry[0].reserve(entry[1], cost=_cost(entry))
        decisions.append(reserved.decision)
        held.append(reserved.reservation)
        if not reserved.decision.allowed:
            break
    own = _counted(decisions)
    if not own.allowed:
        # Give back what was counted before the denial, in entry order. The denial holds nothing.
        refunded = await _refund_each_async(entries, held)
        return _Opened(own, refunded, [None] * len(decisions))
    return _Opened(own, [None] * len(decisions), held)


class Pending(_Sync):
    """The first phase of a combined check, from `peek_all` or `reserve_all`.

    Settle it once: `commit()` when the work happened, `cancel()` when it did not. Calling the
    same one again returns the first result, also from another thread while the first runs;
    calling the other one raises `SettledError`. See "Combined check" in the spec.
    """

    __slots__ = ("_entries", "_held")

    mode: Mode

    def __init__(self, mode: Mode, entries: Entries, opened: _Opened) -> None:
        super().__init__(mode, opened, "pending check")
        self._entries = entries
        self._held = opened.held

    def commit(self) -> CombinedDecision:
        """The work happened.

        In `peek` mode this counts every entry, in order, and stops at the first denial: the
        entries before it stay counted. In `reserve` mode, and on a denied pending, it makes no
        store call and returns the pending's own result.
        """
        own = self._own
        work = (
            (lambda: _count_all(self._entries))
            if self.mode == "peek" and own.allowed
            else (lambda: own)
        )
        result: CombinedDecision = self._settle("commit", work)
        return result

    def cancel(self) -> Cancelled:
        """The work did not happen.

        In `reserve` mode this refunds every reservation, in order; otherwise it makes no store
        call. It never raises for a store failure: a failed refund is `False`.
        """
        result: Cancelled = self._settle("cancel", self._cancelled)
        return result

    def _cancelled(self) -> Cancelled:
        if not self.allowed:
            return Cancelled(self.refunded)
        return Cancelled(tuple(_refund_each(self._entries, self._held)))


class AsyncPending(_Async):
    """`Pending` for `AsyncLimiter`. Concurrent settlements share one task, which starts when
    the first of them runs."""

    __slots__ = ("_entries", "_held")

    mode: Mode

    def __init__(self, mode: Mode, entries: AsyncEntries, opened: _Opened) -> None:
        super().__init__(mode, opened, "pending check")
        self._entries = entries
        self._held = opened.held

    async def commit(self) -> CombinedDecision:
        """The work happened. See `Pending.commit`."""
        result: CombinedDecision = await self._settle("commit", self._committed)
        return result

    async def cancel(self) -> Cancelled:
        """The work did not happen. See `Pending.cancel`."""
        result: Cancelled = await self._settle("cancel", self._cancelled)
        return result

    async def _committed(self) -> CombinedDecision:
        if self.mode == "peek" and self.allowed:
            return await _count_all_async(self._entries)
        return self._own

    async def _cancelled(self) -> Cancelled:
        if not self.allowed:
            return Cancelled(self.refunded)
        return Cancelled(tuple(await _refund_each_async(self._entries, self._held)))


def peek_all(entries: Entries) -> Pending:
    """Open a combined check in `peek` mode: peek every `(limiter, id)` and count nothing.

    `commit()` then counts every limit and `cancel()` leaves them alone. Advisory: two requests
    racing for the last unit can both pass the peek, and then the second is denied while
    committing, with the limits before it already counted. A pending never settled counts
    nothing. Before any store call, raises `ValueError` for an empty list or a cost a limit does
    not accept, and `TypeError` for a limiter of tier approximate.
    """
    return Pending("peek", entries, _peek_each("peek_all", entries))


async def peek_all_async(entries: AsyncEntries) -> AsyncPending:
    """`peek_all` for `AsyncLimiter`."""
    return AsyncPending("peek", entries, await _peek_each_async("peek_all", entries))


def reserve_all(entries: Entries) -> Pending:
    """Open a combined check in `reserve` mode: count every `(limiter, id)`, in order.

    It stops at the first denial and refunds the limits counted before it. When allowed,
    `commit()` keeps the counts and `cancel()` refunds them all. Unlike `peek_all` it never lets
    more through than the limits allow, at the price of holding the earlier limits for a round
    trip when a later one denies. A pending never settled stays counted. Before any store call,
    raises `ValueError` for an empty list or a cost a limit does not accept, and `TypeError` for a
    limiter whose store cannot refund, such as tier approximate.
    """
    return Pending("reserve", entries, _reserve_each("reserve_all", entries))


async def reserve_all_async(entries: AsyncEntries) -> AsyncPending:
    """`reserve_all` for `AsyncLimiter`."""
    return AsyncPending("reserve", entries, await _reserve_each_async("reserve_all", entries))


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
    return Pending("peek", entries, _peek_each("check_all", entries)).commit()


async def check_all_async(entries: AsyncEntries) -> CombinedDecision:
    """`check_all` for `AsyncLimiter`."""
    opened = await _peek_each_async("check_all", entries)
    return await AsyncPending("peek", entries, opened).commit()


@dataclass(frozen=True, slots=True)
class Succeeded:
    """What a gate's `succeed` answers: the gate's own result, with what it reset and refunded.

    `resets` and `refunded` hold one value per limit: `True` done, `False` the store failed, and
    `None` nothing to do (a limit that does not reset on success, or no reservation to refund).
    """

    allowed: bool
    decision: Decision
    decisions: tuple[Decision, ...]
    resets: tuple[bool | None, ...]
    refunded: tuple[bool | None, ...]


@dataclass(frozen=True, slots=True)
class Attempt:
    """What a lockout answers.

    `allowed` is false when there were too many failures: the verification did not run,
    `verified` is `None`, and `decision` is the denial to answer with. Otherwise `verified`
    says how it went, and after a failure `decision` is the one that counted it. `decisions` holds
    every limit's decision, and after a success `resets` says which limits were reset.
    """

    allowed: bool
    verified: bool | None
    decision: Decision
    decisions: tuple[Decision, ...] = ()
    resets: tuple[bool | None, ...] | None = None


@dataclass(frozen=True, slots=True)
class LockoutLimit(Generic[S]):
    """One limit of a lockout: its limiter, how to take its id from the subject, and whether a
    success resets it. `id_of` may be left out only when the subject is a string, its own id."""

    limiter: Limiter | AsyncLimiter
    id_of: Callable[[S], str] | None = None
    reset_on_success: bool = True


def _limits(
    limiter: Any, reset_on_success: bool, limits: Any, mode: str
) -> tuple[tuple[LockoutLimit[Any], ...], LockoutMode]:
    """The limits and mode of either form, checked before any store call."""
    if (limiter is None) == (limits is None):
        raise TypeError("omnilimit: a lockout takes a limiter or limits=, not both")
    if limits is None:
        return (LockoutLimit(limiter, None, reset_on_success),), "advisory"
    if len(limits) == 0:
        raise ValueError("omnilimit: a lockout needs at least one limit")
    if mode not in ("advisory", "reserve"):
        raise ValueError("omnilimit: a lockout's mode is 'advisory' or 'reserve'")
    return tuple(limits), cast("LockoutMode", mode)


def _entries_of(limits: tuple[LockoutLimit[Any], ...], subject: Any) -> list[tuple[Any, str]]:
    entries: list[tuple[Any, str]] = []
    for limit in limits:
        if limit.id_of is not None:
            entries.append((limit.limiter, limit.id_of(subject)))
        elif isinstance(subject, str):
            entries.append((limit.limiter, subject))
        else:
            raise TypeError("omnilimit: a lockout limit needs id_of when the subject is not a str")
    return entries


class LockoutGate(_Sync):
    """One attempt in front of a `Lockout`. Run the verification only when `allowed`, then
    settle it once: `fail()`, `succeed()`, or `cancel()` when the verification could not tell.

    On a denied gate all three make no store call, so a correct guess cannot open a locked gate.
    Settlement follows the rules of `Pending`. See "Lockout" in the spec.
    """

    __slots__ = ("_entries", "_held", "_resetting")

    mode: LockoutMode

    def __init__(
        self, mode: LockoutMode, entries: Entries, resetting: list[bool], opened: _Opened
    ) -> None:
        super().__init__(mode, opened, "lockout gate")
        self._entries, self._resetting, self._held = entries, resetting, opened.held

    def fail(self) -> CombinedDecision:
        """The verification failed.

        Advisory: counts the failure under every limit, in order, without stopping at a denial.
        Reserve: the gate already counted it, so no store call.
        """
        result: CombinedDecision = self._settle("fail", self._failed)
        return result

    def succeed(self) -> Succeeded:
        """The verification passed: resets the limits that reset on success, and in reserve
        mode refunds the others. A failed reset or refund is `False` and does not stop the rest."""
        result: Succeeded = self._settle("succeed", self._succeeded)
        return result

    def cancel(self) -> Cancelled:
        """The verification could not tell (it raised): counts nothing, refunds in reserve mode."""
        result: Cancelled = self._settle("cancel", self._cancelled)
        return result

    def _failed(self) -> CombinedDecision:
        if self.mode == "reserve" or not self.allowed:
            return self._own
        # The failure happened: every limit records it, whatever an earlier one answers.
        return _peeked([entry[0].check(entry[1]) for entry in self._entries])

    def _succeeded(self) -> Succeeded:
        resets: list[bool | None] = [None] * len(self.decisions)
        refunded: list[bool | None] = [None] * len(self.decisions)
        if self.allowed:
            for index, (limiter, id_value, *_) in enumerate(self._entries):
                if self._resetting[index]:
                    resets[index] = limiter.reset(id_value)
                elif self.mode == "reserve":
                    refunded[index] = _refund_each([self._entries[index]], [self._held[index]])[0]
        own = self._own
        return Succeeded(own.allowed, own.decision, own.decisions, tuple(resets), tuple(refunded))

    def _cancelled(self) -> Cancelled:
        if not self.allowed:
            return Cancelled(self.refunded)
        return Cancelled(tuple(_refund_each(self._entries, self._held)))


class AsyncLockoutGate(_Async):
    """`LockoutGate` for `AsyncLockout`."""

    __slots__ = ("_entries", "_held", "_resetting")

    mode: LockoutMode

    def __init__(
        self, mode: LockoutMode, entries: AsyncEntries, resetting: list[bool], opened: _Opened
    ) -> None:
        super().__init__(mode, opened, "lockout gate")
        self._entries, self._resetting, self._held = entries, resetting, opened.held

    async def fail(self) -> CombinedDecision:
        """The verification failed. See `LockoutGate.fail`."""
        result: CombinedDecision = await self._settle("fail", self._failed)
        return result

    async def succeed(self) -> Succeeded:
        """The verification passed. See `LockoutGate.succeed`."""
        result: Succeeded = await self._settle("succeed", self._succeeded)
        return result

    async def cancel(self) -> Cancelled:
        """The verification could not tell. See `LockoutGate.cancel`."""
        result: Cancelled = await self._settle("cancel", self._cancelled)
        return result

    async def _failed(self) -> CombinedDecision:
        if self.mode == "reserve" or not self.allowed:
            return self._own
        # The failure happened: every limit records it, whatever an earlier one answers.
        return _peeked([await entry[0].check(entry[1]) for entry in self._entries])

    async def _succeeded(self) -> Succeeded:
        resets: list[bool | None] = [None] * len(self.decisions)
        refunded: list[bool | None] = [None] * len(self.decisions)
        if self.allowed:
            for index, (limiter, id_value, *_) in enumerate(self._entries):
                if self._resetting[index]:
                    resets[index] = await limiter.reset(id_value)
                elif self.mode == "reserve":
                    given = await _refund_each_async([self._entries[index]], [self._held[index]])
                    refunded[index] = given[0]
        own = self._own
        return Succeeded(own.allowed, own.decision, own.decisions, tuple(resets), tuple(refunded))

    async def _cancelled(self) -> Cancelled:
        if not self.allowed:
            return Cancelled(self.refunded)
        return Cancelled(tuple(await _refund_each_async(self._entries, self._held)))


def _attempt_denied(gate: _Settlement) -> Attempt:
    return Attempt(allowed=False, verified=None, decision=gate.decision, decisions=gate.decisions)


class Lockout(Generic[S]):
    """A limit that counts only failed attempts, as a sign-in does.

    While the budget of failures is spent, even a correct attempt is refused without being
    checked, which is what makes guessing useless. `Lockout(limiter)` is the advisory one-limit
    lockout of earlier versions, with the id as subject. `Lockout(limits=[...], mode=...)` counts
    each failure under every limit, such as per subject and per IP, each with the id its `id_of`
    takes from the subject. `mode="reserve"` counts before the verification and gives back on
    success, so no more verifications run than the limits allow: prefer it for credential checks.
    """

    @overload
    def __init__(
        self: Lockout[str], limiter: Limiter, *, reset_on_success: bool = True
    ) -> None: ...

    @overload
    def __init__(
        self, *, limits: Sequence[LockoutLimit[S]], mode: LockoutMode = "advisory"
    ) -> None: ...

    def __init__(
        self,
        limiter: Limiter | None = None,
        *,
        reset_on_success: bool = True,
        limits: Sequence[LockoutLimit[S]] | None = None,
        mode: LockoutMode = "advisory",
    ) -> None:
        self._limits, self._mode = _limits(limiter, reset_on_success, limits, mode)

    def begin(self, subject: S) -> LockoutGate:
        """Open a gate for `subject`. Raises `TypeError`, before any store call, if a limiter is
        of tier approximate, cannot refund in reserve mode, or has no `id_of` for a subject that is
        not a str."""
        entries = _entries_of(self._limits, subject)
        if self._mode == "reserve":
            opened = _reserve_each("a lockout", entries)
        else:
            opened = _peek_each("a lockout", entries)
        resetting = [limit.reset_on_success for limit in self._limits]
        return LockoutGate(self._mode, entries, resetting, opened)

    def attempt(self, subject: S, verify: Callable[[], bool]) -> Attempt:
        """Run `verify` unless `subject` has failed too often.

        A false result counts as a failure and a true one clears the limits that reset on
        success. If `verify` raises, nothing is counted and the error is yours. Raises as
        `begin` does.
        """
        gate = self.begin(subject)
        if not gate.allowed:
            return _attempt_denied(gate)
        try:
            verified = verify()
        except BaseException:
            # Nothing is counted for an attempt that could not tell.
            gate.cancel()
            raise
        if verified:
            done = gate.succeed()
            return Attempt(True, True, done.decision, done.decisions, done.resets)
        failed = gate.fail()
        return Attempt(True, False, failed.decision, failed.decisions)


class AsyncLockout(Generic[S]):
    """`Lockout` for `AsyncLimiter`. `verify` may be a plain function or a coroutine function."""

    @overload
    def __init__(
        self: AsyncLockout[str], limiter: AsyncLimiter, *, reset_on_success: bool = True
    ) -> None: ...

    @overload
    def __init__(
        self, *, limits: Sequence[LockoutLimit[S]], mode: LockoutMode = "advisory"
    ) -> None: ...

    def __init__(
        self,
        limiter: AsyncLimiter | None = None,
        *,
        reset_on_success: bool = True,
        limits: Sequence[LockoutLimit[S]] | None = None,
        mode: LockoutMode = "advisory",
    ) -> None:
        self._limits, self._mode = _limits(limiter, reset_on_success, limits, mode)

    async def begin(self, subject: S) -> AsyncLockoutGate:
        """Open a gate for `subject`. See `Lockout.begin`."""
        entries = _entries_of(self._limits, subject)
        if self._mode == "reserve":
            opened = await _reserve_each_async("a lockout", entries)
        else:
            opened = await _peek_each_async("a lockout", entries)
        resetting = [limit.reset_on_success for limit in self._limits]
        return AsyncLockoutGate(self._mode, entries, resetting, opened)

    async def attempt(self, subject: S, verify: Callable[[], Awaitable[bool] | bool]) -> Attempt:
        """Run `verify` unless `subject` has failed too often. See `Lockout.attempt`."""
        gate = await self.begin(subject)
        if not gate.allowed:
            return _attempt_denied(gate)
        try:
            result = verify()
            verified = await result if inspect.isawaitable(result) else result
        except BaseException:
            await gate.cancel()
            raise
        if verified:
            done = await gate.succeed()
            return Attempt(True, True, done.decision, done.decisions, done.resets)
        failed = await gate.fail()
        return Attempt(True, False, failed.decision, failed.decisions)
