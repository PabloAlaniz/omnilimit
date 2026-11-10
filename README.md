# omnilimit

Rate limiting that behaves the same across stacks, and proves it.

[![CI](https://github.com/PabloAlaniz/omnilimit/actions/workflows/ci.yml/badge.svg)](https://github.com/PabloAlaniz/omnilimit/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> **Early release.** Available for TypeScript and [Python](python/README.md), using the same spec and vectors.

Most rate limiters cover one framework and leave you to get three things right on your own.
`omnilimit` makes them part of the contract:

- **Same behaviour everywhere.** One [spec](spec/SPEC.md) and shared conformance vectors, run against each store on every commit.
- **Trusted client IPs.** It reads `X-Forwarded-For` from the right. Rotating the header does not evade the limit.
- **An explicit failure mode.** You must decide, per limiter, what happens when the store is down.

## Install

```bash
pip install omnilimit  # see python/README.md
npm install omnilimit
```

Node 20+, Cloudflare Workers and other runtimes with WebCrypto. No dependencies: database clients are yours, and frameworks are optional.

## Use it

```ts
import { Hono } from 'hono';
import { createLimiter, memoryStore } from 'omnilimit';
import { ipKey, rateLimit } from 'omnilimit/hono';

const login = createLimiter({
  store: memoryStore(),
  scope: 'login',
  limit: 5,
  windowMs: 60_000,
  burst: 2,
  onStoreError: 'closed',
  salt: process.env.RATE_LIMIT_SALT,
});

const app = new Hono();
app.post('/login', rateLimit({
  limiter: login,
  key: ipKey({ preset: 'cloudflare' }),
  onMissingKey: 'shared',
}));
```

Denied requests get a `429` and `Retry-After`. If the store is down with `onStoreError: 'closed'`, they get a `503`.

Express and Next use the same options:

```ts
import { ipKey, rateLimit } from 'omnilimit/express';
app.use(rateLimit({ limiter, key: ipKey({ xff: { hops: 1 } }), onMissingKey: 'reject' }));

import { ipKey, withRateLimit } from 'omnilimit/next';
export const POST = withRateLimit(handler, { limiter, key: ipKey({ preset: 'vercel' }), onMissingKey: 'shared' });
```

To answer in your own error format, pass `onDenied`. It gets the status, headers and decision, and in Hono it may throw so your `app.onError` handles it:

```ts
app.post('/login', rateLimit({
  limiter: login,
  key: ipKey({ preset: 'cloudflare' }),
  onMissingKey: 'shared',
  onDenied: (denial) => { throw new ApiError(denial.status, denial.error); },
}));
```

Anywhere else, call the limiter yourself:

```ts
const decision = await limiter.check(userId);
if (!decision.allowed) { /* use decision.status and decision.headers */ }
```

### Count only failures

`peek` answers without counting and `reset` forgets a bucket, so a limit can count failed attempts only, as a login lockout does:

```ts
const id = `${email}:${ip}`;
const gate = await failedLogins.peek(id);
if (!gate.allowed) return tooManyAttempts(gate);   // refuse even a correct password
if (await passwordMatches()) {
  await failedLogins.reset(id);                    // success clears the slate
} else {
  await failedLogins.check(id);                    // only failures count
}
```

`peek` is advisory: a concurrent request can take the last unit, so the `check` that follows stays the authority. Both need an `exact` or `local` store.

## Pick a store

| Store | Import | Tier | Use it for |
|---|---|---|---|
| Redis, Valkey | `omnilimit/redis` | `exact` | Several instances |
| Postgres | `omnilimit/postgres` | `exact` | When Postgres is already there and traffic is moderate |
| Durable Objects | `omnilimit/cloudflare/do` | `exact` | Strict limits on Workers |
| Memory | `omnilimit` | `local` | One process. N instances allow about N times the limit |
| Cloudflare binding | `omnilimit/cloudflare` | `approximate` | Cheap per-location limits, without remaining counts |

```ts
import Redis from 'ioredis';
import { fromIoredis, redisStore } from 'omnilimit/redis';
const store = redisStore(fromIoredis(new Redis(process.env.REDIS_URL)));

import { MIGRATION_SQL, postgresStore } from 'omnilimit/postgres';
const store = postgresStore(pool); // apply MIGRATION_SQL once; schedule store.cleanup()
```

See [stores](docs/stores.md) for details, including Workers configuration.

## Only need the client IP?

`omnilimit/ip` works alone, next to any other limiter:

```ts
import { resolveClientIp } from 'omnilimit/ip';

const ip = resolveClientIp({ headers: req.headers, peer: req.socket.remoteAddress }, { xff: { hops: 1 } });
```

It returns `null` instead of guessing. Read [client IP](docs/client-ip.md) before choosing a preset: each one is only safe in a specific deployment.

## Good to know

- **It limits a rate, not a fixed window count.** `limit: 5` per minute with `burst: 5` can admit 9 requests within one minute. Use a lower `burst` for sensitive routes.
- **A lockout is built from `peek`, `check` and `reset`,** as shown above. There is no separate ban list or escalating penalty.
- **A timed-out store call may still have counted.** The store could finish the hit after the timeout.
- **Run your CORS middleware first,** or browsers will hide the `429` from your frontend.

## How it is verified

Vectors come from a separate generator that shares no code with any port, so no port validates itself. CI runs them on Node 20, 22 and 24, Python 3.11 to 3.14, in real `workerd`, and against real Redis, Valkey and Postgres, where TypeScript and Python also share one counter. It also injects realistic bugs and fails if the suite does not catch them. See the [conformance matrix](docs/matrix.md).

Other libraries may suit you better: see [how it compares](docs/compare.md) and the [roadmap](docs/roadmap.md).

## License

[MIT](LICENSE)
