"""The reindex webhook pulls the served branch one run at a time, and only for it.

A merge with ``--delete-branch`` makes GitHub send two pushes half a second
apart: one for the served branch and one deleting the merged branch. Each
delivery started its own ``git pull`` in the same checkout, so the two ran at
once and the second could fail with rc=128 ("continuing with stale vault").
Every push to another branch, a tag or a deletion also pulled and reindexed
for nothing: the clone follows the served branch only.

These cases drive the real ``server.http_app`` wiring through an in-process
ASGI client, which returns once the response *and* its background run have
finished, so "no pull happened" is a settled fact when a test asserts it. The
vault is a clone of a bare repository under ``tmp_path`` and git is the real
binary, run with ``HOME`` and the global configuration inside ``tmp_path``;
no case reads a real vault or reaches the network.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp.server.fastmcp import FastMCP

from knowledge import gitsync, ports, server, settings, webhook
from knowledge.registry import VaultRegistry, VaultRuntime

REPO_ROOT = Path(__file__).resolve().parent.parent
ALPHA_VAULT = REPO_ROOT / "tests" / "fixtures" / "vaults" / "alpha"

WEBHOOK_SECRET = "webhook-secret-for-the-delivery-tests"
ADMIN_BEARER = "admin-bearer-for-the-delivery-tests"
ZERO_SHA = "0" * 40
URL = "/webhook/reindex?target=vault"


# ---------------------------------------------------------------------------
# A real git, isolated from the developer's configuration
# ---------------------------------------------------------------------------


def _git_env(home: Path) -> dict[str, str]:
    # A pre-push hook exports GIT_DIR and friends; none of them may reach these clones.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        HOME=str(home),
        GIT_CONFIG_NOSYSTEM="1",
        GIT_TERMINAL_PROMPT="0",
        GIT_AUTHOR_NAME="test",
        GIT_AUTHOR_EMAIL="test@example.invalid",
        GIT_COMMITTER_NAME="test",
        GIT_COMMITTER_EMAIL="test@example.invalid",
    )
    return env


def _git(home: Path, *args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, env=_git_env(home), check=True, capture_output=True)


class _GitProbe:
    """Stands in for ``gitsync._run_git``: runs the real git and watches every pull.

    A pull is the ``git fetch`` each refresh of the clone starts with
    (``gitsync.converge``). ``max_in_flight`` is the most pulls that were
    inside this function at once. ``hold_first_pull`` keeps the first pull
    inside it, after git has returned, until :attr:`release` is set: that pull
    has fetched what the remote held at that moment and its run is still in
    flight.
    """

    def __init__(self, home: Path) -> None:
        self._env = _git_env(home)
        self._lock = threading.Lock()
        self._hold = False
        self._held = False
        self.calls: list[list[str]] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.pull_rcs: list[int] = []
        self.first_pull_held = threading.Event()
        self.second_pull_entered = threading.Event()
        self.release = threading.Event()

    def hold_first_pull(self) -> None:
        self._hold = True

    def __call__(self, args: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
        is_pull = "fetch" in args
        hold = False
        with self._lock:
            self.calls.append(list(args))
            if is_pull:
                self.in_flight += 1
                self.max_in_flight = max(self.max_in_flight, self.in_flight)
                if sum("fetch" in call for call in self.calls) >= 2:
                    self.second_pull_entered.set()
                hold = self._hold and not self._held
                self._held = self._held or hold
        rc = 1
        try:
            proc = subprocess.run(
                ["git", *args], capture_output=True, text=True, timeout=timeout, env=self._env, check=False
            )
            rc = proc.returncode
            if hold:
                self.first_pull_held.set()
                self.release.wait(15)
            return rc, proc.stdout.strip(), proc.stderr.strip()
        finally:
            if is_pull:
                with self._lock:
                    self.in_flight -= 1
                    self.pull_rcs.append(rc)


# ---------------------------------------------------------------------------
# A served vault: a clone of a bare repository, behind server.http_app
# ---------------------------------------------------------------------------


@dataclass
class _Served:
    asgi: Any
    rt: VaultRuntime
    bare: Path
    home: Path
    branch: str
    git: _GitProbe
    index_runs: list[bool] = field(default_factory=list)

    def commit(self, rel: str, word: str) -> None:
        """Push one note carrying ``word`` to the served branch, as a teammate's merge would."""
        work = self.home / f"work-{uuid.uuid4().hex[:8]}"
        _git(self.home, "clone", "-q", "--branch", self.branch, str(self.bare), str(work), cwd=self.home)
        note = work / rel
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(
            f"---\ntitle: {note.stem}\ntype: note\nstatus: active\nlast_updated: 2026-10-16\n---\n\n"
            f"# {note.stem}\n\nA note that arrived after the server started: {word}.\n",
            encoding="utf-8",
        )
        _git(self.home, "add", "-A", cwd=work)
        _git(self.home, "commit", "-q", "-m", f"docs: add {note.stem}", cwd=work)
        _git(self.home, "push", "-q", "origin", self.branch, cwd=work)

    def indexed(self) -> set[str]:
        return set(self.rt.indexer.store.file_hashes())


