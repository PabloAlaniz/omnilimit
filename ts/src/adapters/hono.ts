import {
  defaultDenialResponse,
  evaluate,
  ipKey,
  PROBLEM_JSON,
  problemDetails,
  toDenial,
  type Denial,
  type IpKeyOptions,
  type KeyContext,
  type KeyFn,
  type LimiterSource,
  type MissingKeyPolicy,
  type ProblemDetails,
} from './shared.js';

export { ipKey, PROBLEM_JSON, problemDetails };
export type { Denial, IpKeyOptions, KeyContext, KeyFn, LimiterSource, MissingKeyPolicy, ProblemDetails };

/** The parts of Hono's `Context` this adapter touches. */
export interface HonoContextLike {
  req: { raw: Request };
  env: unknown;
  header(name: string, value: string): void;
}

export interface HonoKeyContext extends KeyContext {
  c: HonoContextLike;
}

export interface HonoRateLimitOptions<C extends HonoContextLike = HonoContextLike> {
  /** The limiter, or a function that finds it for each request, for example from `c.env`. */
  limiter: LimiterSource<HonoKeyContext & { c: C }>;
  key: KeyFn<HonoKeyContext & { c: C }>;
  onMissingKey: MissingKeyPolicy;
  /** Return `true` to let a request through without counting it. */
  skip?: (context: HonoKeyContext & { c: C }) => boolean | Promise<boolean>;
  /** How many units this request weighs, from 1 to the limiter's burst. Defaults to 1. */
  cost?: (context: HonoKeyContext & { c: C }) => number | Promise<number>;
  /** How to read the TCP peer. By default uses `@hono/node-server`'s `env.incoming` when present. */
  peer?: (c: C) => string | null | undefined;
  /**
   * Build the response for a denied request, for example in your API's error format. It may
   * also throw, so your `app.onError` handles it. Defaults to `{"error": "..."}` as JSON. The
   * denial headers are set on the context before it runs.
   */
  onDenied?: (denial: Denial, c: C) => Response | Promise<Response>;
  /**
   * Answer a denied request with a problem details body (RFC 9457), as
   * `application/problem+json`, instead of `{"error": "..."}`. `onDenied` takes precedence.
   */
  problemDetails?: boolean;
}

function nodePeer(c: HonoContextLike): string | null {
  const env = c.env as { incoming?: { socket?: { remoteAddress?: string } } } | undefined;
  return env?.incoming?.socket?.remoteAddress ?? null;
}

export function rateLimit<C extends HonoContextLike = HonoContextLike>(options: HonoRateLimitOptions<C>) {
  const readPeer = options.peer ?? nodePeer;
  return async (c: C, next: () => Promise<void>): Promise<Response | void> => {
    const outcome = await evaluate(options, { headers: c.req.raw.headers, peer: readPeer(c) ?? null, c });
    if (outcome.kind === 'deny') {
      const denial = toDenial(outcome);
      if (!options.onDenied) return defaultDenialResponse(denial, options.problemDetails);
      for (const [name, value] of Object.entries(denial.headers)) c.header(name, value);
      return options.onDenied(denial, c);
    }
    for (const [name, value] of Object.entries(outcome.headers)) c.header(name, value);
    await next();
  };
}
