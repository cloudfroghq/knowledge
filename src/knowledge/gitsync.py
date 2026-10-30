"""Git synchronisation for a vault that declares a repository.

A vault is a directory; whether it is also a git checkout is a property of how
it is hosted, not something the server assumes. Only a vault that declares a
repository (``repo`` in ``vaults.yaml``, or ``VAULT_REMOTE`` for a
``VAULT_PATH`` deployment) gets a :class:`GitVaultSync` as its sync. Every
other vault runs no git command at all. The caller says which vault and which
branch; nothing here reads configuration.

Keeping the checkout fresh means converging it on the head of the served
branch on ``origin``, whatever happened upstream:

1. ``git fetch --prune origin``. When it fails (authentication, network) the
   checkout is left exactly as it is, the failure is logged as an error and
   recorded, and the vault keeps being served from what is on disk.
2. When ``HEAD`` is an ancestor of ``origin/<branch>`` (the normal push), a
   ``git merge --ff-only`` brings the new commits in.
3. Otherwise (the remote branch was force-pushed or rewritten, or a
   fast-forward is refused) the checkout is realigned with
   ``git checkout -f -B <branch> origin/<branch>``, the command
   ``infra/bootstrap-vault.sh`` adopts an existing clone with on every
   container start, and a warning names the old and the new commit. Untracked
   files are left in place, as the bootstrap leaves them.

How the last refresh of each vault ended is kept in process
(:func:`last_sync`), and so is the last refresh that moved its checkout
(:func:`last_change`); ``GET /health`` reports both through
:func:`sync_state`, so a vault that stopped following its remote is visible
without reading the logs, and a merge that was fetched stays visible after a
later delivery finds nothing new.
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import cache
from pathlib import Path
from typing import Any

from knowledge.ports import (
    SYNC_FAILED,
    SYNC_PULLED,
    SYNC_SKIPPED_NO_REPO,
    SYNC_SKIPPED_PULL_FALSE,
    SYNC_UP_TO_DATE,
)

log = logging.getLogger(__name__)

#: The remote every vault clone follows: the bootstrap clones and adopts it under this name.
REMOTE = "origin"

#: How a refresh that ran git ended, as ``/health`` reports it in ``last_sync_result``.
RESULT_FAST_FORWARD = "fast_forward"
RESULT_UP_TO_DATE = "up_to_date"
RESULT_REALIGNED = "realigned"
RESULT_FAILED = "failed"
SYNC_RESULTS: tuple[str, ...] = (RESULT_FAST_FORWARD, RESULT_UP_TO_DATE, RESULT_REALIGNED, RESULT_FAILED)
#: The results that moved the checkout, as ``/health`` reports them in ``last_change_result``.
CHANGE_RESULTS: tuple[str, ...] = (RESULT_FAST_FORWARD, RESULT_REALIGNED)

# The tool contract knows five outcomes (``ports.SYNC_OUTCOMES``); a realigned
# checkout brought new content just like a fast-forward did.
_OUTCOME_OF: dict[str, str] = {
    RESULT_FAST_FORWARD: SYNC_PULLED,
    RESULT_REALIGNED: SYNC_PULLED,
    RESULT_UP_TO_DATE: SYNC_UP_TO_DATE,
    RESULT_FAILED: SYNC_FAILED,
}

_MAX_ERROR_CHARS = 300


def _run_git(args: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except FileNotFoundError:
        return 127, "", "git binary not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"git {' '.join(args[:2])} timed out"
    except Exception as exc:  # pragma: no cover - defensive
        return 1, "", f"unexpected error running git: {exc!r}"


# Credentials embedded in a remote URL never reach a log line or /health: the
# same two rules as ``redact`` in infra/bootstrap-vault.sh.
_USERINFO_WITH_PASSWORD = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)[^/@: ]*:[^/@ ]*@")
_HTTP_USERINFO = re.compile(r"(https?://)[^/@ ]*@")


def redact(text: str) -> str:
    """``text`` with the user information of every URL in it replaced by ``***``."""
    return _HTTP_USERINFO.sub(r"\1***@", _USERINFO_WITH_PASSWORD.sub(r"\1***@", text))


def _error_message(what: str, rc: int, stderr: str) -> str:
    """A one-line, redacted, bounded description of a failed git step."""
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    detail = redact(lines[-1]) if lines else "no output"
    message = f"{what} exited rc={rc}: {detail}"
    return message if len(message) <= _MAX_ERROR_CHARS else message[: _MAX_ERROR_CHARS - 3] + "..."


def _log_output(stdout: str, stderr: str) -> None:
    for stream in (stdout, stderr):
        if stream:
            log.info("[gitsync] %s", redact(stream))


def is_git_worktree(path: str) -> bool:
    rc, out, _ = _run_git(["-C", path, "rev-parse", "--is-inside-work-tree"], timeout=15)
    return rc == 0 and out == "true"


def _head(vault: str) -> str | None:
    """The commit ``HEAD`` points at in the checkout holding ``vault``; ``None`` if unknown."""
    rc, out, _ = _run_git(["-C", vault, "rev-parse", "HEAD"], timeout=15)
    return out if rc == 0 and out else None


def _current_branch(vault: str) -> str | None:
    """The branch checked out in the checkout holding ``vault``; ``None`` on a detached ``HEAD``."""
    rc, out, _ = _run_git(["-C", vault, "symbolic-ref", "--quiet", "--short", "HEAD"], timeout=15)
    return out if rc == 0 and out else None


def _short(commit: str | None) -> str:
    return commit[:12] if commit else "unknown"


# ---------------------------------------------------------------------------
# The state of the last refresh, per vault
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SyncStatus:
    """How one refresh of a vault ended."""

    #: When the refresh ended, ISO 8601 in UTC.
    at: str
    #: One of :data:`SYNC_RESULTS`.
    result: str
    #: What failed, redacted and on one line; ``None`` unless ``result`` is ``failed``.
    error: str | None = None
    #: ``HEAD`` before the refresh and after it.
    before: str | None = None
    after: str | None = None


_status_lock = threading.Lock()
_status: dict[str, SyncStatus] = {}
# The last status whose result is one of CHANGE_RESULTS, per vault.
_changes: dict[str, SyncStatus] = {}


def _key(vault_path: Path | str) -> str:
    return str(Path(vault_path).resolve())


def _record(vault: str, status: SyncStatus) -> SyncStatus:
    key = _key(vault)
    with _status_lock:
        _status[key] = status
        if status.result in CHANGE_RESULTS:
            _changes[key] = status
    return status


def last_sync(vault_path: Path | str) -> SyncStatus | None:
    """How the last refresh of ``vault_path`` in this process ended; ``None`` before the first one."""
    with _status_lock:
        return _status.get(_key(vault_path))


def last_change(vault_path: Path | str) -> SyncStatus | None:
    """The last refresh of ``vault_path`` in this process that moved its checkout; ``None`` before the first one.

    Its result is one of :data:`CHANGE_RESULTS`: an ``up_to_date`` or
    ``failed`` refresh never replaces it.
    """
    with _status_lock:
        return _changes.get(_key(vault_path))


def sync_state(vault_path: Path | str) -> dict[str, Any]:
    """The sync fields ``/health`` reports for ``vault_path``.

    ``vault_commit`` is the abbreviated ``HEAD`` the checkout holds now, read on
    every call so it is right from the first request after a start, before any
    refresh ran; ``None`` outside a git checkout. The ``last_sync_*`` fields
    describe the last refresh in this process and are ``None`` until one ran.
    The ``last_change_*`` fields describe the last refresh that moved the
    checkout (``fast_forward`` or ``realigned``) and are ``None`` until one did.
    """
    rc, out, _ = _run_git(["-C", str(vault_path), "rev-parse", "--short=12", "HEAD"], timeout=15)
    status = last_sync(vault_path)
    change = last_change(vault_path)
    return {
        "vault_commit": out if rc == 0 and out else None,
        "last_sync_at": status.at if status else None,
        "last_sync_result": status.result if status else None,
        "last_sync_error": status.error if status else None,
        "last_change_at": change.at if change else None,
        "last_change_result": change.result if change else None,
    }


# ---------------------------------------------------------------------------
# Converging a checkout on its remote branch
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _failed(vault: str, before: str | None, message: str) -> SyncStatus:
    log.error("[gitsync] %s: %s; serving the vault from what is on disk", vault, message)
    return _record(vault, SyncStatus(at=_now(), result=RESULT_FAILED, error=message, before=before, after=before))


def converge(vault_path: Path | str, branch: str | None = None) -> SyncStatus:
    """Bring the checkout holding ``vault_path`` to the head of ``origin/<branch>`` and record how it ended.

    ``branch`` is the served branch; without one, the branch checked out now.
    The caller has checked that ``vault_path`` sits inside a git worktree.
    """
    vault = str(vault_path)
    before = _head(vault)
    target = branch or _current_branch(vault)
    if not target:
        return _failed(vault, before, "HEAD is detached and no branch is configured to follow")

    rc, stdout, stderr = _run_git(["-C", vault, "fetch", "--prune", REMOTE])
    _log_output(stdout, stderr)
    if rc != 0:
        return _failed(vault, before, _error_message("git fetch", rc, stderr))

    remote_ref = f"{REMOTE}/{target}"
    rc, remote_head, stderr = _run_git(
        ["-C", vault, "rev-parse", "--verify", "--quiet", f"{remote_ref}^{{commit}}"], timeout=15
    )
    if rc != 0 or not remote_head:
        return _failed(vault, before, f"{remote_ref} does not exist after the fetch")

    on_branch = _current_branch(vault) == target
    if on_branch and before == remote_head:
        return _record(vault, SyncStatus(at=_now(), result=RESULT_UP_TO_DATE, before=before, after=before))

    if on_branch and before is not None:
        rc, _, _ = _run_git(["-C", vault, "merge-base", "--is-ancestor", before, remote_head], timeout=15)
        if rc == 0:
            rc, stdout, stderr = _run_git(["-C", vault, "merge", "--ff-only", remote_ref])
            _log_output(stdout, stderr)
            if rc == 0:
                after = _head(vault)
                log.info("[gitsync] %s fast-forwarded %s -> %s", vault, _short(before), _short(after))
                return _record(
                    vault, SyncStatus(at=_now(), result=RESULT_FAST_FORWARD, before=before, after=after)
                )
            log.warning("[gitsync] %s: %s; realigning", vault, _error_message("git merge --ff-only", rc, stderr))

    # Same command as the bootstrap's adoption of an existing clone.
    rc, stdout, stderr = _run_git(["-C", vault, "checkout", "-f", "-B", target, remote_ref])
    _log_output(stdout, stderr)
    if rc != 0:
        return _failed(vault, before, _error_message("git checkout -f -B", rc, stderr))
    after = _head(vault)
    log.warning(
        "[gitsync] %s could not fast-forward to %s (history rewritten or diverged, or another "
        "branch checked out); realigned %s -> %s",
        vault,
        remote_ref,
        _short(before),
        _short(after),
    )
    return _record(vault, SyncStatus(at=_now(), result=RESULT_REALIGNED, before=before, after=after))


def pull_vault_outcome(vault_path: Path, *, enabled: bool = True, branch: str | None = None) -> str:
    """Converge the repo containing ``vault_path`` on its remote branch and say how it ended.

    One of :data:`~knowledge.ports.SYNC_OUTCOMES`:

    - ``skipped_pull_false`` with ``enabled=False``: nothing runs, not even a
      ``rev-parse``;
    - ``skipped_no_repo`` when the vault sits outside any git worktree (or in
      a bind-mounted subdirectory without its ``.git``). Not an error:
      indexing works with whatever files are on disk;
    - ``failed`` when the fetch or the realignment fails; the checkout is
      untouched by a failed fetch and the vault keeps being served from what
      is on disk;
    - ``pulled`` when the checkout was fast-forwarded or realigned,
      ``up_to_date`` when it already held the remote head.

    The finer result (:data:`SYNC_RESULTS`) is recorded for :func:`sync_state`.
    """
    if not enabled:
        return SYNC_SKIPPED_PULL_FALSE
    vault = str(vault_path)
    if not is_git_worktree(vault):
        log.info("vault %s is not inside a git worktree — skipping pull", vault)
        return SYNC_SKIPPED_NO_REPO
    return _OUTCOME_OF[converge(vault, branch).result]


def pull_vault(vault_path: Path, *, enabled: bool = True, branch: str | None = None) -> bool:
    """Converge the repo containing ``vault_path`` on its remote branch. Returns True on success.

    Success is a refresh that ended on the remote head, whether or not it
    brought commits; :func:`pull_vault_outcome` tells the cases apart. With
    ``enabled=False`` nothing runs and the answer is ``False``, as it is for a
    vault outside a git worktree and for a failed refresh.
    """
    return pull_vault_outcome(vault_path, enabled=enabled, branch=branch) in (SYNC_PULLED, SYNC_UP_TO_DATE)


class _GitVaultSync:
    """The git refresh as a :class:`~knowledge.ports.ReportingVaultSync`, following ``branch``."""

    def __init__(self, branch: str | None = None) -> None:
        self.branch = branch

    def __repr__(self) -> str:
        return "GitVaultSync" if self.branch is None else f"GitVaultSync(branch={self.branch!r})"

    def __call__(self, root: Path, /) -> bool:
        return pull_vault(root, branch=self.branch)

    def outcome(self, root: Path, /) -> str:
        return pull_vault_outcome(root, branch=self.branch)


#: The vault sync that follows whichever branch the checkout has checked out.
GitVaultSync = _GitVaultSync()


@cache
def git_vault_sync(branch: str) -> _GitVaultSync:
    """The vault sync of a vault that declares a repository and serves ``branch``; one per branch."""
    return _GitVaultSync(branch)
