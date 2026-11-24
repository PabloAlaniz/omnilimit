# Threat model

What omnilimit defends against, how, and what it leaves to you. It is reviewed when the spec gains a section, and before each minor release.

## The system

```text
client ──> your proxies ──> adapter ──> limiter ──> store
untrusted  trusted by       reads the   decides     Redis, Valkey, Postgres,
           configuration    identity                Durable Objects, memory
```

| Actor | Trust | Controls |
|---|---|---|
| Client | None | Every header, the timing and number of requests, the identifiers it submits |
| Proxies you run | As configured | What they append to `X-Forwarded-For`, the single-IP header they set |
| Application | Full | The policy, the key, the failure mode, the salt |
| Store | Full | The counters and the clock |
| Other users of the same store | Can read keys | Nothing in omnilimit's keys, unless they share the scope, policy and salt |

The library has no network listener, no configuration file and no runtime dependency. Its interfaces are the functions each port exports, the adapters, the `Store` interface, and the scripts it sends to the store (`spec/scripts/`).

## What is protected

1. **The limit.** A client does not get more than `burst + limit - 1` requests in a window, by any choice of headers or timing.
2. **Availability on your terms.** When the store fails, the outcome is the one you chose.
3. **Identifiers.** IPs, emails and user ids do not reach the store or the errors in clear.

## Threats

| # | Threat | Defence | What remains |
|---|---|---|---|
| 1 | Rotating `X-Forwarded-For` to get a fresh bucket each time | The chain is read from the right, at a fixed hop count. A short chain or an invalid value gives `null`, never the first value | A preset that does not match the deployment. No library can detect that: see [client IP](client-ip.md) |
| 2 | Reaching the origin around the proxy, with a forged single-IP header | Documented as the condition of every preset | Restricting the origin is yours to do |
| 3 | Rotating addresses inside one IPv6 allocation | Key helpers group by `/64` | Clients holding a larger prefix. Widen `ipv6Prefix` if you see it |
| 4 | Dropping the identity so that no key is built | `onMissingKey` is required, and `shared` keeps those requests under one limit | `skip` removes the limit for them, by your choice |
| 5 | Many requests at once, to pass between a read and a write | One atomic operation per hit in every `exact` store. Concurrency vectors send 200 simultaneous hits and require exactly `burst` allowed | The memory store is exact per process only. `checkAll` is advisory and says so; `reserveAll` never admits more |
| 6 | Retrying while denied, to push the reset further or to wear the store | A denial writes nothing | A denied request still costs one store round trip |
| 7 | Clock skew between servers, or a clock set back | The store's clock is the only one used, and `t = max(now, ts)` never refunds | A store whose own clock jumps forward drains buckets early |
| 8 | Making the store fail, to remove the limit | `onStoreError` has no default. `closed` answers 503 | `open` lets traffic through during the outage, and `closed` turns a store outage into an application outage. Choose per limiter |
| 9 | A cost chosen to overflow or to pass for free | Cost is an integer from 1 to `burst`, checked before any store call. Policies that could exceed 2^53 - 1 are rejected | None known |
| 10 | Reading identifiers from the store | The key holds 128 bits of `SHA-256(salt, 0x00, id)` | Without a salt, or with a leaked one, a small id space such as IPv4 can be enumerated. Set `salt` to a secret |
| 11 | Colliding with another limiter's counter | The scope and the policy are part of the key, and the scope has a strict grammar | Two limiters given the same scope, policy and salt share a counter, by design |
| 12 | Filling the store with new keys | Redis sets a TTL of one drain period, a Durable Object deletes itself once drained, and the memory store holds at most `maxKeys` | Postgres needs `cleanup()` scheduled. A full memory store evicts live buckets, which resets their limits |
| 13 | Locking a victim out by failing on their account | A lockout over several limits counts per account and per address, each with its own budget | A limit keyed only by account can be used against its owner: see [recipes](recipes.md) |
| 14 | Learning a limit that is not enforced yet | Shadow mode sends no headers | In enforcing mode the headers state the policy, as the IETF draft intends. The `none` profile sends only `Retry-After` |
| 15 | A tampered release or dependency | No runtime dependencies. Releases are built in CI and published with provenance through OIDC; actions are pinned by commit; lockfiles are frozen in CI | The maintainer's account is a single point of trust: see [governance](https://github.com/PabloAlaniz/omnilimit/blob/main/GOVERNANCE.md) |

## Out of scope

- **Volumetric attacks.** By the time a request reaches the limiter it has already cost you a connection. That belongs at the network edge.
- **Authentication.** omnilimit trusts the id the application gives it.
- **The store's own security.** Access control, TLS and persistence of Redis or Postgres are the operator's.

## Report a gap

If a defence above does not hold, that is a vulnerability: see the [security policy](https://github.com/PabloAlaniz/omnilimit/blob/main/SECURITY.md).
