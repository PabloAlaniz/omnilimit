"""Rate limiting that behaves the same across stacks, and proves it. See spec/SPEC.md."""

from importlib.metadata import PackageNotFoundError, version

from omnilimit._generated import SPEC_VERSION
from omnilimit._headers import build_headers
from omnilimit._limiter import AsyncLimiter, Limiter
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
    Reason,
    Store,
    Tier,
)
from omnilimit.stores.memory import MemoryStore

try:
    __version__ = version("omnilimit")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0.1.1"

__all__ = [
    "SPEC_VERSION",
    "AsyncLimiter",
    "AsyncStore",
    "Decision",
    "HeaderInput",
    "HeaderProfile",
    "HitParams",
    "Limiter",
    "LimiterEvent",
    "MemoryStore",
    "OnStoreError",
    "Op",
    "RawHit",
    "Reason",
    "Store",
    "Tier",
    "__version__",
    "build_headers",
]
