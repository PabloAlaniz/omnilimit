import {
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

/** The parts of Express's request and response this adapter touches. */
export interface ExpressRequestLike {
  headers: Record<string, string | string[] | undefined>;
  socket?: { remoteAddress?: string | undefined } | undefined;
}

export interface ExpressResponseLike {
  setHeader(name: string, value: string): unknown;
  status(code: number): { json(body: unknown): unknown };
}

export interface ExpressKeyContext extends KeyContext {
  req: ExpressRequestLike;
}

export interface ExpressRateLimitOptions<Req extends ExpressRequestLike = ExpressRequestLike, Res extends ExpressResponseLike = ExpressResponseLike> {
  /** The limiter, or a function that finds it for each request. */
  limiter: LimiterSource<ExpressKeyContext & { req: Req }>;
  key: KeyFn<ExpressKeyContext & { req: Req }>;
  onMissingKey: MissingKeyPolicy;
  /** Return `true` to let a request through without counting it. */
  skip?: (context: ExpressKeyContext & { req: Req }) => boolean | Promise<boolean>;
  /** How many units this request weighs, from 1 to the limiter's burst. Defaults to 1. */
  cost?: (context: ExpressKeyContext & { req: Req }) => number | Promise<number>;
  /**
   * Answer a denied request yourself, for example in your API's error format. The denial headers
   * are already set on `res`. If it throws, the error goes to `next`. Defaults to
   * `res.status(status).json({ error })`.
   */
  onDenied?: (denial: Denial, req: Req, res: Res) => void | Promise<void>;
  /**
   * Answer a denied request with a problem details body (RFC 9457), as
   * `application/problem+json`, instead of `{"error": "..."}`. `onDenied` takes precedence.
   */
  problemDetails?: boolean;
}

/**
 * Express middleware. It reads the socket and headers directly rather than `req.ip`, so
 * the IP strategy you configure here does not depend on Express's `trust proxy` setting.
 */
export function rateLimit<Req extends ExpressRequestLike = ExpressRequestLike, Res extends ExpressResponseLike = ExpressResponseLike>(
  options: ExpressRateLimitOptions<Req, Res>,
) {
  return async (req: Req, res: Res, next: (error?: unknown) => void): Promise<void> => {
    try {
      const peer = req.socket?.remoteAddress ?? null;
      const outcome = await evaluate(options, { headers: req.headers, peer, req });
      for (const [name, value] of Object.entries(outcome.headers)) res.setHeader(name, value);
      if (outcome.kind === 'deny') {
        const denial = toDenial(outcome);
        if (options.onDenied) await options.onDenied(denial, req, res);
        else if (options.problemDetails) {
          // Express keeps a content type that is already set when it sends JSON.
          res.setHeader('content-type', PROBLEM_JSON);
          res.status(denial.status).json(problemDetails(denial));
        } else res.status(denial.status).json({ error: denial.error });
        return;
      }
      next();
    } catch (error) {
      next(error);
    }
  };
}
