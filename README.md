# omnilimit

Rate limiting that behaves the same across stacks, and proves it.

[![CI](https://github.com/PabloAlaniz/omnilimit/actions/workflows/ci.yml/badge.svg)](https://github.com/PabloAlaniz/omnilimit/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/PabloAlaniz/omnilimit/badge)](https://scorecard.dev/viewer/?uri=github.com/PabloAlaniz/omnilimit)

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

`omnilimit/web` has the same two helpers for anything that receives a Fetch API `Request`: a Worker's `fetch`, an Astro or SvelteKit endpoint. See [adapters](docs/adapters.md), which also covers a limiter built per request (a Workers binding in `env`) and `skip`.

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

A login should not lock out someone who types the right password, and should refuse even the right one once there were too many wrong ones. `createLockout` does both:

```ts
import { createLockout } from 'omnilimit';

const logins = createLockout({ limiter: failedLogins });

const attempt = await logins.attempt(`${email}:${ip}`, () => passwordMatches(email, password));
if (!attempt.allowed) return tooManyAttempts(attempt.decision);   // verify did not run
if (!attempt.verified) return wrongPassword();                    // this one was counted
```

A success clears the count. If the verification throws, nothing is counted. A lockout can also count each failure under several limits, such as per account and per IP, and in `reserve` mode it never runs the verification more often than the limits allow, however many attempts arrive at once: see [recipes](docs/recipes.md#several-limits-and-reserve-mode).

### Several limits on one request

Per IP and per account is the usual pair. Checking them one after the other spends the first even when the second denies. `checkAll` looks at all of them first and counts only if every one would allow:

```ts
import { checkAll } from 'omnilimit';

const { allowed, decision } = await checkAll([
  { limiter: perIp, id: ip },
  { limiter: perAccount, id: email },
]);
if (!allowed) return new Response(null, { status: decision.status ?? 429, headers: decision.headers });
```

`decision` is the one to answer with: the longest wait when denied, the fewest units left when allowed. Both recipes are built on `peek`, which is advisory, so they need an `exact` or `local` store and are not atomic. `reserveAll` counts first and refunds instead, so it never lets more through, and both split into two phases around work that may not happen: see [recipes](docs/recipes.md).

### Requests that weigh more than one

A batch of 20 items, or a call that costs more than the rest, can count as several units:

```ts
const decision = await limiter.check(userId, { cost: items.length });
```

A denied request counts nothing, whatever its cost. The cost is an integer from 1 to the `burst`; a larger one could never pass, so it throws instead of denying. Adapters take `cost: (context) => number`.

### Give back a request whose work did not happen

Count first, and give the request back if its work is cancelled:

```ts
const { decision, reservation } = await limiter.reserve(userId, { cost: items.length });
if (!decision.allowed) return tooMany(decision);
try {
  await work();
} catch (error) {
  await limiter.refund(reservation); // only what has not drained yet
  throw error;
}
```

`reserve` decides like `check`. `refund` gives back what is left of the request after the meter drained since it was counted, so a bucket never ends up more generous than if the request had never happened: at once, it leaves no trace; after the meter drained its units, it gives nothing. A reservation is refunded at most once, and only by the limiter that made it. `refund` resolves `false` instead of throwing when the store fails, and does not retry, since a timed-out refund may have been applied. It needs an `exact` or `local` store. See the [spec](spec/SPEC.md#refund).

### Limits from configuration

A policy can come from a string, with the same strict grammar in every port:

```ts
import { createLimiter, parsePolicy } from 'omnilimit';

createLimiter({ ...parsePolicy(process.env.API_LIMIT ?? '100/minute'), store, scope: 'api', onStoreError: 'open' });
```

`10/minute`, `5/15minute` and `1000/day` are valid; `10/min`, `10 / minute` and `10/minutes` are not. See the [spec](spec/SPEC.md#policy-shorthand).

### Watch a limit before enforcing it

A new limit on traffic that already exists can turn away more than you expect. `shadow: true` decides and counts as usual but never denies:

```ts
const api = createLimiter({
  ...parsePolicy('100/minute'),
  store,
  scope: 'api',
  onStoreError: 'open',
  shadow: true,
  onEvent: (event) => {
    if (event.shadowed) log.warn('api limit would deny', event);
  },
});

const decision = await api.check(userId);
// decision.allowed is true; decision.shadowed says whether enforcement would have said no.
```

A request that would be limited, or refused by `onStoreError: 'closed'`, comes back allowed with `shadowed: true`, keeping the denial's `reason`, `remaining` and `retryAfterMs`. Its event is marked `shadowed`. The store sees the same calls as under enforcement, so the counters fill as they would. No rate limit headers are sent, so clients cannot react to a limit that is not enforced. A shadowed request counted nothing, so it is not a reservation. Recipes treat a shadowed decision as allowed, and a limit can be shadowed next to others that enforce. Remove `shadow` to enforce. See the [spec](spec/SPEC.md#shadow-mode).

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
- **A lockout is `createLockout`,** which is `peek`, `check` and `reset` in a fixed order. There is no separate ban list or escalating penalty.
- **A timed-out store call may still have counted.** The store could finish the hit after the timeout.
- **Run your CORS middleware first,** or browsers will hide the `429` from your frontend.

## How it is verified

Vectors come from a separate generator that shares no code with any port, so no port validates itself. CI runs them on Node 20, 22 and 24, Python 3.11 to 3.14, in real `workerd`, and against real Redis, Valkey and Postgres, where TypeScript and Python also share one counter. It also injects realistic bugs and fails if the suite does not catch them. See the [conformance matrix](docs/matrix.md).

Releases are built in CI and published with provenance, with no runtime dependencies. [Quality and security practices](docs/quality.md) lists each practice with its evidence and how to verify a release, and the [threat model](docs/threat-model.md) says what the library defends against.

Other libraries may suit you better: see [how it compares](docs/compare.md) and the [roadmap](docs/roadmap.md).

## License

[MIT](LICENSE)
