"""Bucket keys. The id is hashed: keys stay short and stores never hold raw IPs."""

from __future__ import annotations

import hashlib

from omnilimit._gcra import ResolvedPolicy

_HASH_HEX_LENGTH = 32
_STATE_FORMAT = 1


def _utf8(text: str) -> bytes:
    """Encode like JavaScript's TextEncoder, where a lone surrogate becomes U+FFFD."""
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:
        repaired = text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
        return repaired.encode("utf-8")


def derive_key(scope: str, policy: ResolvedPolicy, salt: str, id_value: str) -> str:
    """Build the bucket key. The policy is part of it, since levels are measured in windows."""
    digest = hashlib.sha256(_utf8(salt) + b"\x00" + _utf8(id_value)).hexdigest()
    shape = f"{policy.limit}-{policy.window_ms}-{policy.burst}"
    return f"ol:{_STATE_FORMAT}:{scope}:{shape}:{digest[:_HASH_HEX_LENGTH]}"
