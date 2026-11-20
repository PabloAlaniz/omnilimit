# Recipes

A few compositions of `check`, `peek`, `reset`, `reserve` and `refund` come up often enough to ship with a fixed meaning, the same in every port. They add no state and no store operation. The exact steps are in the [spec](https://github.com/PabloAlaniz/omnilimit/blob/main/spec/SPEC.md#recipes).

| | TypeScript | Python |
|---|---|---|
| Several limits on one request | `checkAll(entries)` | `check_all(entries)`, `check_all_async(entries)` |
| The same, around work that may not happen | `peekAll(entries)`, `reserveAll(entries)` | `peek_all`, `reserve_all`, and their `_async` forms |
| Count only failures | `createLockout({ limiter })` | `Lockout(limiter)`, `AsyncLockout(limiter)` |

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

const logins = createLockout({ limiter: failedLogins });
const attempt = await logins.attempt(id, () => passwordMatches(email, password));
```

```python
from omnilimit import Lockout

logins = Lockout(failed_logins)
attempt = logins.attempt(id, lambda: password_matches(email, password))
```

| `attempt.allowed` | `attempt.verified` | Meaning | `attempt.decision` |
|---|---|---|---|
| `false` | `null` | Too many failures. The verification did not run | The denial to answer with |
| `true` | `true` | Verified. The count was cleared | What the peek saw |
| `true` | `false` | Wrong. This attempt was counted | The decision that counted it |

The limiter's policy is the budget of failures: `limit: 5, windowMs: 900_000` tolerates five wrong attempts and then gives one back every three minutes.

Things to decide:

- **The id.** `email` alone lets anyone lock an account out by failing on purpose. `email:ip` avoids that but lets an attacker spread guesses across addresses. Many sites use both, one lockout each.
- **`resetOnSuccess: false`** keeps the failures after a success. Use it when the id is shared, such as an IP behind which many people log in, so one person's success does not clear everyone's count.
- **An exception in the verification** counts nothing and reaches you. A database that is down should not lock anyone out.
- **Overlapping attempts.** A failure is counted after the verification, so attempts that arrive together can all pass the peek first. Under a flood of parallel guesses the verification can run more than `burst` times: each unit freed lets through about the attacker's rate times the time between the peek and the count. A plain limit on all attempts in front of the lockout bounds that.
