# Operating ring 1 on GitHub Actions

Runbook for whoever administers `cloudfroghq/knowledge`: what runs on Actions, what the `protect-main` ruleset requires, what it costs in minutes, how to rotate a read-only deploy key, how to roll back without leaving PRs blocked, and how a release is published to PyPI and npm. The weekly stale-documents report does not run here: each vault's workflow runs it, fetching `scripts/weekly-stale-report.py` and `scripts/notify-stale-report.sh` from this repository. What each job does and why is in [`CONTRIBUTING.md`, "What CI runs"](../../CONTRIBUTING.md#what-ci-runs).

## 1. State

| What | Value | How to check |
|---|---|---|
| Actions | on; GitHub-owned actions only (`allowed_actions: selected`, `github_owned_allowed: true`, no `verified_allowed` and no patterns); `sha_pinning_required: true` | `gh api repos/cloudfroghq/knowledge/actions/permissions` y `.../actions/permissions/selected-actions` |
| Workflow token | `default_workflow_permissions: read`; each workflow declares `permissions: contents: read` | `gh api repos/cloudfroghq/knowledge/actions/permissions/workflow` |
| `ring1.yml` | `pull_request` to `main`, `push` to `main` and `workflow_dispatch`; no secrets | `gh workflow view ring1.yml -R cloudfroghq/knowledge` |
| `release-check.yml` | push of `v*` tags and `workflow_dispatch`; no secrets and no environment | `gh workflow view release-check.yml -R cloudfroghq/knowledge` |
| `publish.yml` | push of `v*` tags and `workflow_dispatch` (`dry_run`, true by default); a read-only `build` job and a `publish` job with the `release` environment and `id-token: write` (trusted publishing on PyPI and npm, no secrets) | `gh workflow view publish.yml -R cloudfroghq/knowledge` |
| Repository secrets | none | `gh api repos/cloudfroghq/knowledge/actions/secrets --jq .total_count` → `0` |
| Environment `release` | `v*` tag policy, no secrets; only the `publish` job of `publish.yml` declares it, and it is the environment that the PyPI and npm trusted publishers name | `gh api repos/cloudfroghq/knowledge/environments/release/deployment-branch-policies` |
| Trusted publishers | PyPI `cloudfrog-knowledge` and npm `@cloudfrog/knowledge`: owner `cloudfroghq`, repository `knowledge`, workflow `publish.yml`, environment `release` | section 7 |
| Read-only deploy keys | one per vault that fetches tools from this repository (`vault-lint`, weekly report), read-only, registered here | `gh repo deploy-key list -R cloudfroghq/knowledge` |

## 2. Required checks in `protect-main`

The `protect-main` ruleset (default branch; its id comes from `gh api repos/cloudfroghq/knowledge/rulesets`) requires, besides the PR, these five contexts from the GitHub Actions app (`integration_id` `15368`), with `strict_required_status_checks_policy: false`:

- `Lint + pytest with coverage`
- `Layered architecture: import-linter (ADR-022)`
- `Secrets: gitleaks`
- `SAST: Semgrep CE`
- `Diff coverage (threshold from pyproject.toml)`

These are the jobs that run on a `pull_request`. `Image: build and smoke test (amd64)` is not required: it does not run on PRs, to save minutes, and it is validated on every `push` to `main` and on any `gh workflow run ring1.yml --ref <branch>`. Before merging a change to `Dockerfile`, `infra/` or `tests/test_image.py`, dispatch ring1 on the branch and wait for the image job to go green.

The job name is the check context: renaming a job in `ring1.yml` without changing the ruleset leaves every PR waiting for a check that never arrives. `tests/test_pipeline_contract.py` pins the jobs of a PR to be exactly those five.

```bash
ID=$(gh api repos/cloudfroghq/knowledge/rulesets --jq '.[]|select(.name=="protect-main").id')
gh api "repos/cloudfroghq/knowledge/rulesets/$ID" \
  --jq '.rules[]|select(.type=="required_status_checks")|.parameters.required_status_checks[].context'
```

