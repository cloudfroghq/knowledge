"""Write client of the memory server over HTTP, standard library only.

The ``memory-bridge`` extension publishes what a vault knows about its
project to the memory server that runs on the person's own machine: the
project card (``PUT /projects/{slug}``), the runbook index
(``POST /projects/{slug}/runbooks/sync`` with ``source: vault-fs``, so memory
scans the local checkout itself) and, for the ``jira`` extension, tasks
(``POST /projects/{slug}/tasks``, upserted by ``jira_key``). It also wraps the
memory command line for the promotion bridge (``memory project <slug>
promote list|stamp``), which reads and stamps observations in memory and
writes nothing into the vault.

Every request carries ``X-Memory-Client: cloudfrog-knowledge/<contract>``,
``Content-Type: application/json`` and ``Accept: application/json``, plus
``Authorization: Bearer <MEMORY_HTTP_TOKEN>`` whenever that variable is set.
The rules that hold whatever the memory server accepts:

* Loopback only. A ``KNOWLEDGE_MEMORY_URL`` that is not plain ``http`` on
  ``127.0.0.1``, ``localhost`` or ``::1`` refuses every write
  (``E_MEMORY_URL_NOT_LOOPBACK``). The opener ignores the proxy variables and
  follows no redirect, so the bearer only ever reaches the host the URL names.
* A bearer by default. Without ``MEMORY_HTTP_TOKEN`` nothing is written
  (``E_MEMORY_TOKEN_MISSING``) unless ``KNOWLEDGE_MEMORY_REQUIRE_TOKEN=0``,
  for a memory server on the loopback that runs without one.
* Short timeouts: five seconds for a card or a task, thirty for a runbook
  scan, which reads the whole ``Runbooks/`` tree on the memory side.
* Errors carry a stable code (:class:`~knowledge.errors.MemoryWriteFailed`):
  ``E_MEMORY_UNAVAILABLE`` when the server cannot be reached,
  ``E_MEMORY_WRITE_FAILED`` when it answers with an error, with the HTTP
  status and the memory server's own error code (``payload_too_large`` for a
  413). The body the server sends back is never echoed, and neither the
  bearer nor the request URL reaches a message or a log line.
"""

from __future__ import annotations

import http.client
import json
import logging
import re
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from knowledge import contract, errors
from knowledge.errors import KnowledgeToolError, MemoryWriteFailed
from knowledge.settings import MemorySettings, VaultSpec, header_token_valid, is_loopback_http_url

log = logging.getLogger(__name__)

#: Seconds a project card or a task write may take, connection included.
TIMEOUT_SECONDS = 5.0
#: Seconds a runbook sync may take: memory scans the vault's ``Runbooks/`` tree.
RUNBOOKS_TIMEOUT_SECONDS = 30.0
#: Seconds the memory command line may take for one ``promote`` call.
PROMOTE_TIMEOUT_SECONDS = 30.0

#: ``X-Memory-Client`` value: who is writing, and under which tool contract.
CLIENT_ID = f"cloudfrog-knowledge/{contract.CONTRACT_VERSION}"

#: The folder memory scans for runbooks, at the root of the vault.
RUNBOOKS_DIR = "Runbooks"

#: A memory project slug, as the memory server validates it.
PROJECT_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
#: A Jira project key, as ``integrations.jira.projects`` lists it.
JIRA_PROJECT_PATTERN = re.compile(r"^[A-Z][A-Z0-9]+$")
#: An observation id the memory command line accepts: a number or ``obs-<hex>``.
OBSERVATION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
#: The shape of the error codes the memory server answers with.
_MEMORY_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
#: Bytes of an error body read to find its ``code``; the rest is discarded.
_MAX_ERROR_BODY = 64 * 1024
#: Statuses with which the memory server refuses one task on its own merits
#: (``invalid_enum``, ``missing_field``, ``task_key_conflict``, an invalid
#: ``knowledge_ref``): the task is skipped, the run goes on.
TASK_SKIP_STATUSES = frozenset({400, 409, 422})
#: Codes that mean no write could be attempted, as opposed to one that failed;
#: they read as ``skipped`` only when no request reached the server.
NOT_CONFIGURED = frozenset(
    {errors.E_MEMORY_TOKEN_MISSING, errors.E_MEMORY_URL_NOT_LOOPBACK, errors.E_MEMORY_PROJECT_MISSING}
)

Opener = Callable[..., Any]
Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: a write never follows the server elsewhere with its bearer."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        return None


