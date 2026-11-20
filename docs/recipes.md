# Recipes

A few compositions of `check`, `peek`, `reset`, `reserve` and `refund` come up often enough to ship with a fixed meaning, the same in every port. They add no state and no store operation. The exact steps are in the [spec](https://github.com/PabloAlaniz/omnilimit/blob/main/spec/SPEC.md#recipes).

| | TypeScript | Python |
|---|---|---|
| Several limits on one request | `checkAll(entries)` | `check_all(entries)`, `check_all_async(entries)` |
| The same, around work that may not happen | `peekAll(entries)`, `reserveAll(entries)` | `peek_all`, `reserve_all`, and their `_async` forms |
| Count only failures | `createLockout({ limiter })` or `createLockout({ limits, mode })` | `Lockout(limiter)` or `Lockout(limits=..., mode=...)`, and `AsyncLockout` |

They need stores that can peek, so not the `approximate` tier, and `reserveAll` needs stores that can refund. Every entry is checked before the first store call: an empty list, a cost a limit does not accept or an `approximate` limiter raises an error and touches no store. Both are tested by the shared `recipe` vectors, down to the order of every store call.

## Several limits on one request

```ts
import { checkAll } from 'omnilimit';

const result = await checkAll([
  { limiter: perIp, id: ip },
  { limiter: perAccount, id: email },
]);
```

```python
from omnilimit import check_all

result = check_all([(per_ip, ip), (per_account, email)])
```

1. Every limit is peeked, in order. If any would deny, nothing is counted.
2. Otherwise every limit is counted, in order.

The result has `allowed`, the `decision` to answer with, and all the `decisions`:

- **Denied:** `decision` is the denial with the longest wait, so its `Retry-After` is the one that is true for the request.
- **Allowed:** `decision` is the one with the fewest units left, so the headers describe the limit the client will hit first.

An entry can carry a cost, the units the request weighs under that limit: `{ limiter, id, cost: 6 }`, or `(limiter, id, 6)` in Python.

### It is not atomic

A peek is advisory. Two requests racing for the last unit of one limit can both pass step 1; in step 2 the second is denied, and the limits listed before that one stay counted for it. Put the limit most likely to deny first to keep that small, or use `reserveAll` (below), which never lets more through. An atomic version needs one store call over several keys, which is on the [roadmap](roadmap.md).

A store failure follows each limiter's own `onStoreError`: a `closed` limiter that cannot reach its store denies the whole request with a `503`.

## Two phases: decide, work, then settle

`checkAll` decides and counts in one go. When the request does work that can still fail or be abandoned after it is admitted, such as an upload or a job, split it in two: open a pending check, do the work, then `commit` it if the work happened or `cancel` it if it did not.

```ts
import { reserveAll } from 'omnilimit';

const pending = await reserveAll([
  { limiter: perIp, id: ip },
  { limiter: perUser, id: user, cost: items.length },
]);
if (!pending.allowed) return deny(pending.decision);
try {
  await runJob(items);
  await pending.commit();
} catch (error) {
  await pending.cancel(); // gives the units back
  throw error;
}
```

```python
from omnilimit import reserve_all

pending = reserve_all([(per_ip, ip), (per_user, user, len(items))])
if not pending.allowed:
    return deny(pending.decision)
try:
    run_job(items)
    pending.commit()
except Exception:
    pending.cancel()  # gives the units back
    raise
```

There are two modes, and they fail in opposite directions:

| | `peekAll` | `reserveAll` |
|---|---|---|
| Opening | Peeks every limit, counts nothing | Counts every limit, in order. If one denies, refunds the ones before it |
| `commit()` | Counts every limit, stops at the first denial | No store call: the counts stay |
| `cancel()` | No store call | Refunds every limit |
| Never settled | Nothing counted | Everything stays counted |
| Under concurrency | Can let a few more through: the peek is advisory, and `commit` can still be denied | Never lets more through than the limits allow. A request denied by a later limit holds the earlier ones for one round trip, so a concurrent request can be denied by them |

Use `reserveAll` when letting too much through costs more than a false denial, and `peekAll` when the work rarely fails and the opposite holds. `checkAll` is `peekAll` followed at once by `commit`.

The pending has `allowed`, `decision`, `decisions` and `refunded`. `commit()` answers with the combined result. `cancel()` answers with `refunded`, one value per decision: `true` when that limit was given back, `false` when its refund failed and it stays counted, `null` when nothing was counted for it. A denied `reserveAll` already refunded what it counted, and reports it in `pending.refunded`.

A pending settles once. Calling the same method again answers with the first result without touching the store, also while the first call is still running. Calling the other one throws a `SettledError` (`SettledError` is a `RuntimeError` in Python): a job cannot be both done and not done. A refund gives back only what has not drained yet, so cancel soon after the work fails; and do not `reset` a limit that a pending still holds.

## Count only failures

```ts
import { createLockout } from 'omnilimit';

const lockout = createLockout({ limiter: failures });
const attempt = await lockout.attempt(id, () => secretMatches(id, secret));
```

```python
from omnilimit import Lockout

lockout = Lockout(failures)
attempt = lockout.attempt(id, lambda: secret_matches(id, secret))
```

| `attempt.allowed` | `attempt.verified` | Meaning | `attempt.decision` |
|---|---|---|---|
| `false` | `null` | Too many failures. The verification did not run | The denial to answer with |
| `true` | `true` | Verified. The count was cleared | What the gate saw |
| `true` | `false` | Wrong. This attempt was counted | The decision that counted it |

`attempt.decisions` holds one decision per limit, and after a success `attempt.resets` says which limits were reset (`true`), failed to reset (`false`) or do not reset (`null`).

The limiter's policy is the budget of failures: `limit: 5, windowMs: 900_000` tolerates five wrong attempts and then gives one back every three minutes.

### Several limits, and reserve mode

Most credential checks want more than one limit: one per account, so guesses spread across addresses still add up, and one per address, so one address cannot lock out every account by failing on purpose. Give the lockout `limits`, each with the id it takes from the subject:

```ts
const lockout = createLockout<{ account: string; ip: string }>({
  limits: [
    { limiter: perAccount, idOf: (s) => s.account },
    { limiter: perIp, idOf: (s) => s.ip, resetOnSuccess: false },
  ],
  mode: 'reserve',
});

const attempt = await lockout.attempt({ account, ip }, () => secretMatches(account, secret));
```

```python
from omnilimit import Lockout, LockoutLimit

lockout = Lockout(
    limits=[
        LockoutLimit(per_account, id_of=lambda s: s["account"]),
        LockoutLimit(per_ip, id_of=lambda s: s["ip"], reset_on_success=False),
    ],
    mode="reserve",
)
attempt = lockout.attempt({"account": account, "ip": ip}, lambda: secret_matches(account, secret))
```

A failure counts under every limit, even when an earlier one is already spent: under a flood of guesses, the shared per-IP limit must see every one of them. A success resets the limits that reset on success (the default) and leaves the others alone, so one success behind a shared address does not clear everyone's count.

The two modes differ in when the failure is counted:

| | `advisory` (default) | `reserve` (recommended) |
|---|---|---|
| Before the verification | Peeks every limit | Counts one failure under every limit. If one denies, gives the others back |
| Wrong | Counts the failure under every limit | Nothing more: it was counted already |
| Right | Resets the limits that reset | Resets those, and gives back the failure counted under the others |
| Verification throws | Counts nothing | Gives everything back |
| Under concurrency | Attempts that overlap all pass the peek, so the verification can run more than `burst` times | The verification never runs more often than the limits have units |

Use `reserve` for credential checks: an attacker running many guesses in parallel gets no more verifications than the budget allows, whatever the timing. Its cost is a store call more on a success, and a request that a later limit denies holds the earlier limits for one round trip. `advisory` is the default only so that a lockout of earlier versions behaves exactly as before.

When the verification is not a single function, open the gate yourself and settle it once:

```ts
const gate = await lockout.begin({ account, ip });
if (!gate.allowed) return deny(gate.decision);   // the verification must not run
// ... verify ...
await (ok ? gate.succeed() : gate.fail());       // or gate.cancel() if it could not tell
```

A denied gate makes no store call whatever you call on it, so a correct guess cannot open a locked gate. The gate settles once, like a pending check: calling the other method afterwards throws a `SettledError`.

### Things to decide

- **The id.** An account id alone lets anyone lock an account out by failing on purpose; an account and address pair lets an attacker spread guesses across accounts. Several limits cover both.
- **`resetOnSuccess: false`** keeps the failures after a success. Use it when the id is shared, such as an IP behind which many people sign in, so one person's success does not clear everyone's count.
- **An exception in the verification** counts nothing and reaches you. A database that is down should not lock anyone out.
- **Overlapping attempts in advisory mode.** A failure is counted after the verification, so attempts that arrive together can all pass the peek first. Under a flood of parallel guesses the verification can run more than `burst` times: each unit freed lets through about the attacker's rate times the time between the peek and the count. Reserve mode does not have this problem.