A PR opened before the checks existed has none and is left waiting for them. It is unblocked by any push to its branch (the `synchronize` event triggers ring1) or by `gh pr update-branch <n> -R cloudfroghq/knowledge` when it has no conflicts.

### Bypass

The administrator role (`RepositoryRole` 5) has bypass in `pull_request` mode: it can merge a PR with pending or red checks (`gh pr merge <n> --admin`), and direct push to `main` stays blocked. That bypass is used only during a GitHub or runner outage, and the reason is noted on the PR. A check that is red because of the code is fixed in the PR.

## 3. Actions minutes

In a private repository each job consumes Linux minutes from the owning account's plan, rounded up to the minute per job; in a public one the standard GitHub runners consume no minutes. Measured on real runs:

| Run | Jobs | Billable minutes |
|---|---|---|
| ring1 on `pull_request` | 5 (the image job is skipped) | ~7 |
| ring1 on `push` to `main` or by `workflow_dispatch` | 5 (the patch coverage job is skipped) | ~7 |
| release-check (`v*` tag or dispatch) | 1 | 1 (the job takes 36 s; run 38362509479) |
| `vault-lint` in a vault (PR, push to `main` or dispatch) | 1 | ~1 (the job takes about 20 s) |

Total measured when everything was put into operation: about 68 billable minutes over 20 runs on 2026-10-13 (ring1 per PR ≈7 min without the image, per push to `main` ≈7 min with the image, `vault-lint` ≈1 min per run); measured when the repository was private.

```bash
gh api organizations/cloudfroghq/settings/billing/usage/summary
gh run view <id> -R cloudfroghq/knowledge --json jobs --jq '.jobs[]|[.name,.startedAt,.completedAt]|@tsv'
```

`actions/runs/<id>/timing` answers `total_ms` as `0` for the account and the per-repository usage report arrives late: the per-job calculation is the immediate measure. If the month's minutes run out and there is no spending budget, jobs do not start and the five required checks block every PR: in that case the rule is removed in knowledge and in the vaults (section 5, step 1) and put back when the month changes.

## 4. Rotating a read-only deploy key

The vaults' workflows (`vault-lint`, weekly report) fetch the tools of this repository with a read-only deploy key registered here; its private half lives in the vault's `KNOWLEDGE_DEPLOY_KEY` secret and in a file with `600` permissions on the owner's machine. It is never printed or copied into a document.

```bash
F=<new file, outside any repository>
V=<owner>/<vault>
ssh-keygen -q -t ed25519 -N '' -C "ci knowledge $V (read-only)" -f "$F"
chmod 600 "$F"
gh repo deploy-key add "$F.pub" -R cloudfroghq/knowledge --title "<key title>"   # read-only by default
gh secret set KNOWLEDGE_DEPLOY_KEY -R "$V" < "$F"
gh workflow run <workflow>.yml -R "$V" --ref main                                              # must come out green
gh repo deploy-key list -R cloudfroghq/knowledge --json id,title,createdAt
gh repo deploy-key delete <id of the old key> -R cloudfroghq/knowledge
```

The weekly report's notification to a chat (`STALE_REPORT_WEBHOOK_URL`, `vars.STALE_REPORT_NOTIFY`) is configured in the vault that runs the report, not here.

## 5. Rollback

The order matters: if Actions is turned off while required checks are active, no PR ever goes green again and every merge needs the bypass. This applies to knowledge and to every vault that requires the `vault-lint` check in its own `protect-main`. The `protect-main` id of this repository and that of each vault come from `gh api repos/<owner>/<repo>/rulesets`.

Before running anything, confirm that each id belongs to its repository (the output carries the repository, the id and the name):

