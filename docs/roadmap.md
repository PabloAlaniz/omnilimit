# Roadmap

What may come next, in rough order of value. Nothing here is promised, and each item has to keep the
contract: one behaviour across ports, verified by vectors from the independent generator.

| Item | Why | Shape it would take |
|---|---|---|
| Weighted cost | An export or an LLM call is not one unit | `check(id, { cost })`: the hit adds `cost * W`, vectors for costs above the burst |
| Composite limits | Per IP and per user, without the first consuming when the second denies | Peek every limiter, then hit them all; or one store call over several keys where the store can do it atomically |
| Local deny cache | A denied client should not cost a store round trip until it may retry | In-process "blocked until" map honouring `retryAfterMs`, opt-in, tier stays honest |
| Shadow mode | Roll a limit out without enforcing it | Decide and report through `onEvent`, but allow |
| Metrics | Dashboards without writing an `onEvent` hook | OpenTelemetry counters for allowed, limited, store error and latency |
| Durable Object placement | Lower latency, data residency | `locationHint` and jurisdiction options for `durableObjectStore` |
| Cold-start aware timeouts | The first call to a new Durable Object can take over a second, longer than the 1 s default, so a `closed` limit answers 503 right after a deploy | A per-store default timeout (higher for Durable Objects), documented next to `onStoreError` |
| Penalties | Repeat offenders wait longer | Escalating windows on top of peek and reset, if it can be specified without floats |

Done in 0.2: `peek` and `reset` (lockouts that count only failures), and `onDenied` in every adapter.
