# Security Policy

## Supported versions

Only the latest release of each package receives fixes, security fixes included. A release stops being supported the moment a newer one is published: before 1.0 there are no maintenance branches.

## Reporting a vulnerability

Please do not open a public issue for security problems. Use GitHub's private vulnerability reporting instead:

https://github.com/PabloAlaniz/omnilimit/security/advisories/new

What to expect:

| Step | When |
|---|---|
| Acknowledgement | Within 7 days |
| Assessment: accepted or not, and why | Within 14 days |
| Fix released and advisory published | Together, as soon as the fix is verified, and no later than 90 days after the report |

Disclosure is coordinated: the details stay private until the fix is out, and the date is agreed with you. Confirmed vulnerabilities are published as [GitHub security advisories](https://github.com/PabloAlaniz/omnilimit/security/advisories), with the affected versions. The report is credited there and in the changelog unless you prefer otherwise.

The [threat model](docs/threat-model.md) says what the library defends against and what it leaves to you.

## In scope

- Evading a limit by forging a client IP under a correctly chosen strategy.
- A store failure allowing traffic when `onStoreError: 'closed'` is set.
- Decisions that break the guarantees of a store's tier.
- Identifiers leaking through keys or errors.

## Out of scope

- A preset used in a deployment it does not match: see `docs/client-ip.md`.
- Exceeding a limit by the documented `burst + limit - 1` peak.
- Volumetric attacks, which belong at your network edge.
