"""Git runs only for a vault that declares a repository.

A vault is a directory; whether it is also a git checkout is a hosting
decision. These cases pin one rule, applied the same way by the ``reindex``
tool and by the webhook: a vault with ``repo`` in ``vaults.yaml`` (or, for a
``VAULT_PATH`` deployment, ``VAULT_REMOTE``) is converged on the head of its
served branch before it is indexed; any other vault runs no git command at
all, not even the probe that asks whether it sits inside a checkout. A failed
fetch is logged as an error and the vault keeps being served from what is on
disk.

``conftest._no_git_subprocess`` refuses every git call for the whole suite.
The cases up to (f) replace it with a recorder that answers like git would, so
the calls themselves are what is asserted; the cases from (g) on run the real
git binary against a bare repository and its clone under ``tmp_path``, with
``HOME`` and the global configuration inside it, and never reach the network.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import shutil
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

from knowledge import gitsync, ports, server, settings, vault_lint, webhook
from knowledge.registry import VaultRegistry, VaultRuntime

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_VAULTS = REPO_ROOT / "tests" / "fixtures" / "vaults"
_REMOTE = "git@github.com:example/alpha-vault.git"

GitAnswer = Callable[[list[str]], tuple[int, str, str]]


def _call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    result = fn(*args, **kwargs)
    return asyncio.run(result) if inspect.isawaitable(result) else result


@pytest.fixture()
def git_calls(monkeypatch: pytest.MonkeyPatch) -> Callable[[GitAnswer], list[list[str]]]:
    """Record every git invocation and answer it with ``answer``."""

    def install(answer: GitAnswer) -> list[list[str]]:
        calls: list[list[str]] = []

        def _record(args: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
            del timeout
            calls.append(list(args))
            return answer(args)

        monkeypatch.setattr(gitsync, "_run_git", _record)
        return calls

    return install


_HEAD_BEFORE = "a" * 40
_HEAD_AFTER = "b" * 40


def _a_checkout(fetch_rc: int = 0, *, brings_commits: bool = False, branch: str = "main") -> GitAnswer:
    """A checkout on ``branch`` whose fetch exits ``fetch_rc`` and, if it succeeds, brings commits or not."""
    head = [_HEAD_BEFORE]
    remote = _HEAD_AFTER if brings_commits else _HEAD_BEFORE

    def answer(args: list[str]) -> tuple[int, str, str]:
        if "--is-inside-work-tree" in args:
            return 0, "true", ""
        if args[-2:] == ["rev-parse", "HEAD"]:
            return 0, head[0], ""
        if "symbolic-ref" in args:
            return 0, branch, ""
        if "fetch" in args:
            if fetch_rc != 0:
                return fetch_rc, "", "fatal: Could not read from remote repository."
            return 0, "", ""
        if "--verify" in args:
            return 0, remote, ""
        if "merge-base" in args:
            return 0, "", ""
        if args[2:4] == ["merge", "--ff-only"]:
            head[0] = remote
            return 0, "Fast-forward", ""
        return 1, "", "unexpected git call"

    return answer


def _not_a_checkout(args: list[str]) -> tuple[int, str, str]:
    del args
    return 128, "", "fatal: not a git repository"


def _alpha(tmp_path: Path) -> Path:
    """A copy of the alpha fixture vault with no ``.git`` anywhere near it."""
    target = tmp_path / "alpha"
    shutil.copytree(FIXTURE_VAULTS / "alpha", target)
    return target


def _compat(tmp_path: Path, **extra: str) -> settings.Settings:
    env = {
        "VAULT_PATH": str(_alpha(tmp_path)),
        "CHROMA_PERSIST_DIR": str(tmp_path / "chroma"),
        "KNOWLEDGE_EMBEDDINGS_PROVIDER": "bag-of-words",
    }
    env.update(extra)
    return settings.load_settings(env, project_root=tmp_path)


def _registered(tmp_path: Path, **entry: Any) -> settings.Settings:
    vault = _alpha(tmp_path)
    vaults_file = tmp_path / "vaults.yaml"
    vaults_file.write_text(yaml.safe_dump({"vaults": [{"slug": "alpha", "path": str(vault), **entry}]}))
    env = {
        "KNOWLEDGE_VAULTS": str(vaults_file),
        "KNOWLEDGE_HOME": str(tmp_path / "home"),
        "KNOWLEDGE_EMBEDDINGS_PROVIDER": "bag-of-words",
    }
    return settings.load_settings(env, project_root=tmp_path)


def _serve(monkeypatch: pytest.MonkeyPatch, s: settings.Settings) -> VaultRuntime:
    reg = VaultRegistry.from_settings(s, sync_factory=server.vault_sync_for)
    monkeypatch.setattr(server, "_registry", reg)
    return reg.default


class _FakeIndexer:
    def __init__(self) -> None:
        self.runs: list[bool] = []

    def index(self, *, force: bool = False, target: str = "vault") -> dict[str, int]:
        assert target == "vault"
        self.runs.append(force)
        return {"files": 2}


# --- (a) no repository, no git ----------------------------------------------------


def test_a_vault_without_a_repository_reindexes_without_any_git_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_calls: Any
) -> None:
    calls = git_calls(_a_checkout())
    rt = _serve(monkeypatch, _compat(tmp_path))

    stats = _call(server.reindex, target="vault", pull=True)

    assert rt.spec.repo is None
    assert server.vault_sync_for(rt.spec) is None
    assert stats["files"] == 2
    assert "export_hint" not in stats
    assert calls == []


def test_auto_pull_off_runs_no_git_even_with_a_repository(tmp_path: Path, git_calls: Any) -> None:
    calls = git_calls(_a_checkout())
    s = _registered(tmp_path, repo=_REMOTE, git={"auto_pull": False})
    rt = VaultRegistry.from_settings(s, sync_factory=server.vault_sync_for).default

    assert server.vault_sync_for(rt.spec) is None
    assert rt.sync_vault() is False
    assert calls == []


# --- (b) a declared repository follows its served branch -----------------------------


_FETCH = ["fetch", "--prune", "origin"]


def test_a_declared_repository_fetches_and_follows_its_served_branch(tmp_path: Path, git_calls: Any) -> None:
    calls = git_calls(_a_checkout(brings_commits=True, branch="trunk"))
    s = _registered(tmp_path, repo=_REMOTE, git={"branch": "trunk"})
    rt = VaultRegistry.from_settings(s, sync_factory=server.vault_sync_for).default

    assert server.vault_sync_for(rt.spec) is gitsync.git_vault_sync("trunk")
    assert rt.sync_vault() is True
    assert ["-C", str(rt.vault_path), *_FETCH] in calls
    assert ["-C", str(rt.vault_path), "rev-parse", "--verify", "--quiet", "origin/trunk^{commit}"] in calls


# --- (c) a failed fetch is an error and the vault keeps being served ------------------


def test_a_failed_fetch_logs_an_error_and_the_vault_is_still_served(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_calls: Any, caplog: pytest.LogCaptureFixture
) -> None:
    git_calls(_a_checkout(fetch_rc=1))
    rt = _serve(monkeypatch, _registered(tmp_path, repo=_REMOTE))

    with caplog.at_level(logging.WARNING, logger="knowledge.gitsync"):
        assert rt.sync_vault() is False
        stats = _call(server.reindex, target="vault")

    assert any(r.levelno == logging.ERROR and "rc=1" in r.getMessage() for r in caplog.records)
    assert stats["files"] == 2
    hits = {r["file_path"] for r in _call(server.search, "gateway telvia token", limit=3)}
    assert "Services/Gateway Service.md" in hits


# --- (d) VAULT_REMOTE keeps a VAULT_PATH deployment pulling ---------------------------


def test_vault_remote_alone_enables_the_pull_of_a_vault_path_deployment(tmp_path: Path, git_calls: Any) -> None:
    calls = git_calls(_a_checkout())
    s = _compat(tmp_path, VAULT_REMOTE=_REMOTE)
    rt = VaultRegistry.from_settings(s, sync_factory=server.vault_sync_for).default

    assert rt.spec.repo == _REMOTE
    assert rt.sync_vault() is True
    assert any("fetch" in args for args in calls)


# --- (e) the webhook follows the same rule -------------------------------------------


def test_the_webhook_reindex_follows_the_vault_sync_it_was_given(tmp_path: Path, git_calls: Any) -> None:
    calls = git_calls(_a_checkout())
    plain = VaultRegistry.from_settings(_compat(tmp_path / "plain"), sync_factory=server.vault_sync_for).default
    pulled = VaultRegistry.from_settings(
        _registered(tmp_path / "pulled", repo=_REMOTE), sync_factory=server.vault_sync_for
    ).default
    indexer = _FakeIndexer()

    webhook._run_reindex_job(indexer, vault_sync=plain.sync_vault)
    webhook._run_reindex_job(indexer, vault_sync=None)
    assert calls == []

    webhook._run_reindex_job(indexer, vault_sync=pulled.sync_vault)
    assert ["-C", str(pulled.vault_path), *_FETCH] in calls
    assert indexer.runs == [False, False, False]


# --- (f) how a pull ended: the five vault_sync outcomes ------------------------------


def test_pull_vault_outcome_disabled_runs_no_git(tmp_path: Path, git_calls: Any) -> None:
    calls = git_calls(_a_checkout())

    assert gitsync.pull_vault_outcome(tmp_path, enabled=False) == ports.SYNC_SKIPPED_PULL_FALSE
    assert calls == []


def test_pull_vault_outcome_outside_a_worktree_is_skipped(tmp_path: Path, git_calls: Any) -> None:
    calls = git_calls(_not_a_checkout)

    assert gitsync.pull_vault_outcome(tmp_path) == ports.SYNC_SKIPPED_NO_REPO
    assert not any("fetch" in args for args in calls)


def test_pull_vault_outcome_tells_up_to_date_from_pulled(tmp_path: Path, git_calls: Any) -> None:
    git_calls(_a_checkout())
    assert gitsync.pull_vault_outcome(tmp_path) == ports.SYNC_UP_TO_DATE

    git_calls(_a_checkout(brings_commits=True))
    assert gitsync.pull_vault_outcome(tmp_path) == ports.SYNC_PULLED


def test_pull_vault_outcome_reports_a_failed_fetch(tmp_path: Path, git_calls: Any) -> None:
    git_calls(_a_checkout(fetch_rc=1))

    assert gitsync.pull_vault_outcome(tmp_path) == ports.SYNC_FAILED


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (_a_checkout(), True),
        (_a_checkout(brings_commits=True), True),
        (_a_checkout(fetch_rc=1), False),
        (_not_a_checkout, False),
    ],
    ids=["up-to-date", "pulled", "failed", "no-worktree"],
)
def test_pull_vault_keeps_its_boolean(tmp_path: Path, git_calls: Any, answer: GitAnswer, expected: bool) -> None:
    git_calls(answer)

    assert gitsync.pull_vault(tmp_path) is expected


def test_the_git_sync_reports_its_outcome(tmp_path: Path, git_calls: Any) -> None:
    git_calls(_a_checkout(brings_commits=True))

    assert isinstance(gitsync.GitVaultSync, ports.ReportingVaultSync)
    assert gitsync.GitVaultSync.outcome(tmp_path) == ports.SYNC_PULLED


@pytest.mark.parametrize(
    ("repo", "answer", "pull", "expected"),
    [
        (None, _a_checkout(), True, ports.SYNC_SKIPPED_NO_REPO),
        (_REMOTE, _a_checkout(), False, ports.SYNC_SKIPPED_PULL_FALSE),
        (_REMOTE, _a_checkout(), True, ports.SYNC_UP_TO_DATE),
        (_REMOTE, _a_checkout(brings_commits=True), True, ports.SYNC_PULLED),
        (_REMOTE, _a_checkout(fetch_rc=1), True, ports.SYNC_FAILED),
    ],
    ids=["no-repo", "pull-false", "up-to-date", "pulled", "failed"],
)
def test_reindex_reports_how_the_vault_sync_ended(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    git_calls: Any,
    repo: str | None,
    answer: GitAnswer,
    pull: bool,
    expected: str,
) -> None:
    calls = git_calls(answer)
    entry = {} if repo is None else {"repo": repo}
    _serve(monkeypatch, _registered(tmp_path, **entry))

    stats = _call(server.reindex, target="vault", pull=pull)

    assert stats["vault_sync"] == expected
    assert stats["vault"] == "alpha"
    assert "export_hint" not in stats
    assert stats["files"] > 0, "the vault is indexed whatever the pull did"
    if expected in (ports.SYNC_SKIPPED_NO_REPO, ports.SYNC_SKIPPED_PULL_FALSE):
        assert calls == []


def test_reindex_does_not_pull_unless_asked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_calls: Any) -> None:
    """R11: a vault with a repository is indexed as the checkout holds it; only ``pull=True`` runs git."""
    calls = git_calls(_a_checkout(brings_commits=True))
    _serve(monkeypatch, _registered(tmp_path, repo=_REMOTE))

    stats = _call(server.reindex, target="vault")

    assert stats["vault_sync"] == ports.SYNC_SKIPPED_PULL_FALSE
    assert calls == []


def test_reindex_validation_counts_the_linter_findings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault = _alpha(tmp_path)
    (vault / "Broken.md").write_text("---\ntitle: [unclosed\n---\n\nSee [[Nowhere At All]].\n", encoding="utf-8")
    env = {
        "VAULT_PATH": str(vault),
        "CHROMA_PERSIST_DIR": str(tmp_path / "chroma"),
        "KNOWLEDGE_EMBEDDINGS_PROVIDER": "bag-of-words",
    }
    _serve(monkeypatch, settings.load_settings(env, project_root=tmp_path))
    report = vault_lint.lint(vault)

    stats = _call(server.reindex, target="vault")

    assert report.errors, "the broken note must give the linter something to report"
    assert stats["validation"] == {"ok": False, "errors": len(report.errors), "warnings": len(report.warnings)}


def test_reindex_validation_keeps_the_1_2_rules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Notes the 1.3 vault rules fail (no ``last_updated``, a foreign scope, a loose type) do not reach ``validation``."""
    vault = _alpha(tmp_path)
    (vault / "Loose.md").write_text("---\ntype: mystery\nscope: elsewhere\n---\n\nNo date, no known type.\n", encoding="utf-8")
    env = {
        "VAULT_PATH": str(vault),
        "CHROMA_PERSIST_DIR": str(tmp_path / "chroma"),
        "KNOWLEDGE_EMBEDDINGS_PROVIDER": "bag-of-words",
    }
    _serve(monkeypatch, settings.load_settings(env, project_root=tmp_path))

    stats = _call(server.reindex, target="vault")

    assert vault_lint.lint(vault, rules=vault_lint.RULES_1_3).errors, "the 1.3 rules must have something to say"
    assert stats["validation"] == {"ok": True, "errors": 0, "warnings": 0}


