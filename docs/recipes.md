# Recipes

Two compositions of `check`, `peek` and `reset` come up often enough to ship with a fixed meaning, the same in every port. They add no state and no store operation. The exact steps are in the [spec](https://github.com/PabloAlaniz/omnilimit/blob/main/spec/SPEC.md#recipes).

| | TypeScript | Python |
|---|---|---|
| Several limits on one request | `checkAll(entries)` | `check_all(entries)`, `check_all_async(entries)` |
| Count only failures | `createLockout({ limiter })` | `Lockout(limiter)`, `AsyncLockout(limiter)` |

Both need stores that can peek, so not the `approximate` tier.

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

A peek is advisory. Two requests racing for the last unit of one limit can both pass step 1; in step 2 the second is denied, and the limits listed before that one stay counted for it. Put the limit most likely to deny first to keep that small. An atomic version needs one store call over several keys, which is on the [roadmap](roadmap.md).

A store failure follows each limiter's own `onStoreError`: a `closed` limiter that cannot reach its store denies the whole request with a `503`.

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
