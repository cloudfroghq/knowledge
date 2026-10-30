"""Reindex webhook + health-check endpoints for the streamable-http transport.

Two routes are registered on the FastMCP server, each by its own function:

- ``GET  /health``           — public health check used by Dokploy; it also
                               names the tool contract the process serves
                               (``contract_version``), the layout of the
                               index file (``index_version``), how its
                               vectors are ranked (``vector_backend``) and,
                               for a vault with a repository, the commit the
                               checkout holds, how its last refresh ended and
                               the last refresh that moved it
                               (``vault_commit``, ``last_sync_*``,
                               ``last_change_*``).
- ``POST /webhook/reindex``  — refreshes the vault (converges its checkout on
                               the served branch of ``origin`` unless the
                               vault turns ``git.auto_pull`` off) and runs a
                               reindex in the background. The server registers
                               it only for a vault that declares a repository
                               (``repo`` or ``VAULT_REMOTE``). Authenticates via webhook HMAC
                               (``X-Hub-Signature-256``, or ``X-Hub-Signature``
                               with a ``sha256=`` digest) OR a manual Bearer
                               token whose principal holds the ``admin`` scope;
                               a known bearer without it gets 403 with
                               ``E_SCOPE_REQUIRED``.

A signed delivery that moves a ref other than the served branch (``git.branch``
in ``vaults.yaml``, ``VAULT_BRANCH`` for a ``VAULT_PATH`` deployment), or that
deletes a ref, is answered 200 with ``"status": "ignored"`` and runs nothing:
the clone follows the served branch only, so such a push brings it nothing.
GitHub, Gitea and Forgejo pushes name their ref at the top level; a Bitbucket
Cloud ``repo:push`` lists every ref it updated in ``push.changes`` and is
judged by those changes.
Runs of one vault never overlap: a delivery that arrives while one is in flight
adds a single follow-up pass instead of a second ``git pull`` in the same
checkout (:class:`SerialRefresh`).

The reindex is incremental by default: a push must never trigger a full
re-embedding run, which costs a complete pass over the OpenAI API. ``force``
exists because an incremental run skips unchanged files, so a metadata field
added after an index was built is never backfilled — and it is accepted only
on the manual Bearer route, where a human is the one paying for it. A
signature-authenticated push cannot request it.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from knowledge import config, contract, errors, principals
from knowledge import settings as _settings
from knowledge.stores import SCHEMA_VERSION

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


#: Signature headers, in the order they are tried. GitHub sends both on a
#: webhook with a secret: ``X-Hub-Signature`` carries a ``sha1=`` digest this
#: server never accepts, ``X-Hub-Signature-256`` the ``sha256=`` one.
#: Bitbucket sends only ``X-Hub-Signature``, with a ``sha256=`` digest.
SIGNATURE_HEADERS: tuple[str, ...] = ("x-hub-signature-256", "x-hub-signature")


def _verify_hmac_signature(body: bytes, header: str | None) -> bool:
    """Validate a webhook HMAC header against the raw body.

    Only a ``sha256=<hex>`` digest is accepted, under either header name of
    :data:`SIGNATURE_HEADERS`: the construction is the same, the name is the
    provider's choice.

    The body must be the bytes as received: reformatting the JSON changes the
    digest.
    """
    if not header or not header.startswith("sha256="):
        return False
    if not config.MCP_WEBHOOK_SECRET:
        return False

    sent_hex = header[len("sha256=") :].strip()
    expected_hex = hmac.new(
        config.MCP_WEBHOOK_SECRET.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()

    return secrets.compare_digest(sent_hex.encode("utf-8"), expected_hex.encode("utf-8"))


def _signature_ok(body: bytes, request: Request) -> bool:
    """Whether any signature header of the request carries a valid ``sha256=`` digest of ``body``."""
    return any(_verify_hmac_signature(body, request.headers.get(name)) for name in SIGNATURE_HEADERS)


def _manual_principal(header: str | None) -> principals.Principal | None:
    """The principal of a manual ``Authorization: Bearer <token>`` header, or ``None``."""
    token = principals.bearer_token(header)
    if token is None:
        return None
    try:
        table = principals.PrincipalTable.current()
    except errors.KnowledgeConfigError as exc:
        log.error("[webhook] bearer configuration refused (%s); manual bearer denied", exc.code)
        return None
    return table.resolve(token)


# ---------------------------------------------------------------------------
# Background reindex job
# ---------------------------------------------------------------------------


# Reindex jobs run in worker threads, and GitHub can deliver two pushes back
# to back, so this is a counter rather than a boolean: the first job to finish
# must not clear the flag while a second one is still running.
_indexing_lock = threading.Lock()
_indexing_jobs = 0


def _indexing_active() -> bool:
    """True while at least one reindex job is in flight."""
    with _indexing_lock:
        return _indexing_jobs > 0


@contextmanager
def indexing_scope() -> Iterator[None]:
    """Mark a reindex as in flight so /health reports progress, not staleness.

    Public on purpose: every entry point that pulls the vault before indexing
    must claim it. A run that skips this scope pulls the repo, bumps every
    mtime past last_indexed_at and leaves /health advertising a stale index
    for the whole run.
    """
    global _indexing_jobs

    with _indexing_lock:
        _indexing_jobs += 1
    try:
        yield
    finally:
        with _indexing_lock:
            _indexing_jobs -= 1
        # Drop the cached staleness so the next /health reflects this run
        # immediately instead of serving a pre-reindex verdict for up to a
        # full TTL.
        _staleness_cache["checked_at"] = 0.0


def force_requested(raw: str | None, *, manual_auth: bool) -> bool:
    """Whether this request may run a full re-embedding pass.

    Split out as a pure function so the rule is testable without an HTTP
    stack: a truthy ``force`` query value counts only when the caller
    authenticated with the manual Bearer token. A repository push carries an
    HMAC signature, and a push must never be able to spend a full embedding
    run of the whole vault.
    """
    if not manual_auth:
        return False
    return str(raw or "").strip().lower() in ("1", "true", "yes", "on")


class SerialRefresh:
    """One vault's pull and reindex, one run at a time, with requests coalesced.

    GitHub delivers pushes back to back: a merge with ``--delete-branch``
    sends two half a second apart. Two runs side by side would start two
    ``git pull`` in the same checkout, and the second fails with rc=128 and
    indexes a stale tree. Here a request that arrives while a run is in
    flight starts nothing beside it: it marks the run dirty, and the running
    thread does one more pass when it ends, which pulls whatever the remote
    holds by then. However many requests arrive meanwhile, they add one pass;
    if any of them asked for ``force``, that pass is forced.

    ``lock`` is held for every pass. Give it the vault's refresh lock
    (``VaultRuntime.refresh_lock``), which the ``reindex`` tool holds too, so
    a pass never runs beside a ``reindex(pull=true)`` on the same checkout.
    """

    def __init__(self, lock: threading.Lock | None = None) -> None:
        self._lock = lock if lock is not None else threading.Lock()
        self._state = threading.Lock()
        self._running = False
        # ``None``: no follow-up pass is owed; otherwise the ``force`` it runs with.
        self._follow_up: bool | None = None

    def run(self, job: Callable[[bool], None], force: bool = False) -> bool:
        """Run ``job(force)`` now, or leave it to the run in flight; ``True`` when this call ran it."""
        with self._state:
            if self._running:
                self._follow_up = force or bool(self._follow_up)
                return False
            self._running = True
        while True:
            try:
                with self._lock:
                    job(force)
            except BaseException:
                with self._state:
                    self._running = False
                    self._follow_up = None
                raise
            # Checking for a follow-up and clearing ``_running`` happen under
            # one hold of ``_state``: a request that slipped in between them
            # would be marked as owed to a run that already ended.
            with self._state:
                if self._follow_up is None:
                    self._running = False
                    return True
                force, self._follow_up = self._follow_up, None


def _run_reindex_job(
    indexer: Any,
    force: bool = False,
    *,
    vault_sync: Callable[[], bool] | None = None,
    refresh: SerialRefresh | None = None,
) -> None:
    """Refresh the vault and run the reindex. Logs everything.

    ``vault_sync`` refreshes the vault the indexer serves; without one the
    vault is indexed as it is on disk and no git command runs. ``refresh``
    serializes the runs of that vault and folds the requests that arrive
    during one into a single follow-up pass; without it this call runs once,
    whatever else is running.
    """

    def once(force_now: bool) -> None:
        with indexing_scope():
            try:
                if vault_sync is not None:
                    vault_sync()

                log.info("[webhook] starting reindex (force=%s)", force_now)
                stats = indexer.index(force=force_now, target="vault")
                log.info("[webhook] reindex finished: %s", stats)
            except Exception:
                log.exception("[webhook] reindex failed")

    if refresh is None:
        once(force)
    elif not refresh.run(once, force):
        log.info("[webhook] a reindex is in flight; it runs one more pass for this request (force=%s)", force)


async def _run_reindex_async(
    indexer: Any,
    force: bool = False,
    *,
    vault_sync: Callable[[], bool] | None = None,
    refresh: SerialRefresh | None = None,
) -> None:
    """Run the (sync) reindex job off the event loop."""
    await asyncio.to_thread(_run_reindex_job, indexer, force, vault_sync=vault_sync, refresh=refresh)


# ---------------------------------------------------------------------------
# Which pushes concern the served branch
# ---------------------------------------------------------------------------

#: ``reason`` of an ignored delivery: the push moved a ref other than the served branch.
IGNORED_NOT_SERVED_BRANCH = "not_served_branch"
#: ``reason`` of an ignored delivery: the push deleted its ref.
IGNORED_REF_DELETED = "ref_deleted"

_ZERO_SHA = "0" * 40


def ignored_push(body: bytes, branch: str) -> tuple[str, str] | None:
    """Why a push needs no pull for a vault that serves ``branch``, as ``(reason, ref)``; ``None`` when it does.

    The clone of a vault follows its served branch only, so a push that moves
    another branch or a tag brings it nothing, and neither does deleting a
    ref. Two payload shapes can be judged:

    - a top-level ``ref`` (GitHub, Gitea and Forgejo pushes): a deletion is
      ``deleted: true``, or an all-zero ``after`` from senders that omit
      ``deleted``;
    - a Bitbucket Cloud ``repo:push``, whose ``push.changes`` lists every ref
      the push updated (:func:`_ignored_bitbucket_push`).

    A ``ping``, a body that is not JSON or any other shape keeps refreshing
    the vault.
    """
    try:
        payload = json.loads(body)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    ref = payload.get("ref")
    if isinstance(ref, str) and ref:
        if payload.get("deleted") is True or payload.get("after") == _ZERO_SHA:
            return IGNORED_REF_DELETED, ref
        if ref != f"refs/heads/{branch}":
            return IGNORED_NOT_SERVED_BRANCH, ref
        return None
    push = payload.get("push")
    if isinstance(push, dict) and isinstance(push.get("changes"), list):
        return _ignored_bitbucket_push(push["changes"], branch)
    return None


#: Bitbucket reference types and the git namespace each one lives in.
_BITBUCKET_REF_PREFIX: dict[str, str] = {"branch": "refs/heads/", "tag": "refs/tags/", "annotated_tag": "refs/tags/"}


def _bitbucket_ref(state: Any) -> str | None:
    """The full git ref a Bitbucket ``new`` or ``old`` reference state names; ``None`` if it names none."""
    if not isinstance(state, dict):
        return None
    kind, name = state.get("type"), state.get("name")
    prefix = _BITBUCKET_REF_PREFIX.get(kind) if isinstance(kind, str) else None
    if prefix is None or not isinstance(name, str) or not name:
        return None
    return prefix + name


def _ignored_bitbucket_push(changes: list[Any], branch: str) -> tuple[str, str] | None:
    """:func:`ignored_push` for the ``push.changes`` of a Bitbucket Cloud ``repo:push``.

    Each change carries the state of one reference after the push (``new``)
    and before it (``old``): ``{"type": "branch" | "tag" | ..., "name": ...,
    "target": {"hash": ...}}``, with ``new`` null when the push deleted the
    reference and ``old`` null when it created it. The delivery runs a sync
    as soon as one change leaves the served branch at a commit (``new`` of
    type ``branch`` named ``branch``); otherwise it is ignored. The reason is
    ``ref_deleted`` when a change deleted the served branch, and otherwise
    the one of the first change: ``ref_deleted`` for a deletion,
    ``not_served_branch`` for a push to another branch or a tag.

    A change this cannot read (not an object, or a ``new`` that names no
    reference type and name), or an empty list, might move the served branch
    and keeps the sync running.
    """
    served = f"refs/heads/{branch}"
    judged: list[tuple[str, str]] = []
    for change in changes:
        if not isinstance(change, dict):
            return None
        new = change.get("new")
        if new is None:
            judged.append((IGNORED_REF_DELETED, _bitbucket_ref(change.get("old")) or ""))
            continue
        ref = _bitbucket_ref(new)
        if ref is None:
            return None
        if ref == served:
            return None
        judged.append((IGNORED_NOT_SERVED_BRANCH, ref))
    if not judged:
        return None
    if (IGNORED_REF_DELETED, served) in judged:
        return IGNORED_REF_DELETED, served
    return judged[0]


# ---------------------------------------------------------------------------
# Index staleness probe (cached — /health may be polled every few seconds)
# ---------------------------------------------------------------------------

#: Keys ``/health`` always carries about the vault's git checkout: the
#: abbreviated commit it holds; when and how its last refresh in this process
#: ended (``fast_forward``, ``up_to_date``, ``realigned`` or ``failed``, with a
#: redacted message for ``failed``); and when and how the last refresh that
#: moved the checkout ended (``fast_forward`` or ``realigned``), which a later
#: ``up_to_date`` or ``failed`` leaves in place. ``null`` when the vault
#: declares no repository or before the first refresh of each kind.
SYNC_STATE_KEYS: tuple[str, ...] = (
    "vault_commit",
    "last_sync_at",
    "last_sync_result",
    "last_sync_error",
    "last_change_at",
    "last_change_result",
)

_STALENESS_TTL_SECONDS = 60.0
_staleness_cache: dict[str, Any] = {"checked_at": 0.0, "payload": {}}


def _read_index_meta(state_dir: Path | None = None) -> dict[str, Any]:
    root = Path(config.CHROMA_PERSIST_DIR) if state_dir is None else state_dir
    meta_path = root / "index_meta.json"
    try:
        return json.loads(meta_path.read_text())
    except Exception:
        return {}


def _staleness_probe(
    vault_root: Path | None = None, state_dir: Path | None = None
) -> dict[str, Any]:
    """Compare the newest vault mtime against the last indexing run.

    ``vault_root`` and ``state_dir`` default to the configured ``VAULT_PATH``
    and the default vault's index directory, as the config facade exposes them.
    """
    import time

    now = time.time()
    if now - _staleness_cache["checked_at"] < _STALENESS_TTL_SECONDS:
        return _staleness_cache["payload"]

    payload: dict[str, Any] = {"last_indexed_at": None, "stale": None, "stale_days": None}
    meta = _read_index_meta(state_dir)
    raw_ts = meta.get("last_indexed_at")
    if raw_ts:
        try:
            last_indexed = datetime.fromisoformat(raw_ts)
            payload["last_indexed_at"] = raw_ts
            root = Path(config.VAULT_PATH) if vault_root is None else vault_root
            # Same notes the indexer reads: a file under a dot-prefixed
            # directory is never indexed, so it cannot make the index stale.
            vault_mtime = max(
                (
                    p.stat().st_mtime
                    for p in root.rglob("*.md")
                    if not any(part.startswith(".") for part in p.relative_to(root).parts)
                ),
                default=0.0,
            )
            payload["stale"] = vault_mtime > last_indexed.timestamp()
            if payload["stale"]:
                payload["stale_days"] = round(
                    (now - last_indexed.timestamp()) / 86400, 1
                )
        except Exception:
            log.exception("[health] staleness probe failed")

    _staleness_cache["checked_at"] = now
    _staleness_cache["payload"] = payload
    return payload


# ---------------------------------------------------------------------------
# Public registration helper
# ---------------------------------------------------------------------------


def register_webhook_routes(
    mcp: Any,
    indexer: Any,
    *,
    index_ready_flag: dict[str, bool],
    vault_sync: Callable[[], bool] | None = None,
    vault_root: Path | None = None,
    state_dir: Path | None = None,
    branch: str | None = None,
    refresh_lock: threading.Lock | None = None,
    sync_state: Callable[[], Mapping[str, Any]] | None = None,
) -> None:
    """Register both ``/health`` and ``/webhook/reindex`` on the FastMCP server.

    The server registers them separately (``/webhook/reindex`` only for a
    vault that declares a repository); this registers both, with the
    arguments of :func:`register_health_route` and
    :func:`register_reindex_route`.
    """
    register_health_route(
        mcp,
        indexer,
        index_ready_flag=index_ready_flag,
        vault_root=vault_root,
        state_dir=state_dir,
        sync_state=sync_state,
    )
    register_reindex_route(mcp, indexer, vault_sync=vault_sync, branch=branch, refresh_lock=refresh_lock)


def register_health_route(
    mcp: Any,
    indexer: Any,
    *,
    index_ready_flag: dict[str, bool],
    vault_root: Path | None = None,
    state_dir: Path | None = None,
    sync_state: Callable[[], Mapping[str, Any]] | None = None,
) -> None:
    """Register the public ``GET /health`` on the FastMCP server.

    ``index_ready_flag`` is a 1-key dict ``{"ready": bool}`` shared with the
    server module so ``/health`` reflects the lazy-indexing state without
    forcing the index on healthcheck calls. ``vault_root`` and ``state_dir``
    describe the vault ``indexer`` serves and default to the configured ones.
    ``sync_state`` returns the :data:`SYNC_STATE_KEYS` of that vault's git
    checkout (``gitsync.sync_state``); without it, for a vault that declares
    no repository, those keys are ``null``.
    """

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_request: Request) -> Response:  # noqa: ARG001
        indexing = _indexing_active()
        payload: dict[str, Any] = {
            "status": "ok",
            "contract_version": contract.CONTRACT_VERSION,
            # The tool profile this process serves: ``all`` or ``legacy``.
            "profile": _settings.current().output.profile,
            "indexed": 0,
            "embedding_model": config.EMBEDDING_MODEL,
            "transport": config.MCP_TRANSPORT,
            "indexing": indexing,
            # Whether this process warmed up the index yet. Kept separate from
            # the chunk count: the index is persisted, so it holds chunks
            # long before any tool call loads the graph.
            "index_loaded": bool(index_ready_flag.get("ready")),
            "index_version": SCHEMA_VERSION,
            "vector_backend": None,
            **dict.fromkeys(SYNC_STATE_KEYS),
        }
        try:
            # count() reads the persisted index and does NOT trigger the
            # lazy index, so it must not be gated on the ready flag. Gating it
            # made /health report zero documents after every restart until some
            # tool happened to run — a total-data-loss reading for a warm cache.
            store = indexer.store
            payload["indexed"] = store.count()
            payload["vector_backend"] = store.vector_backend
            payload["index_version"] = int(store.get_meta().get("index_version", SCHEMA_VERSION))
        except Exception:
            log.exception("[health] failed to read the index")
            payload["status"] = "degraded"

        staleness = await asyncio.to_thread(_staleness_probe, vault_root, state_dir)
        payload.update(staleness)

        if sync_state is not None:
            try:
                state = await asyncio.to_thread(sync_state)
                payload.update({key: state.get(key) for key in SYNC_STATE_KEYS})
            except Exception:
                log.exception("[health] failed to read the vault sync state")

        if indexing:
            # A reindex pulls the vault before embedding, which bumps every
            # mtime past last_indexed_at. The staleness comparison is therefore
            # true by construction until the run commits its meta, so report
            # the work in progress rather than a stale index.
            payload["stale"] = False
            payload["stale_days"] = None
            if payload["status"] == "ok":
                payload["status"] = "indexing"
        elif staleness.get("stale") and (staleness.get("stale_days") or 0) > 7:
            payload["status"] = "stale"

        return JSONResponse(payload, status_code=200)


def register_reindex_route(
    mcp: Any,
    indexer: Any,
    *,
    vault_sync: Callable[[], bool] | None = None,
    branch: str | None = None,
    refresh_lock: threading.Lock | None = None,
) -> None:
    """Register ``POST /webhook/reindex`` on the FastMCP server.

    ``vault_sync`` refreshes the vault ``indexer`` serves before each run;
    without it a reindex runs no git command. ``branch`` is the branch that
    vault serves: a signed push for another ref, or one deleting a ref, is
    answered ``ignored`` (:func:`ignored_push`); without it every signed
    delivery runs. ``refresh_lock`` is the vault's refresh lock, shared with
    the ``reindex`` tool (:class:`SerialRefresh`).
    """
    refresh = SerialRefresh(refresh_lock)

    @mcp.custom_route("/webhook/reindex", methods=["POST"])
    async def webhook_reindex(request: Request) -> Response:
        body = await request.body()

        signature_ok = _signature_ok(body, request)
        principal = None if signature_ok else _manual_principal(request.headers.get("authorization"))

        if not signature_ok:
            if principal is None:
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            if not principal.allows(principals.ADMIN):
                log.warning("[webhook] principal=%s lacks the admin scope; reindex refused", principal.id)
                return JSONResponse(
                    {"error": f"{errors.E_SCOPE_REQUIRED}: /webhook/reindex requires the admin scope"},
                    status_code=403,
                )
        manual_ok = principal is not None

        target = request.query_params.get("target", "vault")
        if target != "vault":
            # ADR-039 retired code indexing: "code" and "all" used to fall back
            # to "vault" silently, which made a request for a target this
            # server can no longer honor look like a completed run. An
            # explicit rejection is the honest answer.
            return JSONResponse(
                {
                    "error": (
                        f"target {target!r} is retired (ADR-039) — code indexing was "
                        "removed; only 'vault' is supported. Use graphify to build or "
                        "update a code graph instead."
                    )
                },
                status_code=400,
            )

        if signature_ok and branch is not None:
            # A manual bearer is a person asking for a run; only a signed
            # delivery is a push whose ref can make it pointless.
            ignored = ignored_push(body, branch)
            if ignored is not None:
                reason, ref = ignored
                log.info("[webhook] delivery ignored: %s (ref=%r, served branch=%r)", reason, ref, branch)
                return JSONResponse(
                    {
                        "status": "ignored",
                        "mode": "signature",
                        "reason": reason,
                        "ref": ref,
                        "branch": branch,
                        "target": target,
                    },
                    status_code=200,
                )

        mode = "signature" if signature_ok else "manual"
        force = force_requested(
            request.query_params.get("force"), manual_auth=manual_ok and not signature_ok
        )
        queued_at = datetime.now(timezone.utc).isoformat()
        log.info(
            "[webhook] reindex queued via %s auth at %s (principal=%s, target=%s, force=%s)",
            mode,
            queued_at,
            principal.id if principal is not None else "-",
            target,
            force,
        )

        return JSONResponse(
            {
                "status": "accepted",
                "mode": mode,
                "queued_at": queued_at,
                "target": target,
                "force": force,
            },
            status_code=202,
            background=BackgroundTask(_run_reindex_async, indexer, force, vault_sync=vault_sync, refresh=refresh),
        )
