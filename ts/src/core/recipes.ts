import { assertLimiterCost, assertLimiterRefund } from './limiter.js';
import type { Decision, Limiter, Reservation } from './types.js';

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

/**
 * What a pending check gave back, one value per decision: `true` when that entry's reservation
 * was refunded, `false` when its refund failed and it stays counted, `null` when nothing of it
 * was counted.
 */
export interface Cancelled {
  refunded: (boolean | null)[];
}

/**
 * The first phase of a combined check, from `peekAll` or `reserveAll`. Settle it once, with
 * `commit` when the work happened or `cancel` when it did not. See "Combined check" in the spec.
 */
export interface Pending extends CombinedDecision {
  /** `peek`: nothing is counted until `commit`. `reserve`: everything is counted until `cancel`. */
  readonly mode: 'peek' | 'reserve';
  /**
   * What opening it gave back, one value per decision as in `Cancelled`. Only a denied `reserve`
   * has anything here: the reservations taken before the denial, refunded at once.
   */
  readonly refunded: (boolean | null)[];
  /** True once `commit` or `cancel` was called. */
  readonly settled: boolean;
  /**
   * The work happened. In `peek` mode this counts every entry, in order, and stops at the first
   * denial: the entries before it stay counted. In `reserve` mode, and on a denied pending, it
   * makes no store call and answers with the pending's own result.
   */
  commit(): Promise<CombinedDecision>;
  /**
   * The work did not happen. In `reserve` mode this refunds every reservation, in order; otherwise
   * it makes no store call. It never rejects for a store failure: a failed refund is `false`.
   */
  cancel(): Promise<Cancelled>;
}

/**
 * A pending check that was already settled the other way: `cancel` after `commit`, or `commit`
 * after `cancel`. Calling the same one again is not an error: it answers with the first result.
 */
export class SettledError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'SettledError';
  }
}

const options = (cost: number | undefined) => (cost === undefined ? {} : { cost });

/** Everything a recipe can refuse, checked before its first store call. See "Recipes" in the spec. */
function validate(recipe: string, entries: readonly LimitEntry[], mode: 'peek' | 'reserve'): void {
  if (entries.length === 0) throw new RangeError(`omnilimit: ${recipe} needs at least one limit`);
  for (const { limiter, cost } of entries) {
    if (limiter.tier === 'approximate') {
      throw new TypeError(`omnilimit: ${recipe} ${mode === 'peek' ? 'peeks' : 'refunds'}, and a store of tier approximate cannot`);
    }
    if (mode === 'reserve') assertLimiterRefund(limiter);
    assertLimiterCost(limiter, cost);
  }
}

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

/** Rule 8: one settlement, marked before its first store call; the same one again shares it. */
function pending(
  mode: Pending['mode'],
  own: CombinedDecision,
  refunded: (boolean | null)[],
  onCommit: () => Promise<CombinedDecision>,
  onCancel: () => Promise<Cancelled>,
): Pending {
  let settledBy: 'commit' | 'cancel' | null = null;
  let outcome: Promise<CombinedDecision | Cancelled> | undefined;
  const settle = (op: 'commit' | 'cancel', work: () => Promise<CombinedDecision | Cancelled>) => {
    if (settledBy === null) {
      settledBy = op;
      outcome = work();
    } else if (settledBy !== op) {
      return Promise.reject(new SettledError(`omnilimit: this pending check was already settled by ${settledBy}`));
    }
    return outcome as Promise<CombinedDecision | Cancelled>;
  };
  return {
    mode,
    allowed: own.allowed,
    decision: own.decision,
    decisions: own.decisions,
    refunded,
    get settled() {
      return settledBy !== null;
    },
    commit: () => settle('commit', onCommit) as Promise<CombinedDecision>,
    cancel: () => settle('cancel', onCancel) as Promise<Cancelled>,
  };
}

async function countAll(entries: readonly LimitEntry[]): Promise<CombinedDecision> {
  const decisions: Decision[] = [];
  for (const { limiter, id, cost } of entries) {
    const decision = await limiter.check(id, options(cost));
    decisions.push(decision);
    if (!decision.allowed) return { allowed: false, decision, decisions };
  }
  return { allowed: true, decision: fewestLeft(decisions), decisions };
}