#: No proxies (an empty ``ProxyHandler`` replaces the one built from the
#: environment) and no redirects: the request reaches the loopback host the
#: URL names, or nothing.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


#: An observation type ``promote list --types`` passes to the memory command.
OBSERVATION_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")


def _check_project(project: str) -> str:
    if not PROJECT_PATTERN.fullmatch(project or ""):
        raise MemoryWriteFailed(
            errors.E_MEMORY_PROJECT_MISSING,
            f"memory.project {project!r} is not a memory project slug (lowercase letters, digits and dashes)",
        )
    return project


class MemoryWriter:
    """The three writes of the memory-bridge, on one loopback memory server."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        require_token: bool = True,
        opener: Opener = _OPENER.open,
        timeout: float = TIMEOUT_SECONDS,
        runbooks_timeout: float = RUNBOOKS_TIMEOUT_SECONDS,
    ) -> None:
        if require_token and not token:
            raise MemoryWriteFailed(
                errors.E_MEMORY_TOKEN_MISSING,
                "MEMORY_HTTP_TOKEN is not set; export it, or set KNOWLEDGE_MEMORY_REQUIRE_TOKEN=0 "
                "for a memory server on this machine that runs without a bearer",
            )
        if token and not header_token_valid(token):
            raise MemoryWriteFailed(
                errors.E_MEMORY_TOKEN_MISSING,
                "MEMORY_HTTP_TOKEN holds whitespace or characters a bearer cannot carry; export the token alone",
            )
        if not is_loopback_http_url(url):
            raise MemoryWriteFailed(
                errors.E_MEMORY_URL_NOT_LOOPBACK, "KNOWLEDGE_MEMORY_URL must be http on 127.0.0.1, localhost or ::1"
            )
        self._base = url.rstrip("/")
        self._token = token
        self._open = opener
        self._timeout = timeout
        self._runbooks_timeout = runbooks_timeout

    def __repr__(self) -> str:
        return f"MemoryWriter(url={self._base!r})"

    # -- the three routes ------------------------------------------------------

    def put_project_card(self, project: str, fields: Mapping[str, str]) -> dict[str, Any]:
        """Create or update the card of ``project`` with ``fields``; ``{"status": "created"|"updated"}``.

        Only the fields given travel: a field left out keeps the value the
        card already has.
        """
        path = f"/projects/{_check_project(project)}"
        status, _ = self._call("PUT", path, dict(fields), self._timeout)
        return {"status": "created" if status == 201 else "updated"}

    def upsert_task(self, project: str, task: Mapping[str, Any]) -> dict[str, Any]:
        """Upsert one task; ``{"outcome": "created"|"updated"|"skipped", ...}``.

        A task the server refuses on its own merits (400, 409, 422) is
        ``skipped`` with ``reason`` set to the memory server's error code;
        every other failure raises :class:`~knowledge.errors.MemoryWriteFailed`.
        """
        path = f"/projects/{_check_project(project)}/tasks"
        try:
            _, payload = self._call("POST", path, dict(task), self._timeout)
        except MemoryWriteFailed as exc:
            if exc.code == errors.E_MEMORY_WRITE_FAILED and exc.status in TASK_SKIP_STATUSES:
                return {"outcome": "skipped", "reason": exc.reason}
            raise
        created = payload.get("created") is True
        return {"outcome": "created" if created else "updated", "card_created": payload.get("card_created") is True}

    def sync_runbooks(self, project: str, vault_dir: Path, *, prune_missing: bool = False) -> dict[str, int]:
        """Have memory scan ``<vault_dir>/Runbooks`` into the runbook index of ``project``.

        Returns memory's counts (``scanned``, ``upserted``, ``unchanged``,
        ``pruned``, ``stale_count``, and ``skipped`` as a count). A project
        memory has no card for is ``E_MEMORY_PROJECT_MISSING``.
        """
        path = f"/projects/{_check_project(project)}/runbooks/sync"
        body = {"source": "vault-fs", "vault_dir": str(vault_dir.resolve()), "prune_missing": prune_missing}
        try:
            _, payload = self._call("POST", path, body, self._runbooks_timeout)
        except MemoryWriteFailed as exc:
            if exc.status == 404 and exc.reason == "no_card":
                raise MemoryWriteFailed(
                    errors.E_MEMORY_PROJECT_MISSING,
                    f"memory has no project card {project!r}; "
                    "run `cloudfrog-knowledge memory-bridge sync --card` first",
                    status=404,
                    reason="no_card",
                ) from None
            raise
        counts = {
            key: payload[key]
            for key in ("scanned", "upserted", "unchanged", "pruned", "stale_count")
            if isinstance(payload.get(key), int) and not isinstance(payload.get(key), bool)
        }
        skipped = payload.get("skipped")
        counts["skipped"] = len(skipped) if isinstance(skipped, list) else 0
        return counts

    # -- transport -------------------------------------------------------------

    def _call(self, method: str, path: str, body: Mapping[str, Any], timeout: float) -> tuple[int, dict[str, Any]]:
        headers = {
            "X-Memory-Client": CLIENT_ID,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        request = urllib.request.Request(
            f"{self._base}{path}",
            data=json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8"),
            headers=headers,
            method=method,
        )
        try:
            with self._open(request, timeout=timeout) as response:
                status = int(getattr(response, "status", 200))
                raw = response.read()
        except urllib.error.HTTPError as exc:
            reason = _memory_code(exc)
            exc.close()
            raise _refused(method, exc.code, reason) from None
        except ValueError:
            # http.client refuses a header it cannot send with a message that
            # quotes the header, bearer included; only the class is kept.
            log.warning("memory write failed: %s %s (invalid request)", errors.E_MEMORY_WRITE_FAILED, method)
            raise MemoryWriteFailed(
                errors.E_MEMORY_WRITE_FAILED,
                "the request to the memory server could not be built",
                reason="invalid_request",
            ) from None
        except (OSError, http.client.HTTPException) as exc:
            # URLError, a refused connection and a timeout are all OSError;
            # only the class of the cause is kept, never its text, which may
            # carry the URL.
            cause = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            name = type(cause).__name__ if isinstance(cause, BaseException) else "URLError"
            log.warning("memory write failed: %s %s (%s)", errors.E_MEMORY_UNAVAILABLE, method, name)
            raise MemoryWriteFailed(
                errors.E_MEMORY_UNAVAILABLE, f"the memory server could not be reached ({name})", reason=name
            ) from None
        if status not in (200, 201):
            raise _refused(method, status, f"http_{status}")
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            log.warning(
                "memory write failed: %s %s (response is not a JSON object)", errors.E_MEMORY_WRITE_FAILED, method
            )
            raise MemoryWriteFailed(
                errors.E_MEMORY_WRITE_FAILED,
                f"the memory server answered HTTP {status} with a body that is not a JSON object",
                status=status,
                reason="invalid_response",
            )
        return status, payload


def _memory_code(exc: urllib.error.HTTPError) -> str:
    """The memory server's own error code for a refused write, or a stand-in built from the status."""
    try:
        payload = json.loads(exc.read(_MAX_ERROR_BODY))
    except (OSError, ValueError, http.client.HTTPException):
        payload = None
    code = payload.get("code") if isinstance(payload, dict) else None
    if isinstance(code, str) and _MEMORY_CODE.fullmatch(code):
        return code
    if exc.code == 401:
        return "unauthorized"
    if exc.code == 413:
        return "payload_too_large"
    return f"http_{exc.code}"


