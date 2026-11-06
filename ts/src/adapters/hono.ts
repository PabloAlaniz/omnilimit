import { evaluate, ipKey, type IpKeyOptions, type KeyContext, type KeyFn, type MissingKeyPolicy } from './shared.js';
import type { Limiter } from '../core/types.js';

export { ipKey };
export type { IpKeyOptions, KeyContext, KeyFn, MissingKeyPolicy };

/** The parts of Hono's `Context` this adapter touches. */
export interface HonoContextLike {
  req: { raw: Request };
  env: unknown;
  header(name: string, value: string): void;
}

export interface HonoKeyContext extends KeyContext {
  c: HonoContextLike;
}

export interface HonoRateLimitOptions {
  limiter: Limiter;
  key: KeyFn<HonoKeyContext>;
  onMissingKey: MissingKeyPolicy;
  /** How to read the TCP peer. By default uses `@hono/node-server`'s `env.incoming` when present. */
  peer?: (c: HonoContextLike) => string | null | undefined;
}

function nodePeer(c: HonoContextLike): string | null {
  const env = c.env as { incoming?: { socket?: { remoteAddress?: string } } } | undefined;
  return env?.incoming?.socket?.remoteAddress ?? null;
}

export function rateLimit(options: HonoRateLimitOptions) {
  const readPeer = options.peer ?? nodePeer;
  return async (c: HonoContextLike, next: () => Promise<void>): Promise<Response | void> => {
    const outcome = await evaluate(options, { headers: c.req.raw.headers, peer: readPeer(c) ?? null, c });
    if (outcome.kind === 'deny') {
      return new Response(JSON.stringify(outcome.body), {
        status: outcome.status,
        headers: { 'content-type': 'application/json', ...outcome.headers },
      });
    }
    for (const [name, value] of Object.entries(outcome.headers)) c.header(name, value);
    await next();
  };
}
