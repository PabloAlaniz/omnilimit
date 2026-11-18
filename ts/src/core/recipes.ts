import type { Decision, Limiter } from './types.js';

/** One limit of a combined check: the limiter and the id to count under it. */
export interface LimitEntry {
  limiter: Limiter;
  id: string;
  /** How many units this request weighs under this limit. Defaults to 1. */
  cost?: number;
}

export interface CombinedDecision {
  allowed: boolean;
  /**
   * The decision to answer with: when denied, the denial with the longest wait; when allowed,
   * the one with the fewest units left.
   */
  decision: Decision;
  /** Every decision taken, in the order given. Shorter than the input when a step denied. */
  decisions: Decision[];
}

const options = (cost: number | undefined) => (cost === undefined ? {} : { cost });

function longestWait(decisions: Decision[]): Decision {
  let governing = decisions[0] as Decision;
  for (const decision of decisions) {
    if ((decision.retryAfterMs ?? 0) > (governing.retryAfterMs ?? 0)) governing = decision;
  }
  return governing;
}

function fewestLeft(decisions: Decision[]): Decision {
  let governing = decisions[0] as Decision;
  for (const decision of decisions) {
    if (decision.remaining === null) continue;
    if (governing.remaining === null || decision.remaining < governing.remaining) governing = decision;
  }
  return governing;
}

/**
 * Apply several limits to one request, such as per IP and per account, so that a request one
 * of them denies does not use up the others. It peeks every limit first and counts only when
 * all of them would allow. See "Recipes" in the spec.
 *
 * The peek is advisory: two requests racing for the last unit can both pass it, and then the
 * second is denied while counting. The limits already counted for it stay counted. Needs
 * stores that can peek, so not the `approximate` tier.
 */
export async function checkAll(entries: readonly LimitEntry[]): Promise<CombinedDecision> {
  if (entries.length === 0) throw new RangeError('omnilimit: checkAll needs at least one limit');
  const peeks: Decision[] = [];
  for (const { limiter, id, cost } of entries) peeks.push(await limiter.peek(id, options(cost)));
  const refused = peeks.filter((decision) => !decision.allowed);
  if (refused.length > 0) return { allowed: false, decision: longestWait(refused), decisions: peeks };

  const decisions: Decision[] = [];
  for (const { limiter, id, cost } of entries) {
    const decision = await limiter.check(id, options(cost));
    decisions.push(decision);
    if (!decision.allowed) return { allowed: false, decision, decisions };
  }
  return { allowed: true, decision: fewestLeft(decisions), decisions };
}

export interface LockoutOptions {
  /** Counts the failed attempts. Its policy is how many failures are tolerated, and how fast they come back. */
  limiter: Limiter;
  /** Forget the failures after a success. Defaults to `true`. */
  resetOnSuccess?: boolean;
}

export type Attempt =
  /** Too many failures: `verify` was not called. Answer with `decision`. */
  | { allowed: false; verified: null; decision: Decision }
  /** `verify` ran. On failure, `decision` is the one that counted it. */
  | { allowed: true; verified: boolean; decision: Decision };

export interface Lockout {
  /**
   * Run `verify` unless `id` has failed too often. A false result counts as a failure, a true
   * one clears the count. If `verify` throws, nothing is counted and the error is yours.
   */
  attempt(id: string, verify: () => boolean | Promise<boolean>): Promise<Attempt>;
}

/**
 * A limit that counts only failed attempts, as a login does: while the budget of failures is
 * spent, even a correct attempt is refused without being checked, which is what makes guessing
 * useless. See "Recipes" in the spec.
 */
export function createLockout(options: LockoutOptions): Lockout {
  const { limiter, resetOnSuccess = true } = options;
  return {
    async attempt(id, verify) {
      const gate = await limiter.peek(id);
      if (!gate.allowed) return { allowed: false, verified: null, decision: gate };
      if (await verify()) {
        if (resetOnSuccess) await limiter.reset(id);
        return { allowed: true, verified: true, decision: gate };
      }
      return { allowed: true, verified: false, decision: await limiter.check(id) };
    },
  };
}
