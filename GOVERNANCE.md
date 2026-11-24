# Governance

omnilimit is maintained by one person. This page says who can do what, and how that would change.

## Roles

| Role | Who | Can |
|---|---|---|
| Maintainer | [Pablo Alaniz](https://github.com/PabloAlaniz) | Merge to `main`, change repository settings, approve releases, handle security reports |
| Contributor | Anyone | Open issues and pull requests, review, propose changes to the spec |

There are no other collaborators with write access.

## Access to sensitive resources

| Resource | Who has it | How it is protected |
|---|---|---|
| Repository administration | Maintainer | Two-factor authentication on the account |
| `main` | Nobody pushes directly | Changes arrive by pull request, and the `ci-ok` check must pass |
| Publishing to npm and PyPI | The release workflow only | Trusted publishing through OIDC, from the `npm` and `pypi` environments. No publish token exists |
| `RELEASE_PLEASE_TOKEN` | Maintainer | Optional, fine-grained, limited to this repository, with an expiry date |
| Security advisories | Maintainer | GitHub private vulnerability reporting |

## Decisions

Behaviour is decided in [`spec/SPEC.md`](spec/SPEC.md), in public, through pull requests. A change is accepted when it keeps the contract: one behaviour in every port and store of a tier, proven by vectors from the independent generator. The [roadmap](docs/roadmap.md) records what was declined and why.

The maintainer decides when there is no agreement.

## Adding a maintainer

A second maintainer would be someone with a history of merged contributions that touched the spec, the generator and a port. Before getting write access they need two-factor authentication on their account, and the change is announced in this file. Access is the least the role needs: write, not administration.

## Continuity

One maintainer is a risk, and it is stated here rather than hidden. Releases need no personal credential, the spec and the vectors are enough to rebuild a port, and the licence allows a fork. If the project stops being maintained, the README will say so.