def _refused(method: str, status: int, reason: str) -> MemoryWriteFailed:
    log.warning("memory write failed: %s %s (HTTP %d, %s)", errors.E_MEMORY_WRITE_FAILED, method, status, reason)
    return MemoryWriteFailed(
        errors.E_MEMORY_WRITE_FAILED,
        f"the memory server refused the write (HTTP {status}, {reason})",
        status=status,
        reason=reason,
    )


def writer_for(memory: MemorySettings, *, opener: Opener | None = None) -> MemoryWriter:
    """The writer of ``memory``; raises :class:`~knowledge.errors.MemoryWriteFailed` when it may not write.

    The checks run before any connection: a bearer (unless
    ``require_token`` is off), then a loopback ``http`` URL.
    """
    if opener is None:
        return MemoryWriter(memory.url, memory.token, require_token=memory.require_token)
    return MemoryWriter(memory.url, memory.token, require_token=memory.require_token, opener=opener)


# ---------------------------------------------------------------------------
# What a vault publishes
# ---------------------------------------------------------------------------


def jira_project(spec: VaultSpec) -> str | None:
    """The first key of ``integrations.jira.projects`` of ``spec``, or ``None``."""
    block = spec.integrations.get("jira")
    projects = block.get("projects") if isinstance(block, Mapping) else None
    if not isinstance(projects, Sequence) or isinstance(projects, str):
        return None
    for key in projects:
        if isinstance(key, str) and JIRA_PROJECT_PATTERN.fullmatch(key.strip()):
            return key.strip()
    return None


