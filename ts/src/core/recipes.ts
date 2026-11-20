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
function settlement<Op extends string>(what: string) {
  let settledBy: Op | null = null;
  let outcome: Promise<unknown> | undefined;
  return {
    get settled() {
      return settledBy !== null;
    },
    settle<T>(op: Op, work: () => Promise<T>): Promise<T> {
      if (settledBy === null) {
        settledBy = op;
        outcome = work();
      } else if (settledBy !== op) {
        return Promise.reject(new SettledError(`omnilimit: this ${what} was already settled by ${settledBy}`));
      }
      return outcome as Promise<T>;
    },
  };
}

/** What opening a combined check found: its own result, what it gave back, and what it holds. */
interface Opened {
  own: CombinedDecision;
  refunded: (boolean | null)[];
  held: (Reservation | null)[];
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

/** Rule 3 over a list where any decision may be denied. */
function governing(decisions: Decision[]): CombinedDecision {
  const refused = decisions.filter((decision) => !decision.allowed);
  return refused.length > 0
    ? { allowed: false, decision: longestWait(refused), decisions }
    : { allowed: true, decision: fewestLeft(decisions), decisions };
}

async function peekEach(recipe: string, entries: readonly LimitEntry[]): Promise<Opened> {
  validate(recipe, entries, 'peek');
  const peeks: Decision[] = [];
  for (const { limiter, id, cost } of entries) peeks.push(await limiter.peek(id, options(cost)));
  const nothing = peeks.map(() => null);
  return { own: governing(peeks), refunded: nothing, held: nothing };
}

async function reserveEach(recipe: string, entries: readonly LimitEntry[]): Promise<Opened> {
  validate(recipe, entries, 'reserve');
  const decisions: Decision[] = [];
  const held: (Reservation | null)[] = [];
  for (const { limiter, id, cost } of entries) {
    const { decision, reservation } = await limiter.reserve(id, options(cost));
    decisions.push(decision);
    held.push(reservation);
    if (!decision.allowed) {
      // Give back what was counted before the denial, in entry order. The denial holds nothing.
      const refunded = await refundEach(entries, held);
      return { own: { allowed: false, decision, decisions }, refunded, held: decisions.map(() => null) };
    }
  }
  return { own: { allowed: true, decision: fewestLeft(decisions), decisions }, refunded: decisions.map(() => null), held };
}

function pending(mode: Pending['mode'], entries: readonly LimitEntry[], { own, refunded, held }: Opened): Pending {
  const once = settlement<'commit' | 'cancel'>('pending check');
  return {
    mode,
    allowed: own.allowed,
    decision: own.decision,
    decisions: own.decisions,
    refunded,
    get settled() {
      return once.settled;
    },
    commit: () => once.settle('commit', async () => (mode === 'peek' && own.allowed ? countAll(entries) : own)),
    cancel: () => once.settle('cancel', async () => ({ refunded: own.allowed ? await refundEach(entries, held) : [...refunded] })),
  };
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
export async function peekAll(entries: readonly LimitEntry[]): Promise<Pending> {
  return pending('peek', entries, await peekEach('peekAll', entries));
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
  return pending('reserve', entries, await reserveEach('reserveAll', entries));
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
  return pending('peek', entries, await peekEach('checkAll', entries)).commit();
}

/** The one-limit form of earlier versions: an advisory lockout with the id as subject. */
export interface LockoutOptions {
  /** Counts the failed attempts. Its policy is how many failures are tolerated, and how fast they come back. */
  limiter: Limiter;
  /** Forget the failures after a success. Defaults to `true`. */
  resetOnSuccess?: boolean;
}

/** One limit of a lockout. `idOf` may be left out only when the subject is a string, its own id. */
export type LockoutLimit<S = string> = {
  /** Counts the failed attempts under this limit. */
  limiter: Limiter;
  /** Forget this limit's failures after a success. Defaults to `true`. */
  resetOnSuccess?: boolean;
} & (S extends string ? { idOf?: (subject: S) => string } : { idOf: (subject: S) => string });

/** A lockout over several limits, such as per subject and per IP. */
export interface MultiLockoutOptions<S = string> {
  limits: readonly LockoutLimit<S>[];
  /**
   * `advisory` (the default) peeks before the verification and counts a failure after it: under
   * concurrency, more verifications than the limits allow can run. `reserve` counts before the
   * verification and gives back on success: never more verifications in flight than the limits
   * have units. Prefer `reserve` for credential checks.
   */
  mode?: 'advisory' | 'reserve';
}

/** What a gate's `succeed` answers: the gate's own result, with what it reset and refunded, one value per limit. */
export interface Succeeded extends CombinedDecision {
  /** `true` reset, `false` the reset failed, `null` this limit does not reset on success. */
  resets: (boolean | null)[];
  /** `true` refunded, `false` the refund failed, `null` nothing to refund (and always in advisory mode). */
  refunded: (boolean | null)[];
}

/**
 * One attempt in front of a lockout. Run the verification only when `allowed`, then settle the
 * gate once: `fail()`, `succeed()`, or `cancel()` when the verification could not tell. On a
 * denied gate all three make no store call, so a correct guess cannot open a locked gate. See
 * "Lockout" in the spec.
 */
export interface LockoutGate {
  readonly mode: 'advisory' | 'reserve';
  readonly allowed: boolean;
  /** The governing decision of the gate: when denied, the one to answer with. */
  readonly decision: Decision;
  readonly decisions: Decision[];
  /** In reserve mode, what a denied gate gave back, as in `Pending`. */
  readonly refunded: (boolean | null)[];
  readonly settled: boolean;
  /**
   * The verification failed. Advisory: counts the failure under every limit, in order, without
   * stopping at a denial. Reserve: the gate already counted it, so no store call.
   */
  fail(): Promise<CombinedDecision>;
  /** The verification passed: resets the limits that reset on success, and in reserve mode refunds the others. */
  succeed(): Promise<Succeeded>;
  /** The verification could not tell (it threw): counts nothing, refunding in reserve mode. */
  cancel(): Promise<Cancelled>;
}

export type Attempt =
  /** Too many failures: `verify` was not called. Answer with `decision`. */
  | { allowed: false; verified: null; decision: Decision; decisions: Decision[] }
  /**
   * `verify` ran. On failure, `decision` is the one that counted it. After a success, `resets`
   * says which limits were reset.
   */
  | { allowed: true; verified: boolean; decision: Decision; decisions: Decision[]; resets?: (boolean | null)[] };

export interface Lockout<S = string> {
  /**
   * Open a gate for `subject`. Throws, before any store call, a `TypeError` if a limiter is of
   * tier `approximate`, or cannot refund in reserve mode, or if a limit has no `idOf` and the
   * subject is not a string.
   */
  begin(subject: S): Promise<LockoutGate>;
  /**
   * Run `verify` unless `subject` has failed too often. A false result counts as a failure, a true
   * one clears the limits that reset on success. If `verify` throws, nothing is counted and the
   * error is yours. Throws as `begin` does.
   */
  attempt(subject: S, verify: () => boolean | Promise<boolean>): Promise<Attempt>;
}

function gate(mode: LockoutGate['mode'], entries: LimitEntry[], resetting: boolean[], opened: Opened): LockoutGate {
  const { own, refunded, held } = opened;
  const once = settlement<'fail' | 'succeed' | 'cancel'>('lockout gate');
  const nothing = () => own.decisions.map(() => null);
  const fail = async (): Promise<CombinedDecision> => {
    if (mode === 'reserve' || !own.allowed) return own;
    // The failure happened: every limit records it, whatever an earlier one answers.
    const checks: Decision[] = [];
    for (const { limiter, id } of entries) checks.push(await limiter.check(id));
    return governing(checks);
  };
  const succeed = async (): Promise<Succeeded> => {
    const resets: (boolean | null)[] = nothing();
    const given: (boolean | null)[] = nothing();
    if (own.allowed) {
      for (const [index, { limiter, id }] of entries.entries()) {
        if (resetting[index]) resets[index] = await limiter.reset(id);
        else if (mode === 'reserve') given[index] = held[index] === null ? null : await limiter.refund(held[index] as Reservation);
      }
    }
    return { ...own, resets, refunded: given };
  };
  // Nothing is counted for an attempt that could not tell: in reserve mode, give it all back.
  const cancel = async (): Promise<Cancelled> => ({ refunded: own.allowed ? await refundEach(entries, held) : [...refunded] });
  return {
    mode,
    allowed: own.allowed,
    decision: own.decision,
    decisions: own.decisions,
    refunded,
    get settled() {
      return once.settled;
    },
    fail: () => once.settle('fail', fail),
    succeed: () => once.settle('succeed', succeed),
    cancel: () => once.settle('cancel', cancel),
  };
}

/**
 * A limit that counts only failed attempts, as a sign-in does: while the budget of failures is
 * spent, even a correct attempt is refused without being checked, which is what makes guessing
 * useless. Given one `limiter`, it is the advisory one-limit lockout of earlier versions. Given
 * `limits`, it counts each failure under every limit, such as per subject and per IP, with the id
 * each one's `idOf` takes from the subject. See "Lockout" in the spec.
 */
export function createLockout(options: LockoutOptions): Lockout<string>;
export function createLockout<S = string>(options: MultiLockoutOptions<S>): Lockout<S>;
export function createLockout<S = string>(options: LockoutOptions | MultiLockoutOptions<S>): Lockout<S> {
  const multi = 'limits' in options;
  const limits: readonly LockoutLimit<S>[] = multi
    ? options.limits
    : [{ limiter: options.limiter, resetOnSuccess: options.resetOnSuccess } as LockoutLimit<S>];
  const mode = multi ? (options.mode ?? 'advisory') : 'advisory';
  if (limits.length === 0) throw new RangeError('omnilimit: a lockout needs at least one limit');
  if (mode !== 'advisory' && mode !== 'reserve') throw new TypeError("omnilimit: a lockout's mode is 'advisory' or 'reserve'");
  const resetting = limits.map((limit) => limit.resetOnSuccess ?? true);

  const begin = async (subject: S): Promise<LockoutGate> => {
    const entries = limits.map(({ limiter, idOf }) => {
      if (idOf) return { limiter, id: idOf(subject) };
      if (typeof subject !== 'string') throw new TypeError('omnilimit: a lockout limit needs idOf when the subject is not a string');
      return { limiter, id: subject };
    });
    const opened = mode === 'reserve' ? await reserveEach('a lockout', entries) : await peekEach('a lockout', entries);
    return gate(mode, entries, resetting, opened);
  };

  return {
    begin,
    async attempt(subject, verify) {
      const opened = await begin(subject);
      if (!opened.allowed) return { allowed: false, verified: null, decision: opened.decision, decisions: opened.decisions };
      let verified: boolean;
      try {
        verified = await verify();
      } catch (error) {
        // Nothing is counted for an attempt that could not tell.
        await opened.cancel();
        throw error;
      }
      if (verified) {
        const { decision, decisions, resets } = await opened.succeed();
        return { allowed: true, verified: true, decision, decisions, resets };
      }
      const { decision, decisions } = await opened.fail();
      return { allowed: true, verified: false, decision, decisions };
    },
  };
}
