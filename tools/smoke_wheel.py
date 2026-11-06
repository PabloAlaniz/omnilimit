#!/usr/bin/env python3
"""Install the built wheel in clean environments and use it the way a user would.

Usage: smoke_wheel.py <dist dir>
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

CORE = """
import importlib.metadata as m
import sys
from omnilimit import Limiter, MemoryStore, SPEC_VERSION
from omnilimit._generated import REDIS_HIT_LUA, POSTGRES_MIGRATION_SQL
from omnilimit.ip import resolve_client_ip
import omnilimit.stores.redis, omnilimit.stores.postgres
assert REDIS_HIT_LUA and POSTGRES_MIGRATION_SQL and SPEC_VERSION == "1"
assert not m.requires("omnilimit") or all("extra ==" in r for r in m.requires("omnilimit"))
assert "redis" not in sys.modules and "psycopg" not in sys.modules
limiter = Limiter(store=MemoryStore(), scope="s", limit=1, window_ms=60000, on_store_error="closed")
assert limiter.check("a").allowed
denied = limiter.check("a")
assert denied.status == 429 and int(denied.headers["retry-after"]) > 0
assert resolve_client_ip({"x-forwarded-for": "6.6.6.6, 1.2.3.4"}, preset="cloud-run") == "1.2.3.4"
print("core ok")
"""

FASTAPI = """
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from omnilimit import Limiter, MemoryStore
from omnilimit.fastapi import ip_key, rate_limit
limiter = Limiter(store=MemoryStore(), scope="s", limit=1, window_ms=60000, on_store_error="closed")
dependency = rate_limit(limiter, key=ip_key(preset="cloud-run"), on_missing_key="reject")
app = FastAPI(dependencies=[Depends(dependency)])
app.get("/")(lambda: {"ok": True})
client = TestClient(app)
assert client.get("/", headers={"x-forwarded-for": "1.1.1.1, 1.2.3.4"}).status_code == 200
denied = client.get("/", headers={"x-forwarded-for": "2.2.2.2, 1.2.3.4"})
assert denied.status_code == 429 and "retry-after" in denied.headers
print("fastapi ok")
"""


def run(*command: str) -> None:
    subprocess.run(command, check=True)  # noqa: S603


def main() -> int:
    wheel = next(Path(sys.argv[1]).glob("*.whl")).resolve()
    with tempfile.TemporaryDirectory() as tmp:
        for name, extras, packages, code in (
            ("core", "", [], CORE),
            ("fastapi", "[fastapi]", ["httpx"], FASTAPI),
        ):
            venv = Path(tmp) / name
            python = str(venv / "bin" / "python")
            run("uv", "venv", "-q", str(venv))
            run("uv", "pip", "install", "-q", "--python", python, f"{wheel}{extras}", *packages)
            # -I keeps the repository off sys.path, so only the installed wheel can be imported.
            run(python, "-I", "-c", code)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