@pytest.fixture
def served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Iterator[_Served]:
    """The ``alpha`` fixture vault served from a clone of a bare repository on ``branch``."""
    branch: str = getattr(request, "param", "main")
    home = tmp_path / "home"
    home.mkdir()
    bare = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    clone = tmp_path / "clone"
    _git(home, "init", "-q", "--bare", "-b", branch, str(bare), cwd=tmp_path)
    shutil.copytree(ALPHA_VAULT, seed)
    _git(home, "init", "-q", "-b", branch, cwd=seed)
    _git(home, "add", "-A", cwd=seed)
    _git(home, "commit", "-q", "-m", "docs: seed", cwd=seed)
    _git(home, "push", "-q", str(bare), branch, cwd=seed)
    _git(home, "clone", "-q", "--branch", branch, str(bare), str(clone), cwd=tmp_path)

    monkeypatch.setenv("VAULT_PATH", str(clone))
    monkeypatch.setenv("VAULT_REMOTE", str(bare))
    monkeypatch.setenv("VAULT_BRANCH", branch)
    monkeypatch.setenv("KNOWLEDGE_INDEX_DIR", str(tmp_path / "index"))
    monkeypatch.setenv("MCP_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("MCP_API_KEY", ADMIN_BEARER)
    monkeypatch.setenv("MCP_TRANSPORT", "http")
    settings.reset_current()
    reg = VaultRegistry.from_settings(settings.current(), sync_factory=server.vault_sync_for)
    monkeypatch.setattr(server, "_registry", reg)
    rt = reg.default
    assert rt.spec.git_branch == branch and rt.spec.repo == str(bare)
    rt.indexer.index(force=False, target="vault")
    rt.index_state["ready"] = True

    probe = _GitProbe(home)
    monkeypatch.setattr(gitsync, "_run_git", probe)
    state = _Served(asgi=None, rt=rt, bare=bare, home=home, branch=branch, git=probe)
    index = rt.indexer.index

    def _counted(*args: Any, **kwargs: Any) -> Any:
        state.index_runs.append(bool(kwargs.get("force", False)))
        return index(*args, **kwargs)

    monkeypatch.setattr(rt.indexer, "index", _counted)
    state.asgi = server.http_app(FastMCP("webhook-deliveries"))
    try:
        yield state
    finally:
        probe.release.set()
        reg.close()


# ---------------------------------------------------------------------------
# Deliveries
# ---------------------------------------------------------------------------


def _client(asgi: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=asgi), base_url="http://webhook.test", timeout=60)


def _push(ref: str, *, deleted: bool = False) -> dict[str, Any]:
    """The fields of a GitHub ``push`` payload that say which ref moved and how."""
    return {
        "ref": ref,
        "before": "1" * 40,
        "after": ZERO_SHA if deleted else "2" * 40,
        "created": False,
        "deleted": deleted,
        "forced": False,
        "repository": {"full_name": "example/vault"},
    }


def _signed(payload: dict[str, Any]) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(payload).encode()
    digest = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return body, {
        "Content-Type": "application/json",
        "X-GitHub-Event": "push",
        "X-Hub-Signature-256": f"sha256={digest}",
    }


async def _deliver(client: httpx.AsyncClient, payload: dict[str, Any]) -> httpx.Response:
    body, headers = _signed(payload)
    return await client.post(URL, content=body, headers=headers)


def _deliver_once(served: _Served, payload: dict[str, Any]) -> httpx.Response:
    async def _go() -> httpx.Response:
        async with _client(served.asgi) as client:
            return await _deliver(client, payload)

    return asyncio.run(_go())


