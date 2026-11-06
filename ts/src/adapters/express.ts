import { evaluate, ipKey, type IpKeyOptions, type KeyContext, type KeyFn, type MissingKeyPolicy } from './shared.js';
import type { Limiter } from '../core/types.js';

export { ipKey };
export type { IpKeyOptions, KeyContext, KeyFn, MissingKeyPolicy };

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

export interface ExpressRateLimitOptions {
  limiter: Limiter;
  key: KeyFn<ExpressKeyContext>;
  onMissingKey: MissingKeyPolicy;
}

/**
 * Express middleware. It reads the socket and headers directly rather than `req.ip`, so
 * the IP strategy you configure here does not depend on Express's `trust proxy` setting.
 */
export function rateLimit(options: ExpressRateLimitOptions) {
  return async (req: ExpressRequestLike, res: ExpressResponseLike, next: (error?: unknown) => void): Promise<void> => {
    try {
      const peer = req.socket?.remoteAddress ?? null;
      const outcome = await evaluate(options, { headers: req.headers, peer, req });
      for (const [name, value] of Object.entries(outcome.headers)) res.setHeader(name, value);
      if (outcome.kind === 'deny') {
        res.status(outcome.status).json(outcome.body);
        return;
      }
      next();
    } catch (error) {
      next(error);
    }
  };
}
