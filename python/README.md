# omnilimit for Python

Rate limiting that behaves the same across stacks, and proves it. This port follows the same [spec](https://github.com/PabloAlaniz/omnilimit/blob/main/spec/SPEC.md) and passes the same conformance vectors as the TypeScript one, and both can share a counter in Redis or Postgres.

- **Trusted client IPs.** It reads `X-Forwarded-For` from the right. Rotating the header does not evade the limit.
- **An explicit failure mode.** You must decide, per limiter, what happens when the store is down.
- **No dependencies.** Extras only install a client: `omnilimit[redis]`, `omnilimit[postgres]`, `omnilimit[fastapi]`.

```bash
pip install omnilimit
```

## Usage

```python
from redis import Redis

from omnilimit import Limiter
from omnilimit.stores.redis import RedisStore

client = Redis.from_url(url, socket_timeout=0.25, socket_connect_timeout=0.25)
limiter = Limiter(
    store=RedisStore(client),
    scope="login",
    limit=5,
    window_ms=60_000,
    burst=2,
    on_store_error="closed",  # required: "open" lets requests through, "closed" answers 503
)

decision = limiter.check(user_id)
if not decision.allowed:
    ...  # answer decision.status with decision.headers, which include Retry-After
```

`5` per minute with burst 5 can admit 9 requests in one minute: this is a rate, not a counter that resets. Lower `burst` if you need fewer.

## Peek and reset

```python
decision = limiter.peek(user_id)  # what check() would answer now, without counting
limiter.reset(user_id)  # forget the bucket, e.g. after a successful login
```

`peek` builds its decision like `check` and follows `on_store_error` the same way. When allowed, `remaining` includes the request you asked about. It is advisory: a concurrent request can take the last unit, so the `check` that follows is still the authority. `reset` returns `True` once the bucket is gone (or was never there) and `False` if the store failed or timed out, which `on_event` reports; it never raises for a store failure. Both raise `TypeError` if the store cannot do them, which is a configuration error. Every event carries `op` (`"check"`, `"peek"` or `"reset"`), and a successful reset emits type `"reset"`.

## Sync or async

| Code | Limiter | Stores |
|---|---|---|
| Flask, Django, `def` dependencies | `Limiter` | `MemoryStore`, `RedisStore`, `PostgresStore` |
| `async def` | `AsyncLimiter` | `MemoryStore`, `AsyncRedisStore`, `AsyncPostgresStore` |

`Limiter` runs each store call in its own daemon thread, because Python cannot abandon a blocking call. `store_timeout_ms` bounds the request, and `max_store_threads` bounds the calls in flight. Set socket timeouts on your client as well, so a hung call ends.

redis-py caps its pool and raises past that cap, which `omnilimit` reports as a store failure. If you expect bursts of concurrent checks, use `BlockingConnectionPool`.

## FastAPI

```python
from fastapi import Depends, FastAPI

from omnilimit import MemoryStore, Limiter
from omnilimit.fastapi import ip_key, rate_limit

limiter = Limiter(
    store=MemoryStore(), scope="api", limit=100, window_ms=60_000, on_store_error="open"
)
app = FastAPI(dependencies=[
    Depends(rate_limit(limiter, key=ip_key(preset="cloud-run"), on_missing_key="shared")),
])
```

Denials raise `HTTPException`, so your own handlers format them. Headers on allowed requests are not added when a route returns its own `Response` object, as a file download usually does.

To build the denial response yourself, pass `on_denied` and register its handler. It receives a `Denial` with `status`, `headers`, `error` (`"rate_limited"`, `"rate_limiter_unavailable"` or `"client_not_identified"`) and `decision` (`None` for a missing key):

```python
from fastapi.responses import JSONResponse

from omnilimit.fastapi import RateLimitDenied, denied_handler


def on_denied(request, denial):
    return JSONResponse({"error": denial.error}, status_code=denial.status, headers=denial.headers)


app.add_exception_handler(RateLimitDenied, denied_handler)  # without it, a denial raises TypeError
limit = Depends(rate_limit(limiter, key=..., on_missing_key="reject", on_denied=on_denied))
```

With an `AsyncLimiter`, `on_denied` may be `async`.

## Client IP only

`omnilimit.ip` uses only the standard library and also works next to `slowapi`:

```python
from omnilimit.ip import resolve_client_ip

ip = resolve_client_ip(request.headers, request.client.host, preset="cloud-run-lb")
```

It returns `None` rather than guessing. Check that the preset matches your deployment: see [client IP](https://github.com/PabloAlaniz/omnilimit/blob/main/docs/client-ip.md).

## Postgres

`omnilimit.stores.postgres.MIGRATION_SQL` creates the table and functions: apply it with your migration tool. Stores take a psycopg 3 pool or a connection with `autocommit=True`. Each hit is a write, so it is not meant for high traffic, and `cleanup()` is yours to schedule. `MIGRATION_SQL` is idempotent: apply it again after upgrading, since `peek` needs `omnilimit_peek_v1`.
