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


def key_prefix(scope: str, policy: ResolvedPolicy) -> str:
    """The part of a bucket key that a limiter never changes: everything before the hash."""
    shape = f"{policy.limit}-{policy.window_ms}-{policy.burst}"
    return f"ol:{_STATE_FORMAT}:{scope}:{shape}:"


def salt_prefix(salt: str) -> bytes:
    """What is hashed before the id."""
    return _utf8(salt) + b"\x00"


def bucket_key(prefix: str, salted: bytes, id_value: str) -> str:
    """`derive_key`, from a `key_prefix` and a `salt_prefix` computed once."""
    try:
        encoded = id_value.encode()
    except UnicodeEncodeError:
        encoded = _utf8(id_value)
    return prefix + hashlib.sha256(salted + encoded).hexdigest()[:_HASH_HEX_LENGTH]


def derive_key(scope: str, policy: ResolvedPolicy, salt: str, id_value: str) -> str:
    """Build the bucket key. The policy is part of it, since levels are measured in windows."""
    return bucket_key(key_prefix(scope, policy), salt_prefix(salt), id_value)