```bash
for r in cloudfroghq/knowledge <owner>/<vault> ...; do
  gh api "repos/$r/rulesets" --jq '.[]|"\(.source) \(.id) \(.name)"'
done
```

```bash
K=cloudfroghq/knowledge
# 1) Remove required_status_checks from protect-main in knowledge and in every vault that requires it.
drop_checks() {   # <repo> <id of the protect-main ruleset>
  gh api "repos/$1/rulesets/$2" \
    | jq '{name,target,enforcement,conditions,bypass_actors,rules:[.rules[]|select(.type!="required_status_checks")]}' \
    | gh api -X PUT "repos/$1/rulesets/$2" --input -
}
drop_checks "$K" <id of knowledge's protect-main ruleset>
drop_checks <owner>/<vault> <id of that vault's protect-main ruleset>   # one line per vault
# 2) Turn off Actions in knowledge and the vault-lint workflow in every vault.
gh api -X PUT repos/$K/actions/permissions -F enabled=false
gh workflow disable vault-lint.yml -R <owner>/<vault>                    # one per vault
# 3) Revoke the credentials: the read-only deploy keys registered in knowledge and each vault's secret.
gh repo deploy-key list -R $K --json id,title
gh repo deploy-key delete <id of each vault key> -R $K
gh secret delete KNOWLEDGE_DEPLOY_KEY -R <owner>/<vault>                  # one per vault
```

The deploy keys of the vaults' workflows live in knowledge (read-only, one per vault); the ones that serve a deployment are documented in that deployment's vault, under `Deploy/`, and the rollback does not touch them.

The function receives each repository as an argument so that it works the same in bash and zsh (zsh does not split an unquoted variable on spaces). YAML changes are reverted by PR (`git revert` of the corresponding merge). The platform's automatic deployment does not depend on Actions: the rollback does not touch it.

## 6. Verifying a release

`release-check.yml` is the verification between a tag and the marketplace pull request. It runs on every `git push origin v<x.y.z>` and by hand. It reads no secrets or environments, and its token is read-only. It checks, in this order: that `pyproject.toml`, `plugin.json`, the `cloudfrog-knowledge==<version>` of `.mcp.json`, `npm/package.json` and `uv.lock` agree (`scripts/check-version.sh`, which also compares the tag when the ref is a tag; from a branch there is no tag and it compares only the files); that each `tests/golden/v*/SHA256SUMS` holds and that `test_contract_frozen.py`, `test_contract_golden.py` and `test_plugin_manifest.py` pass with `sqlite-vec` loaded; that `claude plugin validate --strict plugin/claude-code` passes (Node 22 and Claude Code `2.1.296`, pinned exactly); and that the sdist and the wheel build and carry only the package (`tests/test_sdist_contents.py`). Running `uv build --out-dir <temporary dir outside the repo>` locally is fine as a packaging check, but `dist/` is never committed and a locally built artifact is never uploaded: every published sdist and wheel is built and published only by `.github/workflows/publish.yml` on the runner, and this workflow checks them.

A dispatch uses the workflow of the ref you choose: `gh workflow run release-check.yml --ref main` works, a tag older than `release-check.yml` does not (it lacks the file).

Order of a release:

1. The release pull request (`scripts/bump-version.sh X.Y.Z`) passes ring1 and is merged.
2. Before pushing the tag, rehearse on `main` with the same file the tag will use: `gh workflow run release-check.yml -R cloudfroghq/knowledge --ref main` and `gh workflow run publish.yml -R cloudfroghq/knowledge --ref main` (builds and verifies without publishing), and `gh run watch` each one. Without a tag, `check-version.sh` compares only the files.
3. `git push origin v<x.y.z>` on the merge (signed tag, section 7). The tag run additionally compares the tag with the versions, and `publish.yml` publishes to PyPI and npm.
4. Only with `release-check` green, and with `publish.yml` done publishing that version to PyPI, is the `cloudfroghq/claude-plugins` pull request opened that moves the marketplace to the new tag. The plugin's `.mcp.json` and its `SessionStart` hook launch `uvx --managed-python --python 3.12 --from cloudfrog-knowledge==<x.y.z>`: a plugin version whose package is not on PyPI yet fails to start for every user who updates. `release-check` does not check PyPI (it would race `publish.yml` on the same tag); check it by hand before opening the pull request:

   ```bash
   gh run watch "$(gh run list -R cloudfroghq/knowledge -w publish -e push -L1 --json databaseId --jq '.[0].databaseId')" -R cloudfroghq/knowledge --exit-status
   uvx --managed-python --python 3.12 --from cloudfrog-knowledge==<x.y.z> cloudfrog-knowledge --version   # cloudfrog-knowledge <x.y.z>
   ```