# ---------------------------------------------------------------------------
# (1) Concurrent deliveries: one pull at a time per vault, the last commit wins
# ---------------------------------------------------------------------------


def test_concurrent_deliveries_pull_one_at_a_time_and_end_on_the_latest_commit(served: _Served) -> None:
    ref = f"refs/heads/{served.branch}"
    served.commit("Notes/First Merge.md", "albatross")
    served.git.hold_first_pull()

    async def scenario() -> list[httpx.Response]:
        async with _client(served.asgi) as client:
            first = asyncio.create_task(_deliver(client, _push(ref)))
            assert await asyncio.to_thread(served.git.first_pull_held.wait, 10), "the first pull never ran"
            # Another merge lands while the first run is still in flight, and
            # GitHub delivers its push (twice, as a redelivery would).
            served.commit("Notes/Second Merge.md", "bittern")
            later = [await asyncio.wait_for(_deliver(client, _push(ref)), 30) for _ in range(2)]
            served.git.release.set()
            return [await asyncio.wait_for(first, 60), *later]

    responses = asyncio.run(scenario())

    assert [r.status_code for r in responses] == [202, 202, 202]
    assert all(r.json()["status"] == "accepted" for r in responses)
    assert served.git.max_in_flight == 1, "two git pulls ran in the same checkout at once"
    assert served.git.pull_rcs == [0, 0], "one run plus a single follow-up for both mid-run deliveries"
    assert served.index_runs == [False, False]
    assert {"Notes/First Merge.md", "Notes/Second Merge.md"} <= served.indexed(), "the index missed the latest commit"


def test_the_reindex_tool_waits_for_a_webhook_run_on_the_same_checkout(served: _Served) -> None:
    """``reindex(pull=true)`` pulls the same clone; it never runs beside a webhook run."""
    served.commit("Notes/First Merge.md", "albatross")
    served.git.hold_first_pull()

    async def scenario() -> str:
        async with _client(served.asgi) as client:
            delivery = asyncio.create_task(_deliver(client, _push(f"refs/heads/{served.branch}")))
            assert await asyncio.to_thread(served.git.first_pull_held.wait, 10), "the webhook pull never ran"
            tool = asyncio.create_task(asyncio.to_thread(server._reindex_blocking, False, True))
            # Room for the tool to start its own pull beside the held one, if it can.
            await asyncio.to_thread(served.git.second_pull_entered.wait, 1.0)
            served.git.release.set()
            assert (await asyncio.wait_for(delivery, 60)).status_code == 202
            _stats, outcome, _validation = await asyncio.wait_for(tool, 60)
            return str(outcome)

    outcome = asyncio.run(scenario())

    assert served.git.max_in_flight == 1, "the tool pulled the checkout while the webhook run held it"
    assert served.git.pull_rcs == [0, 0]
    assert outcome == ports.SYNC_UP_TO_DATE, "the tool pulls after the webhook run, which already fetched the merge"


# ---------------------------------------------------------------------------
# (2) A push to another ref is ignored: no pull, no reindex
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("served", "ref"),
    [
        pytest.param("main", "refs/heads/fix/typo", id="another-branch"),
        pytest.param("main", "refs/heads/main-backup", id="branch-sharing-the-prefix"),
        pytest.param("main", "refs/tags/v1.0.0", id="tag"),
        pytest.param("trunk", "refs/heads/main", id="main-when-the-vault-serves-trunk"),
    ],
    indirect=["served"],
)
def test_a_push_to_another_ref_is_ignored(served: _Served, ref: str) -> None:
    response = _deliver_once(served, _push(ref))

    assert response.status_code == 200
    assert response.json() == {
        "status": "ignored",
        "mode": "signature",
        "reason": "not_served_branch",
        "ref": ref,
        "branch": served.branch,
        "target": "vault",
    }
    assert served.git.calls == [], "an ignored delivery runs no git command"
    assert served.index_runs == [], "an ignored delivery runs no reindex"


