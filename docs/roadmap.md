# Roadmap

What may come next. Nothing here is promised, and each item has to keep the contract: one
behaviour across ports, verified by vectors from the independent generator.

## Versions

| Version | Contents |
|---|---|
| Next release | Shadow mode: decide and count, report through `onEvent`, but never deny |
| 0.4 | `reserve` and `refund`, combined checks in two phases, lockouts over several limits |
| 0.3 | See [done in 0.3](#done-in-03) |
| 0.2 | `peek` and `reset`, and `onDenied` in every adapter |

## Next

Items that came up while the library was being adopted in applications, in rough order of value.

| Item | Why | Shape it would take |
|---|---|---|
| Atomic composite limits | `checkAll` peeks and then counts, so a race can still spend one limit when another denies; `reserveAll` never over-admits but holds earlier limits for a round trip | One store call over several keys, where the store can do it atomically |
| REST-based Redis | Serverless platforms without TCP cannot use the Redis store | A `RedisEval` adapter for an HTTP Redis client, tested against a real service |
| Serverless HTTP Postgres drivers in the matrix | `fromRowQuery` adapts them, but only `pg` is tested | One such driver in the store suite |
| Flask and Django adapters | Python has FastAPI and plain ASGI only | A decorator for Flask, a middleware and a decorator for Django, on the blocking `Limiter` |
| Server action helper for Next.js | A server action has no `Request` to pass to `checkRateLimit` | A helper that reads `headers()` and returns the decision |
| `Forwarded` header | [RFC 7239](https://www.rfc-editor.org/rfc/rfc7239) is the standard form of `X-Forwarded-For`, and some proxies send only that | A client IP strategy that reads `for=` from the right, with the same hop count rule |
| Problem Details responses | [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457) gives denials a standard body | An opt-in `application/problem+json` body for 429, 503 and 400 in every adapter |
| More client IP presets | Each platform documents its own trusted header | Presets added only with a published statement from the platform that the header cannot be forged |
| A newer rate limit headers draft | `draft-11` is pinned to one revision of the IETF document | A new profile next to it, never a silent change of the existing one |

## Later

Larger pieces, or ones that need a decision first.

| Item | Why | Shape it would take |
|---|---|---|
| Local deny cache | A denied client should not cost a store round trip until it may retry | In-process "blocked until" map honouring `retryAfterMs`, opt-in, tier stays honest |
| Metrics | Dashboards without writing an `onEvent` hook | OpenTelemetry counters for allowed, limited, store error and latency |
| Durable Object placement | Lower latency, data residency | `locationHint` and jurisdiction options for `durableObjectStore` |
| Refunds that are safe to send twice | A client that resends a command after a reconnect applies a refund twice. Today that is avoided in the client's configuration | The store remembers which reservations it gave back, which needs a new state format |
| Penalties | Repeat offenders wait longer | Escalating windows on top of peek and reset, if it can be specified without floats |
| SQLite-compatible stores | Edge databases and single-file deployments have no Redis or Postgres | The hit as one atomic statement, if the engine can give the guarantees of the `exact` tier |
| MySQL store | Some applications have only MySQL | The Postgres functions rewritten, with their own conformance run |
| More ports | Services in other languages cannot share a counter today | A port follows the spec and passes every vector before it is listed |

## Not planned

These were considered and left out on purpose.

| Item | Reason |
|---|---|
| Quotas per calendar period, such as "1000 per day, resetting at midnight" | It is a different algorithm. The library has one, so that every port and store can be verified against the same vectors. A long window comes close, with the peak rate described in the README |
| Workers KV store | KV is not atomic and takes time to propagate, so it cannot meet any tier. The `Store` interface is public for those who accept that |
| Throttling outgoing calls | Waiting before calling someone else's API is a different problem, with its own libraries |
| Ban lists | A list of blocked clients is application state, not a rate |

## Done in 0.3

- `omnilimit/web`: `checkRateLimit` and `withRateLimit` for any Fetch API `Request`.
- A limiter resolved per request, for stores that only exist inside it.
- `skip`, to let requests through without counting them.
- `cost`, for requests that weigh several units: `check(id, { cost })`.
- `checkAll`, for several limits on one request, and `createLockout`, which counts only failures.
- The policy shorthand: `parsePolicy('10/minute')`.
- `clientKey`, the trusted client IP already grouped by prefix.
- FastAPI: keying by a resolved dependency (`identity`), and a limiter chosen per request.
- `omnilimit.asgi.RateLimitMiddleware`, for any ASGI application.
- Postgres stores for a SQLAlchemy engine and for an asyncpg pool, and `fromRowQuery` for clients that return rows directly.
- A default timeout per store. The Durable Object store uses 3000 ms instead of 1000 ms.
- A clock for the memory store, for tests.