```bash
RUN=$(gh run list -R cloudfroghq/knowledge -w release-check -e push -L1 --json databaseId --jq '.[0].databaseId')
gh run watch "$RUN" -R cloudfroghq/knowledge --exit-status
```

A dispatch with `--ref v0.6.0` does not work: it uses the tag's tree, which does not contain `release-check.yml`. If `check-version.sh` fails, the tag does not match one of the five versions (`pyproject.toml`, `plugin.json`, `.mcp.json`, `npm/package.json` and `uv.lock`): `publish.yml` fails at the same step and publishes nothing. Fix it in a pull request and cut a new tag: a tag that has already published something is not moved, because PyPI and npm do not accept republishing a version. If `claude plugin validate` fails, reproduce it locally with `claude plugin validate --strict plugin/claude-code`.

## 7. Publishing to PyPI and npm

Each `v<x.y.z>` tag goes out through three channels with the same version: the tag itself (`uvx --from git+https://github.com/cloudfroghq/knowledge.git@v<x.y.z> …`), PyPI (`cloudfrog-knowledge`) and npm (`@cloudfrog/knowledge`, which runs the PyPI version with a pinned `uv`). `publish.yml` does the last two.

### What `publish.yml` does

| Job | Permissions | What it does |
|---|---|---|
| `Build and verify the release` | `contents: read` | Installs `uv` from its release, verified against `npm/lib/uv-release.json` (`scripts/ci-install-uv.sh`); compares the tag with the five versions (`check-version.sh`); checks the npm package's `uv` checksums against the release's `.sha256` files and installs it with the package's own code (`npm/scripts/uv-release.js --check` and `--smoke`); runs `npm test` and `npm pack --dry-run`; builds sdist and wheel; installs the wheel in a clean Python 3.12 environment and requires `cloudfrog-knowledge <version>` and `knowledge <version>` from `--version`; uploads `dist/`. |
| `Publish to PyPI and npm` | `contents: read`, `id-token: write`; environment `release` | Only with a tag ref: on the tag push, or by hand from the tag with `dry_run` unchecked. PyPI first (`uv publish --trusted-publishing always` with the wheel and sdist from the previous job), then npm (`npm publish --provenance --access public` from `npm/`, with npm 11.20.0). Before each one it queries the registry: `200` (that version already exists) skips with a warning, `404` publishes and any other response fails. |

`uv` ignores the upper bound of `requires-python`, so the job installs the wheel with an explicit `--python 3.12`, like the npm package and the plugin.

### One time only

