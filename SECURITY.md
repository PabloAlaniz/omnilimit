# Security Policy

## Supported versions

Only the latest release receives fixes.

## Reporting a vulnerability

Please do not open a public issue for security problems. Use GitHub's private vulnerability reporting instead:

https://github.com/PabloAlaniz/omnilimit/security/advisories/new

You should get an acknowledgement within a few days. Once a fix is released, the report is credited in the changelog unless you prefer otherwise.

## In scope

- Evading a limit by forging a client IP under a correctly chosen strategy.
- A store failure allowing traffic when `onStoreError: 'closed'` is set.
- Decisions that break the guarantees of a store's tier.
- Identifiers leaking through keys or errors.

## Out of scope

- A preset used in a deployment it does not match: see `docs/client-ip.md`.
- Exceeding a limit by the documented `burst + limit - 1` peak.
- Volumetric attacks, which belong at your network edge.
