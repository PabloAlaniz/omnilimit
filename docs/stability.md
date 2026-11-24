# Stability

What you can rely on from one release to the next, and how a change that breaks something is made.

## Versions

Each package has its own version and follows [semantic versioning](https://semver.org/).

| Before 1.0 | From 1.0 |
|---|---|
| A patch release never breaks anything | The same |
| A minor release may break an API. The changelog says so under a heading of its own | A minor release only adds |
| | A major release is the only one that breaks |

The TypeScript and Python packages do not share a version number. What they share is the spec version, which both report as `SPEC_VERSION`.

## What counts as the API

| Part | Stable | Not covered |
|---|---|---|
| TypeScript | Everything a documented entry point exports: `omnilimit`, `omnilimit/ip`, `/redis`, `/postgres`, `/cloudflare`, `/cloudflare/do`, `/web`, `/hono`, `/next`, `/express` | Files under `dist/` reached any other way |
| Python | Every name without a leading underscore in `omnilimit`, `omnilimit.ip`, `omnilimit.asgi`, `omnilimit.fastapi` and `omnilimit.stores.*` | Modules and names that start with an underscore |
| Decisions | The fields of a decision and of an event, and what the spec says of their values | The text of error messages |
| Stores | The `Store` interface, so a store of your own keeps working | |

## What the spec promises

- **A vector never changes meaning within a spec version.** Behaviour that the vectors pin can only change with a new spec version. New behaviour arrives as new sections and new vectors.
- **Header profiles are frozen.** `draft-11` will always send what it sends today. A newer IETF draft gets a profile of its own, and you choose when to move.
- **Problem details bodies** keep their members and their order.

## What the stored state promises

Buckets in Redis, Postgres and Durable Objects outlive a deploy, and during a rolling deploy two versions of your application use them at once.

- **Releases with the same state format read each other's buckets.** CI runs the last published release and the current code against one counter in real Redis and real Postgres, taking turns, and compares every decision with the vectors.
- **The two ports share buckets too,** which CI checks the same way.
- **A new state format gets a new key prefix.** Keys start with `ol:1:`. A format 2 would write to `ol:2:`, so an old version never misreads a new bucket: the two count separately until the old one is gone. The release that introduces it will say what that means for a limit during the deploy.
- **Postgres migrations only add,** and are safe to reapply. A release that needs a new function says so in the changelog, and the old functions keep working for the versions that still call them.

## Deprecation

Something that will be removed is first marked as deprecated in the docs and in the changelog, with what replaces it. It keeps working for at least one more minor release before 1.0, and until the next major release after it.

## Runtimes

Supported means CI runs the suite on it: today Node 20, 22 and 24, Python 3.11 to 3.14, and `workerd`. A runtime is dropped in a minor release after its own maintainers stop supporting it, and the changelog says so.

Stores are tested against the versions in the [conformance matrix](matrix.md).

## Security fixes

Only the latest release of each package gets them: see the [security policy](https://github.com/PabloAlaniz/omnilimit/blob/main/SECURITY.md).

## What 1.0 waits for

- The spec has gone two minor releases without a change that breaks an API.
- Every item under "Next" in the [roadmap](roadmap.md) that would change an existing API is done or declined.
- The promises on this page have held in CI across those releases.
