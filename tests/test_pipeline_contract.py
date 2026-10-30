"""Guards on ``.github/workflows/`` — the ring-1 gate, the release check and the publish workflow.

The workflow is the authoritative merge gate and the one file in the repository
that nothing else exercises: its first real run happens on GitHub, after the
change already looks fine to a reviewer. Everything checked here is a failure
mode that would otherwise be discovered in a run.

* The three workflows parse as YAML. Not "a regex found the words": a real parser,
  because a malformed workflow file is reported by GitHub as "no workflow", not
  as a failure.
* Every repository path they name exists in the tree. A step that runs a script
  nobody committed turns a manual run into a permanent red.
* Each workflow declares exactly its triggers: ring1 on pull requests to main,
  pushes to main and on demand; the release check and the publish workflow on
  pushed ``v*`` tags and on demand. Adding or dropping a trigger is an edit this test then has to be told
  about.
* The gate jobs are all present, and the ones a pull request runs are exactly
  the checks the protect-main ruleset requires. The image job never runs on a
  pull request. A gate that is deleted from the file is a gate that never runs.
* No review-decay report is wired into the gate: it reads a whole vault and
  depends on frontmatter humans edit, so it runs from the vault's own repository.
* The release check is the gate between a version tag and the marketplace pull
  request: it verifies versions, frozen goldens, the plugin and the sdist, and
  reads no secret and no environment.
* Least privilege: every workflow holds ``contents: read`` and, but for the
  publish job of publish.yml, nothing else; no workflow reads a secret, and no
  checkout leaves a token behind where the job does not need one.
* Scanner and installer versions are pinned, and every action is pinned to a
  full commit SHA with its release tag in a trailing comment.
* The publish workflow splits building from publishing: only its publish job
  holds ``id-token: write`` and the ``release`` environment, it runs only for a
  tag ref and never on a dry run, it verifies versions, the wheel and both
  console scripts before anything leaves, and each registry is skipped only
  when that exact version already exists there.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
PIPELINE = REPO_ROOT / ".github" / "workflows" / "ring1.yml"
RELEASE_CHECK = REPO_ROOT / ".github" / "workflows" / "release-check.yml"
PUBLISH = REPO_ROOT / ".github" / "workflows" / "publish.yml"

# Directories a step can name that are produced by the build itself, so their
# absence in a checkout says nothing.
_GENERATED_PREFIXES = ("build/",)

# A repository-relative path as it appears inside a script line.
_PATH_TOKEN = re.compile(r"(?<![\w./-])((?:src|tests|scripts|npm)/[A-Za-z0-9_./-]+)")

# Jobs the ring-1 workflow must define; a gate nobody declares is decoration.
_REQUIRED_GATE_JOBS = {
    "Lint + pytest with coverage",
    "Layered architecture: import-linter (ADR-022)",
    "Secrets: gitleaks",
    "SAST: Semgrep CE",
    "Diff coverage (threshold from pyproject.toml)",
    "Image: build and smoke test (amd64)",
}

_REPORT_JOBS = {"Review decay: stale docs by owner", "Notify owners"}

# Triggers each workflow must declare, no more and no fewer.
_EXPECTED_TRIGGERS = {
    "ring1.yml": {"pull_request", "push", "workflow_dispatch"},
    "release-check.yml": {"push", "workflow_dispatch"},
    "publish.yml": {"push", "workflow_dispatch"},
}

# The checks a pull request runs. These are the contexts the protect-main
# ruleset requires, by job name: renaming one here without the ruleset leaves
# every pull request waiting for a check that never reports.
_PULL_REQUEST_CHECKS = _REQUIRED_GATE_JOBS - {"Image: build and smoke test (amd64)"}

_ONLY_ON_PULL_REQUESTS = "github.event_name == 'pull_request'"
_NEVER_ON_PULL_REQUESTS = "github.event_name != 'pull_request'"

# A `uses:` line pinned to a full commit SHA with its release tag in a trailing
# comment. Any `uses:` line that does not match fails: a tag, a branch or a SHA
# without the tag that names it.
_PINNED_USES = re.compile(r"^\s*-?\s*uses:\s*[\w.-]+/[\w./-]+@[0-9a-f]{40}\s+#\s*v\d+\.\d+\.\d+\s*$")


def _load(path: Path) -> dict:
    assert path.is_file(), f"the workflow is missing: {path}"
    loaded = yaml.safe_load(path.read_text())
    assert isinstance(loaded, dict), f"{path.name} must parse to a mapping"
    return loaded


@pytest.fixture(scope="module")
def pipeline() -> dict:
    return _load(PIPELINE)


@pytest.fixture(scope="module")
def release_check() -> dict:
    return _load(RELEASE_CHECK)


@pytest.fixture(scope="module")
def publish() -> dict:
    return _load(PUBLISH)


def _triggers(workflow: dict) -> dict:
    # PyYAML (YAML 1.1) parses the bare key ``on`` as the boolean True.
    return workflow.get("on", workflow.get(True))


def _run_lines(workflow: dict) -> list[str]:
    """Every shell string a job executes."""
    out: list[str] = []
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            if isinstance(step.get("run"), str):
                out.extend(step["run"].splitlines())
    return out


def _job_names(workflow: dict) -> set[str]:
    return {str(job["name"]) for job in workflow["jobs"].values() if "name" in job}


def test_the_workflows_are_valid_yaml(pipeline, release_check, publish) -> None:
    """A file GitHub cannot parse produces no run, not a failed one."""
    for workflow in (pipeline, release_check, publish):
        assert "jobs" in workflow, "no `jobs:` section — nothing would ever run"


def test_every_path_the_workflows_name_exists(pipeline, release_check, publish) -> None:
    """A step that runs a file nobody committed is a red run waiting for a trigger."""
    missing: list[str] = []
    for workflow in (pipeline, release_check, publish):
        for line in _run_lines(workflow):
            for token in _PATH_TOKEN.findall(line):
                if token.startswith(_GENERATED_PREFIXES):
                    continue
                if not (REPO_ROOT / token).exists():
                    missing.append(token)

    assert sorted(set(missing)) == [], (
        f"the workflows run paths that are not in the tree: {sorted(set(missing))}"
    )


@pytest.mark.parametrize("path", [PIPELINE, RELEASE_CHECK, PUBLISH], ids=lambda p: p.name)
def test_each_workflow_declares_exactly_its_triggers(path: Path) -> None:
    """A trigger nobody declared here runs the workflow on events nobody planned for."""
    triggers = _triggers(_load(path))
    expected = _EXPECTED_TRIGGERS[path.name]
    assert isinstance(triggers, dict) and set(triggers) == expected, (
        f"{path.name} declares triggers "
        f"{sorted(map(str, triggers)) if isinstance(triggers, dict) else triggers!r}, "
        f"expected {sorted(expected)}"
    )


def test_the_gate_listens_only_to_main(pipeline) -> None:
    """A pull request or push to any other branch is not what the gate protects."""
    triggers = _triggers(pipeline)
    for event in ("pull_request", "push"):
        assert triggers[event] == {"branches": ["main"]}, (
            f"ring1.yml `{event}` must be limited to `branches: [main]`, got {triggers[event]!r}"
        )


def test_the_gate_jobs_are_all_defined(pipeline) -> None:
    """Ring 1 is only a gate if every job of the gate is declared."""
    missing = _REQUIRED_GATE_JOBS - _job_names(pipeline)
    assert not missing, f"gate jobs missing from ring1.yml: {sorted(missing)}"


def test_the_gate_runs_the_commands_it_names(pipeline) -> None:
    """A job that keeps its name but drops its command gates nothing."""
    lines = "\n".join(_run_lines(pipeline))
    for command in (
        "uv sync --frozen --all-groups",
        "uv run ruff check",
        "uv run pytest",
        "uv run lint-imports",
        "gitleaks detect",
        "semgrep --config",
        "uv run diff-cover build/coverage.xml -c pyproject.toml",
        "uv run pytest -m image",
        "npm test",
    ):
        assert command in lines, f"ring1.yml does not run: {command}"


def test_the_report_is_never_part_of_the_gate(pipeline) -> None:
    """The review-decay report reads the whole vault and depends on frontmatter
    humans edit, so wiring it into the gate would fail runs for reasons the
    author cannot fix. It runs from the vault's own repository.
    """
    assert not (_REPORT_JOBS & _job_names(pipeline)), (
        "a report job is wired into ring1.yml; it is a report, not a gate"
    )


def test_every_scanner_and_action_is_pinned_to_an_exact_version() -> None:
    """A floating scanner or a movable tag changes what blocks a merge with no commit here."""
    text = PIPELINE.read_text()

    assert "gitleaks/releases/download/v8.30.1/" in text, "gitleaks release is not pinned"
    assert "astral.sh/uv/0.10.4/install.sh" in text, "the uv installer is not pinned"
    assert "semgrep==1.175.0" in text, "semgrep is not pinned"

    for path in (PIPELINE, RELEASE_CHECK, PUBLISH):
        uses_lines = [line for line in path.read_text().splitlines() if re.match(r"^\s*-?\s*uses:", line)]
        assert uses_lines, f"{path.name}: no `uses:` line found — the check would pass vacuously"
        for line in uses_lines:
            assert _PINNED_USES.match(line), (
                f"{path.name}: action not pinned to a commit SHA with its tag: {line.strip()}"
            )


def _job(workflow: dict, name: str) -> dict:
    jobs = [job for job in workflow["jobs"].values() if job.get("name") == name]
    assert len(jobs) == 1, f"expected exactly one job named {name!r}, found {len(jobs)}"
    return jobs[0]


def test_a_pull_request_runs_exactly_the_required_checks(pipeline) -> None:
    """The ruleset requires these contexts; a job a pull request skips cannot be one."""
    runs_on_pr = {
        str(job["name"])
        for job in pipeline["jobs"].values()
        if job.get("if") in (None, _ONLY_ON_PULL_REQUESTS)
    }
    assert runs_on_pr == _PULL_REQUEST_CHECKS, (
        f"jobs a pull request runs: {sorted(runs_on_pr)}; required: {sorted(_PULL_REQUEST_CHECKS)}"
    )
    assert _job(pipeline, "Image: build and smoke test (amd64)").get("if") == _NEVER_ON_PULL_REQUESTS, (
        "the image job must run on a push to main and on demand, never on a pull request"
    )
    assert _job(pipeline, "Diff coverage (threshold from pyproject.toml)").get("if") == _ONLY_ON_PULL_REQUESTS


def test_the_image_job_fails_without_a_docker_daemon(pipeline) -> None:
    """Without a daemon the image tests skip, and a job of skipped tests is an empty green."""
    runs = [
        step["run"]
        for step in _job(pipeline, "Image: build and smoke test (amd64)")["steps"]
        if isinstance(step.get("run"), str)
    ]
    assert any(run.startswith("docker info") for run in runs), "the image job does not check the daemon"
    first_docker = next(i for i, run in enumerate(runs) if run.startswith("docker info"))
    first_pytest = next(i for i, run in enumerate(runs) if "pytest -m image" in run)
    assert first_docker < first_pytest, "the daemon check must run before the image tests"


@pytest.mark.parametrize("path", [PIPELINE, RELEASE_CHECK], ids=lambda p: p.name)
def test_the_workflows_hold_least_privilege(path: Path) -> None:
    """The token can read the repository and nothing else, and runs do not pile up."""
    workflow = _load(path)
    assert workflow.get("permissions") == {"contents": "read"}, (
        f"{path.name} permissions: {workflow.get('permissions')!r}"
    )
    for key, job in workflow["jobs"].items():
        assert "permissions" not in job, f"{path.name}: job {key} widens the token's permissions"
    assert isinstance(workflow.get("concurrency"), dict) and workflow["concurrency"].get("group"), (
        f"{path.name} has no concurrency group"
    )


def test_the_gate_reads_no_secret() -> None:
    """Ring 1 runs on every pull request; a secret it can read is a secret a PR can print."""
    assert "secrets." not in PIPELINE.read_text(), "ring1.yml references a secret"


def test_the_release_check_triggers_only_on_version_tags(release_check) -> None:
    """A release check on a branch push would run on every commit and prove nothing about a tag."""
    assert _triggers(release_check)["push"] == {"tags": ["v*"]}


def test_the_release_check_reads_no_secret_and_no_environment(release_check) -> None:
    """It runs on a tag anyone with write access can push: nothing it reads can be a secret."""
    assert "secrets." not in RELEASE_CHECK.read_text(), "release-check.yml references a secret"
    for key, job in release_check["jobs"].items():
        assert "environment" not in job, f"release-check.yml job {key} declares an environment"
        for step in job["steps"]:
            if str(step.get("uses", "")).startswith("actions/checkout@"):
                assert step.get("with", {}).get("persist-credentials") is False, (
                    "the release check keeps the token in .git/config"
                )


def test_the_release_check_compares_the_tag_only_for_a_tag_ref(release_check) -> None:
    """On a branch the ref name is not a tag, and the script rejects it with exit 2."""
    step = next(
        step
        for job in release_check["jobs"].values()
        for step in job["steps"]
        if "check-version.sh" in str(step.get("run", ""))
    )
    run = step["run"]
    assert step["env"]["REF_TYPE"] == "${{ github.ref_type }}"
    assert 'if [ "$REF_TYPE" = tag ]' in run
    assert 'scripts/check-version.sh "$REF_NAME"' in run
    assert re.search(r"else\s+scripts/check-version\.sh\s+fi", run), (
        "the branch case must run check-version.sh without an argument"
    )


def test_the_release_check_runs_what_a_release_depends_on(release_check) -> None:
    """A job that keeps its name but drops a command checks nothing."""
    lines = "\n".join(_run_lines(release_check))
    for command in (
        "scripts/check-version.sh",
        "sha256sum -c SHA256SUMS",
        "uv sync --frozen --all-groups",
        "tests/test_contract_frozen.py",
        "tests/test_contract_golden.py",
        "tests/test_plugin_manifest.py",
        "claude plugin validate --strict plugin/claude-code",
        "uv build",
        "tests/test_sdist_contents.py",
    ):
        assert command in lines, f"release-check.yml does not run: {command}"
    assert "@anthropic-ai/claude-code@2.1.296" in lines, "Claude Code is not pinned to an exact version"
    assert "KNOWLEDGE_REQUIRE_SQLITE_VEC" in RELEASE_CHECK.read_text()
    assert "astral.sh/uv/0.10.4/install.sh" in RELEASE_CHECK.read_text(), "the uv installer is not pinned"


def test_the_suite_job_does_not_keep_the_token(pipeline) -> None:
    """The suite installs and runs packages from PyPI and never fetches or pushes."""
    checkouts = [
        step
        for step in _job(pipeline, "Lint + pytest with coverage")["steps"]
        if str(step.get("uses", "")).startswith("actions/checkout@")
    ]
    assert checkouts and all(step.get("with", {}).get("persist-credentials") is False for step in checkouts)


# ---------------------------------------------------------------------------
# publish.yml
# ---------------------------------------------------------------------------

_PUBLISH_JOB = "Publish to PyPI and npm"
_BUILD_JOB = "Build and verify the release"


def _job_run_lines(job: dict) -> str:
    return "\n".join(step["run"] for step in job["steps"] if isinstance(step.get("run"), str))


def test_publish_triggers_on_version_tags_and_on_a_dry_run_by_default(publish) -> None:
    triggers = _triggers(publish)
    assert triggers["push"] == {"tags": ["v*"]}
    dry_run = triggers["workflow_dispatch"]["inputs"]["dry_run"]
    assert dry_run["type"] == "boolean"
    assert dry_run["default"] is True, "a manual run must not publish unless asked to"


def test_only_the_publish_job_can_mint_a_token(publish) -> None:
    """Building runs code from PyPI and npm; it must not be able to publish."""
    assert publish.get("permissions") == {"contents": "read"}
    assert isinstance(publish.get("concurrency"), dict) and publish["concurrency"].get("group")
    assert publish["concurrency"].get("cancel-in-progress") is False, "a publish is never cancelled halfway"
    build = _job(publish, _BUILD_JOB)
    assert "permissions" not in build and "environment" not in build
    job = _job(publish, _PUBLISH_JOB)
    assert job["permissions"] == {"contents": "read", "id-token": "write"}
    assert job["environment"] == "release"
    assert set(publish["jobs"]) == {"build", "publish"}
    assert job["needs"] == "build"
    assert "secrets." not in PUBLISH.read_text(), "trusted publishing needs no secret"


def test_publish_runs_only_for_a_tag_and_never_on_a_dry_run(publish) -> None:
    condition = _job(publish, _PUBLISH_JOB)["if"]
    assert condition.startswith("github.ref_type == 'tag' && ")
    assert "github.event_name == 'push'" in condition
    assert "inputs.dry_run == false" in condition


def test_no_checkout_in_publish_keeps_the_token(publish) -> None:
    for job in publish["jobs"].values():
        for step in job["steps"]:
            if str(step.get("uses", "")).startswith("actions/checkout@"):
                assert step.get("with", {}).get("persist-credentials") is False


def test_the_build_job_verifies_before_anything_is_published(publish) -> None:
    build = _job(publish, _BUILD_JOB)
    lines = _job_run_lines(build)
    for command in (
        "scripts/ci-install-uv.sh",
        'scripts/check-version.sh "$REF_NAME"',
        "node npm/scripts/uv-release.js --check",
        "node npm/scripts/uv-release.js --smoke",
        "npm test",
        "npm pack --dry-run",
        "uv build --out-dir dist",
        "uv venv --python 3.12",
        "for command in cloudfrog-knowledge knowledge",
        '"$RUNNER_TEMP/venv/bin/$command" --version',
    ):
        assert command in lines, f"the build job does not run: {command}"
    assert "astral.sh" not in PUBLISH.read_text(), "publish.yml installs uv only from the verified archive"


def test_each_registry_is_skipped_only_when_the_version_exists(publish) -> None:
    lines = _job_run_lines(_job(publish, _PUBLISH_JOB))
    pypi = lines.index("https://pypi.org/pypi/cloudfrog-knowledge/$version/json")
    npm = lines.index("https://registry.npmjs.org/@cloudfrog%2Fknowledge/$version")
    upload = lines.index("uv publish --trusted-publishing always")
    release = lines.index("npm publish --provenance --access public")
    assert pypi < upload < npm < release, "PyPI first, then npm, each after its own existence check"
    # 200 skips, 404 publishes, anything else fails the job.
    assert lines.count("200) echo \"::notice::") == 2
    assert lines.count("404) ;;") == 2
    assert lines.count("*) echo \"::error::") == 2


def test_npm_publishes_with_a_cli_that_supports_trusted_publishing(publish) -> None:
    lines = _job_run_lines(_job(publish, _PUBLISH_JOB))
    found = re.search(r"npm install -g npm@(\d+)\.(\d+)\.(\d+)", lines)
    assert found, "the npm CLI is not pinned"
    assert tuple(map(int, found.groups())) >= (11, 5, 1)
    setup = [
        step
        for step in _job(publish, _PUBLISH_JOB)["steps"]
        if str(step.get("uses", "")).startswith("actions/setup-node@")
    ]
    assert [step["with"]["registry-url"] for step in setup] == ["https://registry.npmjs.org"]
