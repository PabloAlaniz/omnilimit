# Benchmarks

What a check costs with each store, and how many calls to the store each operation makes.

Read the numbers for their shape, not their size. They come from one machine with the stores on the same host, where a round trip takes microseconds. Across a real network a round trip takes a millisecond or more, and it is nearly all of the cost. Measure your own setup before deciding anything:

```bash
docker compose -f compose.test.yml -p omnilimit-test up -d --wait
export OMNILIMIT_TEST_REDIS_URLS=redis://127.0.0.1:56379
export OMNILIMIT_TEST_POSTGRES_URLS=postgres://postgres:omnilimit@127.0.0.1:55417/omnilimit

pnpm bench                                          # TypeScript
cd python && uv run python ../tools/bench.py        # Python
```

Point the URLs at a store where your application would reach it, and the numbers are yours.

## What is measured

- **Baseline:** the cheapest call the same connection can make. `PING` for Redis, `SELECT 1` for Postgres. For the memory store, the SHA-256 that derives a key, which every check has to do.
- **`check`:** 3000 calls one after another, each on its own key, after 300 to warm up. The median and the 99th percentile.
- **Denied:** the same on one key that is over its limit. A denial writes nothing.
- **`check` / baseline:** how many baseline calls one check costs. One round trip should cost about one round trip.
- **Checks per second:** many calls at once, over 5000 keys, on one connection or pool.

## Calls to the store

These are exact, the same on any machine, and CI fails if one changes.

| Operation | Calls |
|---|---|
| `check`, allowed or denied | 1 |
| `peek` | 1 |
| `reset` | 1 |
| `reserve` | 1 |
| `refund` | 1 |
| `checkAll` of 2 limits | 4: a peek and a hit for each |
| `reserveAll` of 2 limits | 2 |
| Cancelling it | 2 |
| Lockout attempt with a wrong secret, 2 limits | 4 |
| The same in `reserve` mode | 2 |

The first use of each Redis script on a server costs one more call, to load it.

## One run

A 4-core Linux container, Redis 7.0 and Postgres 16 on the same host over loopback, November 2026. Postgres with its default settings, so every hit waits for its commit to reach the disk.

**TypeScript**, Node 22, ioredis and pg:

| Store | Baseline | `check`, median | 99th percentile | Denied | `check` / baseline | Checks/s, 64 at once |
|---|---|---|---|---|---|---|
| Memory | 34 µs | 41 µs | 180 µs | 42 µs | 1.2 | 46,000 |
| Redis | 42 µs | 101 µs | 307 µs | 90 µs | 2.4 | 33,000 |
| Postgres | 53 µs | 337 µs | 587 µs | 296 µs | 6.4 | 9,800 |

**Python**, 3.13, redis-py and psycopg 3:

| Store | Baseline | `check`, median | 99th percentile | Denied | `check` / baseline | Checks/s, 16 threads | Checks/s, async |
|---|---|---|---|---|---|---|---|
| Memory | 0.3 µs | 10 µs | 29 µs | 10 µs | 32 | 12,500 | |
| Redis | 60 µs | 294 µs | 489 µs | 297 µs | 4.9 | 2,200 | 9,100 |
| Postgres | 153 µs | 766 µs | 1.4 ms | 680 µs | 5.0 | 1,300 | |

## What it says

- **A check is one round trip, and costs like one.** With Redis in TypeScript it is about twice a `PING`: the script reads, computes and writes. Over a network both take the same millisecond.
- **Postgres costs several times Redis,** because a hit takes a row lock and commits. It is exact and convenient when Postgres is already there. For busy limits, use Redis.
- **Many callers on one key** fall to half the Postgres throughput, since they wait for the same row. Redis does not care.
- **The blocking Python limiter pays for its timeout.** It runs each store call on a thread of its own, to be able to give up on it, which costs over 200 µs here. `AsyncLimiter` does not, and does four times the work on Redis. Prefer it under load.
- **`checkAll` costs two calls per limit.** `reserveAll` costs one, plus one per limit to give back when a later limit denies.

## What CI checks

The `bench` job runs both scripts with `--check` on every pull request, and writes its tables to the job summary.

- The calls each operation makes must match the table above, exactly.
- `check` / baseline must stay under a loose ceiling: 10 for Redis and 40 for Postgres in TypeScript. On a shared runner the ratio moves, so the ceiling only catches a check that became several times dearer.

Times are reported and not compared with an earlier run: a shared runner is not steady enough for that.