def test_reindex_validation_is_null_when_the_linter_cannot_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, _compat(tmp_path))

    def _boom(root: Path) -> None:
        raise OSError("disk went away")

    monkeypatch.setattr(vault_lint, "lint", _boom)

    stats = _call(server.reindex, target="vault", pull=True)

    assert stats["validation"] is None
    assert stats["vault_sync"] == ports.SYNC_SKIPPED_NO_REPO


# ---------------------------------------------------------------------------
# A real git: a bare remote and the clone the server serves
# ---------------------------------------------------------------------------


def _git_env(home: Path) -> dict[str, str]:
    # A hook may export GIT_DIR and friends; none of them may reach these clones,
    # and neither may the developer's global configuration (signing, hooks, aliases).
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        HOME=str(home),
        XDG_CONFIG_HOME=str(home / ".config"),
        GIT_CONFIG_NOSYSTEM="1",
        GIT_TERMINAL_PROMPT="0",
        GIT_AUTHOR_NAME="test",
        GIT_AUTHOR_EMAIL="test@example.invalid",
        GIT_COMMITTER_NAME="test",
        GIT_COMMITTER_EMAIL="test@example.invalid",
    )
    return env


def _note(stem: str, word: str) -> str:
    return (
        f"---\ntitle: {stem}\ntype: note\nstatus: active\nlast_updated: 2026-10-29\n---\n\n"
        f"# {stem}\n\nA note about {word}.\n"
    )


