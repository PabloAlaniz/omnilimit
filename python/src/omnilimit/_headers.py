"""Response headers. `draft-11` is pinned: a newer draft becomes a new profile."""

from __future__ import annotations

from omnilimit._gcra import ceil_div
from omnilimit._types import HeaderInput, HeaderProfile


def _seconds(ms: int) -> int:
    return ceil_div(ms, 1000)


def build_headers(profile: HeaderProfile, data: HeaderInput) -> dict[str, str]:
    decision = data.decision
    out: dict[str, str] = {}
    if decision.remaining is not None:
        t = _seconds(data.effective_ms)
        if profile == "draft-11":
            out["ratelimit-policy"] = f'"{data.scope}";q={data.limit};w={_seconds(data.window_ms)}'
            out["ratelimit"] = f'"{data.scope}";r={decision.remaining};t={t}'
        elif profile == "legacy":
            out["x-ratelimit-limit"] = str(data.limit)
            out["x-ratelimit-remaining"] = str(decision.remaining)
            out["x-ratelimit-reset"] = str(t)
    if not decision.allowed and decision.retry_after_ms is not None:
        out["retry-after"] = str(_seconds(decision.retry_after_ms))
    return out