1. **PyPI**: the pending publisher already exists (project `cloudfrog-knowledge`, owner `cloudfroghq`, repository `knowledge`, workflow `publish.yml`, environment `release`). The first run of `publish.yml` creates the project and the publisher becomes a normal one; check it at `https://pypi.org/manage/project/cloudfrog-knowledge/settings/publishing/`.
2. **npm, first version by hand**: npm only lets you configure a trusted publisher on a package that already exists, so the first version is published by the owner of the `@cloudfrog` scope from their machine, with `main` at the release merge and before pushing the tag:

   ```bash
   cd ~/Projects/Tools/cloudfroghq-knowledge
   git switch main && git pull --ff-only
   scripts/check-version.sh                      # the five versions at <x.y.z>
   cd npm
   npm login                                     # the account that owns the @cloudfrog scope
   npm whoami
   npm test
   npm pack --dry-run                            # LICENSE, README.md, package.json, bin/ and lib/
   npm publish --access public                   # no --provenance: only CI signs provenance
   npm view @cloudfrog/knowledge@<x.y.z> version
   ```

   With 2FA on the account, `npm publish` asks for the code (or `--otp=<code>`). That version has no provenance; from the next one on, `publish.yml` publishes it with provenance. Until the tag publishes to PyPI, `npx @cloudfrog/knowledge@<x.y.z>` does not find the Python package: push the tag right away.
3. **npm, trusted publisher**: at `https://www.npmjs.com/package/@cloudfrog/knowledge/access` (Settings), "Trusted Publisher" section, choose GitHub Actions and fill in: organization or user `cloudfroghq`, repository `knowledge`, workflow file `publish.yml`, environment `release`. Save. Once a CI-published version already has provenance (`npm view @cloudfrog/knowledge@<version> dist.attestations`), under "Publishing access" choose "Require two-factor authentication and disallow tokens": from then on only CI publishes.

### Every release

1. Merge the release pull request (`scripts/bump-version.sh X.Y.Z`, entry in `docs/contract/CHANGELOG.md`).
2. Rehearsal on `main`: `gh workflow run publish.yml -R cloudfroghq/knowledge --ref main` (and `release-check.yml`, section 6) and `gh run watch`.
3. First time only: the manual `npm publish` and the trusted publisher above.
4. Signed tag on the merge, then push (the `protect-release-tags` ruleset lets only administrators create `v*` tags):

   ```bash
   git switch main && git pull --ff-only
   git tag -s v<x.y.z> -m "v<x.y.z>"
   git tag -v v<x.y.z>                           # valid signature with the repository's signing key
   git push origin v<x.y.z>
   gh run watch "$(gh run list -R cloudfroghq/knowledge -w publish -e push -L1 --json databaseId --jq '.[0].databaseId')" -R cloudfroghq/knowledge --exit-status
   ```

5. Check the three channels (on a machine or in a directory with no warm cache if you want to test the download):

   ```bash
   uvx --managed-python --python 3.12 cloudfrog-knowledge@<x.y.z> --version                     # cloudfrog-knowledge <x.y.z>
   uvx --managed-python --python 3.12 --from cloudfrog-knowledge==<x.y.z> knowledge --version    # knowledge <x.y.z>
   npx -y @cloudfrog/knowledge@<x.y.z> --version                                                # knowledge <x.y.z>
   pnpm dlx @cloudfrog/knowledge@<x.y.z> --version
   uvx --managed-python --python 3.12 --from git+https://github.com/cloudfroghq/knowledge.git@v<x.y.z> cloudfrog-knowledge --version
   npm view @cloudfrog/knowledge@<x.y.z> dist.attestations                                       # provenance (the first version does not have it)
   ```

6. Only now open the marketplace pull request (`cloudfroghq/claude-plugins`): the plugin launches the PyPI release (`--from cloudfrog-knowledge==<x.y.z>`), so the PyPI check of step 5 must have printed the version first (section 6, step 4).

### If something fails

- **Before publishing** (versions, `uv` checksums, tests, wheel): nothing went out. Fix it in a pull request and cut a new tag.
- **PyPI rejects the token** (`invalid-publisher`): the publisher does not match the run's owner, repository, workflow or environment. Fix it on PyPI and rerun the job: `gh run rerun <id> -R cloudfroghq/knowledge --failed`.
- **npm fails with PyPI already published** (for example, the trusted publisher was not configured): fix it and rerun the failed job; PyPI answers `200` and is skipped, and npm publishes.
- **A half-published version on PyPI** (the wheel uploaded and the sdist did not): a new run sees it as published and skips it, so the sdist no longer uploads. `uv` and `pip` install from the wheel; if the sdist is needed, a patch release is published.
- **A version published with a defect**: neither PyPI nor npm lets you republish it. Cut a patch release (`X.Y.Z+1`); on npm, `npm deprecate @cloudfrog/knowledge@<x.y.z> "<reason>"`, and on PyPI mark it as *yanked* from the project page.

