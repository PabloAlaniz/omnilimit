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

To read the policy from configuration, `parse_policy("10/minute")` returns `limit` and `window_ms` ready to spread: `Limiter(**parse_policy(setting), store=..., scope=..., on_store_error=...)`. The grammar is `<limit>/<count><unit>` with `second`, `minute`, `hour` or `day`, and it is strict: `5/15minute` is valid, `10/min` is not.

## Peek and reset

```python
decision = limiter.peek(user_id)  # what check() would answer now, without counting
limiter.reset(user_id)  # forget the bucket, e.g. after a successful login
```

`peek` builds its decision like `check` and follows `on_store_error` the same way. When allowed, `remaining` includes the request you asked about. It is advisory: a concurrent request can take the last unit, so the `check` that follows is still the authority. `reset` returns `True` once the bucket is gone (or was never there) and `False` if the store failed or timed out, which `on_event` reports; it never raises for a store failure. Both raise `TypeError` if the store cannot do them, which is a configuration error. Every event carries `op` (`"check"`, `"peek"` or `"reset"`), and a successful reset emits type `"reset"`.

## Recipes

```python
from omnilimit import Lockout, check_all

# Several limits on one request: nothing is counted unless all of them allow.
result = check_all([(per_ip, ip), (per_account, email)])
if not result.allowed:
    ...  # answer result.decision.status with result.decision.headers

# Count only failed attempts, and refuse even a correct one once they are spent.
attempt = Lockout(failed_logins).attempt(f"{email}:{ip}", lambda: password_matches(email, password))
if not attempt.allowed:
    ...  # the verification did not run
```

With an `AsyncLimiter`, use `check_all_async` and `AsyncLockout`. Both are built on `peek`, so they are not atomic: see [recipes](https://github.com/PabloAlaniz/omnilimit/blob/main/docs/recipes.md).

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

To key by user or API key, pass your authentication dependency as `identity`. What it returns arrives as `context.identity`, FastAPI resolves it once per request, and a request it rejects is not counted:

```python
limit = rate_limit(limiter, identity=current_user, key=lambda ctx: ctx.identity.id, on_missing_key="reject")
```

`limiter` may also be a function of the context, to choose a limiter per request.

To let some requests through without counting them, pass `skip`. It runs before `key`:

```python
Depends(rate_limit(limiter, key=..., on_missing_key="shared", skip=lambda ctx: ctx.request.url.path == "/health"))
```

## Any ASGI application

`omnilimit.asgi.RateLimitMiddleware` limits a whole application, with `skip` for the paths to leave alone. It is plain ASGI, so it needs no extra and works under Starlette, FastAPI, Litestar, Quart or Django:

```python
from omnilimit.asgi import RateLimitMiddleware, ip_key

app.add_middleware(
    RateLimitMiddleware,
    limiter=limiter,
    key=ip_key(preset="cloud-run"),
    on_missing_key="shared",
    skip=lambda ctx: ctx.path == "/health",
)
```

It runs before routing, so it cannot key by an identity a dependency resolves later. See [adapters](https://github.com/PabloAlaniz/omnilimit/blob/main/docs/adapters.md).

## Client IP only

`omnilimit.ip` uses only the standard library and also works next to `slowapi`:

```python
from omnilimit.ip import resolve_client_ip

ip = resolve_client_ip(request.headers, request.client.host, preset="cloud-run-lb")
```

`client_key` takes the same arguments and also groups IPv6 by /64, which is the id a hand-written key wants: `client_key(request.headers, preset="cloudflare") or "shared"`.

It returns `None` rather than guessing. Check that the preset matches your deployment: see [client IP](https://github.com/PabloAlaniz/omnilimit/blob/main/docs/client-ip.md).

## Postgres

`omnilimit.stores.postgres.MIGRATION_SQL` creates the table and functions: apply it with your migration tool. Stores take a psycopg 3 pool or a connection with `autocommit=True`. Each hit is a write, so it is not meant for high traffic, and `cleanup()` is yours to schedule. `MIGRATION_SQL` is idempotent: apply it again after upgrading, since `peek` needs `omnilimit_peek_v1`.
