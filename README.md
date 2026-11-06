# omnilimit

Rate limiting that behaves the same across stacks, and proves it.

[![CI](https://github.com/PabloAlaniz/omnilimit/actions/workflows/ci.yml/badge.svg)](https://github.com/PabloAlaniz/omnilimit/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> **Early release.** TypeScript is available. Python comes next, using the same spec and vectors.

Most rate limiters cover one framework and leave you to get three things right on your own.
`omnilimit` makes them part of the contract:

- **Same behaviour everywhere.** One [spec](spec/SPEC.md) and shared conformance vectors, run against each store on every commit.
- **Trusted client IPs.** It reads `X-Forwarded-For` from the right. Rotating the header does not evade the limit.
- **An explicit failure mode.** You must decide, per limiter, what happens when the store is down.

## Install

```bash
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

Anywhere else, call the limiter yourself:

```ts
const decision = await limiter.check(userId);
if (!decision.allowed) { /* use decision.status and decision.headers */ }
```

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
- **It is not a lockout.** Blocking an account after failed logins is a separate feature.
- **A timed-out store call may still have counted.** The store could finish the hit after the timeout.
- **Run your CORS middleware first,** or browsers will hide the `429` from your frontend.

## How it is verified

Vectors come from a separate Python generator, so TypeScript is not validating itself. CI runs them on Node 20, 22 and 24, in real `workerd`, and against real Redis, Valkey and Postgres. It also injects realistic bugs and fails if the suite does not catch them. See the [conformance matrix](docs/matrix.md).

Other libraries may suit you better: see [how it compares](docs/compare.md).

## License

[MIT](LICENSE)
