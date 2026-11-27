"""Rate limiting that behaves the same across stacks, and proves it. See spec/SPEC.md."""

from importlib.metadata import PackageNotFoundError, version

from omnilimit._gcra import PolicyArgs, parse_policy
from omnilimit._generated import SPEC_VERSION
from omnilimit._headers import build_headers
from omnilimit._limiter import AsyncLimiter, Limiter
from omnilimit._recipes import (
    AsyncLockout,
    AsyncLockoutGate,
    AsyncPending,
    Attempt,
    Cancelled,
    CombinedDecision,
    Lockout,
    LockoutGate,
    LockoutLimit,
    Pending,
    SettledError,
    Succeeded,
    check_all,
    check_all_async,
    peek_all,
    peek_all_async,
    reserve_all,
    reserve_all_async,
)
from omnilimit._types import (
    AsyncStore,
    Decision,
    HeaderInput,
    HeaderProfile,
    HitParams,
    LimiterEvent,
    OnStoreError,
    Op,
    RawHit,
    RawRefund,
    Reason,
    Reservation,
    Reserved,
    Store,
    Tier,
)
from omnilimit.stores.memory import MemoryStore

try:
    __version__ = version("omnilimit")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0.6.1"

__all__ = [
    "SPEC_VERSION",
    "AsyncLimiter",
    "AsyncLockout",
    "AsyncLockoutGate",
    "AsyncPending",
    "AsyncStore",
    "Attempt",
    "Cancelled",
    "CombinedDecision",
    "Decision",
    "HeaderInput",
    "HeaderProfile",
    "HitParams",
    "Limiter",
    "LimiterEvent",
    "Lockout",
    "LockoutGate",
    "LockoutLimit",
    "MemoryStore",
    "OnStoreError",
    "Op",
    "Pending",
    "PolicyArgs",
    "RawHit",
    "RawRefund",
    "Reason",
    "Reservation",
    "Reserved",
    "SettledError",
    "Store",
    "Succeeded",
    "Tier",
    "__version__",
    "build_headers",
    "check_all",
    "check_all_async",
    "parse_policy",
    "peek_all",
    "peek_all_async",
    "reserve_all",
    "reserve_all_async",
]