@dataclass
class _Repo:
    home: Path
    bare: Path
    clone: Path
    calls: list[list[str]]

    def git(self, *args: str, cwd: Path) -> str:
        proc = subprocess.run(
            ["git", *args], cwd=cwd, env=_git_env(self.home), check=True, capture_output=True, text=True
        )
        return proc.stdout.strip()

    def head(self, where: Path | None = None) -> str:
        return self.git("rev-parse", "HEAD", cwd=where or self.clone)

    def remote_head(self) -> str:
        return self.git("rev-parse", "main", cwd=self.bare)

    def _work(self, name: str) -> Path:
        work = self.home / name
        if not work.exists():
            self.git("clone", "-q", "--branch", "main", str(self.bare), str(work), cwd=self.home)
        else:
            self.git("fetch", "-q", "origin", cwd=work)
            self.git("reset", "-q", "--hard", "origin/main", cwd=work)
        return work

    def push(self, rel: str, word: str) -> str:
        """A teammate's merge: one new note on top of ``main``, pushed normally."""
        work = self._work("work")
        (work / rel).parent.mkdir(parents=True, exist_ok=True)
        (work / rel).write_text(_note(Path(rel).stem, word), encoding="utf-8")
        self.git("add", "-A", cwd=work)
        self.git("commit", "-q", "-m", f"docs: add {Path(rel).stem}", cwd=work)
        self.git("push", "-q", "origin", "main", cwd=work)
        return self.remote_head()

    def rewrite(self, *, drop: str, add: str, word: str) -> str:
        """A history rewrite of ``main``: the tip is amended (one note dropped, one added) and force-pushed."""
        work = self._work("rewriter")
        self.git("rm", "-q", drop, cwd=work)
        (work / add).parent.mkdir(parents=True, exist_ok=True)
        (work / add).write_text(_note(Path(add).stem, word), encoding="utf-8")
        self.git("add", "-A", cwd=work)
        self.git("commit", "-q", "--amend", "-m", "docs: rewritten history", cwd=work)
        self.git("push", "-q", "--force", "origin", "main", cwd=work)
        return self.remote_head()

    def commit_in_clone(self, rel: str, word: str) -> str:
        """A commit made inside the served clone itself, which the remote never receives."""
        (self.clone / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.clone / rel).write_text(_note(Path(rel).stem, word), encoding="utf-8")
        self.git("add", "-A", cwd=self.clone)
        self.git("commit", "-q", "-m", f"docs: local {Path(rel).stem}", cwd=self.clone)
        return self.head()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Repo:
    """The alpha fixture vault in a bare repository on ``main`` and a clone of it, with git really running."""
    home = tmp_path / "home"
    home.mkdir()
    bare = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    clone = tmp_path / "clone"
    state = _Repo(home=home, bare=bare, clone=clone, calls=[])
    state.git("init", "-q", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    shutil.copytree(FIXTURE_VAULTS / "alpha", seed)
    state.git("init", "-q", "-b", "main", cwd=seed)
    state.git("add", "-A", cwd=seed)
    state.git("commit", "-q", "-m", "docs: seed", cwd=seed)
    (seed / "Notes").mkdir()
    (seed / "Notes" / "Doomed.md").write_text(_note("Doomed", "a note the rewrite drops"), encoding="utf-8")
    state.git("add", "-A", cwd=seed)
    state.git("commit", "-q", "-m", "docs: add Doomed", cwd=seed)
    state.git("push", "-q", str(bare), "main", cwd=seed)
    state.git("clone", "-q", "--branch", "main", str(bare), str(clone), cwd=tmp_path)

    env = _git_env(home)

    def _real(args: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
        state.calls.append(list(args))
        proc = subprocess.run(["git", *args], capture_output=True, text=True, timeout=timeout, env=env, check=False)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()

    monkeypatch.setattr(gitsync, "_run_git", _real)
    return state


@pytest.fixture
def served_repo(repo: _Repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[VaultRuntime]:
    """The clone of :func:`repo` served as a ``VAULT_PATH`` deployment with ``VAULT_REMOTE``, already indexed."""
    env = {
        "VAULT_PATH": str(repo.clone),
        "VAULT_REMOTE": str(repo.bare),
        "VAULT_BRANCH": "main",
        "KNOWLEDGE_INDEX_DIR": str(tmp_path / "index"),
        "KNOWLEDGE_EMBEDDINGS_PROVIDER": "bag-of-words",
        "MCP_TRANSPORT": "http",
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    settings.reset_current()
    reg = VaultRegistry.from_settings(settings.current(), sync_factory=server.vault_sync_for)
    monkeypatch.setattr(server, "_registry", reg)
    rt = reg.default
    rt.indexer.index(force=False, target="vault")
    rt.index_state["ready"] = True
    repo.calls.clear()
    try:
        yield rt
    finally:
        reg.close()


def _indexed(rt: VaultRuntime) -> set[str]:
    return set(rt.indexer.store.file_hashes())


def _webhook_run(rt: VaultRuntime) -> None:
    """One webhook pass, exactly as ``/webhook/reindex`` runs it in the background."""
    webhook._run_reindex_job(rt.indexer, vault_sync=rt.sync_vault, refresh=webhook.SerialRefresh(rt.refresh_lock))


# --- (g) a normal push fast-forwards the clone ---------------------------------------


def test_a_push_fast_forwards_the_clone_and_reindexes(repo: _Repo, served_repo: VaultRuntime) -> None:
    before = repo.head()
    remote = repo.push("Notes/Late Addition.md", "cormorant")

    _webhook_run(served_repo)

    status = gitsync.last_sync(repo.clone)
    assert status is not None
    assert (status.result, status.before, status.after, status.error) == (
        gitsync.RESULT_FAST_FORWARD,
        before,
        remote,
        None,
    )
    assert repo.head() == remote
    assert not any("checkout" in call for call in repo.calls), "a fast-forward never forces the worktree"
    assert "Notes/Late Addition.md" in _indexed(served_repo)


def test_a_clone_already_on_the_remote_head_is_up_to_date(repo: _Repo, served_repo: VaultRuntime) -> None:
    head = repo.head()

    assert gitsync.pull_vault_outcome(repo.clone, branch="main") == ports.SYNC_UP_TO_DATE

    status = gitsync.last_sync(repo.clone)
    assert status is not None and status.result == gitsync.RESULT_UP_TO_DATE
    assert repo.head() == head


# --- (h) a rewritten or diverged history realigns the clone --------------------------


@pytest.mark.parametrize("local_commit", [False, True], ids=["remote-force-pushed", "clone-diverged"])
def test_a_rewritten_remote_realigns_the_clone_and_reindexes(
    repo: _Repo, served_repo: VaultRuntime, caplog: pytest.LogCaptureFixture, local_commit: bool
) -> None:
    if local_commit:
        repo.commit_in_clone("Notes/Local Only.md", "a commit the remote never receives")
    before = repo.head()
    remote = repo.rewrite(drop="Notes/Doomed.md", add="Notes/Rewritten.md", word="heron")

    with caplog.at_level(logging.INFO, logger="knowledge.gitsync"):
        _webhook_run(served_repo)

    status = gitsync.last_sync(repo.clone)
    assert status is not None
    assert (status.result, status.before, status.after, status.error) == (
        gitsync.RESULT_REALIGNED,
        before,
        remote,
        None,
    )
    assert repo.head() == remote
    assert repo.git("symbolic-ref", "--short", "HEAD", cwd=repo.clone) == "main"
    assert repo.git("status", "--porcelain", "--untracked-files=no", cwd=repo.clone) == ""
    assert ["-C", str(repo.clone), "checkout", "-f", "-B", "main", "origin/main"] in repo.calls
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(before[:12] in message and remote[:12] in message for message in warnings), warnings
    indexed = _indexed(served_repo)
    assert "Notes/Rewritten.md" in indexed
    assert "Notes/Doomed.md" not in indexed
    assert "Notes/Local Only.md" not in indexed


def test_the_next_push_after_a_realignment_fast_forwards_again(repo: _Repo, served_repo: VaultRuntime) -> None:
    repo.rewrite(drop="Notes/Doomed.md", add="Notes/Rewritten.md", word="heron")
    _webhook_run(served_repo)
    remote = repo.push("Notes/After.md", "ibis")

    _webhook_run(served_repo)

    status = gitsync.last_sync(repo.clone)
    assert status is not None and status.result == gitsync.RESULT_FAST_FORWARD
    assert repo.head() == remote
    assert "Notes/After.md" in _indexed(served_repo)


def test_a_refused_fast_forward_realigns_the_clone(repo: _Repo, served_repo: VaultRuntime) -> None:
    """A local edit the fast-forward would overwrite cannot pin the clone to an old commit."""
    work = repo._work("editor")
    note = "Notes/Doomed.md"
    (work / note).write_text(_note("Doomed", "an upstream edit"), encoding="utf-8")
    repo.git("commit", "-q", "-am", "docs: edit Doomed", cwd=work)
    repo.git("push", "-q", "origin", "main", cwd=work)
    remote = repo.remote_head()
    (repo.clone / note).write_text(_note("Doomed", "an edit made inside the served clone"), encoding="utf-8")

    _webhook_run(served_repo)

    status = gitsync.last_sync(repo.clone)
    assert status is not None and status.result == gitsync.RESULT_REALIGNED
    assert repo.head() == remote
    assert "an upstream edit" in (repo.clone / note).read_text(encoding="utf-8")
    assert repo.git("status", "--porcelain", cwd=repo.clone) == ""


def test_a_served_branch_missing_on_the_remote_fails_without_touching_the_clone(repo: _Repo) -> None:
    head = repo.head()

    assert gitsync.pull_vault_outcome(repo.clone, branch="trunk") == ports.SYNC_FAILED

    status = gitsync.last_sync(repo.clone)
    assert status is not None
    assert (status.result, status.error) == (gitsync.RESULT_FAILED, "origin/trunk does not exist after the fetch")
    assert repo.head() == head
    assert repo.git("symbolic-ref", "--short", "HEAD", cwd=repo.clone) == "main"


# --- (i) a failed fetch leaves the clone untouched and is recorded --------------------


@pytest.mark.parametrize(
    "remote_url",
    [
        pytest.param(None, id="remote-gone"),
        pytest.param("https://deploy:s3cret-token@127.0.0.1:9/vault.git", id="credentials-in-the-url"),
    ],
)
def test_a_failed_fetch_keeps_the_clone_and_records_the_failure(
    repo: _Repo,
    served_repo: VaultRuntime,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    remote_url: str | None,
) -> None:
    repo.push("Notes/Unreachable.md", "never fetched")
    if remote_url is None:
        repo.bare.rename(tmp_path / "moved.git")
    else:
        repo.git("remote", "set-url", "origin", remote_url, cwd=repo.clone)
    head = repo.head()
    tree = repo.git("ls-files", "-s", cwd=repo.clone)

    with caplog.at_level(logging.INFO, logger="knowledge.gitsync"):
        _webhook_run(served_repo)

    status = gitsync.last_sync(repo.clone)
    assert status is not None
    assert status.result == gitsync.RESULT_FAILED
    assert status.error and status.error.startswith("git fetch exited rc=")
    assert repo.head() == head
    assert repo.git("ls-files", "-s", cwd=repo.clone) == tree
    assert repo.git("status", "--porcelain", cwd=repo.clone) == ""
    assert not any(call[2:3] in (["merge"], ["checkout"]) for call in repo.calls), "nothing touches the worktree"
    assert any(r.levelno == logging.ERROR and "git fetch exited" in r.getMessage() for r in caplog.records)
    assert "Notes/Unreachable.md" not in _indexed(served_repo)
    assert "Services/Gateway Service.md" in _indexed(served_repo), "the vault is still served from disk"
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "s3cret-token" not in status.error and "s3cret-token" not in logged


def test_redact_hides_the_user_information_of_a_url() -> None:
    line = "fatal: unable to access 'https://deploy:s3cret@example.invalid/v.git/' and ssh://git:pw@host/x"
    redacted = gitsync.redact(line)

    assert "s3cret" not in redacted and ":pw@" not in redacted
    assert "https://***@example.invalid/v.git/" in redacted
    assert "ssh://***@host/x" in redacted
    assert gitsync.redact("git@bitbucket.org:team/vault.git") == "git@bitbucket.org:team/vault.git"


def test_a_detached_head_without_a_configured_branch_fails_without_touching_the_clone(repo: _Repo) -> None:
    repo.git("checkout", "-q", "--detach", "HEAD", cwd=repo.clone)
    head = repo.head()

    assert gitsync.pull_vault_outcome(repo.clone) == ports.SYNC_FAILED

    status = gitsync.last_sync(repo.clone)
    assert status is not None and status.result == gitsync.RESULT_FAILED
    assert status.error == "HEAD is detached and no branch is configured to follow"
    assert repo.head() == head
    assert not any("fetch" in call for call in repo.calls)


# --- (j) /health reports the commit and the last sync --------------------------------


def _health(rt: VaultRuntime) -> dict[str, Any]:
    import httpx
    from mcp.server.fastmcp import FastMCP

    asgi = server.http_app(FastMCP("health-sync"))

    async def _get() -> dict[str, Any]:
        transport = httpx.ASGITransport(app=asgi)
        async with httpx.AsyncClient(transport=transport, base_url="http://health.test") as client:
            response = await client.get("/health")
            assert response.status_code == 200
            body: dict[str, Any] = response.json()
            return body

    return asyncio.run(_get())


def test_health_reports_the_vault_commit_and_how_the_last_sync_ended(
    repo: _Repo, served_repo: VaultRuntime, tmp_path: Path
) -> None:
    started = _health(served_repo)
    assert started["vault_commit"] == repo.head()[:12], "the commit is right before any refresh ran"
    assert (started["last_sync_at"], started["last_sync_result"], started["last_sync_error"]) == (None, None, None)
    assert (started["last_change_at"], started["last_change_result"]) == (None, None)
    assert {"status", "contract_version", "indexed", "index_version", "vector_backend", "stale"} <= set(started)

    remote = repo.rewrite(drop="Notes/Doomed.md", add="Notes/Rewritten.md", word="heron")
    _webhook_run(served_repo)
    realigned = _health(served_repo)
    assert realigned["vault_commit"] == remote[:12]
    assert realigned["last_sync_result"] == gitsync.RESULT_REALIGNED
    assert realigned["last_sync_error"] is None
    assert realigned["last_sync_at"]

    repo.bare.rename(tmp_path / "moved.git")
    _webhook_run(served_repo)
    failed = _health(served_repo)
    assert failed["vault_commit"] == remote[:12], "a failed fetch leaves the served commit where it was"
    assert failed["last_sync_result"] == gitsync.RESULT_FAILED
    assert failed["last_sync_error"].startswith("git fetch exited rc=")
    assert failed["last_sync_at"] >= realigned["last_sync_at"]
    assert failed["status"] == realigned["status"], "a failed sync is reported in its own fields"


def test_health_keeps_the_last_change_across_syncs_that_move_nothing(
    repo: _Repo, served_repo: VaultRuntime, tmp_path: Path
) -> None:
    """``last_change_*`` is the last sync that moved the clone; ``up_to_date`` and ``failed`` never replace it."""
    repo.push("Notes/Late Addition.md", "cormorant")
    _webhook_run(served_repo)
    forwarded = _health(served_repo)
    assert (forwarded["last_sync_result"], forwarded["last_change_result"]) == (
        gitsync.RESULT_FAST_FORWARD,
        gitsync.RESULT_FAST_FORWARD,
    )
    assert forwarded["last_change_at"] == forwarded["last_sync_at"]

    _webhook_run(served_repo)
    idle = _health(served_repo)
    assert idle["last_sync_result"] == gitsync.RESULT_UP_TO_DATE
    assert (idle["last_change_result"], idle["last_change_at"]) == (
        gitsync.RESULT_FAST_FORWARD,
        forwarded["last_change_at"],
    ), "an up-to-date sync leaves the last change in place"

    remote = repo.rewrite(drop="Notes/Doomed.md", add="Notes/Rewritten.md", word="heron")
    _webhook_run(served_repo)
    realigned = _health(served_repo)
    assert realigned["vault_commit"] == remote[:12]
    assert (realigned["last_sync_result"], realigned["last_change_result"]) == (
        gitsync.RESULT_REALIGNED,
        gitsync.RESULT_REALIGNED,
    )
    assert realigned["last_change_at"] == realigned["last_sync_at"]
    change = gitsync.last_change(repo.clone)
    assert change is not None and (change.before, change.after) != (None, None)

    repo.bare.rename(tmp_path / "moved.git")
    _webhook_run(served_repo)
    failed = _health(served_repo)
    assert failed["last_sync_result"] == gitsync.RESULT_FAILED
    assert (failed["last_change_result"], failed["last_change_at"]) == (
        gitsync.RESULT_REALIGNED,
        realigned["last_change_at"],
    ), "a failed sync leaves the last change in place"


def test_the_last_change_is_kept_per_vault(tmp_path: Path, git_calls: Any) -> None:
    git_calls(_a_checkout(brings_commits=True))
    moved, idle = tmp_path / "moved", tmp_path / "idle"

    assert gitsync.pull_vault_outcome(moved, branch="main") == ports.SYNC_PULLED

    change = gitsync.last_change(moved)
    assert change is not None and change.result in gitsync.CHANGE_RESULTS
    assert gitsync.last_change(idle) is None


def test_health_carries_null_sync_fields_for_a_vault_without_a_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_calls: Any
) -> None:
    calls = git_calls(_a_checkout())
    monkeypatch.setenv("MCP_TRANSPORT", "http")
    rt = _serve(monkeypatch, _compat(tmp_path))

    body = _health(rt)

    assert {key: body[key] for key in webhook.SYNC_STATE_KEYS} == dict.fromkeys(webhook.SYNC_STATE_KEYS)
    assert calls == [], "a vault without a repository runs no git command, not even for /health"


# --- (k) the webhook and the bootstrap realign a clone the same way --------------------


def test_the_bootstrap_adopts_a_clone_with_the_commands_the_webhook_realigns_with() -> None:
    """``infra/bootstrap-vault.sh`` and :func:`gitsync.converge` must not drift apart."""
    script = (REPO_ROOT / "infra" / "bootstrap-vault.sh").read_text(encoding="utf-8")

    assert 'git -C "$clone_dir" fetch --prune origin' in script
    assert 'git -C "$clone_dir" checkout -f -B "$branch" "origin/$branch"' in script
    assert 'git -C "$dest" fetch --prune origin' in script
    assert 'git -C "$dest" checkout -f -B "$vbranch" "origin/$vbranch"' in script
    assert " clean " not in script, "the bootstrap keeps untracked files, and so does the realignment"
    assert gitsync.REMOTE == "origin"