def card_fields(spec: VaultSpec) -> dict[str, str]:
    """The project card fields vault ``spec`` declares: ``display_name``, ``jira_project``, ``knowledge_hub_path``.

    A field the vault does not declare is left out, so the card keeps what it has.
    """
    fields = {"display_name": spec.name, "jira_project": jira_project(spec), "knowledge_hub_path": spec.memory_hub}
    return {key: value for key, value in fields.items() if value}


def runbooks_dir(spec: VaultSpec) -> Path | None:
    """``<vault>/Runbooks`` when it is a directory, else ``None``."""
    if spec.path is None:
        return None
    candidate = spec.path / RUNBOOKS_DIR
    return candidate if candidate.is_dir() else None


def _outcome(exc: MemoryWriteFailed) -> str:
    """``skipped`` when the bridge is not configured and sent nothing, ``failed`` otherwise."""
    return "skipped" if exc.code in NOT_CONFIGURED and exc.status is None else "failed"


def _failure(exc: MemoryWriteFailed) -> dict[str, Any]:
    out: dict[str, Any] = {"status": _outcome(exc), "code": exc.code}
    if exc.status is not None:
        out["http_status"] = exc.status
    if exc.reason is not None:
        out["reason"] = exc.reason
    return out


def sync_vault(spec: VaultSpec, writer: MemoryWriter, *, card: bool) -> dict[str, Any]:
    """Publish vault ``spec`` to its memory project: the card when ``card``, then the runbook index.

    The report says what happened to each step; ``ok`` is false when a step
    failed. A failed card stops the run, since the runbook sync needs it.
    """
    project = spec.memory_project or ""
    report: dict[str, Any] = {"vault": spec.slug, "memory_project": project, "ok": True}
    if card:
        fields = card_fields(spec)
        try:
            report["card"] = {**writer.put_project_card(project, fields), "fields": sorted(fields)}
        except MemoryWriteFailed as exc:
            report["card"] = _failure(exc)
            report["ok"] = False
            return report
    directory = runbooks_dir(spec)
    if directory is None:
        report["runbooks"] = {"status": "skipped", "reason": f"the vault has no {RUNBOOKS_DIR}/ directory"}
        return report
    try:
        report["runbooks"] = {"status": "synced", **writer.sync_runbooks(project, spec.require_path())}
    except MemoryWriteFailed as exc:
        report["runbooks"] = _failure(exc)
        report["ok"] = False
    return report


def reindex_status(spec: VaultSpec, memory: MemorySettings, *, opener: Opener | None = None) -> dict[str, str]:
    """``reindex.memory_bridge`` for vault ``spec``: ``{"status": ..., "code"?: ...}``.

    ``synced`` after memory rescanned the vault's runbooks; ``skipped`` when
    the vault has no ``Runbooks/`` (no code) or the bridge is not configured
    and sent nothing (the ``E_MEMORY_*`` code that says why); ``failed`` with
    the code of the write memory refused or could not take (a project memory
    has no card for is ``E_MEMORY_PROJECT_MISSING``). Never raises: the index
    is already rebuilt.
    """
    directory = runbooks_dir(spec)
    if directory is None:
        return {"status": "skipped"}
    try:
        if not spec.memory_project:
            raise MemoryWriteFailed(
                errors.E_MEMORY_PROJECT_MISSING, f"{spec.slug}: the vault declares no memory.project"
            )
        writer_for(memory, opener=opener).sync_runbooks(spec.memory_project, spec.require_path())
    except MemoryWriteFailed as exc:
        return {"status": _outcome(exc), "code": exc.code}
    return {"status": "synced"}


# ---------------------------------------------------------------------------
# Promotion bridge: the memory command line
# ---------------------------------------------------------------------------


def memory_command(env: Mapping[str, str]) -> str:
    """The memory binary: ``MEMORY_BIN`` when set, else ``memory`` resolved through ``PATH``."""
    return (env.get("MEMORY_BIN") or "").strip() or "memory"


