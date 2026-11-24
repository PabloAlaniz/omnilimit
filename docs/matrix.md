# Conformance matrix

CI runs these on every pull request, and the `ci-ok` check requires all of them. Spec version 1, TypeScript and Python ports.

| Environment | Store | `sequence` | `concurrency` | `failure` | `contract` | `key`, `ip` | Real clock |
|---|---|---|---|---|---|---|---|
| Node 20, 22, 24 | Memory | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Node 24 | Redis 7 (ioredis, node-redis) | ✓ | ✓ | | | | ✓ |
| Node 24 | Valkey 8 (ioredis, node-redis) | ✓ | ✓ | | | | ✓ |
| Node 24 | Redis 7 as a cluster (ioredis, node-redis) | ✓ | ✓ | | | | ✓ |
| Node 24 | Postgres 15, 17 | ✓ | ✓ | | | | ✓ |
| workerd | Memory | ✓ | | | | `ip` | |
| workerd | Durable Objects | ✓ | ✓ | | | | ✓ |
| workerd | Rate limit binding | | | | Real binding | | ✓ |

| Python 3.11, 3.12, 3.13, 3.14 | Memory, sync and async | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Python 3.13 | Redis 7 (redis-py, valkey-py), sync and async | ✓ | ✓ | | | | ✓ |
| Python 3.13 | Valkey 8 (redis-py, valkey-py), sync and async | ✓ | ✓ | | | | ✓ |
| Python 3.13 | Redis 7 as a cluster (redis-py), sync and async | ✓ | ✓ | | | | ✓ |
| Python 3.13 | Postgres 15, 17 (psycopg 3), sync and async | ✓ | ✓ | | | | ✓ |

TypeScript and Python also take turns on one counter in real Redis and Postgres, using sequence vectors. That proves they derive the same key and read each other's state.

An empty cell means that kind does not exercise the store. For example, `failure` runs against simulated store faults at the limiter level.

## Beyond the vectors

The vectors are fixed, so they only cover what someone thought of. Four more checks run on every pull request, in both ports.

| Check | What it does | Where |
|---|---|---|
| Fuzz | Draws new vectors from the independent model, over far wider ranges than the committed ones: policies up to the largest the spec allows, clocks that go back, hostile `X-Forwarded-For` values, random recipes with faults. Both ports run them on the memory store and on real Redis and Postgres. A pull request uses a fixed seed; the weekly run draws a new one | `tools/fuzz_vectors.py` |
| Every interleaving | Lists every order in which the store calls of two or three concurrent callers can happen, about 19,000 across 15 recipes, checks the bounds of the spec in each, and drives each port through each order, call by call, comparing with the model | `tools/explore_interleavings.py` |
| Faults | Puts a proxy between each real client and its store and loses the reply, the request or the connection in the middle of a call. A timed-out hit counts once, never twice; a refund whose reply is lost is never applied twice; the limit stays exactly the burst | `ts/test/stores/faults.test.ts`, `python/tests/test_faults.py` |
| Calls and cost | Counts the calls each operation makes to the store, exactly, and measures a check against a bare round trip | [Benchmarks](benchmarks.md) |

The cluster is one node holding every slot. That proves each script only touches the key it declares, which is what a cluster requires. It does not exercise failover or resharding.

## Not verified

- Redis-compatible services other than Redis and Valkey, including Upstash.
- Throughput on your hardware and network. The [benchmarks](benchmarks.md) say what a check costs next to a round trip on one machine, not what you will get.
- A Redis Cluster during failover or resharding, and Redis Sentinel.
- Clients that send a command again by themselves after a reconnect: see [stores](stores.md#when-the-connection-drops).
- That each IP preset matches your deployment: see [client IP](client-ip.md).