## 8. Follow-up

### The tag path in `release-check`

The only `release-check` runs so far are by `workflow_dispatch` (run 38362509479, green): the tag path, which additionally compares the tag with the five versions, is untested. After the next push of a `v*` tag, record the run by `push` event:

```bash
gh run list -R cloudfroghq/knowledge -w release-check -e push -L1
```

As long as there is no run by tag, that command returns an empty list.

### Dynamic Dependency Graph under `sha_pinning_required`

GitHub recalculates the dependency graph of `uv.lock` with a dynamic workflow that the repository does not version (`path: dynamic/dependabot/update-graph`, event `dynamic`, job `update-uv-graph`, action `github/dependabot-action@main`). It is not a check of any ruleset.

```bash
gh api 'repos/cloudfroghq/knowledge/actions/runs?per_page=30' \
  --jq '.workflow_runs[]|select(.path|startswith("dynamic/"))|[.id,.created_at,.head_sha[0:7],.conclusion]|@tsv'
```

What it shows on 2026-10-13:

- There are 12 runs: 11 green (the last at 02:08Z) and 1 red at 06:05Z (run 38354820108, commit `02e30f2`). The 11 of October 12 and 13 (UTC) each coincide with a push to `main` whose batch includes a commit that touches `pyproject.toml` or `uv.lock`; the five pushes after the red run (11 commits) touched neither and generated no run.
- The red run did not fail because of the pin. The job started, downloaded `github/dependabot-action@main` (it resolves to the same SHA as in the green run) and ran the updater. It failed at the end: `POST /update_jobs/1624282216/create_dependency_submission` answered `500` after 10 s and the updater logged `snapshots_unavailable_graph_error: Unable to submit data to the Dependency Snapshot API`. In the green run that call answered `204`. The job's `GITHUB_TOKEN` has the same permissions in both (`Contents`, `Metadata` and `Packages` read-only), so `default_workflow_permissions: read` does not explain the difference either.
- That does not prove that the dynamic workflow coexists with the pin. The permissions API returns only the current state, without the date on which `sha_pinning_required` was activated, and the red run precedes the first ring1 run (06:45Z) by 40 minutes: it most likely ran without the pin. There is no later dynamic run.

The test is the next dynamic run, which will already run with the pin active: it arrives with the next push to `main` that changes `pyproject.toml` or `uv.lock` (the next release's bump does). Its log is read with `gh api --allow-escape-sequences repos/cloudfroghq/knowledge/actions/jobs/<job id>/logs`, because the update's web page asks for write access. How to decide depending on the result:

- Green: the graph coexists with the pin and there is nothing to change.
- Red because the action is rejected at `Set up job` (a message about allowed actions or about pinning by SHA): the dynamic workflow uses `github/dependabot-action@main`, a reference the repository does not control, and it clashes with `sha_pinning_required`. No official documentation was found saying whether Dependabot workflows fall outside that policy and outside `allowed_actions`, so the decision is made on this evidence and not on an assumed rule. The graph is informational and does not protect `main`; the pin protects ring1 and release-check, which verify every merge and every release. The default criterion is to keep the pin and accept a graph that is not updated; turning it off (the same `actions/permissions` API as in section 1) is only justified if the graph is needed, and `tests/test_pipeline_contract.py` would still require SHAs in the versioned workflows.
- Red with `snapshots_unavailable_graph_error` again: it is an error of the GitHub service, not of the pin. Retry with the next push and, if it persists, report it to GitHub Support.