# ---------------------------------------------------------------------------
# (3) A ref deletion is ignored
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(_push("refs/heads/fix/webhook-race", deleted=True), id="merged-branch-deleted"),
        pytest.param(_push("refs/heads/main", deleted=True), id="served-branch-deleted"),
        # GitLab and Gitea send no ``deleted``: the zero ``after`` is the signal.
        pytest.param({"ref": "refs/heads/main", "before": "1" * 40, "after": ZERO_SHA}, id="zero-after-only"),
    ],
)
def test_a_ref_deletion_is_ignored(served: _Served, payload: dict[str, Any]) -> None:
    response = _deliver_once(served, payload)

    assert response.status_code == 200
    assert response.json() == {
        "status": "ignored",
        "mode": "signature",
        "reason": "ref_deleted",
        "ref": payload["ref"],
        "branch": "main",
        "target": "vault",
    }
    assert served.git.calls == []
    assert served.index_runs == []


# ---------------------------------------------------------------------------
# (4) A push to the served branch still pulls and reindexes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("served", ["main", "trunk"], indirect=True)
def test_a_push_to_the_served_branch_pulls_and_reindexes(served: _Served) -> None:
    served.commit("Notes/Late Addition.md", "cormorant")

    response = _deliver_once(served, _push(f"refs/heads/{served.branch}"))

    assert response.status_code == 202
    body = response.json()
    assert (body["status"], body["mode"], body["target"], body["force"]) == ("accepted", "signature", "vault", False)
    assert served.git.pull_rcs == [0]
    assert served.index_runs == [False]
    assert "Notes/Late Addition.md" in served.indexed()


def test_a_delivery_that_names_no_ref_still_reindexes(served: _Served) -> None:
    """GitHub's ``ping`` (and any test delivery without a ``ref``) keeps refreshing the vault."""
    served.commit("Notes/Late Addition.md", "cormorant")

    response = _deliver_once(served, {"zen": "Keep it logically awesome.", "hook_id": 1})

    assert response.status_code == 202
    assert response.json()["status"] == "accepted"
    assert served.git.pull_rcs == [0]
    assert "Notes/Late Addition.md" in served.indexed()


def test_a_manual_bearer_reindexes_whatever_the_body_names(served: _Served) -> None:
    """A human with the admin bearer asked for a run: the body is not a push to filter."""
    served.commit("Notes/Late Addition.md", "cormorant")
    body = json.dumps(_push("refs/heads/fix/typo")).encode()

    async def _go() -> httpx.Response:
        async with _client(served.asgi) as client:
            return await client.post(URL, content=body, headers={"Authorization": f"Bearer {ADMIN_BEARER}"})

    response = asyncio.run(_go())

    assert response.status_code == 202
    assert response.json()["mode"] == "manual"
    assert served.git.pull_rcs == [0]
    assert "Notes/Late Addition.md" in served.indexed()


# ---------------------------------------------------------------------------
# The two rules on their own
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"payload=not-json", id="form-encoded"),
        pytest.param(b"\xff\xfe", id="not-utf8"),
        pytest.param(b'["refs/heads/fix/typo"]', id="not-an-object"),
        pytest.param(b'{"ref": ""}', id="empty-ref"),
        pytest.param(b'{"ref": 7}', id="ref-not-a-string"),
        pytest.param(b'{"push": {"changes": []}}', id="bitbucket-no-changes"),
        pytest.param(b'{"push": {"changes": "main"}}', id="bitbucket-changes-not-a-list"),
        pytest.param(b'{"push": {"changes": ["main"]}}', id="bitbucket-change-not-an-object"),
        pytest.param(b'{"push": {"changes": [{"new": {"name": "fix/typo"}}]}}', id="bitbucket-new-without-type"),
        pytest.param(
            b'{"push": {"changes": [{"new": {"type": "bookmark", "name": "main"}}]}}', id="bitbucket-unknown-type"
        ),
        pytest.param(
            b'{"push": {"changes": [{"new": null, "old": null}, {"new": {"type": "branch"}}]}}',
            id="bitbucket-one-change-unreadable",
        ),
    ],
)
def test_a_body_that_names_no_judgeable_ref_is_never_ignored(body: bytes) -> None:
    assert webhook.ignored_push(body, "main") is None


def test_a_failing_pass_frees_the_vault_for_the_next_request() -> None:
    refresh = webhook.SerialRefresh()

    def boom(force: bool) -> None:
        raise RuntimeError("index exploded")

    with pytest.raises(RuntimeError):
        refresh.run(boom)
    ran: list[bool] = []

    assert refresh.run(ran.append) is True
    assert ran == [False]


