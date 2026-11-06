import { REDIS_HIT_LUA, REDIS_HIT_SHA1 } from '../generated/assets.js';
import type { HitParams, RawHit, Store } from '../core/types.js';

/** The two calls the store needs. Use `fromIoredis` or `fromNodeRedis`, or implement it yourself. */
export interface RedisEval {
  evalsha(sha: string, keys: string[], args: string[]): Promise<unknown>;
  eval(script: string, keys: string[], args: string[]): Promise<unknown>;
}

interface IoredisLike {
  evalsha(sha: string, numKeys: number, ...keysAndArgs: string[]): Promise<unknown>;
  eval(script: string, numKeys: number, ...keysAndArgs: string[]): Promise<unknown>;
}

interface NodeRedisLike {
  evalSha(sha: string, options: { keys: string[]; arguments: string[] }): Promise<unknown>;
  eval(script: string, options: { keys: string[]; arguments: string[] }): Promise<unknown>;
}

export function fromIoredis(client: IoredisLike): RedisEval {
  return {
    evalsha: (sha, keys, args) => client.evalsha(sha, keys.length, ...keys, ...args),
    eval: (script, keys, args) => client.eval(script, keys.length, ...keys, ...args),
  };
}

export function fromNodeRedis(client: NodeRedisLike): RedisEval {
  return {
    evalsha: (sha, keys, args) => client.evalSha(sha, { keys, arguments: args }),
    eval: (script, keys, args) => client.eval(script, { keys, arguments: args }),
  };
}

function isNoScript(error: unknown): boolean {
  return error instanceof Error && error.message.includes('NOSCRIPT');
}

/** Atomic and shared across instances and languages. Uses the Redis server clock. */
export function redisStore(client: RedisEval): Store {
  return {
    tier: 'exact',
    async hit(key: string, params: HitParams, nowMs?: number): Promise<RawHit> {
      const args = [params.limit, params.windowMs, params.capacity, params.drainMs].map(String);
      args.push(nowMs === undefined ? '' : String(nowMs));
      let reply: unknown;
      try {
        reply = await client.evalsha(REDIS_HIT_SHA1, [key], args);
      } catch (error) {
        if (!isNoScript(error)) throw error;
        reply = await client.eval(REDIS_HIT_LUA, [key], args);
      }
      if (!Array.isArray(reply) || reply.length !== 3) {
        throw new Error('omnilimit: unexpected reply from the Redis script');
      }
      return { allowed: Number(reply[0]) === 1, level: Number(reply[1]), ts: Number(reply[2]) };
    },
  };
}
