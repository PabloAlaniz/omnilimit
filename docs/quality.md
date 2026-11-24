# Quality and security practices

How the project keeps its promises, and where to find the proof. Each practice is listed with the control of the [OpenSSF Open Source Project Security Baseline](https://baseline.openssf.org/versions/2026-08-28) it answers, version 2026-08-28, so that anyone can check it.

This is a self-assessment. No third party has audited it.

## The loop

Every change to behaviour goes through the same steps, and CI refuses a change that skips one.

| Step | What happens | Where |
|---|---|---|
| Specify | The behaviour is written down first | [`spec/SPEC.md`](https://github.com/PabloAlaniz/omnilimit/blob/main/spec/SPEC.md) |
| Derive | An independent model, which shares no code with any port, generates the vectors | `tools/gen_vectors.py`, `spec/vectors/` |
| Implement | Each port changes until every vector of its tier passes | `ts/`, `python/` |
| Verify | Vectors run on every runtime and against real stores; both ports share one counter | [Conformance matrix](matrix.md) |
| Check the checker | Realistic bugs are injected, and the build fails if the suite misses one | `tools/run_mutants.mjs`, `tools/run_mutants_py.py` |
| Release | Versions and changelogs come from the commit history; packages are built and published by CI | `.github/workflows/release.yml` |
| Audit | Scorecard, CodeQL and the whole suite run every week on `main`, changes or not | `.github/workflows/` |

**When a bug escapes.** The fix comes with a vector that fails without it, and with a mutant when the bug can be written as one. The changelog records it as a `fix`. A bug that the vectors could not have expressed means the spec was silent, so the spec changes too.

**When the spec changes meaning.** A vector never changes meaning within a spec version. That needs a new version, in `spec/VERSION`.

## Policies

### Tests

- Every pull request runs the suite: the `ci-ok` check gathers all jobs and `main` requires it.
- A change to behaviour adds or updates vectors. A change to a port alone adds tests in that port.
- Coverage has a 95% floor in both ports.
- How to run each suite locally is in [CONTRIBUTING](https://github.com/PabloAlaniz/omnilimit/blob/main/CONTRIBUTING.md).

### Dependencies

- **Runtime: none,** in either package. CI fails if the npm package gains one, and the Python package declares an empty list. Database clients and frameworks are the application's own.
- **Development:** declared in `ts/package.json` and `python/pyproject.toml`, locked in `pnpm-lock.yaml` and `python/uv.lock`, and installed frozen in CI.
- **Updates:** Dependabot proposes them weekly, a week after each version is published. Security updates do not wait.
- **GitHub Actions** are pinned to a commit, with the version in a comment. CI fails on one that is not.

### Vulnerabilities and static analysis

| Finding | Source | Rule |
|---|---|---|
| Vulnerable dependency added by a pull request, moderate or higher | Dependency review | Blocks the merge |
| Vulnerable dependency already in the tree, high or critical | Dependabot alerts | Fixed or overridden before the next release |
| Same, moderate or lower | Dependabot alerts | Fixed within 30 days, or dismissed with the reason when it is not reachable |
| CodeQL alert, security severity high or critical | CodeQL, `security-extended` | Fixed before the next release |
| CodeQL alert, lower | CodeQL | Fixed or dismissed with the reason within 30 days |

Every dependency is a development dependency, so a vulnerability in one affects contributors and CI, not users of the packages. A dismissal says so when that is the reason.

Vulnerabilities in omnilimit itself follow the [security policy](https://github.com/PabloAlaniz/omnilimit/blob/main/SECURITY.md), and the [threat model](threat-model.md) says what counts as one.

### Secrets

- Publishing uses OIDC trusted publishing. There is no npm or PyPI token to leak.
- The only stored secret is the optional `RELEASE_PLEASE_TOKEN`: fine-grained, limited to this repository, with an expiry date, replaced when it expires or when exposure is suspected.
- Workflows start with read-only permissions and each job asks for what it needs. Pull requests from forks get no secrets.
- The passwords in `compose.test.yml` and `ci.yml` belong to throwaway test databases.
- GitHub secret scanning and push protection are expected to be on.

### Releases and support

- Each package has its own version, tag and changelog: `ts-v0.5.0`, `py-v0.5.0`.
- Only the latest release of each package is supported. Before 1.0 there are no maintenance branches, and a minor version may break compatibility; the changelog says when.
- Each GitHub release carries a CycloneDX bill of materials for its package.

### Verifying a release

Both packages are published from this repository's release workflow, with a signed statement of where they were built.

```bash
# npm: checks registry signatures and provenance for what is installed
npm audit signatures

# PyPI: checks that the file was published from this repository
pip install pypi-attestations
pypi-attestations verify pypi \
  --repository https://github.com/PabloAlaniz/omnilimit \
  pypi:omnilimit-0.5.0-py3-none-any.whl
```

The npm package page shows the same provenance: the source commit and the workflow run that built it. A package of this name that does not verify did not come from here.

## Baseline controls

`Met` has its evidence in the repository. `Setting` depends on a repository setting that the files cannot show; the weekly Scorecard run reports on several of them. `Gap` is not met, with the reason.

### Level 1

| Control | Status | Evidence |
|---|---|---|
| AC-01.01 Multi-factor authentication | Setting | The maintainer's account |
| AC-02.01 Least privilege for new collaborators | Met | No collaborators; [governance](https://github.com/PabloAlaniz/omnilimit/blob/main/GOVERNANCE.md) |
| AC-03.01 No direct commits to `main` | Setting | Branch protection requiring a pull request and `ci-ok` |
| AC-03.02 `main` cannot be deleted | Setting | Branch protection |
| BR-01.01 Untrusted metadata is sanitised in CI | Met | Pull request titles reach scripts through environment variables, never by interpolation |
| BR-01.03 Untrusted code gets no credentials | Met | No `pull_request_target`; publishing jobs run only on `main` |
| BR-03.01, BR-03.02 Encrypted, authenticated channels | Met | GitHub, npm and PyPI over HTTPS; signed provenance |
| BR-07.01 No secrets in the repository | Met | [Secrets](#secrets) |
| DO-01.01 User guides | Met | README and these docs |
| DO-02.01 How to report defects | Met | Issue forms |
| GV-02.01 Public discussion | Met | Issues and pull requests |
| GV-03.01 Contribution process | Met | CONTRIBUTING |
| LE-02.01 to LE-03.02 Licence | Met | MIT, in `LICENSE` and inside both packages |
| QA-01.01, QA-01.02 Public repository and history | Met | |
| QA-02.01 Dependency list | Met | [Dependencies](#dependencies) |
| QA-04.01 List of codebases | Met | One repository |
| QA-05.01, QA-05.02 No generated or binary artifacts | Met | Generated sources are text, and CI checks they match their origin |
| VM-02.01 Security contacts | Met | SECURITY |

### Level 2

| Control | Status | Evidence |
|---|---|---|
| AC-04.01 CI defaults to the lowest permissions | Met | `permissions` at the top of every workflow |
| BR-02.01 Unique version per release | Met | release-please |
| BR-04.01 Changelog per release | Met | `ts/CHANGELOG.md`, `python/CHANGELOG.md` |
| BR-05.01 Standard dependency tooling | Met | pnpm and uv, frozen lockfiles |
| BR-06.01 Signed releases | Met | npm provenance and PyPI attestations |
| DO-06.01 How dependencies are chosen and tracked | Met | [Dependencies](#dependencies) |
| DO-07.01 Build instructions | Met | CONTRIBUTING |
| GV-01.01, GV-01.02 Members and roles | Met | Governance |
| GV-03.02 Requirements for contributions | Met | CONTRIBUTING, pull request template |
| LE-01.01 Contributors assert their right to contribute, on every commit | Gap | No sign-off is required. Contributions are accepted under the MIT licence of the repository |
| QA-03.01 Status checks pass before merge | Setting | `ci-ok` required on `main` |
| QA-06.01 Tests run before merge | Met | `.github/workflows/ci.yml` |
| SA-01.01 Design documentation | Met | The spec, and the system section of the [threat model](threat-model.md) |
| SA-02.01 External interfaces | Met | [Adapters](adapters.md), [stores](stores.md), the spec |
| SA-03.01 Security assessment | Met | [Threat model](threat-model.md) |
| VM-01.01 Coordinated disclosure with a timeframe | Met | SECURITY |
| VM-03.01 Private reporting | Setting | GitHub private vulnerability reporting |
| VM-04.01 Published vulnerability data | Met | GitHub security advisories. None so far |

### Level 3

| Control | Status | Evidence |
|---|---|---|
| AC-04.02 Minimum permissions per job | Met | Each job in `.github/workflows/` |
| BR-01.04 Collaborator input is sanitised in CI | Met | Workflow inputs reach scripts through environment variables |
| BR-02.02 Assets tied to the release | Met | Tags per package; provenance names the commit |
| BR-07.02 Secrets policy | Met | [Secrets](#secrets) |
| DO-03.01, DO-03.02 How to verify a release and its author | Met | [Verifying a release](#verifying-a-release) |
| DO-04.01, DO-05.01 Support scope and end | Met | [Releases and support](#releases-and-support) |
| GV-04.01 Review before granting access | Met | Governance |
| QA-02.02 Bill of materials with each release | Met | Attached to each GitHub release from the first one after this page |
| QA-04.02 Subprojects as strict as the main one | Met | One repository |
| QA-06.02, QA-06.03 When tests run, and tests with major changes | Met | [Tests](#tests) |
| QA-07.01 Approval by someone other than the author | Gap | One maintainer. Automated checks stand in for the second person |
| SA-03.02 Threat model | Met | [Threat model](threat-model.md) |
| VM-04.02 VEX for vulnerabilities that do not apply | Gap | Not published. With no runtime dependencies there has been nothing to state; dismissed alerts carry the reason |
| VM-05.01 to VM-05.03 Dependency findings: thresholds, before release, blocking | Met | [Vulnerabilities and static analysis](#vulnerabilities-and-static-analysis) |
| VM-06.01 Static analysis threshold | Met | Same section |
| VM-06.02 Static analysis blocks violations | Setting | A code scanning rule on `main`, for high and critical alerts |

## Known gaps

- **One maintainer.** No second reviewer, and one account to trust. See [governance](https://github.com/PabloAlaniz/omnilimit/blob/main/GOVERNANCE.md).
- **No contributor sign-off.**
- **Throughput is not measured.** See the [conformance matrix](matrix.md#not-verified).
- **No external audit.**
