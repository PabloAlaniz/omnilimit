# Conformance matrix

CI runs these on every pull request, and the `ci-ok` check requires all of them. Spec version 1, TypeScript port.

| Environment | Store | `sequence` | `concurrency` | `failure` | `contract` | `key`, `ip` | Real clock |
|---|---|---|---|---|---|---|---|
| Node 20, 22, 24 | Memory | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Node 24 | Redis 7 (ioredis, node-redis) | ✓ | ✓ | | | | ✓ |
| Node 24 | Valkey 8 (ioredis, node-redis) | ✓ | ✓ | | | | ✓ |
| Node 24 | Postgres 15, 17 | ✓ | ✓ | | | | ✓ |
| workerd | Memory | ✓ | | | | `ip` | |
| workerd | Durable Objects | ✓ | ✓ | | | | ✓ |
| workerd | Rate limit binding | | | | Real binding | | ✓ |

An empty cell means that kind does not exercise the store. For example, `failure` runs against simulated store faults at the limiter level.

## Not verified

- Redis-compatible services other than Redis and Valkey, including Upstash.
- Throughput. Nothing here is a benchmark.
- That each IP preset matches your deployment: see [client IP](client-ip.md).
