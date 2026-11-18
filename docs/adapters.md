# Adapters

Every adapter takes the same four things: a `limiter`, a `key`, `onMissingKey`, and optionally `onDenied` and `skip`.

| Runtime | Import | Shape |
|---|---|---|
| Hono | `omnilimit/hono` | `rateLimit(options)` middleware |
| Express | `omnilimit/express` | `rateLimit(options)` middleware |
| Next.js | `omnilimit/next` | `withRateLimit(handler, options)`, `checkRateLimit(request, options)` |
| Anything with a Fetch API `Request` | `omnilimit/web` | the same two functions |
| FastAPI | `omnilimit.fastapi` | `Depends(rate_limit(limiter, ...))` |

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

## Letting some requests through

`skip` returns `true` for requests that should pass without being counted: a health check, a test environment, a feature flag. It runs before `key`, and a skipped response carries no rate limit headers.

```ts
rateLimit({ limiter, key, onMissingKey: 'shared', skip: ({ c }) => c.env.RATE_LIMIT_OFF === 'true' });
```

```python
Depends(rate_limit(limiter, key=key, on_missing_key="shared", skip=lambda ctx: ctx.request.url.path == "/health"))
```

Keep the condition narrow. A `skip` that is true in production is a limit that does nothing, and unlike a store failure it emits no event.

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
