"""Knowledge MCP Server.

Provides semantic search and structured queries over the Obsidian
documentation vault ``VAULT_PATH`` points at.

Run:
    uv run --directory <checkout> python -m knowledge.server
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import functools
import importlib
import inspect
import json
import logging
import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar, Token
from pathlib import Path
from types import ModuleType
from typing import Any, NoReturn

import frontmatter
import pydantic_core
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, ContentBlock, Icon, TextContent

from knowledge import (
    contract,
    errors,
    extensions,
    gitsync,
    memory_http,
    pointers,
    profiles,
    rerank as _rerank,
    review,
    settings as _settings,
    usage as _usage,
    vault_lint,
)
from knowledge.errors import KnowledgeConfigError, KnowledgeToolError, MemoryUnavailable
from knowledge.ports import (
    SYNC_SKIPPED_PULL_FALSE,
    ChunkFilter,
    ChunkHit,
    DocRecord,
    Judge,
    LexicalIndex,
    NullJudge,
    VaultSync,
)
from knowledge.registry import VaultRegistry, VaultRuntime
from knowledge.stores import SCHEMA_VERSION

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


class KnowledgeMCP(FastMCP):
    """FastMCP that counts every ``tools/call`` it serves, errors included.

    The count goes to the usage tracker of the default vault (section
    ``tool_calls`` of its ``usage_stats.json``): the tool name, the client
    that sent the call and the principal of its bearer, never an argument.
    How long the call took goes to section ``tool_latency``, as one count in
    a fixed bucket under the day and the tool name alone. The timer only reads
    the clock around the call.
    The response is the one FastMCP builds, untouched, and a count that
    cannot be recorded is logged and never fails the call. The count is
    recorded off the event loop and the response does not wait for it: a
    flush that waits on another process's file lock delays neither this call
    nor any other.

    A call to a tool this server does not register but the contract knows
    answers with a stable code instead of the SDK's unknown-tool text: a
    retired tool with ``E_TOOL_RETIRED`` and where to go instead, an
    extension tool no served vault turns on with ``E_FEATURE_DISABLED``. A
    client that cached an older catalog gets an answer it can act on.
    """

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Sequence[ContentBlock] | dict[str, Any]:
        failed = True
        started = time.perf_counter()
        try:
            if self._tool_manager.get_tool(name) is None:
                _refuse_unregistered(name)
            result = await super().call_tool(name, arguments)
            failed = isinstance(result, CallToolResult) and bool(result.isError)
            return result
        finally:
            _count_call(self, name, failed=failed, latency_ms=(time.perf_counter() - started) * 1000.0)


def _refuse_unregistered(name: str) -> None:
    """Raise the coded error of a contract tool this server does not register; return for any other name."""
    if name in contract.TOOL_REMOVED:
        raise KnowledgeToolError(errors.E_TOOL_RETIRED, contract.retired_message(name))
    extension = contract.TOOL_EXTENSION.get(name)
    if extension is None:
        return
    s = _settings.current()
    declared = extensions.declaring(s.vaults, extension)
    if declared:
        transport = s.http.transport.lower().strip() or extensions.STDIO
        raise KnowledgeToolError(
            errors.E_FEATURE_DISABLED,
            f"the {extension} extension of {name} is not loaded over {transport}; "
            f"vaults that declare it: {', '.join(declared)}",
        )
    raise KnowledgeToolError(errors.E_FEATURE_DISABLED, contract.unloaded_message(name))


def _caller(app: FastMCP) -> tuple[str | None, str | None]:
    """``(client, principal)`` of the running MCP request; ``(None, None)`` outside one.

    The client is the ``clientInfo.name`` the session sent in ``initialize``.
    The principal is the id the bearer middleware left on the HTTP request,
    :data:`knowledge.usage.STDIO_PRINCIPAL` for a call without one on a stdio
    server, and ``None`` (reported as unknown) for any other case. The
    bearer itself is never read here.
    """
    try:
        ctx = app._mcp_server.request_context
    except LookupError:
        return None, None
    params = getattr(ctx.session, "client_params", None)
    client = getattr(getattr(params, "clientInfo", None), "name", None)
    request = ctx.request
    if request is None:
        stdio = _settings.current().http.transport.lower().strip() != "http"
        return client, _usage.STDIO_PRINCIPAL if stdio else None
    principal = getattr(getattr(request, "state", None), "knowledge_principal", None)
    return client, principal if isinstance(principal, str) else None


# One worker, so counts are recorded in the order the calls ended and a count
# that waits on the file lock never takes a thread from the offloaded tools.
_COUNTS = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="tool-call-count")


def _count_call(app: FastMCP, name: str, *, failed: bool, latency_ms: float | None = None) -> None:
    """Queue one count of ``name`` for the default vault's tracker; never blocks the caller.

    The client and the principal are read here, inside the request, and the
    count itself runs on the counting worker: recording it can take the usage
    file's lock (every few calls the tracker flushes), and that lock may be
    held by another process on the same vault.
    """
    try:
        client, principal = _caller(app)
        _COUNTS.submit(_record_count, name, client, principal, failed, latency_ms)
    except Exception:
        log.exception("tool-call count failed — the call is unaffected")


def _record_count(
    name: str, client: str | None, principal: str | None, failed: bool, latency_ms: float | None = None
) -> None:
    """Count one call of ``name`` on the default vault's tracker, when that vault is open.

    ``serve`` opens the default vault before it serves, so every call of a
    running server is counted; a registry not built yet or a default vault
    not opened yet (a test, a script) is left alone instead of being opened
    just to count.
    """
    try:
        reg = _registry
        runtime = reg.default_if_open() if reg is not None else None
        if runtime is None:
            return
        runtime.usage.record_call(name, client=client, principal=principal, error=failed, latency_ms=latency_ms)
    except Exception:
        log.exception("tool-call count failed — the call is unaffected")


def drain_tool_call_counts(timeout: float | None = None) -> None:
    """Wait until every count queued so far is recorded; ``TimeoutError`` after ``timeout`` seconds."""
    _COUNTS.submit(lambda: None).result(timeout=timeout)


# The CloudFrog mark, shipped in the package and sent inline as a data: URI:
# over stdio there is no origin to serve it from, and a client fetches an icon
# only from the server's own origin. Not contract: serverInfo carries it.
SERVER_ICON = Icon(
    src="data:image/svg+xml;base64,"
    + base64.b64encode(Path(__file__).with_name("icon.svg").read_bytes()).decode("ascii"),
    mimeType="image/svg+xml",
    sizes=["any"],
)

# The host is a literal on purpose: FastMCP turns on DNS-rebinding protection
# when it is constructed with a loopback host, so the real bind address is
# applied in serve() (see _configure_transport), never at import. The
# instructions announce the contract version in the handshake.
mcp = KnowledgeMCP("Knowledge", host="0.0.0.0", instructions=contract.INSTRUCTIONS, icons=[SERVER_ICON])

_registry: VaultRegistry | None = None


def vault_sync_for(spec: _settings.VaultSpec) -> VaultSync | None:
    """How a vault is kept fresh: git runs only when it declares a repository.

    A vault declares one with ``repo`` in ``vaults.yaml`` or, for a
    ``VAULT_PATH`` deployment, with ``VAULT_REMOTE``; its checkout then follows
    the served branch (``git.branch``, ``VAULT_BRANCH``) on ``origin``. Without
    a repository no git command runs at all, not even a probe of whether the
    directory is a checkout.
    """
    if spec.repo is not None and spec.git_auto_pull:
        return gitsync.git_vault_sync(spec.git_branch)
    return None


def _sync_state_for(rt: VaultRuntime) -> Callable[[], dict[str, Any]] | None:
    """What ``/health`` reports about the git checkout of ``rt``; ``None`` for a vault without a repository."""
    if rt.spec.repo is None:
        return None
    vault_path = rt.vault_path
    return lambda: gitsync.sync_state(vault_path)


def registry() -> VaultRegistry:
    """The vaults this process serves, built on first use.

    ``serve()`` installs it from the settings it validated; anything that
    reaches a tool without going through ``serve()`` (the test suite, scripts)
    gets one built from the process-wide settings snapshot.
    """
    global _registry
    if _registry is None:
        _registry = _registry_for(_settings.current())
    return _registry


def _registry_for(s: _settings.Settings) -> VaultRegistry:
    """A registry over the vaults ``s`` declares, with git sync, memory reads and judges wired in."""
    return VaultRegistry.from_settings(
        s,
        sync_factory=vault_sync_for,
        memory_factory=memory_http.factory(s.memory),
        judge_factory=_judge_factory(s.rerank_operator),
    )


def _judge_factory(operator: _settings.RerankOperator) -> Callable[[_settings.VaultSpec], Judge]:
    """How a vault gets its judge: the ``rerank`` extension builds it when the vault declares one.

    A vault without a ``rerank`` block, or whose provider the operator rules
    out, gets a :class:`~knowledge.ports.NullJudge` and the extension module is
    never imported.
    """

    def build(spec: _settings.VaultSpec) -> Judge:
        if not spec.rerank.configured or not operator.allows(spec.rerank.provider):
            return NullJudge()
        judge: Judge = _load_extension("rerank").build_judge(spec.rerank, operator)
        return judge

    return build


def set_registry(reg: VaultRegistry | None) -> None:
    """Install (or, with ``None``, drop) the registry the tools serve."""
    global _registry
    _registry = reg


def default_runtime() -> VaultRuntime:
    """The runtime of the default vault: the one a tool serves without ``vault``."""
    return registry().default


# The vault the running tool call serves, by slug; ``None`` is the default
# vault. Set by ``_in_vault`` for the duration of one call, inside the thread
# the call runs in, so two concurrent calls never see each other's vault.
_current_vault: ContextVar[str | None] = ContextVar("knowledge_current_vault", default=None)


def _vault() -> VaultRuntime:
    """The runtime of the vault the running tool call serves, opened on first use."""
    slug = _current_vault.get()
    return default_runtime() if slug is None else registry().get(slug)

# Read-only aliases kept for callers that address the default runtime's
# pieces as module attributes (``server._indexer``, ``server._index_state``).
_RUNTIME_ALIASES: dict[str, str] = {
    "_indexer": "indexer",
    "_usage": "usage",
    "_index_state": "index_state",
    "_hebbian_dirty": "hebbian_dirty",
}


def __getattr__(name: str) -> Any:
    if name in _RUNTIME_ALIASES:
        return getattr(default_runtime(), _RUNTIME_ALIASES[name])
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _ensure_indexed(rt: VaultRuntime | None = None) -> VaultRuntime:
    """Lazy indexing: runs once on first tool invocation instead of at import time."""
    rt = rt or _vault()
    if rt.index_state["ready"]:
        return rt
    try:
        if not rt.indexer.is_indexed():
            log.info("No existing index found — running initial indexing…")
            stats = rt.indexer.index()
            log.info("Initial indexing done: %s", stats)
        elif rt.indexer.stale_identity():
            # Built under another provider, model or chunk plan: its vectors
            # and chunks cannot answer this configuration, so it is rebuilt
            # before the first answer instead of serving a mix.
            log.info("Index built under another configuration — rebuilding before serving…")
            stats = rt.indexer.index()
            log.info("Rebuild done: %s", stats)
        else:
            rt.indexer.load_graph()
            log.info("Loaded existing index (%d chunks).", rt.indexer.store.count())
    except Exception:
        log.exception("Indexing failed — tools will work without index until reindex is called")
    rt.index_state["ready"] = True
    return rt


def _offloaded(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Run a blocking tool in a worker thread.

    FastMCP awaits plain ``def`` tools inline on the event loop, so a single
    call that touches the index, the filesystem or the network freezes the whole
    server — /health and every other tool included — until it returns. Applied
    below ``@_tool`` so the registered callable is the async wrapper while
    functools.wraps keeps the original signature the schema is built from.
    """

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(functools.partial(fn, *args, **kwargs))

    return wrapper


def _require_admin(tool: str) -> None:
    """Refuse the running call unless its HTTP bearer holds the ``admin`` scope.

    The auth middleware leaves the scopes of the bearer on the Starlette
    request (``request.state.knowledge_scopes``), and the MCP SDK hands that
    same request to the tool call (``RequestContext.request``). A call without
    an MCP request context is a direct Python call, and a request context
    without a request is stdio, where the process runs as the person: both
    pass. A request context without a request on a server configured for
    HTTP is refused, and so is an HTTP request whose scopes are missing, so a
    path that does not carry them fails closed.
    """
    try:
        request = mcp.get_context().request_context.request
    except (LookupError, ValueError):
        return
    if request is None:
        if _settings.current().http.transport.lower().strip() != "http":
            return
        raise KnowledgeToolError(
            errors.E_SCOPE_REQUIRED,
            f"{tool} requires the admin scope; the HTTP call carries no request to read it from",
        )
    state = getattr(request, "state", None)
    scopes = getattr(state, "knowledge_scopes", None)
    if not scopes or _settings.SCOPE_ADMIN not in scopes:
        principal = getattr(state, "knowledge_principal", None) or "unknown"
        raise KnowledgeToolError(
            errors.E_SCOPE_REQUIRED,
            f"{tool} requires the admin scope; bearer {principal} lacks it",
        )


