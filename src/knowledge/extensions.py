"""Declarative registry of the optional extensions a vault may turn on.

Each extension is the name a vault declares under ``features`` (or, for the
judge of ``search``, the ``rerank`` block of its ``vault.yaml``) and what it
brings: the tools and HTTP routes it serves, the module that implements it,
the environment variables its credentials come from and the transports that
load it. This module only describes them and imports nothing it names: the
module of an extension is a dotted path in a string, and ``knowledge.server``
is the one place that turns it into a module, on demand. That keeps the
registry in the core layer without any edge towards ext or http, which an
import contract would see; ``tests/test_dynamic_imports.py`` keeps every core
module free of ``importlib``.

Which tools a process registers follows two rules:

* an extension that is not ``gated`` always registers its tools, whatever
  the vaults declare (none of them brings a tool today);
* a ``gated`` extension registers its tools only when some vault the process
  serves declares it and the transport loads it. The tools of every other
  gated extension are absent (:func:`gated_off_tools`). Under the ``legacy``
  profile an extension marked ``legacy_always`` registers its tools whatever
  the vaults declare, as every server of contract 1.4.0 did.

The reindex webhook route follows its own rule (:func:`serves_reindex_webhook`).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from knowledge.contract import DEFAULT_PROFILE, LEGACY_PROFILE
from knowledge.settings import MEMORY_TOKEN_ENV, VaultSpec, memory_token_required

STDIO = "stdio"
HTTP = "http"
TRANSPORTS: frozenset[str] = frozenset({STDIO, HTTP})


@dataclass(frozen=True)
class Extension:
    """One optional extension, as a vault turns it on with ``features: [<name>]``.

    ``module`` is the dotted path of the module that implements it, imported
    only by ``knowledge.server`` the first time it is needed; ``None`` while
    the extension is declarable but loads nothing. ``requires_env`` names the
    variables its credentials come from by default; a vault overrides them
    under ``integrations.<integration>.secrets`` (``email_env``,
    ``token_env``), keyed by the default's position in ``secret_keys``.
    ``fixed_env`` are variables no vault can rename. ``env_optional`` marks
    credentials whose absence only disables part of the extension: they are
    reported, never refused. ``block`` names the key of the vault's own
    ``vault.yaml`` that turns the extension on instead of ``features``
    (``rerank``); such an extension is never a feature, and the variable that
    block names replaces ``requires_env``. ``legacy_always`` marks a gated
    extension whose tools every 1.4.0 server registered: the ``legacy``
    profile registers them whatever the vaults declare.
    """

    name: str
    module: str | None
    tools: tuple[str, ...] = ()
    routes: tuple[str, ...] = ()
    requires_env: tuple[str, ...] = ()
    secret_keys: tuple[str, ...] = ()
    fixed_env: tuple[str, ...] = ()
    integration: str | None = None
    transports: frozenset[str] = TRANSPORTS
    gated: bool = False
    env_optional: bool = False
    block: str | None = None
    legacy_always: bool = False


#: Every extension, in the order ``features`` documents them.
EXTENSIONS: tuple[Extension, ...] = (
    Extension(
        name="flows",
        module="knowledge.flows",
        tools=("get_flow", "compare_flows"),
        gated=True,
        legacy_always=True,
    ),
    # The server imports knowledge.review at start whatever the vaults
    # declare: search, search_deep and get_document report needs_review and
    # age_days for every vault. Gating only decides whether the two report
    # tools are registered.
    Extension(
        name="governance",
        module="knowledge.review",
        tools=("get_stale_docs", "suggest_links"),
        gated=True,
        legacy_always=True,
    ),
    Extension(
        name="confluence",
        module="knowledge.confluence_sync",
        tools=("sync_confluence",),
        requires_env=("CONFLUENCE_EMAIL", "CONFLUENCE_TOKEN"),
        secret_keys=("email_env", "token_env"),
        integration="confluence",
        gated=True,
        legacy_always=True,
    ),
    Extension(
        name="git-webhook",
        module="knowledge.webhook",
        routes=("/webhook/reindex",),
        requires_env=("MCP_WEBHOOK_SECRET",),
        transports=frozenset({HTTP}),
        env_optional=True,
    ),
    # Both write to a memory server, which listens on the person's own
    # loopback: an HTTP deployment never loads them.
    Extension(
        name="jira",
        module="knowledge.jira_sync",
        tools=("sync_jira",),
        secret_keys=("email_env", "token_env"),
        fixed_env=(MEMORY_TOKEN_ENV,),
        integration="jira",
        transports=frozenset({STDIO}),
        gated=True,
    ),
    Extension(
        name="memory-bridge",
        module="knowledge.memory_bridge",
        requires_env=(MEMORY_TOKEN_ENV,),
        transports=frozenset({STDIO}),
    ),
    # The judge of search: a vault turns it on with its rerank block, not
    # with features, and a missing key only leaves that vault without one.
    Extension(
        name="rerank",
        module="knowledge.judges",
        requires_env=("TYPESAFE_API_KEY", "KEV_API_KEY"),
        env_optional=True,
        block="rerank",
    ),
)

_BY_NAME: Mapping[str, Extension] = {ext.name: ext for ext in EXTENSIONS}


def get(name: str) -> Extension:
    """The extension called ``name``; ``KeyError`` when none is."""
    return _BY_NAME[name]


def names() -> tuple[str, ...]:
    """Every extension name, in registry order."""
    return tuple(ext.name for ext in EXTENSIONS)


def features() -> tuple[str, ...]:
    """The extension names a vault may list under ``features``, in registry order."""
    return tuple(ext.name for ext in EXTENSIONS if ext.block is None)


def enabled(spec: VaultSpec) -> frozenset[str]:
    """The extensions vault ``spec`` declares under ``features``."""
    return frozenset(name for name in spec.features if name in _BY_NAME)


def loads_on(ext: Extension, transport: str) -> bool:
    """Whether ``transport`` (``stdio`` or ``http``) loads ``ext``; anything else reads as stdio."""
    return (HTTP if transport.lower().strip() == HTTP else STDIO) in ext.transports


def _secrets(spec: VaultSpec, ext: Extension) -> Mapping[str, Any]:
    if ext.integration is None:
        return {}
    block = spec.integrations.get(ext.integration)
    if not isinstance(block, Mapping):
        return {}
    secrets = block.get("secrets")
    return secrets if isinstance(secrets, Mapping) else {}


def env_names(spec: VaultSpec, ext: Extension) -> tuple[str, ...]:
    """The variables ``ext`` reads its credentials from for vault ``spec``, in a stable order.

    A name the vault declares under ``integrations.<integration>.secrets``
    wins over the registry default at the same position; a position with
    neither is skipped (the manifest validation of that extension requires
    it). ``fixed_env`` follow. An extension a ``block`` turns on reads the one
    variable that block names (``rerank.key_env``), and its defaults only
    when the block names none.
    """
    if ext.block == "rerank" and spec.rerank.key_env:
        return (spec.rerank.key_env,)
    declared = _secrets(spec, ext)
    out: list[str] = []
    for index, key in enumerate(ext.secret_keys):
        value = declared.get(key)
        if isinstance(value, str) and value.strip():
            out.append(value.strip())
        elif index < len(ext.requires_env):
            out.append(ext.requires_env[index])
    out.extend(ext.requires_env[len(ext.secret_keys) :])
    out.extend(ext.fixed_env)
    return tuple(dict.fromkeys(out))


def missing_env(spec: VaultSpec, ext: Extension, env: Mapping[str, str]) -> list[str]:
    """Names (never values) of the variables ``ext`` needs for ``spec`` that ``env`` lacks or leaves empty.

    ``MEMORY_HTTP_TOKEN`` is not needed when ``KNOWLEDGE_MEMORY_REQUIRE_TOKEN``
    is ``0``: the memory server on the person's loopback runs without a bearer.
    """
    waived = frozenset() if memory_token_required(env) else frozenset({MEMORY_TOKEN_ENV})
    return [name for name in env_names(spec, ext) if name not in waived and not (env.get(name) or "").strip()]


def gated_tools() -> frozenset[str]:
    """Every tool a gated extension brings."""
    return frozenset(tool for ext in EXTENSIONS if ext.gated for tool in ext.tools)


def loaded_gated(specs: Iterable[VaultSpec], transport: str) -> tuple[Extension, ...]:
    """The gated extensions some vault in ``specs`` declares and ``transport`` loads, in registry order."""
    declared = frozenset().union(*(enabled(spec) for spec in specs))
    return tuple(ext for ext in EXTENSIONS if ext.gated and ext.name in declared and loads_on(ext, transport))


def gated_off_tools(specs: Iterable[VaultSpec], transport: str, profile: str = DEFAULT_PROFILE) -> frozenset[str]:
    """The tools of the gated extensions a process serving ``specs`` over ``transport`` does not register.

    The registered catalog is the contract minus these, plus the retired
    tools under ``legacy``: ``(set(TOOL_SINCE) | (set(TOOL_REMOVED) if
    profile == "legacy" else set())) - gated_off_tools(specs, transport,
    profile)``. Under ``legacy`` the ``legacy_always`` extensions are never
    off.
    """
    on = frozenset(tool for ext in loaded_gated(specs, transport) for tool in ext.tools)
    if profile == LEGACY_PROFILE:
        on |= frozenset(tool for ext in EXTENSIONS if ext.legacy_always for tool in ext.tools)
    return gated_tools() - on


def declaring(specs: Iterable[VaultSpec], name: str) -> tuple[str, ...]:
    """Slugs of the vaults in ``specs`` that declare extension ``name``, in order."""
    return tuple(spec.slug for spec in specs if name in enabled(spec))


def serves_reindex_webhook(spec: VaultSpec) -> bool:
    """Whether the HTTP server registers ``/webhook/reindex`` for the default vault ``spec``.

    The route refreshes the vault from its git remote and reindexes it, so it
    exists only for a vault that declares a repository: ``repo`` in
    ``vaults.yaml`` or, for a ``VAULT_PATH`` deployment, ``VAULT_REMOTE``.
    Declaring ``git-webhook`` without one registers nothing (the settings
    warn with ``W_FEATURE_WITHOUT_REPO``); declaring a repository without the
    feature keeps the route, as every deployment with ``VAULT_REMOTE`` has it.
    """
    return spec.repo is not None

