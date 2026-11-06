# Conformance matrix

CI runs these on every pull request, and the `ci-ok` check requires all of them. Spec version 1, TypeScript and Python ports.

| Environment | Store | `sequence` | `concurrency` | `failure` | `contract` | `key`, `ip` | Real clock |
|---|---|---|---|---|---|---|---|
| Node 20, 22, 24 | Memory | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Node 24 | Redis 7 (ioredis, node-redis) | ✓ | ✓ | | | | ✓ |
| Node 24 | Valkey 8 (ioredis, node-redis) | ✓ | ✓ | | | | ✓ |
| Node 24 | Postgres 15, 17 | ✓ | ✓ | | | | ✓ |
| workerd | Memory | ✓ | | | | `ip` | |
| workerd | Durable Objects | ✓ | ✓ | | | | ✓ |
| workerd | Rate limit binding | | | | Real binding | | ✓ |

| Python 3.11, 3.12, 3.13, 3.14 | Memory, sync and async | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Python 3.13 | Redis 7 (redis-py, valkey-py), sync and async | ✓ | ✓ | | | | ✓ |
| Python 3.13 | Valkey 8 (redis-py, valkey-py), sync and async | ✓ | ✓ | | | | ✓ |
| Python 3.13 | Postgres 15, 17 (psycopg 3), sync and async | ✓ | ✓ | | | | ✓ |

TypeScript and Python also take turns on one counter in real Redis and Postgres, using sequence vectors. That proves they derive the same key and read each other's state.

An empty cell means that kind does not exercise the store. For example, `failure` runs against simulated store faults at the limiter level.

## Not verified

- Redis-compatible services other than Redis and Valkey, including Upstash.
- Throughput. Nothing here is a benchmark.
- That each IP preset matches your deployment: see [client IP](client-ip.md).