def run_memory_cli(
    args: Sequence[str],
    *,
    env: Mapping[str, str],
    timeout: float = PROMOTE_TIMEOUT_SECONDS,
    runner: Runner = subprocess.run,
) -> tuple[int, Any]:
    """Run ``memory <args>`` (argument list, no shell) and return its exit status and its JSON output.

    A binary that cannot be started, a run past ``timeout`` or an output that
    is not JSON is ``E_MEMORY_UNAVAILABLE``; memory's own errors come back as
    its JSON envelope and a non-zero status.
    """
    argv = [memory_command(env), *args]
    try:
        done = runner(argv, capture_output=True, text=True, timeout=timeout, env=dict(env), check=False)
    except subprocess.TimeoutExpired:
        raise MemoryWriteFailed(
            errors.E_MEMORY_UNAVAILABLE, f"the memory command did not finish within {timeout:g} s", reason="timeout"
        ) from None
    except OSError as exc:
        raise MemoryWriteFailed(
            errors.E_MEMORY_UNAVAILABLE,
            f"the memory command could not be started ({type(exc).__name__}); install memory or set MEMORY_BIN",
            reason="command_unavailable",
        ) from None
    try:
        payload = json.loads(done.stdout)
    except ValueError:
        raise MemoryWriteFailed(
            errors.E_MEMORY_UNAVAILABLE,
            f"the memory command printed no JSON (exit status {done.returncode})",
            reason="invalid_output",
        ) from None
    return done.returncode, payload


def promote_list(
    project: str,
    *,
    env: Mapping[str, str],
    types: Sequence[str] = (),
    limit: int | None = None,
    runner: Runner = subprocess.run,
) -> tuple[int, Any]:
    """``memory project <project> promote list --json``: the pinned observations a document can come from.

    Each of ``types`` must be an observation type such as ``decision``
    (``E_ARGUMENT_OUT_OF_RANGE`` otherwise), so none can reach the memory
    command as one of its options.
    """
    args = ["project", _check_project(project), "promote", "list", "--json"]
    bad = [kind for kind in types if not OBSERVATION_TYPE_PATTERN.fullmatch(kind)]
    if bad:
        raise KnowledgeToolError(
            errors.E_ARGUMENT_OUT_OF_RANGE,
            "--types takes observation types such as decision,bugfix (lowercase letters, digits, - and _)",
        )
    if types:
        args += ["--types", ",".join(types)]
    if limit is not None:
        args += ["--limit", str(limit)]
    return run_memory_cli(args, env=env, runner=runner)


def check_knowledge_ref(vault_path: Path, knowledge_ref: str) -> str:
    """``knowledge_ref`` when it is the vault-relative path of a Markdown file in the vault.

    Raises ``E_KNOWLEDGE_REF_INVALID`` for an absolute path, a ``..`` or
    empty segment, a backslash, a file that is not ``.md`` or one that does
    not exist under ``vault_path``.
    """
    ref = knowledge_ref.strip()
    parts = ref.split("/")
    shaped = bool(ref) and not ref.startswith("/") and "\\" not in ref and ".." not in parts and "" not in parts
    root = vault_path.resolve()
    target = (root / ref).resolve() if shaped else root
    if not (shaped and ref.endswith(".md") and target.is_relative_to(root) and target.is_file()):
        raise KnowledgeToolError(
            errors.E_KNOWLEDGE_REF_INVALID,
            f"--knowledge-ref {knowledge_ref!r} must be the vault-relative path of a Markdown document "
            "that exists in the vault (the document the pull request merged)",
        )
    return ref


def promote_stamp(
    project: str,
    observation: str,
    knowledge_ref: str,
    *,
    vault_path: Path,
    env: Mapping[str, str],
    runner: Runner = subprocess.run,
) -> tuple[int, Any]:
    """``memory project <project> promote stamp <observation> --knowledge-ref <ref> --json``.

    Records, on the observation in memory, the vault document it became.
    Nothing is written into the vault.
    """
    if not OBSERVATION_PATTERN.fullmatch(observation):
        raise KnowledgeToolError(
            errors.E_ARGUMENT_OUT_OF_RANGE, f"observation {observation!r} must be an observation id or obs-<hex>"
        )
    ref = check_knowledge_ref(vault_path, knowledge_ref)
    args = ["project", _check_project(project), "promote", "stamp", observation, "--knowledge-ref", ref, "--json"]
    return run_memory_cli(args, env=env, runner=runner)


__all__ = [
    "CLIENT_ID",
    "MemoryWriter",
    "card_fields",
    "check_knowledge_ref",
    "jira_project",
    "promote_list",
    "promote_stamp",
    "reindex_status",
    "run_memory_cli",
    "runbooks_dir",
    "sync_vault",
    "writer_for",
]