def _admin(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Run ``fn`` only for a caller that holds the ``admin`` scope (see ``_require_admin``).

    Applied right below ``@_tool``, over the async callable ``_offloaded`` or
    the tool itself returns, so the check runs on the event loop, in the MCP
    request context, before any work starts or any vault is resolved.
    ``functools.wraps`` keeps the signature the input schema is built from.
    """
    if not inspect.iscoroutinefunction(fn):
        raise TypeError(f"{fn.__name__} must be async (apply _admin above _offloaded)")

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        _require_admin(fn.__name__)
        return await fn(*args, **kwargs)

    return wrapper


def _enter_vault(signature: inspect.Signature, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Token[str | None]:
    """Validate the call's ``vault`` argument and make it the current vault.

    An unknown slug fails here, before the tool does any work, with
    ``E_VAULT_UNKNOWN`` and the registered slugs. Only the spec is read: the
    runtime opens when the tool first needs it.
    """
    slug = signature.bind_partial(*args, **kwargs).arguments.get("vault")
    if slug is not None:
        registry().spec(slug)
    return _current_vault.set(slug)


def _in_vault(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Serve one tool call from the vault its ``vault`` argument names.

    The tool declares ``vault: str | None = None`` in its own signature, which
    is what puts the parameter in the input schema; this decorator only
    resolves it into the context variable ``_vault()`` reads, and restores the
    previous value when the call ends. Applied below ``_offloaded``, so for a
    plain ``def`` tool the variable is set inside the worker thread the call
    runs in, never on the event loop.
    """
    signature = inspect.signature(fn)
    if "vault" not in signature.parameters:
        raise TypeError(f"{fn.__name__} must declare a 'vault' parameter")

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            token = _enter_vault(signature, args, kwargs)
            try:
                return await fn(*args, **kwargs)
            finally:
                _current_vault.reset(token)

        return async_wrapper

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        token = _enter_vault(signature, args, kwargs)
        try:
            return fn(*args, **kwargs)
        finally:
            _current_vault.reset(token)

    return wrapper


# ---------------------------------------------------------------------------
# Registration and the wire form of a result
# ---------------------------------------------------------------------------

#: Every tool function by name, in registration order. The module attributes
#: stay the plain functions (callers in this process get the raw value back);
#: what the server registers is the wrapper ``_wired`` builds around each.
_TOOL_FUNCTIONS: dict[str, Callable[..., Any]] = {}


def _text_blocks(result: Any, indent: int | None) -> list[TextContent]:
    """The text blocks of ``result``: one per list element, JSON unless a string.

    The same split FastMCP applies, with the indentation chosen by
    ``KNOWLEDGE_TEXT_INDENT`` (compact when unset) instead of a fixed 2.
    """
    if result is None:
        return []
    if isinstance(result, list | tuple):
        return [block for item in result for block in _text_blocks(item, indent)]
    text = result if isinstance(result, str) else pydantic_core.to_json(result, fallback=str, indent=indent).decode()
    return [TextContent(type="text", text=text)]


#: ``_meta`` of the running tool call's result. ``_wired`` installs an empty
#: dict before it calls the tool and turns whatever the tool put in it into
#: ``CallToolResult._meta``. A dict, not a value: an offloaded tool runs in a
#: copy of the context (``asyncio.to_thread``), where setting the variable
#: would not reach the event loop, but mutating the shared dict does.
_tool_meta: ContextVar[dict[str, Any] | None] = ContextVar("knowledge_tool_meta", default=None)


def _set_result_meta(key: str, value: Any) -> None:
    """Attach ``value`` under ``key`` to the ``_meta`` of the running tool call's result, if any."""
    sink = _tool_meta.get()
    if sink is not None:
        sink[key] = value


def _require_feature(tool: str, extension: str, slug: str | None) -> None:
    """Refuse a call of an extension tool on a vault whose ``vault.yaml`` does not declare the extension.

    A process that serves several vaults registers the tool when any of
    them declares it; the vault the call names (or the default one) must
    declare it too. The message names the vaults that do, so a call without
    ``vault=`` knows which slug to pass.
    """
    reg = registry()
    target = reg.default_slug if slug is None else slug
    if extension in extensions.enabled(reg.spec(target)):
        return
    declared = extensions.declaring((reg.spec(s) for s in reg.slugs()), extension)
    hint = f"; use vault= with one that does: {', '.join(declared)}" if declared else ""
    raise KnowledgeToolError(
        errors.E_FEATURE_DISABLED,
        f"{target} does not declare features: [{extension}], which brings {tool}; "
        f"add it to its vault.yaml by pull request{hint}",
    )


def _wired(fn: Callable[..., Any], extension: str | None = None) -> tuple[Callable[..., Any], dict[str, bool]]:
    """The callable the server registers for ``fn`` and the cell it reads its shape from.

    It returns a finished ``CallToolResult``: the text blocks serialized here
    and, when the registered tool declares an ``outputSchema``, the
    ``structuredContent`` FastMCP would have derived (wrapped in ``result`` for
    the same return types). The cell is filled from the registered tool right
    after registration, so both decisions are FastMCP's own.
    ``functools.wraps`` keeps the signature and the return annotation, so the
    input schema and the ``outputSchema`` are those of ``fn``. With
    ``extension``, the vault of the call must declare it
    (:func:`_require_feature`) before ``fn`` runs.
    """
    shape = {"structured": False, "wrap": False}

    @functools.wraps(fn)
    async def wired(*args: Any, **kwargs: Any) -> CallToolResult:
        if extension is not None:
            _require_feature(fn.__name__, extension, kwargs.get("vault"))
        meta: dict[str, Any] = {}
        token = _tool_meta.set(meta)
        try:
            result = fn(*args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
        finally:
            _tool_meta.reset(token)
        structured: Any = None
        if shape["structured"]:
            payload = {"result": result} if shape["wrap"] else result
            structured = pydantic_core.to_jsonable_python(payload, fallback=str)
        return CallToolResult(
            content=list(_text_blocks(result, _settings.current().output.text_indent)),
            structuredContent=structured,
            isError=False,
            _meta=meta or None,
        )

    return wired, shape


def _untitled(schema: Any) -> Any:
    """A JSON schema without the ``title`` pydantic writes on every node.

    Only schema nodes lose their ``title``; the names under ``properties``
    are parameter names and stay, whatever they are called.
    """
    if isinstance(schema, list):
        return [_untitled(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "title":
            continue
        if key in ("properties", "$defs") and isinstance(value, dict):
            out[key] = {name: _untitled(sub) for name, sub in value.items()}
        else:
            out[key] = _untitled(value)
    return out


def _add_tool(app: FastMCP, fn: Callable[..., Any], *, structured: bool, extension: str | None = None) -> None:
    """Register ``fn`` on ``app`` with its catalog description, ``_meta`` and output mode.

    ``extension`` makes every call check that its vault declares it.
    """
    name = fn.__name__
    meta = contract.TOOL_META.get(name)
    wired, shape = _wired(fn, extension)
    app.add_tool(
        wired,
        name=name,
        description=contract.DESCRIPTIONS[name],
        meta=dict(meta) if meta is not None else None,
        structured_output=None if structured else False,
    )
    tool = app._tool_manager.get_tool(name)
    assert tool is not None
    tool.parameters = _untitled(tool.parameters)
    shape["structured"] = tool.fn_metadata.output_schema is not None
    shape["wrap"] = tool.fn_metadata.wrap_output


def _tool(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Register ``fn`` as a core tool of ``mcp`` and return it unchanged."""
    _TOOL_FUNCTIONS[fn.__name__] = fn
    _add_tool(mcp, fn, structured=True)
    return fn


#: Tool functions the contract retired, by name. ``_legacy_tool`` keeps them
#: here; only the ``legacy`` profile registers them.
_LEGACY_TOOL_FUNCTIONS: dict[str, Callable[..., Any]] = {}


def _legacy_tool(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Keep ``fn`` as a retired tool, registered only under the ``legacy`` profile, and return it unchanged."""
    _LEGACY_TOOL_FUNCTIONS[fn.__name__] = fn
    return fn


def register_tools(app: FastMCP, *, structured: bool, profile: str = profiles.DEFAULT_PROFILE) -> None:
    """Re-register the tools of ``profile`` on ``app``, in its order, with or without structured output.

    ``all`` registers the core tools in their contract order; the gated
    extensions follow through :func:`register_gated_tools`. ``legacy``
    registers the catalog of contract 1.4.0 in its order
    (:data:`knowledge.profiles.LEGACY_ORDER`): the core tools, the extension
    tools 1.4.0 always served (without checking any ``features``) and the
    retired ones. FastMCP decides structured output when a tool is added, so
    every tool is removed first and added again, and none moves to the end.
    """
    for tool in app._tool_manager.list_tools():
        app.remove_tool(tool.name)
    if profile == profiles.LEGACY_PROFILE:
        functions = {**_TOOL_FUNCTIONS, **_GATED_TOOL_FUNCTIONS, **_LEGACY_TOOL_FUNCTIONS}
        for name in profiles.LEGACY_ORDER:
            _add_tool(app, functions[name], structured=structured)
        return
    for name in profiles.select(profile, list(_TOOL_FUNCTIONS)):
        _add_tool(app, _TOOL_FUNCTIONS[name], structured=structured)


# ---------------------------------------------------------------------------
# Extensions
# ---------------------------------------------------------------------------

#: Tool functions of gated extensions, by name. ``_gated_tool`` keeps them
#: here without registering them; ``register_gated_tools`` adds the ones a
#: served vault turns on, after every other tool.
_GATED_TOOL_FUNCTIONS: dict[str, Callable[..., Any]] = {}

#: The extension each gated tool function belongs to, by tool name.
_GATED_EXTENSION: dict[str, str] = {}

#: Extension modules already imported, by extension name.
_LOADED_EXTENSIONS: dict[str, ModuleType] = {}


def _gated_tool(extension: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Keep the decorated function as a tool of the gated ``extension`` and return it unchanged.

    It is not registered here: ``register_gated_tools`` registers it when a
    vault the process serves declares its extension.
    """

    def keep(fn: Callable[..., Any]) -> Callable[..., Any]:
        _GATED_TOOL_FUNCTIONS[fn.__name__] = fn
        _GATED_EXTENSION[fn.__name__] = extension
        return fn

    return keep


def _load_extension(name: str) -> ModuleType:
    """The module that implements extension ``name``, imported the first time it is asked for.

    This is the only place in the package that imports a module by name:
    ``knowledge.extensions`` describes each module as a string so the core
    layer never depends on an extension.
    """
    module = _LOADED_EXTENSIONS.get(name)
    if module is None:
        ext = extensions.get(name)
        if ext.module is None:
            raise RuntimeError(f"extension {name!r} has no module to load")
        module = importlib.import_module(ext.module)
        _LOADED_EXTENSIONS[name] = module
    return module


def load_extension(name: str) -> ModuleType:
    """Public name of :func:`_load_extension`, for the command line."""
    return _load_extension(name)


def register_gated_tools(app: FastMCP, s: _settings.Settings, *, structured: bool) -> list[str]:
    """Register on ``app`` the tools of every gated extension a vault of ``s`` turns on.

    They go after every other tool, in registry order, so the catalog of the
    tools that are always there keeps its order, and every call checks that
    its vault declares the extension. A tool already registered (the
    ``legacy`` profile registers the 1.4.0 ones) is left as it is. Returns
    the names added.
    """
    added: list[str] = []
    for ext in extensions.loaded_gated(s.vaults, s.http.transport):
        for name in ext.tools:
            if app._tool_manager.get_tool(name) is None:
                _add_tool(app, _GATED_TOOL_FUNCTIONS[name], structured=structured, extension=ext.name)
                added.append(name)
    return added


def extension_warnings(s: _settings.Settings, env: Mapping[str, str]) -> list[str]:
    """Check the extensions the vaults of ``s`` declare against ``env``; return the warnings.

    Over HTTP, an extension the transport loads but whose credentials are
    missing stops the server with ``E_SECRET_MISSING`` (names, never values);
    one the transport does not load is reported with
    ``W_EXTENSION_UNAVAILABLE_HTTP`` and its tools stay absent. Over stdio the
    process runs as the person, so missing credentials only warn
    (``W_EXTENSION_UNCONFIGURED``) and the extension answers with the error
    when it is called. Credentials an extension can do without
    (``env_optional``) are left to the settings' own warnings.
    """
    transport = extensions.HTTP if s.http.transport.lower().strip() == extensions.HTTP else extensions.STDIO
    warnings: list[str] = []
    for spec in s.vaults:
        for ext in extensions.EXTENSIONS:
            if ext.name not in extensions.enabled(spec):
                continue
            if not extensions.loads_on(ext, transport):
                if transport == extensions.HTTP:
                    warnings.append(
                        f"{errors.W_EXTENSION_UNAVAILABLE_HTTP}: {spec.slug}: the {ext.name} extension "
                        "is not loaded over HTTP; its tools are absent"
                    )
                continue
            missing = extensions.missing_env(spec, ext, env)
            if not missing or ext.env_optional:
                continue
            names = ", ".join(missing)
            if transport == extensions.HTTP:
                raise KnowledgeConfigError(
                    errors.E_SECRET_MISSING,
                    f"{spec.slug}: the {ext.name} extension needs {names}; export them or drop "
                    f"{ext.name!r} from the vault's features",
                )
            warnings.append(
                f"{errors.W_EXTENSION_UNCONFIGURED}: {spec.slug}: the {ext.name} extension needs {names}; "
                "it answers with an error until they are exported"
            )
    return warnings


# ---------------------------------------------------------------------------
# Field projection and section pointers
# ---------------------------------------------------------------------------

#: Keys a row or a document reads from the file on disk, computed only when the row carries them.
_ON_DEMAND = frozenset({"doc_id", "chunk_id", "section", "snippet", "sections"})

#: Row keys of ``search``, in the order a projected row carries them.
SEARCH_FIELDS: tuple[str, ...] = (
    "file_path",
    "title",
    "score",
    "excerpt",
    "needs_review",
    "age_days",
    "doc_id",
    "chunk_id",
    "section",
    "snippet",
    "rerank_score",
)

#: Row keys of ``search_deep``, in the order a projected row carries them.
SEARCH_DEEP_FIELDS: tuple[str, ...] = (
    "file_path",
    "title",
    "score",
    "excerpt",
    "discovered_via",
    "needs_review",
    "age_days",
    "doc_id",
    "chunk_id",
    "section",
    "snippet",
)

#: Keys of a ``get_document`` result, in the order a projected result carries them.
GET_DOCUMENT_FIELDS: tuple[str, ...] = (
    "file_path",
    "doc_id",
    "metadata",
    "content",
    "truncated",
    "next_offset",
    "chunk_id",
    "section",
    "sections",
)

#: Row keys ``search`` returns without ``fields``: where to read next, never text.
#: ``title`` only when it says more than the file name (:func:`_drop_redundant_title`);
#: ``needs_review``, ``age_days`` and ``rerank_score`` when the row has them.
SEARCH_DEFAULT_FIELDS = frozenset(
    {"file_path", "title", "score", "needs_review", "age_days", "chunk_id", "section", "rerank_score"}
)

#: Row keys ``search_deep`` returns without ``fields``: the ``search`` row plus how it was reached.
SEARCH_DEEP_DEFAULT_FIELDS = frozenset(
    {"file_path", "title", "score", "discovered_via", "needs_review", "age_days", "chunk_id", "section"}
)

#: Most rows one ``search`` and one ``search_deep`` call may ask for.
SEARCH_LIMIT_MAX = 50
SEARCH_DEEP_LIMIT_MAX = 20

#: Frontmatter keys ``get_document`` returns under ``metadata`` unless ``full_metadata=True``:
#: the ones the server indexes or reviews with.
DOCUMENT_METADATA_KEYS: tuple[str, ...] = ("type", "status", "service", "owner", "tags", "last_updated")

#: Characters ``get_document`` returns when the call does not say ``max_chars``.
DOCUMENT_MAX_CHARS = 3000

#: Most headings an ``E_SECTION_NOT_FOUND`` message lists.
SECTION_HINT_LIMIT = 20

#: Row keys of ``search_by_metadata``, in the order a projected row carries them.
SEARCH_BY_METADATA_FIELDS: tuple[str, ...] = ("file_path", "title", "type", "service", "status", "tags", "doc_id")

#: Frontmatter fields ``search_by_metadata(facets=True)`` counts, in row order.
FACET_FIELDS: tuple[str, ...] = ("type", "service", "status", "tags")

#: Row keys of ``get_related``, in the order a projected row carries them.
GET_RELATED_FIELDS: tuple[str, ...] = (
    "file_path",
    "direction",
    "weight",
    "confidence",
    "shared_neighbors",
    "doc_id",
)

#: ``limit`` of ``search_by_metadata`` when the call does not say it, and the most it may ask for.
METADATA_LIMIT_DEFAULT = 20
METADATA_LIMIT_MAX = 200

#: Most rows one ``get_related`` and one ``suggest_links`` call may ask for.
RELATED_LIMIT_MAX = 200
SUGGEST_LIMIT_MAX = 200

#: Most rows one ``get_stale_docs`` call may ask for.
STALE_LIMIT_MAX = 500

#: ``max_items`` of ``get_impact`` when the call does not say it, and the most it may ask for.
IMPACT_MAX_ITEMS_DEFAULT = 20
IMPACT_MAX_ITEMS_MAX = 1000

#: Decimals of a ``get_related`` weight.
WEIGHT_DIGITS = 3

#: Most headings ``get_document`` lists under ``sections``.
SECTIONS_LIMIT = 30

#: ``search_deep`` cuts excerpts (and snippets) at this many characters.
_DEEP_EXCERPT_CHARS = 120

_POINTER_KEYS = frozenset({"chunk_id", "section", "snippet"})


def _fields_arg(fields: Sequence[str] | None, allowed: Sequence[str]) -> frozenset[str] | None:
    """The validated ``fields`` argument; ``None`` keeps the full default shape."""
    if fields is None:
        return None
    unknown = sorted({name for name in fields if name not in allowed})
    if unknown or not fields:
        named = f"unknown field(s) {unknown}" if unknown else "fields is empty"
        raise KnowledgeToolError(errors.E_FIELD_UNKNOWN, f"{named}; allowed: {', '.join(allowed)}")
    return frozenset(fields)


def _project(payload: dict[str, Any], canonical: Sequence[str], keep: frozenset[str]) -> dict[str, Any]:
    """``payload`` restricted to ``keep``, in ``canonical`` order."""
    return {key: payload[key] for key in canonical if key in keep and key in payload}


def _limit_arg(name: str, value: int, low: int, high: int) -> None:
    """Refuse ``value`` outside ``[low, high]`` before anything is searched or counted."""
    if not low <= value <= high:
        raise KnowledgeToolError(errors.E_ARGUMENT_OUT_OF_RANGE, f"{name} must be {low}-{high}, got {value}")


def _redundant_title(title: Any, file_path: str) -> bool:
    """Whether ``title`` says nothing the file name of ``file_path`` does not."""
    text = str(title or "").strip()
    return not text or text == os.path.splitext(os.path.basename(file_path))[0]


def _drop_redundant_title(row: dict[str, Any]) -> dict[str, Any]:
    """``row`` without ``title`` when the title is empty or the file name of its ``file_path``."""
    if "title" in row and _redundant_title(row["title"], str(row.get("file_path") or "")):
        return {key: value for key, value in row.items() if key != "title"}
    return row


def _vault_file(rt: VaultRuntime, file_path: str) -> Path | None:
    """The regular file ``file_path`` names inside the vault of ``rt``; ``None`` for any other path.

    The path is resolved, symlinks included, before it is checked: ``..``
    segments, an absolute path or a link that lands outside the vault name no
    vault file, and the caller answers exactly as for a file that does not
    exist, so a reader cannot probe the server's filesystem.
    """
    root = rt.vault_path.resolve()
    try:
        full_path = (root / file_path).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if not full_path.is_relative_to(root) or not full_path.is_file():
        return None
    return full_path


def _read_body(rt: VaultRuntime, file_path: str) -> str | None:
    """The Markdown body of a vault file without its frontmatter; ``None`` if it is gone or outside the vault."""
    full_path = _vault_file(rt, file_path)
    if full_path is None:
        return None
    try:
        return str(frontmatter.load(str(full_path)).content)
    except Exception:
        return full_path.read_text(errors="replace")


def _query_idf(rt: VaultRuntime, query: str) -> dict[str, float]:
    """Each query term with its idf over the vault's keyword index."""
    index = rt.indexer.keyword_index
    return {term: index.idf(term) for term in pointers.query_terms(query)}


def _block_at(body: str, ordinal: int) -> pointers.Selection | None:
    """The block ``ordinal`` of ``body`` as a selection; ``None`` when the body no longer has it."""
    heads = pointers.headings(body)
    if ordinal > len(heads):
        return None
    section = heads[ordinal - 1].text if ordinal else None
    return pointers.Selection(ordinal, section, pointers.block(body, ordinal, heads))


def _with_pointers(
    rt: VaultRuntime,
    row: dict[str, Any],
    wanted: frozenset[str],
    idf: dict[str, float],
    width: int,
    ordinal: int | None = None,
) -> dict[str, Any]:
    """``row`` plus the pointer keys in ``wanted``, read from the file on disk.

    ``ordinal`` names the block to point at (the one a judge chose); without
    it the block is the one the query terms hit hardest.
    """
    out = dict(row)
    file_path = str(row.get("file_path") or "")
    doc = pointers.doc_id(file_path)
    if "doc_id" in wanted:
        out["doc_id"] = doc
    if wanted & _POINTER_KEYS:
        body = _read_body(rt, file_path) if file_path else None
        hit = None
        if body is not None and ordinal is not None:
            hit = _block_at(body, ordinal)
        if hit is None and body is not None:
            hit = pointers.select_section(body, idf)
        if "chunk_id" in wanted:
            out["chunk_id"] = pointers.chunk_id(doc, hit.ordinal) if hit else None
        if "section" in wanted:
            out["section"] = hit.section if hit else None
        if "snippet" in wanted:
            out["snippet"] = pointers.snippet(hit.block, idf, width) if hit else None
    return out


def _shape_rows(
    rows: list[dict[str, Any]],
    *,
    query: str,
    wanted: frozenset[str] | None,
    snippet: bool,
    width: int,
    canonical: Sequence[str],
    default: frozenset[str],
    chosen: Mapping[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Search rows with the pointer keys asked for, projected onto ``wanted`` or ``default``.

    Without ``fields`` a row carries ``default``: the pointers to read next
    (``chunk_id``, ``section``), never text; ``snippet=True`` adds a snippet
    either way. ``title`` stays only when ``fields`` names it or it says more
    than the file name. Pointers only read files; they never touch usage
    counters or link weights. ``chosen`` maps a file to the ordinal of the
    block a judge preferred, which its pointers name.
    """
    keep = (default if wanted is None else wanted) | ({"snippet"} if snippet else frozenset())
    extra = keep & _ON_DEMAND
    if extra:
        rt = _vault()
        idf = _query_idf(rt, query) if extra & _POINTER_KEYS else {}
        rows = [
            _with_pointers(rt, row, extra, idf, width, (chosen or {}).get(str(row.get("file_path") or "")))
            for row in rows
        ]
    shaped = [_project(row, canonical, keep) for row in rows]
    if wanted is not None and "title" in wanted:
        return shaped
    return [_drop_redundant_title(row) for row in shaped]


# Review-decay thresholds, corpus exclusions and owner resolution live in
# knowledge.review so this server and the offline weekly report
# (scripts/weekly-stale-report.py, which reads the vault without an index)
# cannot drift apart.


# ---------------------------------------------------------------------------
# Usage tracking + Hebbian reinforcement
# ---------------------------------------------------------------------------

_HEBBIAN_SAVE_EVERY = 10


def _track_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Record retrieval usage and reinforce co-retrieved linked pairs.

    Usage feeds the bounded ranking boost; co-retrieval within the 30-day
    window strengthens existing graph edges (Hebbian: docs consulted
    together wire together). Graph saves are debounced.
    """
    fps = [r["file_path"] for r in results if r.get("file_path")]
    if not fps:
        return results
    rt = _vault()
    try:
        rt.usage.record_uses(fps)
        rt.usage.record_pairs(fps)

        graph = rt.indexer.graph
        changed = 0
        for i, a in enumerate(fps):
            for b in fps[i + 1 :]:
                if b in graph.neighbors(a) and graph.reinforce(a, b, rt.usage.co_occurrence(a, b)):
                    changed += 1
        if changed:
            rt.hebbian_dirty["count"] += changed
            if rt.hebbian_dirty["count"] >= _HEBBIAN_SAVE_EVERY:
                rt.indexer.save_graph()
                rt.hebbian_dirty["count"] = 0
    except Exception:
        log.exception("usage tracking failed — results unaffected")
    return results


def _record_query(query: str, results: list[dict[str, Any]], tool: str) -> list[dict[str, Any]]:
    """Remember what a search tool returned so a later read can be paired with it.

    Called from the tool bodies, not from ``_search_impl``: ``search_deep``
    seeds through ``_search_impl``, and a seed list is not what its caller saw.
    Returns ``results`` untouched; a failure here never reaches the response.
    """
    try:
        _vault().usage.record_query(query, [r["file_path"] for r in results if r.get("file_path")], tool=tool)
    except Exception:
        log.exception("query recording failed — results unaffected")
    return results


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@_tool
@_offloaded
@_in_vault
def get_document(
    file_path: str | None = None,
    section: str | None = None,
    start_line: int | None = None,
    end_line: int | None = None,
    max_chars: int = DOCUMENT_MAX_CHARS,
    include_metadata: bool | None = None,
    chunk_id: str | None = None,
    fields: list[str] | None = None,
    full_metadata: bool = False,
    vault: str | None = None,
) -> dict[str, Any]:
    """Read a vault document with section/line slicing and char-bounded pagination.

    ``chunk_id`` alone names the document and the block (the ``doc_id`` it
    carries resolves the path); with ``file_path`` it must belong to that
    document. ``metadata`` holds the frontmatter keys of
    :data:`DOCUMENT_METADATA_KEYS` the document has, and every key with
    ``full_metadata=True``. ``include_metadata`` is retired: any value is
    refused, so a caller written for 1.x learns it instead of silently
    getting the shorter metadata. ``max_chars`` is at least 1 and has no
    upper bound: a larger value reads further in one call.
    """
    wanted = _fields_arg(fields, GET_DOCUMENT_FIELDS)
    if include_metadata is not None:
        raise KnowledgeToolError(
            errors.E_ARGUMENT_RETIRED,
            "include_metadata was retired in contract 2.0.0; metadata carries "
            f"{', '.join(DOCUMENT_METADATA_KEYS)}: pass full_metadata=true for every frontmatter key, "
            "or fields without metadata to leave it out",
        )
    if max_chars < 1:
        raise KnowledgeToolError(
            errors.E_ARGUMENT_OUT_OF_RANGE,
            f"max_chars must be at least 1, got {max_chars}; the default reads {DOCUMENT_MAX_CHARS} characters",
        )
    if chunk_id is not None and (section is not None or start_line is not None or end_line is not None):
        raise KnowledgeToolError(
            errors.E_ARGUMENT_CONFLICT,
            "chunk_id names a block on its own; drop section, start_line and end_line",
        )
    rt = _vault()
    if file_path is None:
        if chunk_id is None:
            raise KnowledgeToolError(
                errors.E_ARGUMENT_MISSING, "pass chunk_id from a search row, or file_path to read a document"
            )
        file_path = _resolve_doc_id(rt, _parsed_chunk_id(chunk_id)[0])
    full_path = _vault_file(rt, file_path)
    if full_path is None:
        raise KnowledgeToolError(
            errors.E_FILE_NOT_FOUND,
            f"no document {file_path!r} in vault {rt.spec.slug!r}; take file_path from a search row, "
            "vault-relative and without wikilink brackets",
        )

    doc = pointers.doc_id(file_path)
    ordinal = _chunk_ordinal(rt, chunk_id, file_path, doc) if chunk_id is not None else None

    content = full_path.read_text(errors="replace")
    try:
        post = frontmatter.load(str(full_path))
        metadata = dict(post.metadata)
        body = post.content
    except Exception:
        metadata = {}
        body = content
    heads = pointers.headings(body) if ordinal is not None else []
    if ordinal is not None and ordinal > len(heads):
        raise KnowledgeToolError(
            errors.E_CHUNK_ID_INVALID,
            f"chunk_id {chunk_id!r} is past the last heading of {file_path!r} ({len(heads)} headings); "
            f"first headings: {[head.text for head in heads[:20]]}",
        )
    extracted = None
    if ordinal is None and section is not None:
        extracted = _extract_section(body, section)
        if extracted is None:
            listed = _list_headers(body)
            raise KnowledgeToolError(
                errors.E_SECTION_NOT_FOUND,
                f"no heading {section!r} in {file_path!r}; its headings"
                f"{f' (first {SECTION_HINT_LIMIT} of {len(listed)})' if len(listed) > SECTION_HINT_LIMIT else ''}: "
                f"{listed[:SECTION_HINT_LIMIT]}",
            )

    # Reading a document is the strongest usage signal — weight it double.
    rt.usage.record_uses([file_path], weight=2.0)
    try:
        rt.usage.record_open(file_path)
    except Exception:
        log.exception("query -> open pairing failed — read unaffected")

    out: dict[str, Any] = {
        "file_path": file_path,
        "metadata": metadata if full_metadata else _whitelisted_metadata(metadata),
    }

    if ordinal is not None:
        _bounded(out, pointers.block(body, ordinal, heads), max_chars)
        out["chunk_id"] = pointers.chunk_id(doc, ordinal)
        out["section"] = heads[ordinal - 1].text if ordinal else None
        return _document_fields(out, body, doc, wanted)

    # section wins over start_line/end_line if both provided
    if section is not None and (start_line is not None or end_line is not None):
        log.warning(
            "get_document: 'section' provided; ignoring start_line/end_line for %s",
            file_path,
        )

    # --- section mode ---
    if extracted is not None:
        # honour max_chars even within sections so callers can paginate huge sections
        _bounded(out, extracted, max_chars)
        return _document_fields(out, body, doc, wanted)

    # --- explicit line slicing (1-indexed, inclusive) ---
    if start_line is not None or end_line is not None:
        lines = body.splitlines()
        s = max(1, start_line) if start_line is not None else 1
        # if only start_line provided we still apply max_chars windowing below
        if end_line is not None:
            e = min(len(lines), end_line)
            _bounded(out, "\n".join(lines[s - 1 : e]), max_chars)
            return _document_fields(out, body, doc, wanted)
        # start_line only: take from that line forward, capped by max_chars
        _bounded(out, "\n".join(lines[s - 1 :]), max_chars)
        return _document_fields(out, body, doc, wanted)

    # --- default: char-bounded pagination from the top ---
    _bounded(out, body, max_chars)
    return _document_fields(out, body, doc, wanted)


def _whitelisted_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """The keys of :data:`DOCUMENT_METADATA_KEYS` that ``metadata`` carries, in the frontmatter's order."""
    return {key: value for key, value in metadata.items() if key in DOCUMENT_METADATA_KEYS}


def _parsed_chunk_id(chunk_id: str) -> tuple[str, int]:
    """``(doc_id, ordinal)`` of ``chunk_id``, or ``E_CHUNK_ID_INVALID``."""
    parsed = pointers.parse_chunk_id(chunk_id)
    if parsed is None:
        raise KnowledgeToolError(
            errors.E_CHUNK_ID_INVALID,
            f"chunk_id {chunk_id!r} is not '<doc_id>:<n>'; take it from a search row",
        )
    return parsed


def _bounded(out: dict[str, Any], text: str, max_chars: int) -> None:
    """Put ``text`` under ``content``, cut at ``max_chars`` with the truncation markers."""
    if len(text) > max_chars:
        out["content"] = text[:max_chars]
        out["truncated"] = True
        out["next_offset"] = max_chars
    else:
        out["content"] = text


def _document_fields(
    out: dict[str, Any], body: str, doc: str, wanted: frozenset[str] | None
) -> dict[str, Any]:
    """A ``get_document`` result with the keys ``fields`` asks for, in canonical order."""
    if wanted is None:
        return out
    if "doc_id" in wanted:
        out["doc_id"] = doc
    truncated_outline = False
    if "sections" in wanted:
        out["sections"], truncated_outline = pointers.outline(body, doc, limit=SECTIONS_LIMIT)
    projected = _project(out, GET_DOCUMENT_FIELDS, wanted)
    if truncated_outline:
        projected["sections_truncated"] = True
    return projected


def _resolve_doc_id(rt: VaultRuntime, doc: str) -> str:
    """The one vault path whose ``doc_id`` is ``doc``.

    A miss rebuilds the map from the files on disk once before it is reported,
    so a document added since the last lookup still resolves.
    """
    paths = rt.indexer.doc_id_paths(doc) or rt.indexer.doc_id_paths(doc, refresh=True)
    if not paths:
        raise KnowledgeToolError(errors.E_DOC_ID_UNKNOWN, f"no document of this vault has doc_id {doc!r}")
    if len(paths) > 1:
        raise KnowledgeToolError(
            errors.E_DOC_ID_AMBIGUOUS, f"doc_id {doc!r} is shared by {list(paths)}; read them by file_path"
        )
    return paths[0]


def _chunk_ordinal(rt: VaultRuntime, chunk_id: str, file_path: str, doc: str) -> int:
    """The heading ordinal ``chunk_id`` names inside ``file_path``.

    A ``chunk_id`` is only read inside the document it was issued for: one
    that belongs to another document is an argument conflict, and the message
    names the document it does belong to.
    """
    owner, ordinal = _parsed_chunk_id(chunk_id)
    if owner != doc:
        raise KnowledgeToolError(
            errors.E_ARGUMENT_CONFLICT,
            f"chunk_id {chunk_id!r} belongs to {_resolve_doc_id(rt, owner)!r}, not to file_path {file_path!r}",
        )
    return ordinal


def _rank_key(row: dict[str, Any]) -> tuple[float, str]:
    """Sort key of a search row: the emitted score descending, then the path.

    The key reads the score as the row carries it (already rounded where the
    tool rounds), so the published list is visibly ordered and rows that show
    the same score come out by ``file_path``, never in the walk order of a
    ``set``. ``limit`` cuts after this sort.
    """
    return (-row["score"], row["file_path"])


def _search_impl(
    query: str,
    limit: int = 5,
    mode: str = "hybrid",
    excerpt_chars: int = 120,
    include_memories: bool = False,
    *,
    track: bool = True,
    details: dict[str, tuple[float, int]] | None = None,
) -> list[dict[str, Any]]:
    """Core search logic, callable in-process (not offloaded to a thread).

    Split from the ``search`` tool so other tools (``search_deep``) can call
    it directly. Calling the ``@_offloaded`` tool wrapper in-process returns
    a coroutine instead of a result — that wrapper only resolves correctly
    when awaited by the MCP dispatch loop.

    ``track=False`` records no usage and moves no link weight: the rerank
    pool is not what the caller sees, so only the final rows are tracked.
    ``details``, in hybrid mode, receives each returned file's unrounded
    score and the number of the chunk its excerpt came from.
    """
    # mode: "hybrid" (0.7*semantic + 0.3*keyword), "semantic", or "keyword".
    rt = _ensure_indexed()

    if mode not in ("hybrid", "semantic", "keyword"):
        mode = "hybrid"

    fetch_limit = limit * 2  # over-fetch for re-ranking in hybrid mode
    flt = _memory_filter(rt, include_memories)
    store = rt.indexer.store

    # file_path -> (similarity, excerpt) of its best chunk.
    semantic_scores: dict[str, tuple[float, str]] = {}
    # file_path -> (normalised score, excerpt) of its best chunk.
    keyword_scores: dict[str, tuple[float, str]] = {}
    # file_path -> number of its best chunk, dense first (as the excerpt).
    best_chunk: dict[str, int] = {}

    def tracked(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return _track_results(rows) if track else rows

    # --- Semantic search ---
    # A provider without meaning (``none``) has no dense path: hybrid ranks by
    # keywords alone and semantic returns nothing.
    if mode in ("hybrid", "semantic") and rt.embedder.semantic:
        embedding = rt.indexer.embed_query(query)
        for hit in store.knn(embedding, fetch_limit, flt):
            sim = max(0.0, hit.score)  # cosine similarity
            # Hits come best first: the first one per file is its best chunk.
            if hit.file_path not in semantic_scores:
                semantic_scores[hit.file_path] = (sim, hit.own_text[:excerpt_chars])
                best_chunk[hit.file_path] = hit.ord

    # --- Keyword search ---
    if mode in ("hybrid", "keyword"):
        ranked = _lexical(rt, query, fetch_limit, flt)
        if ranked:
            max_kw = ranked[0][1] if ranked[0][1] > 0 else 1.0
            for fp, raw_score, text, ordinal in ranked:
                keyword_scores[fp] = (raw_score / max_kw, text[:excerpt_chars])  # normalise to 0-1
                best_chunk.setdefault(fp, ordinal)

    docs = _documents(rt, set(semantic_scores) | set(keyword_scores))

    def row(fp: str, score: float) -> dict[str, Any]:
        # The excerpt is the best chunk's: the dense one when the file has
        # one, the lexical one otherwise. Title and review state come from
        # the document, however the file was found.
        excerpt = (semantic_scores.get(fp) or keyword_scores[fp])[1]
        doc = docs.get(fp)
        return {
            "file_path": fp,
            "title": doc.title if doc else "",
            "score": score,
            "excerpt": excerpt,
            **(review.review_status(doc.metadata(), thresholds=rt.spec.review_thresholds) if doc else {}),
        }

    # --- Combine ---
    if mode == "semantic":
        ranked_rows = sorted((row(fp, sim) for fp, (sim, _) in semantic_scores.items()), key=_rank_key)
        return tracked(ranked_rows[:limit])

    if mode == "keyword":
        out = [row(fp, round(norm, 2)) for fp, (norm, _) in keyword_scores.items()]
        out.sort(key=_rank_key)
        return tracked(out[:limit])

    # mode == "hybrid"
    combined: list[dict[str, Any]] = []
    raw_scores: dict[str, float] = {}
    for fp in set(semantic_scores) | set(keyword_scores):
        sem = semantic_scores[fp][0] if fp in semantic_scores else 0.0
        kw = keyword_scores[fp][0] if fp in keyword_scores else 0.0
        # Usage boost is capped at +10%: reorders ties, never overrides relevance.
        final = (0.7 * sem + 0.3 * kw) * rt.usage.boost(fp)
        raw_scores[fp] = final
        combined.append(row(fp, round(final, 2)))
    combined.sort(key=_rank_key)
    if details is not None:
        for kept in combined[:limit]:
            fp = kept["file_path"]
            details[fp] = (raw_scores[fp], best_chunk[fp])
    return tracked(combined[:limit])


#: Frontmatter ``type`` of a consolidated memory.
MEMORY_TYPE = "memory"

def _memory_filter(rt: VaultRuntime, include_memories: bool) -> ChunkFilter | None:
    """What leaves memories out of a search: their ``type`` or their folder, on both sides."""
    if include_memories:
        return None
    folder = rt.spec.memory_folder
    return ChunkFilter(exclude_types=(MEMORY_TYPE,), exclude_prefixes=(folder,) if folder else ())


def _documents(rt: VaultRuntime, file_paths: set[str]) -> dict[str, DocRecord]:
    """The stored document of each path in ``file_paths``."""
    if not file_paths:
        return {}
    found = rt.indexer.store.documents(ChunkFilter(file_paths=tuple(sorted(file_paths))))
    return {doc.file_path: doc for doc in found}


def _lexical(rt: VaultRuntime, query: str, k: int, flt: ChunkFilter | None) -> list[tuple[str, float, str, int]]:
    """The ``k`` best files by BM25 over their chunks, best first: ``(file_path, -bm25, excerpt source, chunk)``.

    The excerpt source is the text of the file's best chunk without the
    characters it repeats from the previous one. BM25 over chunks ranks for
    every provider: with ``openai`` it measured recall@5 0.930 against 0.920
    for TF-IDF over whole bodies, and 0.837 against 0.803 with ``none``
    (measured on a reference vault with a labeled query set).
    """
    store = rt.indexer.store
    if not isinstance(store, LexicalIndex):
        raise TypeError(f"{type(store).__name__} has no keyword ranking")
    return [(hit.file_path, hit.score, hit.own_text, hit.ord) for hit in store.lexical(query, k, flt)]


@_tool
@_offloaded
@_in_vault
def search(
    query: str,
    limit: int = 5,
    mode: str = "hybrid",
    excerpt_chars: int = 120,
    include_memories: bool = False,
    fields: list[str] | None = None,
    snippet: bool = False,
    vault: str | None = None,
    rerank: str | None = None,
) -> list[dict[str, Any]]:
    """Semantic + keyword search across the vault. Returns the top matches as pointers.

    A row names the file, its score and the block to read next (``chunk_id``,
    ``section``); ``fields`` asks for other keys (``excerpt``, ``doc_id``) and
    ``snippet=True`` adds a short window around the query terms.
    ``excerpt_chars`` bounds both.

    Consolidated memories (type=memory, folder from config) are excluded
    by default so curated docs never compete against them; pass
    include_memories=True to search across both.

    ``rerank="on"`` reorders the hybrid results with the judge the vault
    declares (``rerank`` in its ``vault.yaml``), ``"off"`` never does, and
    omitted follows the vault's ``rerank.default``. Every argument and the
    vault's judge are checked before anything is searched or counted.
    """
    wanted = _fields_arg(fields, SEARCH_FIELDS)
    _limit_arg("limit", limit, 1, SEARCH_LIMIT_MAX)
    rt = _vault()
    asked = _rerank_argument(rt, rerank)
    if asked is None:
        results = _search_impl(
            query=query,
            limit=limit,
            mode=mode,
            excerpt_chars=excerpt_chars,
            include_memories=include_memories,
        )
        chosen: dict[str, int] = {}
        _count_search(rt, reranked=False)
    else:
        results, chosen = _reranked_search(
            rt,
            asked,
            query=query,
            limit=limit,
            mode=mode,
            excerpt_chars=excerpt_chars,
            include_memories=include_memories,
        )
    _record_query(query, results, "search")
    return _shape_rows(
        results,
        query=query,
        wanted=wanted,
        snippet=snippet,
        width=excerpt_chars,
        canonical=SEARCH_FIELDS,
        default=SEARCH_DEFAULT_FIELDS,
        chosen=chosen,
    )


#: Key of ``_meta`` that reports what the judge did for one ``search`` call.
RERANK_META_KEY = "knowledge/rerank"


def _rerank_argument(rt: VaultRuntime, rerank: str | None) -> str | None:
    """The validated ``rerank`` argument resolved against the vault: ``on``, ``off`` or ``None``.

    ``None`` means rerank is not evaluated at all (omitted, and the vault does
    not turn it on by default): the call is the search of contract 1.3.0.
    Asking for ``on`` in a vault that declares no judge is an error, raised
    before any search runs or any counter moves.
    """
    if rerank is not None and rerank not in _rerank.RERANK_ARGUMENTS:
        raise KnowledgeToolError(
            errors.E_ARGUMENT_OUT_OF_RANGE, f"rerank must be 'on', 'off' or omitted, got {rerank!r}"
        )
    spec = rt.spec.rerank
    if rerank == _rerank.RERANK_ON and not spec.configured:
        raise KnowledgeToolError(
            errors.E_RERANK_NOT_CONFIGURED,
            f"vault {rt.spec.slug!r} declares no rerank.provider; omit rerank or ask the vault owner",
        )
    return _rerank.requested(rerank, spec)


def _count_search(rt: VaultRuntime, *, reranked: bool) -> None:
    try:
        rt.usage.record_search(reranked=reranked)
    except Exception:
        log.exception("search counting failed — results unaffected")


def _rerank_candidates(
    rt: VaultRuntime, rows: list[dict[str, Any]], details: Mapping[str, tuple[float, int]], count: int
) -> list[_rerank.Candidate]:
    """The first ``count`` pool rows as judge candidates: raw score, best chunk's section and body."""
    head = [row["file_path"] for row in rows[:count]]
    best = {fp: details[fp][1] for fp in head if fp in details}
    sections = {
        (chunk.file_path, chunk.ord): chunk.section
        for chunk in rt.indexer.store.chunks(head)
        if best.get(chunk.file_path) == chunk.ord
    }
    return [
        _rerank.Candidate(
            file_path=fp,
            raw=details[fp][0] if fp in details else float(row["score"]),
            dense_section=sections.get((fp, best.get(fp, -1)), ""),
            body=_read_body(rt, fp),
        )
        for fp, row in zip(head, rows, strict=False)
    ]


def _reranked_search(
    rt: VaultRuntime,
    asked: str,
    *,
    query: str,
    limit: int,
    mode: str,
    excerpt_chars: int,
    include_memories: bool,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """``search`` when rerank is evaluated: the rows, the blocks the judge chose, and ``_meta`` set.

    The pool is a hybrid search of :func:`knowledge.rerank.pool_limit` rows
    that tracks no usage; the final rows are tracked once they are known.
    Whenever the judge is not applied, the rows are exactly those of the
    search without rerank.
    """
    spec = rt.spec.rerank
    operator = _settings.current().rerank_operator
    judge = rt.judge
    if asked == _rerank.RERANK_OFF:
        reason: str | None = _rerank.REASON_OFF
    else:
        reason = _rerank.precheck(
            spec,
            allowed=operator.allows(spec.provider),
            judge=judge,
            mode=mode,
            include_memories=include_memories,
        )
    outcome = _rerank.Outcome.skipped(reason) if reason is not None else None
    if outcome is None:
        timeout_s = (operator.timeout_ms or spec.timeout_ms) / 1000
        prewarm = getattr(judge, "prewarm", None)
        if callable(prewarm) and not rt.reranker.breaker_open():
            prewarm(timeout_s)
        details: dict[str, tuple[float, int]] = {}
        pool = _search_impl(
            query=query,
            limit=_rerank.pool_limit(limit),
            mode="hybrid",
            excerpt_chars=excerpt_chars,
            include_memories=False,
            track=False,
            details=details,
        )
        extension = _load_extension("rerank")
        outcome = rt.reranker.run(
            query,
            pool,
            _rerank_candidates(rt, pool, details, spec.candidates),
            judge=judge,
            spec=spec,
            idf=_query_idf(rt, query),
            limit=limit,
            timeout_s=timeout_s,
            cost=extension.cost_usd,
            usage=rt.usage,
            prompt_version=str(getattr(judge, "prompt_version", "")),
        )
    if outcome.applied and outcome.rows is not None:
        results = _track_results(outcome.rows)
    else:
        results = _search_impl(
            query=query,
            limit=limit,
            mode=mode,
            excerpt_chars=excerpt_chars,
            include_memories=include_memories,
        )
    _count_search(rt, reranked=outcome.applied)
    try:
        rt.usage.record_rerank(
            applied=outcome.applied,
            reason=outcome.reason,
            units=outcome.units,
            cache_hits=outcome.cache_hits,
            input_tokens=outcome.input_tokens,
            usd=outcome.usd,
            latency_ms=outcome.latency_ms,
        )
    except Exception:
        log.exception("rerank recording failed — results unaffected")
    _set_result_meta(RERANK_META_KEY, _rerank.meta(spec, outcome))
    return results, outcome.chosen if outcome.applied else {}


@_tool
@_offloaded
@_in_vault
def search_by_metadata(
    type: str | None = None,
    service: str | None = None,
    tag: str | None = None,
    status: str | None = None,
    limit: int = METADATA_LIMIT_DEFAULT,
    offset: int = 0,
    fields: list[str] | None = None,
    facets: bool = False,
    vault: str | None = None,
) -> dict[str, Any]:
    """Filter vault files by frontmatter fields. All params optional, AND-ed.

    Answers one page, ``{total, offset, items}``: ``total`` counts every
    matching row, ``items`` holds at most ``limit`` of them from ``offset``.
    Documents come by path; facet rows by facet, count and value.
    """
    wanted = _fields_arg(fields, SEARCH_BY_METADATA_FIELDS)
    if facets and wanted is not None:
        raise KnowledgeToolError(
            errors.E_ARGUMENT_CONFLICT,
            "fields projects document rows and facets=True returns {facet, value, count} rows; drop one",
        )
    _limit_arg("limit", limit, 1, METADATA_LIMIT_MAX)
    if offset < 0:
        raise KnowledgeToolError(errors.E_ARGUMENT_OUT_OF_RANGE, f"offset must be 0 or more, got {offset}")
    rt = _ensure_indexed()
    equals = {key: value for key, value in (("type", type), ("service", service), ("status", status)) if value}
    out: list[dict[str, Any]] = []
    for doc in rt.indexer.store.documents(ChunkFilter(equals=equals, tag=tag or None)):
        fp = doc.file_path
        entry: dict[str, Any] = {"file_path": fp}

        # Title: include only when it differs from the basename of the path.
        # E.g. "Flow - Telvia" vs "Flow - Telvia.md" → drop (redundant).
        if not _redundant_title(doc.title, fp):
            entry["title"] = doc.title

        # Skip empty values — keep non-empty strings and tag lists.
        for key, value in (("type", doc.type), ("service", doc.service), ("status", doc.status)):
            if value:
                entry[key] = value
        if doc.tags:
            entry["tags"] = list(doc.tags)

        out.append(entry)

    # A page is only stable over a stable order: documents by path, facet rows
    # in the order they already have.
    rows = _facet_rows(out) if facets else sorted(out, key=lambda row: str(row["file_path"]))
    items = rows[offset : offset + limit]
    if wanted is not None:
        if "doc_id" in wanted:
            items = [{**row, "doc_id": pointers.doc_id(str(row["file_path"]))} for row in items]
        items = [_project(row, SEARCH_BY_METADATA_FIELDS, wanted) for row in items]
    return {"total": len(rows), "offset": offset, "items": items}


def _facet_rows(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``{facet, value, count}`` rows over ``documents``.

    ``count`` is the number of documents that carry the value (a tag listed
    twice in one document counts once). Facets come in :data:`FACET_FIELDS`
    order and, inside a facet, by count descending and then by value.
    """
    rows: list[dict[str, Any]] = []
    for facet in FACET_FIELDS:
        counts: dict[Any, int] = {}
        for document in documents:
            if facet not in document:
                continue
            raw = document[facet]
            values = raw if isinstance(raw, list) else [raw]
            for value in dict.fromkeys(values):
                counts[value] = counts.get(value, 0) + 1
        ranked = sorted(counts.items(), key=lambda item: (-item[1], str(item[0]), type(item[0]).__name__))
        rows.extend({"facet": facet, "value": value, "count": count} for value, count in ranked)
    return rows


@_tool
@_offloaded
@_in_vault
def search_deep(
    query: str,
    depth: int = 2,
    limit: int = 5,
    fields: list[str] | None = None,
    snippet: bool = False,
    vault: str | None = None,
) -> list[dict[str, Any]]:
    """Hybrid search expanded by traversing wiki-link graph up to *depth* hops.

    Rows are those of ``search`` plus ``discovered_via``.
    """
    wanted = _fields_arg(fields, SEARCH_DEEP_FIELDS)
    _limit_arg("limit", limit, 1, SEARCH_DEEP_LIMIT_MAX)
    rows = _search_deep_impl(query, depth=depth, limit=limit)
    return _shape_rows(
        rows,
        query=query,
        wanted=wanted,
        snippet=snippet,
        width=_DEEP_EXCERPT_CHARS,
        canonical=SEARCH_DEEP_FIELDS,
        default=SEARCH_DEEP_DEFAULT_FIELDS,
    )


def _search_deep_impl(query: str, *, depth: int, limit: int) -> list[dict[str, Any]]:
    """The rows ``search_deep`` returns by default, with the query recorded."""
    rt = _ensure_indexed()

    # depth bounds: 0 = no expansion (plain search); cap at 3 to avoid explosion.
    if depth < 0:
        depth = 0
    if depth > 3:
        log.warning("search_deep: depth %d capped to 3", depth)
        depth = 3

    # If the graph isn't loaded (no nodes), fall back to plain search.
    graph = rt.indexer.graph
    if not graph._outgoing and not graph._incoming:
        log.warning("search_deep: graph empty, falling back to plain search()")
        return _record_query(query, _search_impl(query=query, limit=limit, mode="hybrid"), "search_deep")

    # 1) seed candidates via the existing hybrid search.
    #    NOTE: we deliberately use `limit` (not `limit*N`) as the seed count.
    #    Over-fetching seeds floods the pool with direct hits and starves
    #    hop-discovered docs (whose scores are penalised by 0.7^hops). With
    #    `seed_limit == limit`, hops have a fair shot at the top-`limit`.
    seed_limit = max(limit, 1)
    seeds = _search_impl(query=query, limit=seed_limit, mode="hybrid")

    # discovered_via: file_path → label ("direct" or "hop:<origin> → <direction>")
    # hops_from_seed: file_path → minimum hop distance (0 for direct)
    # origin_score: file_path → score of the seed that introduced this hop
    pool: dict[str, dict[str, Any]] = {}
    hops_from_seed: dict[str, int] = {}
    origin_score: dict[str, float] = {}
    edge_weight: dict[str, float] = {}  # learned weight of the edge that discovered each hop
    for s in seeds:
        fp = s.get("file_path")
        if not fp:
            continue
        pool[fp] = {
            "file_path": fp,
            "title": s.get("title", ""),
            "score": float(s.get("score", 0.0)),
            "excerpt": s.get("excerpt", ""),
            "discovered_via": "direct",
        }
        hops_from_seed[fp] = 0
        origin_score[fp] = float(s.get("score", 0.0))

    # 2) BFS expansion through the graph from each seed up to *depth* hops.
    if depth > 0 and seeds:
        from collections import deque

        # Seed BFS frontier with the direct hits.
        queue: deque[tuple[str, int, str]] = deque()
        # (file, hops_so_far, origin_label) — origin_label is the seed file_path
        for s in seeds:
            fp = s.get("file_path")
            if fp:
                queue.append((fp, 0, fp))

        # Track the best (smallest hop count) entry per file so we don't keep
        # walking the same node deeper than necessary. Neighbours are walked
        # in path order: which edge discovers a hop (and so its weight and
        # label) must not follow the walk order of a ``set``.
        while queue:
            current, hops, origin = queue.popleft()
            if hops >= depth:
                continue
            for fp_next in sorted(graph.outgoing(current)):
                if fp_next in pool:
                    continue
                pool[fp_next] = {
                    "file_path": fp_next,
                    "title": "",
                    "score": 0.0,  # filled in below via embedding
                    "excerpt": "",
                    "discovered_via": f"hop:{origin} -> outgoing",
                }
                hops_from_seed[fp_next] = hops + 1
                origin_score[fp_next] = origin_score.get(origin, 0.0)
                edge_weight[fp_next] = graph.weight(current, fp_next)
                queue.append((fp_next, hops + 1, origin))
            for fp_next in sorted(graph.incoming(current)):
                if fp_next in pool or fp_next == current:
                    continue
                pool[fp_next] = {
                    "file_path": fp_next,
                    "title": "",
                    "score": 0.0,
                    "excerpt": "",
                    "discovered_via": f"hop:{origin} -> incoming",
                }
                hops_from_seed[fp_next] = hops + 1
                origin_score[fp_next] = origin_score.get(origin, 0.0)
                edge_weight[fp_next] = graph.weight(current, fp_next)
                queue.append((fp_next, hops + 1, origin))

    # 3) Score the hop-discovered docs by their best chunk against the query
    #    embedding (computed once).
    hop_files = [fp for fp, entry in pool.items() if entry["discovered_via"] != "direct"]
    if hop_files:
        query_embedding: list[float] | None = None
        if rt.embedder.semantic:
            try:
                query_embedding = rt.indexer.embed_query(query)
            except Exception:
                log.exception("search_deep: failed to embed query for hop scoring")

        if query_embedding is not None:
            # The best chunks of the hop files in one KNN, then the best per file.
            try:
                hits: list[ChunkHit] = rt.indexer.store.knn(
                    query_embedding, max(len(hop_files) * 3, 10), ChunkFilter(file_paths=tuple(hop_files))
                )
            except Exception:
                log.exception("search_deep: knn failed for hop files")
                hits = []
            titles = {fp: doc.title for fp, doc in _documents(rt, {hit.file_path for hit in hits}).items()}

            for hit in hits:
                fp = hit.file_path
                if fp not in pool:
                    continue
                sim = max(0.0, hit.score)
                hops = hops_from_seed.get(fp, 1)
                # Hop scoring: blend the doc's own semantic similarity with
                # the originating seed's score, decayed by hop distance.
                #   final = 0.5 * sim + 0.5 * (origin_score * 0.85^hops)
                # Rationale: a hop is relevant for two reasons — (a) it
                # itself looks like the query and (b) it sits next to a
                # seed that already matched. Combining both metrics gives
                # hops a fair chance to surface above weakly-ranked direct
                # hits without dominating strong ones.
                # Learned edge weight modulates hop propagation: neutral at
                # the default 1.0, up to 2x for learned highways, dampened
                # below 1.0 for weak links.
                w_factor = max(0.5, min(2.0, edge_weight.get(fp, 1.0)))
                decayed_origin = origin_score.get(fp, 0.0) * (0.85 ** hops) * w_factor
                penalised = 0.5 * sim + 0.5 * decayed_origin
                if penalised > pool[fp]["score"]:
                    pool[fp]["score"] = penalised
                    pool[fp]["title"] = titles.get(fp, "") or pool[fp]["title"]
                    pool[fp]["excerpt"] = hit.own_text[:_DEEP_EXCERPT_CHARS]

        # Drop hop entries that never got a score (file not in the index).
        for fp in list(pool.keys()):
            entry = pool[fp]
            if entry["discovered_via"] != "direct" and entry["score"] == 0.0 and not entry["excerpt"]:
                del pool[fp]

    # 4) Final ranking + shape.
    for entry in pool.values():
        entry["score"] = round(entry["score"], 2)
        entry["excerpt"] = (entry.get("excerpt") or "")[:_DEEP_EXCERPT_CHARS]
    ranked = sorted(pool.values(), key=_rank_key)[:limit]
    return _record_query(query, _track_results(ranked), "search_deep")


@_tool
@_offloaded
@_in_vault
def find_path(
    from_path: str,
    to_path: str,
    max_depth: int = 4,
    weighted: bool = False,
    vault: str | None = None,
) -> dict[str, Any]:
    """Shortest wiki-link path between two vault files via bidirectional BFS.

    With weighted=True, runs Dijkstra with edge cost 1/weight so learned
    Hebbian highways are preferred over decorative links.
    """
    rt = _ensure_indexed()

    if max_depth < 1:
        log.warning("find_path: max_depth %d capped to 1", max_depth)
        max_depth = 1
    if max_depth > 6:
        log.warning("find_path: max_depth %d capped to 6", max_depth)
        max_depth = 6

    graph = rt.indexer.graph

    # Trivial case: same file. We still validate it exists in the graph below
    # — but if the user asks for from==to and the node exists, length 0.
    if from_path == to_path:
        if graph.has_node(from_path):
            return {
                "from": from_path,
                "to": to_path,
                "path": [from_path],
                "length": 0,
                "found": True,
            }
        _not_in_graph(rt, [from_path])

    missing = [path for path in (from_path, to_path) if not graph.has_node(path)]
    if missing:
        _not_in_graph(rt, missing)

    if weighted:
        path, cost = graph.shortest_weighted_path(from_path, to_path, max_hops=max_depth)
        if path is None:
            return {
                "from": from_path,
                "to": to_path,
                "path": None,
                "found": False,
                "weighted": True,
                "max_depth": max_depth,
            }
        return {
            "from": from_path,
            "to": to_path,
            "path": path,
            "length": len(path) - 1,
            "cost": round(cost, 3),
            "weighted": True,
            "found": True,
        }

    # Breadth-first over the undirected (outgoing + incoming) view; among
    # several equally short paths the lexicographically smallest one wins.
    path = graph.shortest_path(from_path, to_path, max_hops=max_depth)
    if path is None:
        return {
            "from": from_path,
            "to": to_path,
            "path": None,
            "found": False,
            "max_depth": max_depth,
        }

    return {
        "from": from_path,
        "to": to_path,
        "path": path,
        "length": len(path) - 1,
        "found": True,
    }


def _not_in_graph(rt: VaultRuntime, missing: list[str]) -> NoReturn:
    """Refuse a ``find_path`` whose ends are not documents of the link graph."""
    raise KnowledgeToolError(
        errors.E_FILE_NOT_IN_GRAPH,
        f"not in the link graph of vault {rt.spec.slug!r}: {missing} (no wiki-link reaches it and it links "
        "nowhere, or it is not a vault document); take file_path from a search row, vault-relative and without "
        "wikilink brackets",
    )


@_tool
@_offloaded
@_in_vault
def get_related(
    file_path: str,
    limit: int = 5,
    include_suggested: bool = False,
    fields: list[str] | None = None,
    vault: str | None = None,
) -> list[dict[str, Any]]:
    """List files linked from/to a vault doc via wiki-links (outgoing + backlinks).

    A document linked both ways is one row with direction ``both``; weights
    carry :data:`WEIGHT_DIGITS` decimals. With include_suggested=True, appends
    unlinked docs that share >=2 graph neighbors with this one
    (shared-neighbor discovery, direction "suggested"), computed for this
    document alone.
    """
    wanted = _fields_arg(fields, GET_RELATED_FIELDS)
    _limit_arg("limit", limit, 1, RELATED_LIMIT_MAX)
    rt = _ensure_indexed()
    graph = rt.indexer.graph
    rows: list[dict[str, Any]] = [
        {"file_path": row["file"], "direction": row["direction"], "weight": row["weight"]}
        for row in graph.related(file_path, limit=limit, ndigits=WEIGHT_DIGITS)
    ]
    if include_suggested and len(rows) < limit:
        # Suggestions follow the links, strongest first; ties resolve by path.
        rows.extend(
            {
                "file_path": s["file"],
                "direction": "suggested",
                "confidence": s["confidence"],
                "shared_neighbors": s["shared_neighbors"],
            }
            for s in graph.suggest_for(file_path)[: limit - len(rows)]
        )
    if wanted is None:
        return rows
    if "doc_id" in wanted:
        rows = [{**row, "doc_id": pointers.doc_id(row["file_path"])} for row in rows]
    return [_project(row, GET_RELATED_FIELDS, wanted) for row in rows]


@_gated_tool("governance")
@_offloaded
@_in_vault
def suggest_links(
    min_shared: int = 2,
    min_confidence: float = 0.6,
    limit: int = 30,
    vault: str | None = None,
) -> list[dict[str, Any]]:
    """Vault curation report: unlinked doc pairs that share graph neighbors.

    Pure graph math over the wiki-link graph (no API cost). Use it to find
    documents that probably should reference each other.
    """
    _limit_arg("limit", limit, 1, SUGGEST_LIMIT_MAX)
    rt = _ensure_indexed()
    return rt.indexer.graph.suggest_edges(
        min_shared=min_shared, min_confidence=min_confidence, limit=limit
    )


@_gated_tool("governance")
@_offloaded
@_in_vault
def get_stale_docs(
    type: str | None = None,
    owner: str | None = None,
    limit: int = 30,
    include_unreadable: bool = True,
    missing_last_updated: bool = False,
    vault: str | None = None,
) -> list[dict[str, Any]]:
    """List docs that need revalidation, with the team that has to do it.

    Two things land on this queue, told apart by ``reason``:

    - ``overdue`` — ``last_updated`` is older than the threshold for the
      document's ``type``. Carries ``age_days``; oldest first.
    - ``last_updated_invalid`` — ``last_updated`` is there but no parser can
      read it (``not-a-date``, an unrendered ``YYYY-MM-DD`` placeholder). No
      ``age_days``, because there is no age to report: the document's decay
      is unknown, which is a defect to repair, not a clean bill of health.
      Set ``include_unreadable=False`` to ask for the overdue queue alone.

    Documents with no ``last_updated`` at all stay out — that is genuinely no
    data — and so do non-curated paths: Obsidian's ``.trash``, plugin folders,
    the memory export folder and whatever the vault's ``index.exclude`` covers.

    ``owner`` comes from the document's own frontmatter when it declares one;
    otherwise it is inherited from the ``type: service`` hub matching the
    document's ``service`` (see the vault's ``Conventions/Frontmatter.md``).
    ``owner_source`` says which of the two happened, and ``unassigned`` means
    the chain resolved to nothing. Pass ``owner`` to get only one team's
    queue.
    """
    _limit_arg("limit", limit, 1, STALE_LIMIT_MAX)
    rt = _ensure_indexed()
    store = rt.indexer.store
    docs = store.documents(ChunkFilter(equals={"type": type}) if type else None)

    # Service hubs are fetched separately: a `type` filter above would hide
    # them, and they are the only documents that declare `owner`.
    hubs = store.documents(ChunkFilter(equals={"type": review.SERVICE_HUB_TYPE}))
    service_owners = review.build_service_owner_map(doc.metadata() for doc in hubs)

    excluded = vault_lint.index_exclude_matcher(rt.spec.index_exclude)
    queued: dict[str, dict[str, Any]] = {}
    for doc in docs:
        meta = doc.metadata()
        fp = meta.get("file_path")
        if (
            not fp
            or fp in queued
            or not review.is_curated(str(fp), memory_folder=rt.spec.memory_folder, index_exclude=excluded)
        ):
            continue

        date_state, _raw = review.last_updated_state(meta)
        if date_state == review.LAST_UPDATED_MISSING:
            if not missing_last_updated:
                continue
            annotation: dict[str, Any] = {
                "needs_review": True,
                "reason": review.REASON_LAST_UPDATED_MISSING,
            }
        elif date_state == review.LAST_UPDATED_INVALID:
            if not include_unreadable:
                continue
            annotation = {
                "needs_review": True,
                "reason": review.REASON_LAST_UPDATED_INVALID,
            }
        else:
            status = review.review_status(meta, thresholds=rt.spec.review_thresholds)
            if not status:
                continue
            annotation = {"reason": review.REASON_OVERDUE, **status}

        resolved_owner, owner_source = review.resolve_owner(meta, service_owners)
        if owner and resolved_owner != owner:
            continue
        queued[fp] = {
            "file_path": fp,
            "title": meta.get("title", ""),
            "type": meta.get("type", ""),
            "service": meta.get("service", ""),
            "owner": resolved_owner,
            "owner_source": owner_source,
            "last_updated": meta.get("last_updated", ""),
            "last_verified": meta.get("last_verified", ""),
            "threshold_days": review.threshold_for(
                meta.get("type"), thresholds=rt.spec.review_thresholds
            ),
            **annotation,
        }

    # Overdue first, oldest first inside that block; then the unreadable ones
    # and, when asked for, the undated ones, each block in path order.
    # `age_days` is absent on the last two, so the key never invents a number
    # for them. `limit` cuts after this sort, so undated rows count toward it.
    ranked = sorted(
        queued.values(),
        key=lambda x: (
            _STALE_REASON_RANK[x["reason"]],
            -int(x.get("age_days") or 0),
            str(x["file_path"]),
        ),
    )
    return ranked[:limit]


_STALE_REASON_RANK = {
    review.REASON_OVERDUE: 0,
    review.REASON_LAST_UPDATED_INVALID: 1,
    review.REASON_LAST_UPDATED_MISSING: 2,
}


@_gated_tool("flows")
@_offloaded
@_in_vault
def get_flow(provider: str, vault: str | None = None) -> dict[str, Any]:
    """Return the auth-flow steps for a provider plus the shared login-flow steps."""
    rt = _vault()
    return _load_extension("flows").get_flow(rt.vault_path, rt.spec.memory_folder, provider)


@_gated_tool("flows")
@_offloaded
@_in_vault
def compare_flows(provider1: str, provider2: str, vault: str | None = None) -> dict[str, Any]:
    """Diff two auth flows: common, only-in-1, only-in-2, plus shared login steps."""
    rt = _vault()
    return _load_extension("flows").compare_flows(rt.vault_path, rt.spec.memory_folder, provider1, provider2)


@_tool
@_offloaded
@_in_vault
def get_impact(
    file_path: str,
    max_depth: int = 2,
    max_items: int = IMPACT_MAX_ITEMS_DEFAULT,
    counts: bool = False,
    vault: str | None = None,
) -> dict[str, Any]:
    """Blast radius: files that transitively reference this file via backlinks.

    Each ``depth_N`` list keeps its first ``max_items`` paths; ``omitted``
    says how many each level left out and ``total_affected`` counts them all.
    """
    _limit_arg("max_items", max_items, 0, IMPACT_MAX_ITEMS_MAX)
    rt = _ensure_indexed()
    result = rt.indexer.graph.get_impact(file_path, max_depth=max_depth)
    return _capped_impact(result, max_items=max_items, counts=counts)


def _capped_impact(result: dict[str, Any], *, max_items: int, counts: bool) -> dict[str, Any]:
    """``result`` with each ``depth_N`` cut to ``max_items`` and the totals asked for.

    ``total_affected`` keeps counting every file. ``counts`` lists every
    level's full size; ``omitted`` names only the levels that lost files.
    """
    levels = [key for key in result if key.startswith("depth_")]
    sizes = {key: len(result[key]) for key in levels}
    omitted: dict[str, int] = {}
    for key in levels:
        if sizes[key] > max_items:
            omitted[key] = sizes[key] - max_items
            result[key] = result[key][:max_items]
    if counts:
        result["counts"] = sizes
    if omitted:
        result["omitted"] = omitted
    return result


@_gated_tool("confluence")
@_admin
@_offloaded
@_in_vault
def sync_confluence(
    dry_run: bool = True,
    scope_prefix: str = "Conventions/",
    vault: str | None = None,
) -> dict[str, Any]:
    """Report which Confluence pages under a directory prefix the vault is behind on.

    By default, only files under 'Conventions/' are tracked (ADR-011). Files
    with confluence_id outside the scope are reported explicitly. Read-only:
    nothing is ever written into a vault, so ``dry_run=False`` is
    ``E_WRITE_FENCED`` before any credential is read, whichever vault is
    named; ``cloudfrog-knowledge confluence export`` writes the pages outside
    the vault for a pull request. Without credentials the call is
    ``E_SECRET_MISSING``, and a scope with no tracked file ``E_SCOPE_EMPTY``.

    Args:
        dry_run: Must stay True; anything else is E_WRITE_FENCED.
        scope_prefix: Vault directory prefix to read (relative to the vault); default 'Conventions/'.
    """
    confluence = _load_extension("confluence")
    confluence.check_fence(dry_run)
    rt = _vault()
    # Credentials: the vault's integrations.confluence, else CONFLUENCE_URL/EMAIL/TOKEN.
    return confluence.sync(
        dry_run=dry_run,
        scope_prefix=scope_prefix,
        vault_path=rt.vault_path,
        integrations=rt.spec.integrations,
    )


@_legacy_tool
@_offloaded
@_in_vault
def search_code(
    query: str,
    language: str | None = None,
    limit: int = 5,
    excerpt_chars: int = 180,
    vault: str | None = None,
) -> list[dict[str, Any]]:
    """Retired: no longer searches code. Returns a redirect to graphify.

    ADR-039 retired the code-embeddings collection this tool used to query —
    graphify is the platform's only code-understanding engine, and it runs
    local, not through this server. Contract 2.0.0 retires the tool: only the
    ``legacy`` profile registers it, and every call gets the same redirect.
    Under ``all`` a call answers ``E_TOOL_RETIRED`` with that redirect.
    """
    del query, language, limit, excerpt_chars, vault  # kept for input-schema stability, unused
    advisory = (
        "search_code no longer indexes source code (ADR-039); the code-embeddings "
        "collection was retired and graphify is the only code-understanding engine "
        "now. Use `graphify explain <symbol>` and `graphify affected <file>` for "
        "relationships and impact, `graphify query <symbol>` only with a real "
        "symbol name, `rg` for literal text, templates and QML, and `search` or "
        "`search_deep` for intent — that lives in the vault. This tool is "
        "scheduled for removal in the next revision of the MCP tools contract."
    )
    return [{"info": advisory}]


def _reindex_blocking(force: bool = False, pull: bool = False) -> tuple[dict[str, Any], str, dict[str, Any] | None]:
    """Pull the vault when asked, reindex it and lint it. Kept separate so it can run off the loop.

    Returns the indexer's stats, how the pull ended and the lint counts.
    """
    from knowledge.webhook import indexing_scope

    rt = _vault()
    # One refresh of a vault at a time: the reindex webhook pulls and indexes
    # the same checkout under this lock. The scope must wrap the pull: that is
    # what bumps the vault mtimes and would otherwise make /health report a
    # stale index for the whole run.
    with rt.refresh_lock, indexing_scope():
        outcome = rt.sync_vault_outcome() if pull else SYNC_SKIPPED_PULL_FALSE
        stats: dict[str, Any] = dict(rt.indexer.index(force=force, target="vault"))
    return stats, outcome, _validation(rt)


def _validation(rt: VaultRuntime) -> dict[str, Any] | None:
    """Counts of the vault linter over the files just indexed; ``None`` if it could not run.

    Only counts, never messages: the linter's wording is its own, the counts
    are contract. ``vault-lint`` prints the findings themselves.
    """
    try:
        report = vault_lint.lint(rt.vault_path)
    except Exception:
        log.exception("vault lint failed after reindex — validation omitted")
        return None
    return {"ok": report.ok, "errors": len(report.errors), "warnings": len(report.warnings)}


def _memory_bridge_status(rt: VaultRuntime) -> dict[str, str] | None:
    """``reindex.memory_bridge`` for the vault of ``rt``; ``None`` when it does not declare ``memory-bridge``.

    Over stdio the bridge has memory rescan the vault's runbooks; over HTTP
    it is not loaded (the memory server lives on the person's loopback), so
    the key reports ``skipped`` with ``W_EXTENSION_UNAVAILABLE_HTTP``.
    """
    ext = extensions.get("memory-bridge")
    if ext.name not in extensions.enabled(rt.spec):
        return None
    s = _settings.current()
    if not extensions.loads_on(ext, s.http.transport):
        return {"status": "skipped", "code": errors.W_EXTENSION_UNAVAILABLE_HTTP}
    try:
        status: dict[str, str] = _load_extension(ext.name).reindex_status(rt.spec, s.memory)
    except Exception:
        log.exception("memory-bridge failed after reindex")
        return {"status": "failed", "code": errors.E_MEMORY_WRITE_FAILED}
    return status


@_tool
@_admin
@_in_vault
async def reindex(
    target: str = "vault",
    force: bool = False,
    pull: bool = False,
    vault: str | None = None,
) -> dict[str, Any]:
    """Re-index the vault incrementally via file hashes. Returns stats.

    target: only "vault" is supported. ADR-039 retired code indexing, so
    "code" and "all" now raise instead of silently falling back to "vault" —
    accepting a target this server can no longer honor must not look like a
    completed run. Build or update a code graph with graphify instead.

    force: re-embed every file instead of only the ones whose hash changed.
    Costs a full embedding run, so it is opt-in — but it is the only way to
    backfill metadata fields added after an index was built (a file that did
    not change is skipped, and keeps whatever metadata it was indexed with).

    pull: run ``git pull`` on a vault that declares a ``repo`` before
    indexing. Off by default: the reindex webhook pulls the vault on every
    push, so the tool indexes what the checkout holds and ``vault_sync``
    says ``skipped_pull_false``.
    """
    import json
    from datetime import datetime

    if target != "vault":
        raise ValueError(
            f"reindex target {target!r} is retired (ADR-039) — only 'vault' is "
            "supported now. Use graphify to build or update a code graph instead."
        )

    log.info("Reindex requested (target=%s, force=%s).", target, force)
    # Off the event loop: embedding a batch of documents takes minutes, and
    # running it inline stalls /health and every other tool until it ends —
    # long enough for the container healthcheck to start failing.
    stats, outcome, validation = await asyncio.to_thread(_reindex_blocking, force, pull)
    skipped = list(_vault().indexer.skipped_files)
    if skipped:
        stats["skipped_count"] = len(skipped)
        log_path = _vault().spec.skipped_log
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(json.dumps({"timestamp": datetime.now().isoformat(), "skipped": skipped}, indent=2))
        stats["skipped_log"] = str(log_path)
        log.warning("Skipped %d files — log at %s", len(skipped), log_path)
    stats["index_version"] = SCHEMA_VERSION
    stats["vault"] = _vault().spec.slug
    stats["vault_sync"] = outcome
    stats["validation"] = validation
    bridge = await asyncio.to_thread(_memory_bridge_status, _vault())
    if bridge is not None:
        stats["memory_bridge"] = bridge
    return stats


# --- context_pack -------------------------------------------------------------

#: Bounds of ``context_pack(budget_tokens=…)``, in estimated tokens.
PACK_BUDGET_MIN = 500
PACK_BUDGET_MAX = 12000
#: Vault hits a pack starts from.
PACK_SEARCH_LIMIT = 8
#: Hits a pack asks the memory server for.
PACK_MEMORY_LIMIT = 5
#: Share of the budget memory items may take, so curated sections keep the rest.
PACK_MEMORY_SHARE = 0.3
#: An item that does not fit is cut to the remaining room only when at least
#: this many tokens remain; below that it is dropped.
PACK_MIN_TRIM_TOKENS = 200
#: Constant of the reciprocal rank fusion of vault and memory hits.
PACK_RRF_K = 60


def estimate_tokens(payload: Any) -> int:
    """Tokens ``payload`` costs as compact JSON: ``ceil(utf-8 bytes / 4)``.

    Needs no tokenizer at runtime. It is measured on the whole item, keys and
    escapes included, not on its text alone, so the budget bounds what the
    client actually receives; on Spanish text, where accented letters take two
    bytes, it overestimates.
    """
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return -(-len(raw) // 4)


def _pack_block(body: str, idf: dict[str, float]) -> tuple[int, str | None, str]:
    """``(ordinal, section, text)`` of the block of ``body`` a pack takes.

    The section the query hits; without a query term in the file (a purely
    semantic hit), the opening of the document: its preamble, or its first
    heading's block when there is no preamble.
    """
    hit = pointers.select_section(body, idf)
    if hit is not None:
        return hit.ordinal, hit.section, hit.block
    heads = pointers.headings(body)
    preamble = pointers.block(body, 0, heads)
    if preamble or not heads:
        return 0, None, preamble
    return 1, heads[0].text, pointers.block(body, 1, heads)


def _pack_vault_items(rt: VaultRuntime, query: str, include_memories: bool) -> list[dict[str, Any]]:
    """One item per vault hit, in search order: the block of the section the query hits."""
    rows = _search_impl(query=query, limit=PACK_SEARCH_LIMIT, include_memories=include_memories)
    _record_query(query, rows, "context_pack")
    idf = _query_idf(rt, query)
    items: list[dict[str, Any]] = []
    for row in rows:
        file_path = str(row["file_path"])
        body = _read_body(rt, file_path)
        if body is None:
            continue
        ordinal, section, text = _pack_block(body, idf)
        if not text:
            continue
        doc = pointers.doc_id(file_path)
        items.append(
            {
                "source": "vault",
                "doc_id": doc,
                "file_path": file_path,
                "chunk_id": pointers.chunk_id(doc, ordinal),
                "section": section,
                "score": row["score"],
                "text": text,
            }
        )
    return items


def _pack_memory_items(rt: VaultRuntime, query: str) -> list[dict[str, Any]]:
    """One item per memory hit, best first. Raises ``MemoryUnavailable``."""
    hits = rt.memory.search(query, project=rt.spec.memory_project or "", limit=PACK_MEMORY_LIMIT)
    items: list[dict[str, Any]] = []
    for hit in hits:
        text = str(hit.get("content") or "")
        if not text:
            continue
        items.append(
            {
                "source": "memory",
                "id": hit.get("id"),
                "type": hit.get("type"),
                "title": hit.get("title"),
                "text": text,
            }
        )
    return items


def _fused(vault_items: list[dict[str, Any]], memory_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Both lists merged by reciprocal rank (k = :data:`PACK_RRF_K`).

    Equal ranks put the vault item first; without memory items this is the
    vault order unchanged.
    """
    ranked = [(1 / (PACK_RRF_K + rank), 0, rank, item) for rank, item in enumerate(vault_items, start=1)]
    ranked += [(1 / (PACK_RRF_K + rank), 1, rank, item) for rank, item in enumerate(memory_items, start=1)]
    ranked.sort(key=lambda entry: (-entry[0], entry[1], entry[2]))
    return [entry[3] for entry in ranked]


def _trimmed(item: dict[str, Any], room: int) -> dict[str, Any] | None:
    """``item`` with its text cut so that it costs at most ``room``; ``None`` if no text fits."""
    text = item["text"]
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_tokens({**item, "text": text[:middle], "truncated": True}) <= room:
            low = middle
        else:
            high = middle - 1
    if low == 0:
        return None
    return {**item, "text": text[:low], "truncated": True}


def _dropped(item: dict[str, Any], reason: str) -> dict[str, Any]:
    """How a ``dropped`` entry names an item: enough to read it later, without its text."""
    if item["source"] == "memory":
        return {"source": "memory", "id": item["id"], "reason": reason}
    return {
        "source": "vault",
        "doc_id": item["doc_id"],
        "file_path": item["file_path"],
        "chunk_id": item["chunk_id"],
        "reason": reason,
    }


def _fill(
    candidates: list[dict[str, Any]], budget_tokens: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Greedy fill under a hard cap: ``(items, dropped, used_tokens)``.

    In order, an item already delivered (same ``chunk_id``, or text contained
    in a delivered item's text) is dropped as ``duplicate``; one that fits is
    taken whole; one that does not is cut to the remaining room when at least
    :data:`PACK_MIN_TRIM_TOKENS` remain, and dropped as ``budget`` otherwise.
    Memory items never take more than :data:`PACK_MEMORY_SHARE` of the budget.
    """
    memory_cap = int(budget_tokens * PACK_MEMORY_SHARE)
    items: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    seen_chunks: set[tuple[str, str]] = set()
    used = memory_used = 0
    for item in candidates:
        key = (item["doc_id"], item["chunk_id"]) if item["source"] == "vault" else None
        if (key is not None and key in seen_chunks) or any(item["text"] in kept["text"] for kept in items):
            dropped.append(_dropped(item, "duplicate"))
            continue
        room = budget_tokens - used
        if item["source"] == "memory":
            room = min(room, memory_cap - memory_used)
        cost = estimate_tokens(item)
        taken: dict[str, Any] | None = item
        if cost > room:
            taken = _trimmed(item, room) if room >= PACK_MIN_TRIM_TOKENS else None
        if taken is None:
            dropped.append(_dropped(item, "budget"))
            continue
        cost = estimate_tokens(taken)
        items.append(taken)
        used += cost
        if item["source"] == "memory":
            memory_used += cost
        if key is not None:
            seen_chunks.add(key)
    return items, dropped, used


@_tool
@_offloaded
@_in_vault
def context_pack(
    query: str,
    budget_tokens: int = 3000,
    include_memories: bool = False,
    vault: str | None = None,
) -> dict[str, Any]:
    """Best sections for query under a hard token budget (budget_tokens 500-12000), deduplicated.

    dropped lists what did not fit. include_memories=True adds memory-folder
    docs and, when configured, the memory server.
    """
    if not PACK_BUDGET_MIN <= budget_tokens <= PACK_BUDGET_MAX:
        raise KnowledgeToolError(
            errors.E_ARGUMENT_OUT_OF_RANGE,
            f"budget_tokens must be between {PACK_BUDGET_MIN} and {PACK_BUDGET_MAX}, got {budget_tokens}",
        )
    rt = _ensure_indexed()
    vault_items = _pack_vault_items(rt, query, include_memories)
    memory_items: list[dict[str, Any]] = []
    degraded: list[dict[str, Any]] = []
    if include_memories:
        try:
            memory_items = _pack_memory_items(rt, query)
        except MemoryUnavailable as exc:
            degraded.append({"source": "memory", "code": exc.code})
        except Exception as exc:
            log.warning("context_pack: memory read failed (%s)", type(exc).__name__)
            degraded.append({"source": "memory", "code": errors.E_MEMORY_UNAVAILABLE})
    items, dropped, used = _fill(_fused(vault_items, memory_items), budget_tokens)
    out: dict[str, Any] = {
        "query": query,
        "vault": rt.spec.slug,
        "budget_tokens": budget_tokens,
        "used_tokens": used,
        "items": items,
        "dropped": dropped,
        "sources_used": [source for source in ("vault", "memory") if any(i["source"] == source for i in items)],
    }
    if degraded:
        out["degraded"] = degraded
    return out


# --- list_vaults ---------------------------------------------------------------


def _vault_row(spec: _settings.VaultSpec, default: str) -> dict[str, Any]:
    """What ``list_vaults`` says about one vault: never its path, repository or environment."""
    return {
        "slug": spec.slug,
        "name": spec.name or spec.slug,
        "default": spec.slug == default,
        "features": sorted(spec.features),
    }


@_tool
@_offloaded
def list_vaults() -> dict[str, Any]:
    """Vaults this server serves (slug, name, default, features) and the tool contract version.

    Pass a slug as vault= to any tool.
    """
    reg = registry()
    default = reg.default_slug
    return {
        "contract": contract.CONTRACT_VERSION,
        "default": default,
        "vaults": [_vault_row(reg.spec(slug), default) for slug in reg.slugs()],
    }


@_gated_tool("jira")
@_admin
@_offloaded
@_in_vault
def sync_jira(vault: str | None = None, dry_run: bool = True, since: str | None = None) -> dict[str, Any]:
    """Mirror the Jira issues of the vault's ``integrations.jira`` projects into its memory project's tasks.

    Registered only when a vault this process serves declares the ``jira``
    extension, over stdio. Upserts one task per issue by ``jira_key``;
    ``dry_run=True`` (the default) reads Jira and writes nothing. A vault that
    does not declare ``jira`` answers ``E_FEATURE_DISABLED``.

    Args:
        dry_run: Read Jira and report; write nothing to memory or to the index directory.
        since: ISO date or date-time, or -Nd; default: the last complete run minus a day, else 30 days.
    """
    slug = _current_vault.get()
    reg = registry()
    spec = reg.spec(reg.default_slug if slug is None else slug)
    result: dict[str, Any] = _load_extension("jira").sync(
        spec, env=os.environ, memory=_settings.current().memory, dry_run=dry_run, since=since
    )
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_HEADER_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def _normalise_header_text(text: str) -> str:
    """Normalise a header for fuzzy matching: lowercase, strip #/digits, trim."""
    s = text.strip().lower()
    # Strip leading hashes if any leaked through, plus surrounding whitespace.
    s = s.lstrip("#").strip()
    # Drop leading numeric prefixes like "1.", "1)", "1 -"
    s = re.sub(r"^\d+[.)\-\s]+", "", s).strip()
    return s


def _list_headers(body: str) -> list[str]:
    """Return all markdown headers in *body* in document order (text only)."""
    headers: list[str] = []
    for line in body.splitlines():
        m = _HEADER_RE.match(line)
        if m:
            headers.append(m.group(2).strip())
    return headers


def _extract_section(body: str, section: str) -> str | None:
    """Return the slice of *body* under the header that fuzzy-matches *section*.

    Section ends at the next header of the same level or shallower (smaller
    number of ``#``). Returns None if no matching header is found.
    """
    target = _normalise_header_text(section)
    if not target:
        return None

    lines = body.splitlines()
    start_idx: int | None = None
    start_level: int = 0

    # Two-pass: prefer exact normalised match before falling back to substring.
    # Substring matches require at least 3 chars on the shorter side to avoid
    # silly hits like target "x" matching every header that contains "x".
    exact_idx: int | None = None
    exact_level: int = 0
    fuzzy_idx: int | None = None
    fuzzy_level: int = 0

    for i, line in enumerate(lines):
        m = _HEADER_RE.match(line)
        if not m:
            continue
        level = len(m.group(1))
        text = _normalise_header_text(m.group(2))
        if text == target:
            exact_idx = i
            exact_level = level
            break
        if fuzzy_idx is None and min(len(text), len(target)) >= 3 and (
            target in text or text in target
        ):
            fuzzy_idx = i
            fuzzy_level = level

    if exact_idx is not None:
        start_idx, start_level = exact_idx, exact_level
    elif fuzzy_idx is not None:
        start_idx, start_level = fuzzy_idx, fuzzy_level

    if start_idx is None:
        return None

    end_idx = len(lines)
    for j in range(start_idx + 1, len(lines)):
        m = _HEADER_RE.match(lines[j])
        if m and len(m.group(1)) <= start_level:
            end_idx = j
            break

    return "\n".join(lines[start_idx:end_idx]).strip()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _configure_transport(app: FastMCP, http: _settings.HttpSettings) -> None:
    """Apply the bind address, port and path chosen at startup to ``app``.

    FastMCP enables DNS-rebinding protection only when it is constructed with
    a loopback host, so the same rule is replayed here for a server whose
    host is known only now.
    """
    from mcp.server.transport_security import TransportSecuritySettings

    app.settings.host = http.host
    app.settings.port = http.port
    app.settings.streamable_http_path = http.path
    if app.settings.transport_security is None and http.host in _settings.LOOPBACK_HOSTS:
        app.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"],
            allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"],
        )


def apply_profile(app: FastMCP, profile: str) -> None:
    """Remove from ``app`` every registered tool that ``profile`` does not keep.

    The kept tools stay in their registration order. ``serve`` never calls
    this: it registers the profile it serves with :func:`register_tools`.
    """
    registered = [tool.name for tool in app._tool_manager.list_tools()]
    keep = set(profiles.select(profile, registered))
    for name in registered:
        if name not in keep:
            app.remove_tool(name)


def serve(s: _settings.Settings) -> None:
    """Validate ``s``, open the default vault and run the configured transport.

    Raises :class:`~knowledge.errors.KnowledgeConfigError` when ``s`` cannot
    serve; the vault runtime is opened here, so a broken sidecar fails at
    startup rather than on the first tool call.
    """
    # force: FastMCP installs its own root handler when ``mcp`` is built at
    # import; the server's one-line format replaces it.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        force=True,
    )
    warnings = s.validate()
    warnings += extension_warnings(s, os.environ)
    _settings.install(s)
    for warning in warnings:
        log.warning("%s", warning)
    _configure_transport(mcp, s.http)
    profile = profiles.public(s.output.profile)
    if not s.output.structured or profile != profiles.DEFAULT_PROFILE:
        register_tools(mcp, structured=s.output.structured, profile=profile)
    register_gated_tools(mcp, s, structured=s.output.structured)
    set_registry(_registry_for(s))
    # Open the default vault now, so a broken sidecar fails at startup.
    default_runtime()

    transport = s.http.transport.lower().strip()
    try:
        if transport == "http":
            _run_http(s.http, serve_reindex_webhook=extensions.serves_reindex_webhook(s.default_vault()))
        else:
            mcp.run(transport="stdio")
    finally:
        try:
            drain_tool_call_counts(timeout=10)
        except concurrent.futures.TimeoutError:
            log.warning("tool-call counts still waiting on the usage file lock — closing without them")
        registry().close()


def main() -> None:
    """Entry point: load the settings from the environment and serve them.

    A configuration error exits with status 2 and one ``<code>: <message>``
    line on stderr, never a traceback.
    """
    import sys

    try:
        serve(_settings.load_settings())
    except KnowledgeConfigError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from None


def http_app(app: FastMCP, *, serve_reindex_webhook: bool = True) -> Any:
    """The ASGI application of the streamable-http transport of ``app``, with Bearer auth.

    ``mcp.run(transport="streamable-http")`` is not enough: the custom routes
    (``/health`` always, ``/webhook/reindex`` when ``serve_reindex_webhook``)
    must be registered before ``streamable_http_app()`` materializes the route
    list, and the result is wrapped with :class:`BearerAuthMiddleware`, which
    resolves each bearer to its principal. ``app`` serves the default vault.
    """
    from knowledge.auth import BearerAuthMiddleware
    from knowledge.webhook import register_health_route

    rt = _vault()
    register_health_route(
        app,
        rt.indexer,
        index_ready_flag=rt.index_state,
        vault_root=rt.vault_path,
        state_dir=rt.spec.index_dir,
        sync_state=_sync_state_for(rt),
    )
    if serve_reindex_webhook:
        _load_extension("git-webhook").register_reindex_route(
            app,
            rt.indexer,
            vault_sync=rt.sync_vault,
            branch=rt.spec.git_branch,
            refresh_lock=rt.refresh_lock,
        )
    else:
        log.info(
            "/webhook/reindex is not served: %s declares no repository (repo in vaults.yaml or VAULT_REMOTE)",
            rt.spec.slug,
        )

    asgi = app.streamable_http_app()
    asgi.add_middleware(BearerAuthMiddleware)
    return asgi


def _run_http(http: _settings.HttpSettings, *, serve_reindex_webhook: bool = True) -> None:
    """Run the streamable-http transport of ``mcp`` (see :func:`http_app`) under uvicorn."""
    import uvicorn

    app = http_app(mcp, serve_reindex_webhook=serve_reindex_webhook)

    log.info(
        "Starting streamable-http transport on %s:%d (path=%s)",
        http.host,
        http.port,
        http.path,
    )

    uvicorn.run(
        app,
        host=http.host,
        port=http.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
