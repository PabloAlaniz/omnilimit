# How it compares

Pick the tool that fits. These notes come from each project's public documentation, and may be out of date: check the projects before deciding.

| Library | Stacks | Pick it when |
|---|---|---|
| [express-rate-limit](https://github.com/express-rate-limit/express-rate-limit) | Express | You only run Express and want the most established option |
| [hono-rate-limiter](https://github.com/rhinobase/hono-rate-limiter) | Hono | You only run Hono and want its store ecosystem |
| [@upstash/ratelimit](https://github.com/upstash/ratelimit-js) | TypeScript, with a Python sibling | You use Upstash Redis |
| [slowapi](https://github.com/laurentS/slowapi), [limits](https://github.com/alisaifee/limits) | Python | You only run Python and want their maturity and wider store support |
| [Halt](https://github.com/Surafeljava/halt) | TypeScript and Python | You need its per-plan quotas or weighted endpoints |
| [ThrottleKit](https://www.npmjs.com/package/throttlekit) | TypeScript | You want a formally bounded overshoot on TypeScript |

Pick omnilimit when you want one behaviour across frameworks and stores, verified on every commit, with client IP handling and store failure as part of the contract.

It has a narrower scope on purpose. It has one algorithm, and no quotas per plan or weighted costs. Lockouts that count only failures are built from `peek`, `check` and `reset`; there is no ban list or escalating penalty. See the [roadmap](roadmap.md) for what may come next.

`omnilimit/ip` is independent, so you can use it with any of the libraries above.
