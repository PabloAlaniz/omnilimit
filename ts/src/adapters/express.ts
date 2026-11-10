import {
  evaluate,
  ipKey,
  toDenial,
  type Denial,
  type IpKeyOptions,
  type KeyContext,
  type KeyFn,
  type MissingKeyPolicy,
} from './shared.js';
import type { Limiter } from '../core/types.js';

export { ipKey };
export type { Denial, IpKeyOptions, KeyContext, KeyFn, MissingKeyPolicy };

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
  limiter: Limiter;
  key: KeyFn<ExpressKeyContext & { req: Req }>;
  onMissingKey: MissingKeyPolicy;
  /**
   * Answer a denied request yourself, for example in your API's error format. The denial headers
   * are already set on `res`. If it throws, the error goes to `next`. Defaults to
   * `res.status(status).json({ error })`.
   */
  onDenied?: (denial: Denial, req: Req, res: Res) => void | Promise<void>;
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
        else res.status(denial.status).json({ error: denial.error });
        return;
      }
      next();
    } catch (error) {
      next(error);
    }
  };
}
