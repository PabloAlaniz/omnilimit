# Adapters

Every adapter takes the same things: a `limiter`, a `key`, `onMissingKey`, and optionally `onDenied`, `skip` and `cost`.

| Runtime | Import | Shape |
|---|---|---|
| Hono | `omnilimit/hono` | `rateLimit(options)` middleware |
| Express | `omnilimit/express` | `rateLimit(options)` middleware |
| Next.js | `omnilimit/next` | `withRateLimit(handler, options)`, `checkRateLimit(request, options)` |
| Anything with a Fetch API `Request` | `omnilimit/web` | the same two functions |
| FastAPI | `omnilimit.fastapi` | `Depends(rate_limit(limiter, ...))` |
| Any ASGI application | `omnilimit.asgi` | `RateLimitMiddleware` |

`omnilimit/next` is `omnilimit/web` under the name Next users look for.

## A plain `Request`

`omnilimit/web` works wherever a handler receives a `Request`: a Worker's `fetch`, an Astro or SvelteKit endpoint, a Next `proxy.ts`.

```ts
import { checkRateLimit, ipKey } from 'omnilimit/web';

const options = { limiter, key: ipKey({ preset: 'cloudflare' }), onMissingKey: 'shared' as const };

export async function POST({ request }: { request: Request }) {
  const { denied, headers } = await checkRateLimit(request, options);
  if (denied) return denied;
  return Response.json({ ok: true }, { headers });
}
```

`withRateLimit(handler, options)` does the same around a handler and adds the headers to its response.

A `Request` does not carry the TCP peer. If your runtime exposes it, pass `peer`; otherwise the `direct` preset finds no address.

## A limiter that only exists inside the request

On Workers, a binding lives in `env`, so a limiter built at module load cannot reach its store. Pass a function instead of a limiter. It receives the same context as `key`:

```ts
import { createLimiter, type Limiter } from 'omnilimit';
import { durableObjectStore } from 'omnilimit/cloudflare/do';
import { ipKey, rateLimit } from 'omnilimit/hono';

const limiters = new WeakMap<object, Limiter>();

app.use(rateLimit({
  limiter: ({ c }) => {
    const binding = c.env.RATE_LIMITER;
    let limiter = limiters.get(binding);
    if (!limiter) {
      limiter = createLimiter({ store: durableObjectStore(binding), scope: 'api', limit: 100, windowMs: 60_000, onStoreError: 'open' });
      limiters.set(binding, limiter);
    }
    return limiter;
  },
  key: ipKey({ preset: 'cloudflare' }),
  onMissingKey: 'shared',
}));
```

The function runs on every request, so cache what it builds. It is not called for a request that `skip` let through or that `onMissingKey` already answered.

With `omnilimit/web`, whatever you pass as the third argument of `checkRateLimit` arrives as `extra`. Under `withRateLimit` it is the handler's second argument:

```ts
export default {
  fetch: withRateLimit(handler, {
    limiter: ({ extra: env }) => limiterFor(env.RATE_LIMITER),
    key: ipKey({ preset: 'cloudflare' }),
    onMissingKey: 'shared',
  }),
};
```

## Keying by who the caller is (FastAPI)

A limit by user or API key needs the identity your authentication dependency resolves. Pass that dependency as `identity`: what it returns reaches `key`, `skip` and a limiter function as `context.identity`.

```python
from fastapi import Depends

from omnilimit.fastapi import rate_limit

limit = rate_limit(
    limiter,
    identity=current_user,                  # your own dependency
    key=lambda context: context.identity.id,
    on_missing_key="reject",
)

@app.post("/reports", dependencies=[Depends(limit)])
def create_report(user: User = Depends(current_user)): ...
```

FastAPI resolves a dependency once per request, so `current_user` does not run twice. If it raises, for example a 401, nothing is counted.

The limiter can also be a function of the context, to choose one per request, such as one per plan:

```python
limit = rate_limit(
    lambda context: limiters[context.identity.plan],
    identity=current_user,
    key=lambda context: context.identity.id,
    on_missing_key="reject",
)
```

It runs on every request, so build the limiters once and look them up. A blocking `Limiter` chosen this way runs in the thread pool.

## A limit over a whole ASGI application

`omnilimit.asgi.RateLimitMiddleware` is plain ASGI and imports no framework: it works under Starlette, FastAPI, Litestar, Quart or Django, and around an application mounted inside another.

```python
from omnilimit.asgi import RateLimitMiddleware, ip_key

app.add_middleware(
    RateLimitMiddleware,
    limiter=limiter,
    key=ip_key(preset="cloud-run"),
    on_missing_key="shared",
    skip=lambda context: context.path in ("/health", "/metrics"),
)
```

The context has `headers`, `peer`, `method`, `path` and the raw `scope`. To answer in your own format, `on_denied` returns a `DeniedResponse(body, media_type, status, headers)`; the denial's own headers are always sent.

Two things to know:

- A middleware runs before routing and before any dependency, so it cannot key by an identity resolved later. Use the FastAPI dependency for that.
- If you also use a CORS middleware, add this one first so CORS ends up outermost. Otherwise browsers hide the `429` from your frontend.

## Letting some requests through

`skip` returns `true` for requests that should pass without being counted: a health check, a test environment, a feature flag. It runs before `key`, and a skipped response carries no rate limit headers.

```ts
rateLimit({ limiter, key, onMissingKey: 'shared', skip: ({ c }) => c.env.RATE_LIMIT_OFF === 'true' });
```

```python
Depends(rate_limit(limiter, key=key, on_missing_key="shared", skip=lambda ctx: ctx.request.url.path == "/health"))
```

Keep the condition narrow. A `skip` that is true in production is a limit that does nothing, and unlike a store failure it emits no event.

## Requests that weigh more than one

`cost` returns how many units a request spends, for endpoints that do several things at once:

```ts
rateLimit({ limiter, key, onMissingKey: 'shared', cost: async ({ c }) => (await c.req.json()).items.length });
```

```python
Depends(rate_limit(limiter, key=key, on_missing_key="shared", cost=lambda ctx: int(ctx.headers["x-items"])))
```

It must be an integer from 1 to the limiter's burst, so validate or clamp what the client sends before returning it: anything else raises. A store of tier `approximate` accepts no cost other than 1.

## Writing your own key

`ipKey` covers the common case. When you call the limiter yourself, `clientKey` gives you the same id: the trusted IP with IPv6 grouped by /64, or `null`.

```ts
import { clientKey } from 'omnilimit/ip';

const id = clientKey({ headers: request.headers }, { preset: 'cloudflare' }) ?? 'shared';
const decision = await limiter.check(`${email}:${id}`);
```

```python
from omnilimit.ip import client_key

client = client_key(request.headers, preset="cloudflare") or "shared"
```

Decide what `null` means on purpose. One shared bucket is safe but a single client can exhaust it; see [client IP](client-ip.md).