def test_requests_during_a_pass_fold_into_one_follow_up_that_keeps_force() -> None:
    refresh = webhook.SerialRefresh()
    entered, release = threading.Event(), threading.Event()
    passes: list[bool] = []

    def job(force: bool) -> None:
        passes.append(force)
        if len(passes) == 1:
            entered.set()
            release.wait(10)

    first = threading.Thread(target=refresh.run, args=(job, False))
    first.start()
    assert entered.wait(10)
    coalesced = [refresh.run(job, False), refresh.run(job, True), refresh.run(job, False)]
    release.set()
    first.join(10)

    assert coalesced == [False, False, False]
    assert passes == [False, True], "one follow-up pass, forced because one request asked for it"


# ---------------------------------------------------------------------------
# (5) Bitbucket Cloud ``repo:push``: judged by ``push.changes``
# ---------------------------------------------------------------------------


def _bb_state(kind: str, name: str, commit: str) -> dict[str, Any]:
    """One reference state of a Bitbucket ``repo:push`` change (``new`` or ``old``), as Bitbucket Cloud sends it."""
    plural = "branches" if kind == "branch" else "tags"
    return {
        "type": kind,
        "name": name,
        "target": {
            "type": "commit",
            "hash": commit,
            "message": "docs: a change\n",
            "date": "2026-10-30T04:35:56+00:00",
            "parents": [{"type": "commit", "hash": "9" * 40}],
            "links": {"html": {"href": f"https://bitbucket.org/example/vault/commits/{commit}"}},
        },
        "links": {
            "self": {"href": f"https://api.bitbucket.org/2.0/repositories/example/vault/refs/{plural}/{name}"},
            "html": {"href": f"https://bitbucket.org/example/vault/branch/{name}"},
        },
    }


def _bb_change(kind: str, name: str, *, deleted: bool = False, created: bool = False) -> dict[str, Any]:
    """One entry of ``push.changes``: a deletion has a null ``new``, a creation a null ``old``."""
    return {
        "new": None if deleted else _bb_state(kind, name, "2" * 40),
        "old": None if created else _bb_state(kind, name, "1" * 40),
        "created": created,
        "closed": deleted,
        "forced": False,
        "truncated": False,
        "commits": [] if deleted else [{"type": "commit", "hash": "2" * 40, "message": "docs: a change\n"}],
        "links": {"html": {"href": "https://bitbucket.org/example/vault/branches/compare/2..1"}},
    }


def _bb_push(*changes: dict[str, Any]) -> dict[str, Any]:
    """A Bitbucket Cloud ``repo:push`` payload: no top-level ``ref``, every updated reference in ``push.changes``."""
    return {
        "actor": {"type": "user", "display_name": "Vault Owner", "uuid": "{00000000-0000-0000-0000-000000000001}"},
        "repository": {"type": "repository", "full_name": "example/vault", "name": "vault"},
        "push": {"changes": list(changes)},
    }


def _bb_deliver(served: _Served, payload: dict[str, Any]) -> httpx.Response:
    """Deliver ``payload`` with the headers Bitbucket Cloud sends: ``X-Hub-Signature`` carries the ``sha256=`` digest."""
    body = json.dumps(payload).encode()
    digest = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Bitbucket-Webhooks/2.0",
        "X-Event-Key": "repo:push",
        "X-Hook-UUID": "{00000000-0000-0000-0000-000000000002}",
        "X-Request-UUID": str(uuid.uuid4()),
        "X-Attempt-Number": "1",
        "X-Hub-Signature": f"sha256={digest}",
    }

    async def _go() -> httpx.Response:
        async with _client(served.asgi) as client:
            return await client.post(URL, content=body, headers=headers)

    return asyncio.run(_go())


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(_bb_push(_bb_change("branch", "main")), id="push-to-main"),
        pytest.param(
            _bb_push(
                _bb_change("branch", "docs/notes", created=True),
                _bb_change("branch", "main"),
                _bb_change("tag", "v1.0.0", created=True),
            ),
            id="multi-change-containing-main",
        ),
        pytest.param(_bb_push(_bb_change("branch", "main") | {"forced": True}), id="forced-push-to-main"),
    ],
)
def test_a_bitbucket_push_that_moves_the_served_branch_pulls_and_reindexes(
    served: _Served, payload: dict[str, Any]
) -> None:
    served.commit("Notes/Late Addition.md", "cormorant")

    response = _bb_deliver(served, payload)

    assert response.status_code == 202
    assert response.json()["status"] == "accepted"
    assert served.git.pull_rcs == [0]
    assert served.index_runs == [False]
    assert "Notes/Late Addition.md" in served.indexed()


