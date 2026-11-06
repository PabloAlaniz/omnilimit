"""Trusted client IPs, using only the standard library.

This module depends on nothing else in omnilimit, so it also works next to `slowapi` or anything
else needing a client IP. It returns `None` rather than guessing when the configured source is
missing or malformed, and it never falls back to the first value of `X-Forwarded-For`.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any, Literal

IpPreset = Literal["cloudflare", "cloud-run", "cloud-run-lb", "vercel", "direct"]

_IPV4_OCTET = re.compile(r"0|[1-9][0-9]{0,2}")

# preset: (header to read, X-Forwarded-For hops); neither means the TCP peer
_PRESETS: dict[str, tuple[str | None, int | None]] = {
    "cloudflare": ("cf-connecting-ip", None),
    "cloud-run": (None, 0),
    "cloud-run-lb": (None, 1),
    "vercel": ("x-vercel-forwarded-for", None),
    "direct": (None, None),
}


def _read_header(headers: Any, name: str) -> str | None:
    # A header can arrive on several lines, and `.get` on Starlette or Werkzeug headers only
    # returns the first one: a client-supplied line could then pass for the trusted value.
    getlist = getattr(headers, "getlist", None)
    value = getlist(name) if callable(getlist) else headers.get(name)
    if value is None:
        return None
    if isinstance(value, list | tuple):
        return ",".join(value) if value else None
    return str(value)


def _canonical_ipv4(raw: str) -> str | None:
    parts = raw.split(".")
    if len(parts) != 4:
        return None
    for part in parts:
        if _IPV4_OCTET.fullmatch(part) is None or int(part) > 255:
            return None
    return raw


def _parse_ipv6(raw: str) -> ipaddress.IPv6Address | None:
    # `ipaddress` accepts zone identifiers, which would give one host many buckets.
    if "%" in raw:
        return None
    try:
        return ipaddress.IPv6Address(raw)
    except ValueError:
        return None


def canonical_ip(raw: str | None) -> str | None:
    """Validate and normalise an address (RFC 5952; IPv4-mapped becomes IPv4), or `None`."""
    if not raw:
        return None
    if ":" not in raw:
        return _canonical_ipv4(raw)
    address = _parse_ipv6(raw)
    if address is None:
        return None
    if address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    return address.compressed


def mask_ip(ip: str, prefix: int) -> str | None:
    """Keep an IPv6 network prefix, since one customer commonly controls a whole /64.

    IPv4 addresses are returned unchanged.
    """
    canonical = canonical_ip(ip)
    if canonical is None:
        return None
    if ":" not in canonical:
        return canonical
    if not isinstance(prefix, int) or isinstance(prefix, bool) or not 0 <= prefix <= 128:
        raise ValueError("omnilimit: IPv6 prefix must be an integer from 0 to 128")
    network = ipaddress.IPv6Network((canonical, prefix), strict=False)
    return network.network_address.compressed


def _from_xff(value: str | None, hops: int) -> str | None:
    if value is None:
        return None
    entries = [entry.strip() for entry in value.split(",")]
    # The client controls everything to the left of what your proxies appended, so a chain
    # that is too short must never fall back to its first value.
    if len(entries) <= hops:
        return None
    return canonical_ip(entries[len(entries) - 1 - hops])


def resolve_client_ip(
    headers: Any,
    peer: str | None = None,
    *,
    preset: IpPreset | None = None,
    xff_hops: int | None = None,
    header: str | None = None,
    use_peer: bool = False,
) -> str | None:
    """Resolve the client IP from the one source you name.

    Choose exactly one: `preset`, `xff_hops` (read `X-Forwarded-For` from the right, skipping
    entries added by your own proxies), `header` (a single-IP header your edge overwrites) or
    `use_peer` (the TCP peer). `headers` is any mapping with lowercase names, or the headers
    object of your framework.
    """
    chosen = sum((preset is not None, xff_hops is not None, header is not None, use_peer))
    if chosen != 1:
        raise ValueError("omnilimit: choose exactly one of preset, xff_hops, header or use_peer")
    if preset is not None:
        if preset not in _PRESETS:
            raise ValueError("omnilimit: unknown IP preset")
        header, xff_hops = _PRESETS[preset]
    if xff_hops is not None:
        if not isinstance(xff_hops, int) or isinstance(xff_hops, bool) or xff_hops < 0:
            raise ValueError("omnilimit: hops must be a non-negative integer")
        return _from_xff(_read_header(headers, "x-forwarded-for"), xff_hops)
    if header is not None:
        value = _read_header(headers, header.lower())
        return canonical_ip(value.strip() if value is not None else None)
    return canonical_ip(peer)