async function refundEach(entries: readonly LimitEntry[], held: (Reservation | null)[]): Promise<(boolean | null)[]> {
  const refunded: (boolean | null)[] = [];
  for (const [index, reservation] of held.entries()) {
    refunded.push(reservation === null ? null : await (entries[index] as LimitEntry).limiter.refund(reservation));
  }
  return refunded;
}

async function openPeek(recipe: string, entries: readonly LimitEntry[]): Promise<Pending> {
  validate(recipe, entries, 'peek');
  const peeks: Decision[] = [];
  for (const { limiter, id, cost } of entries) peeks.push(await limiter.peek(id, options(cost)));
  const refused = peeks.filter((decision) => !decision.allowed);
  const own: CombinedDecision =
    refused.length > 0
      ? { allowed: false, decision: longestWait(refused), decisions: peeks }
      : { allowed: true, decision: fewestLeft(peeks), decisions: peeks };
  const nothing = () => peeks.map(() => null);
  return pending(
    'peek',
    own,
    nothing(),
    async () => (own.allowed ? countAll(entries) : own),
    async () => ({ refunded: nothing() }),
  );
}

/**
 * Open a combined check in `peek` mode: peek every limit, in order, and count nothing. `commit`
 * then counts every limit, `cancel` leaves them alone. Advisory: two requests racing for the last
 * unit can both pass the peek, and then the second is denied while committing, with the limits
 * before it already counted. A pending never settled counts nothing. See "Combined check" in the
 * spec.
 *
 * Before any store call, throws a `RangeError` for an empty list or a cost a limit does not
 * accept, and a `TypeError` for a limiter of tier `approximate`.
 */
export function peekAll(entries: readonly LimitEntry[]): Promise<Pending> {
  return openPeek('peekAll', entries);
}

/**
 * Open a combined check in `reserve` mode: count every limit, in order, and stop at the first
 * denial, refunding the limits counted before it. When allowed, `commit` keeps the counts and
 * `cancel` refunds them all. Unlike `peekAll` it never lets more through than the limits allow,
 * at the price of holding the earlier limits for a round trip when a later one denies. A pending
 * never settled stays counted. See "Combined check" in the spec.
 *
 * Before any store call, throws a `RangeError` for an empty list or a cost a limit does not
 * accept, and a `TypeError` for a limiter whose store cannot refund, such as tier `approximate`.
 */
export async function reserveAll(entries: readonly LimitEntry[]): Promise<Pending> {
  validate('reserveAll', entries, 'reserve');
  const decisions: Decision[] = [];
  const held: (Reservation | null)[] = [];
  for (const { limiter, id, cost } of entries) {
    const { decision, reservation } = await limiter.reserve(id, options(cost));
    decisions.push(decision);
    held.push(reservation);
    if (!decision.allowed) {
      // Give back what was counted before the denial, in entry order. The denial holds nothing.
      const refunded = await refundEach(entries, held);
      const own: CombinedDecision = { allowed: false, decision, decisions };
      return pending('reserve', own, refunded, async () => own, async () => ({ refunded: [...refunded] }));
    }
  }
  const own: CombinedDecision = { allowed: true, decision: fewestLeft(decisions), decisions };
  return pending(
    'reserve',
    own,
    decisions.map(() => null),
    async () => own,
    async () => ({ refunded: await refundEach(entries, held) }),
  );
}

/**
 * Apply several limits to one request, such as per IP and per account, so that a request one
 * of them denies does not use up the others. It peeks every limit first and counts only when
 * all of them would allow: `peekAll` followed at once by `commit`. See "Recipes" in the spec.
 *
 * The peek is advisory: two requests racing for the last unit can both pass it, and then the
 * second is denied while counting. The limits already counted for it stay counted. Needs
 * stores that can peek, so not the `approximate` tier.
 *
 * Before any store call, throws a `RangeError` for an empty list or a cost a limit does not
 * accept, and a `TypeError` for a limiter of tier `approximate`.
 */
export async function checkAll(entries: readonly LimitEntry[]): Promise<CombinedDecision> {
  return (await openPeek('checkAll', entries)).commit();
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
   * Throws a `TypeError`, before any store call, if the limiter is of tier `approximate`.
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
      validate('a lockout', [{ limiter, id }], 'peek');
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