@pytest.mark.parametrize(
    ("payload", "reason", "ref"),
    [
        pytest.param(
            _bb_push(_bb_change("branch", "docs/allow-web-merges")),
            "not_served_branch",
            "refs/heads/docs/allow-web-merges",
            id="push-to-another-branch",
        ),
        pytest.param(
            _bb_push(_bb_change("branch", "main-backup", created=True)),
            "not_served_branch",
            "refs/heads/main-backup",
            id="branch-sharing-the-prefix",
        ),
        pytest.param(
            _bb_push(_bb_change("tag", "v1.0.0", created=True)), "not_served_branch", "refs/tags/v1.0.0", id="tag"
        ),
        pytest.param(
            _bb_push(_bb_change("annotated_tag", "v1.1.0", created=True)),
            "not_served_branch",
            "refs/tags/v1.1.0",
            id="annotated-tag",
        ),
        pytest.param(
            _bb_push(_bb_change("branch", "docs/allow-web-merges", deleted=True)),
            "ref_deleted",
            "refs/heads/docs/allow-web-merges",
            id="merged-branch-deleted",
        ),
        pytest.param(
            _bb_push(_bb_change("branch", "main", deleted=True)),
            "ref_deleted",
            "refs/heads/main",
            id="served-branch-deleted",
        ),
        pytest.param(
            _bb_push(_bb_change("branch", "fix/typo"), _bb_change("branch", "main", deleted=True)),
            "ref_deleted",
            "refs/heads/main",
            id="served-branch-deleted-beside-another-push",
        ),
        pytest.param(
            _bb_push(_bb_change("branch", "fix/typo"), _bb_change("tag", "v2", created=True)),
            "not_served_branch",
            "refs/heads/fix/typo",
            id="multi-change-without-main",
        ),
        pytest.param(
            _bb_push(_bb_change("tag", "v0.9", deleted=True)), "ref_deleted", "refs/tags/v0.9", id="tag-deleted"
        ),
    ],
)
def test_a_bitbucket_push_that_does_not_move_the_served_branch_is_ignored(
    served: _Served, payload: dict[str, Any], reason: str, ref: str
) -> None:
    response = _bb_deliver(served, payload)

    assert response.status_code == 200
    assert response.json() == {
        "status": "ignored",
        "mode": "signature",
        "reason": reason,
        "ref": ref,
        "branch": "main",
        "target": "vault",
    }
    assert served.git.calls == [], "an ignored delivery runs no git command"
    assert served.index_runs == [], "an ignored delivery runs no reindex"


@pytest.mark.parametrize("served", ["trunk"], indirect=True)
def test_a_bitbucket_push_to_main_is_ignored_when_the_vault_serves_trunk(served: _Served) -> None:
    response = _bb_deliver(served, _bb_push(_bb_change("branch", "main")))

    assert response.status_code == 200
    assert (response.json()["reason"], response.json()["ref"]) == ("not_served_branch", "refs/heads/main")
    assert served.git.calls == []


def test_a_bitbucket_web_merge_that_closes_the_source_branch_keeps_the_fast_forward(served: _Served) -> None:
    """A web merge sends two deliveries: the served branch moves, then the source branch is deleted.

    Only the first one may run a sync; the deletion is ignored, so ``/health``
    keeps reporting the fast-forward the merge brought.
    """
    before = gitsync._head(str(served.rt.vault_path))
    served.commit("Notes/Merged.md", "egret")

    merged = _bb_deliver(served, _bb_push(_bb_change("branch", "main")))
    closed = _bb_deliver(served, _bb_push(_bb_change("branch", "docs/allow-web-merges", deleted=True)))

    assert (merged.status_code, closed.status_code) == (202, 200)
    assert closed.json()["reason"] == "ref_deleted"
    assert served.git.pull_rcs == [0], "the deletion runs no second sync"
    state = gitsync.sync_state(served.rt.vault_path)
    assert state["last_sync_result"] == gitsync.RESULT_FAST_FORWARD
    assert state["last_change_result"] == gitsync.RESULT_FAST_FORWARD
    assert state["last_change_at"] == state["last_sync_at"]
    assert state["vault_commit"] != (before or "")[:12]
    assert "Notes/Merged.md" in served.indexed()
