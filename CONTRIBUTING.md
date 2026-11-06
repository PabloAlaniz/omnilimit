# Contributing to omnilimit

Thanks for taking the time.

## Setup

Requires Node 20+, pnpm, Python 3.10+ and Docker.

```bash
pnpm install
docker compose -f compose.test.yml -p omnilimit-test up -d --wait
```

```bash
pnpm test           # core conformance, no services
pnpm check:spec     # vectors and generated assets are in sync
pnpm test:workerd   # real workerd: Durable Objects and the rate limit binding
pnpm check:package  # publint and arethetypeswrong

export OMNILIMIT_TEST_REDIS_URLS=redis://127.0.0.1:56379,redis://127.0.0.1:56380
export OMNILIMIT_TEST_POSTGRES_URLS=postgres://postgres:omnilimit@127.0.0.1:55415/omnilimit,postgres://postgres:omnilimit@127.0.0.1:55417/omnilimit
pnpm test:stores    # real Redis, Valkey and Postgres
pnpm mutants        # injects bugs and fails if the suite misses one
```

The store tests write random keys and may truncate `omnilimit_state`: only point them at throwaway databases.

## Changing behaviour

Behaviour lives in `spec/`, and ports follow it:

1. Update `spec/SPEC.md`.
2. Change the independent model in `tools/gen_vectors.py` and run it. Never edit `spec/vectors/` by hand.
3. Run `tools/sync_assets.py` if you touched `spec/scripts/`, and never edit `ts/src/generated/` by hand.
4. Update the port until the vectors pass.

If you fix a bug the suite missed, add a vector, and a mutant to `tools/run_mutants.mjs` if it fits.

## Pull requests

PRs are squash-merged, so titles must be [conventional commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:`...). The title drives the changelog and the version. Coverage has a 95% floor.
