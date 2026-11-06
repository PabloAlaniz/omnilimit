# Stores

## Redis and Valkey

```ts
import { fromIoredis, fromNodeRedis, redisStore } from 'omnilimit/redis';
const store = redisStore(fromIoredis(client));   // ioredis
const store = redisStore(fromNodeRedis(client)); // node-redis
```

The store runs one Lua script per hit, using `EVALSHA` and loading the script on `NOSCRIPT`. It uses the Redis server clock and sets a TTL so idle buckets disappear.

Other Redis-compatible services work if they support `EVALSHA`, `TIME` inside scripts and `PEXPIRE`, but only Redis and Valkey are in the test matrix. To connect another client, implement `RedisEval`.

## Postgres

```ts
import { MIGRATION_SQL, postgresStore } from 'omnilimit/postgres';
const store = postgresStore(pool); // any object with query(sql, params), such as a pg Pool
```

1. Apply `MIGRATION_SQL` once with your migration tool. omnilimit never runs it for you, and it is safe to reapply.
2. Schedule `store.cleanup()`, which deletes expired buckets in batches.

Each hit takes a row lock and writes a row, so busy buckets contend and create dead tuples. It is exact, and convenient when Postgres is already there, but it has not been benchmarked: use Redis for high traffic.

## Durable Objects

```ts
import { durableObjectStore, OmnilimitDurableObject } from 'omnilimit/cloudflare/do';
export { OmnilimitDurableObject };

const store = durableObjectStore(env.RATE_LIMITER);
```

```jsonc
// wrangler.jsonc
{
  "durable_objects": { "bindings": [{ "name": "RATE_LIMITER", "class_name": "OmnilimitDurableObject" }] },
  "migrations": [{ "tag": "v1", "new_sqlite_classes": ["OmnilimitDurableObject"] }]
}
```

There is one object per bucket, and an alarm deletes its storage after it drains. Requests incur Durable Object billing.

## Cloudflare rate limit binding

```ts
import { cloudflareBindingStore } from 'omnilimit/cloudflare';
const store = cloudflareBindingStore({ binding: env.MY_LIMITER, periodSec: 60 });
```

Cloudflare enforces its own configured limit per location and only reports allowed or denied. So:

- The limit in your Wrangler configuration is the real one. Keep the limiter's `limit` and `windowMs` in step with it.
- `periodSec` must be 10 or 60.
- Decisions have `remaining: null` and `resetMs: null`, and only send `Retry-After`.
- A missing binding is a store failure, handled by `onStoreError`. It does not silently allow traffic.

## Memory

```ts
import { memoryStore } from 'omnilimit';
const store = memoryStore({ maxKeys: 100_000 });
```

It works within one process. Several instances each keep their own count, and serverless platforms may start from zero often. At `maxKeys`, expired buckets go first, then those with the oldest writes, so an attacker rotating ids can push out other buckets.

## Workers KV

Not supported. KV is not atomic and changes can take 60 seconds or more to reach other locations, so it cannot keep the promises above. The `Store` interface is public if you accept that trade-off in your own code.
