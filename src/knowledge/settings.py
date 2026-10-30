"""Explicit, side-effect-free configuration.

Importing this module reads nothing: no ``.env``, no ``config.json``, no
directory is created. A ``Settings`` value exists only once something calls
:func:`load_settings`, which reads the environment and the static
``config.json`` it is pointed at and returns an immutable snapshot.
:meth:`Settings.validate` then decides whether that snapshot can serve, and
what it requires depends on what it is asked to do: an embeddings key only when
the provider is ``openai``, an HTTP bearer only when the transport is ``http``.

Precedence for each value is the one the server has always applied: an
environment variable that is *present* wins (even when empty), then
``config.json``, then the built-in default. The exceptions are the variables
where an empty value can only be a mistake — ``VAULT_PATH``,
``KNOWLEDGE_INDEX_DIR``, ``CHROMA_PERSIST_DIR``, ``KNOWLEDGE_HOME``, ``KNOWLEDGE_CONFIG_JSON``,
``KNOWLEDGE_VAULTS``, ``VAULT_REMOTE``, ``KNOWLEDGE_EMBEDDINGS_PROVIDER``,
``KNOWLEDGE_MEMORY_URL``, ``MEMORY_HTTP_TOKEN``, ``KNOWLEDGE_STRUCTURED_OUTPUT``,
``KNOWLEDGE_TEXT_INDENT`` and ``KNOWLEDGE_PROFILE`` — where empty means absent.
In particular an empty ``VAULT_PATH`` never resolves to the checkout root.

Over HTTP every request presents a bearer, and each bearer is a principal with
scopes (``read``, ``admin``). Without ``MCP_ADMIN_KEY`` and without
``bearers`` in ``vaults.yaml``, ``MCP_API_KEY`` holds both scopes. With
``MCP_ADMIN_KEY``, ``MCP_API_KEY`` is read-only and ``MCP_ADMIN_KEY`` holds
both. ``bearers`` (``vaults.yaml`` only) replaces the two keys entirely: each
entry names the variable its token comes from, never the token.

Which vaults the process serves is decided once, in this order:

1. ``KNOWLEDGE_VAULTS`` names a ``vaults.yaml``: every vault it declares, with
   one default;
2. ``VAULT_PATH`` (or, without it, ``vault_path`` in ``config.json``): a
   registry of one vault, whose index stays where it always lived;
3. ``$XDG_CONFIG_HOME/knowledge/vaults.yaml`` or ``~/.config/knowledge/vaults.yaml``
   when it exists (an installed package with no checkout around it).

A vault's own ``vault.yaml`` may also declare a ``rerank`` block: the judge
that reorders ``search`` results and the variable its key comes from, never the
key. A block that is malformed leaves only that vault without a judge (a
``W_RERANK_INVALID`` warning); a block that names a credential variable the
vault may not use, or a URL the key could leak to, stops the server.

A ``VAULT_PATH`` therefore always wins over a ``vaults.yaml`` the operator did
not name. Each registered vault keeps its index directory (``index.sqlite``,
link graph, usage statistics) under ``<knowledge_home>/<slug>/``, and its
own ``vault.yaml`` may declare review thresholds, the memory folder, the
embeddings it was indexed with and a display ``name``.

:func:`current` keeps one process-wide snapshot for the legacy
``knowledge.config`` facade; :func:`install` pins it to the snapshot a server
validated, and :func:`reset_current` drops it so the next read re-reads the
environment.
"""

from __future__ import annotations

import json
import math
import os
import re
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from knowledge import contract, errors

PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]

EMBEDDING_PROVIDERS: frozenset[str] = frozenset({"openai", "none", "bag-of-words"})
LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})


#: What a bearer token may hold to travel in an ``Authorization`` header:
#: printable ASCII, no space. Anything else makes ``http.client`` refuse the
#: header with an error whose text carries the token.
_HEADER_TOKEN = re.compile(r"[\x21-\x7e]+")


def header_token_valid(token: str) -> bool:
    """Whether ``token`` can travel as ``Authorization: Bearer <token>``."""
    return _HEADER_TOKEN.fullmatch(token) is not None


def is_loopback_http_url(url: str) -> bool:
    """Whether ``url`` is plain ``http`` on a loopback host (``127.0.0.1``, ``localhost``, ``::1``).

    The memory server is only ever called on such a URL, so its bearer never
    leaves the machine.
    """
    parsed = urllib.parse.urlsplit(url)
    return parsed.scheme == "http" and (parsed.hostname or "") in LOOPBACK_HOSTS


#: Scope every authenticated HTTP caller needs to use the server at all.
SCOPE_READ = "read"
#: Scope the tools and routes that change the index or the vault require.
SCOPE_ADMIN = "admin"
BEARER_SCOPES: frozenset[str] = frozenset({SCOPE_READ, SCOPE_ADMIN})
#: A bearer id is logged with every request it authenticates, so it stays short.
BEARER_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")

#: Features a vault may declare in ``vaults.yaml``.
VAULT_FEATURES: frozenset[str] = frozenset(
    {"flows", "governance", "confluence", "git-webhook", "jira", "memory-bridge"}
)
#: A slug names the vault's index directory, so it stays a short, path-safe name.
SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}[a-z0-9]$")
#: Where the memory server listens when ``KNOWLEDGE_MEMORY_URL`` is not set:
#: its default port, on the loopback interface.
DEFAULT_MEMORY_URL = "http://127.0.0.1:6366"
#: Variable that carries the memory server's bearer.
MEMORY_TOKEN_ENV = "MEMORY_HTTP_TOKEN"
#: Variable that, set to ``0``, lets the memory-bridge write to a loopback
#: memory server that runs without a bearer. Any other value, or none,
#: requires one.
MEMORY_REQUIRE_TOKEN_ENV = "KNOWLEDGE_MEMORY_REQUIRE_TOKEN"


def memory_token_required(env: Mapping[str, str]) -> bool:
    """Whether a write to the memory server needs ``MEMORY_HTTP_TOKEN``.

    It does unless ``KNOWLEDGE_MEMORY_REQUIRE_TOKEN`` is exactly ``0``: a
    misspelled value keeps the requirement, so a typo never opens an
    unauthenticated write.
    """
    return (env.get(MEMORY_REQUIRE_TOKEN_ENV) or "").strip() != "0"


#: The tool contract this package serves; a vault's ``knowledge_contract``
#: range is checked against it.
TOOL_CONTRACT_VERSION: tuple[int, ...] = contract.CONTRACT_VERSION_INFO

VAULTS_FILE_NAME = "vaults.yaml"
VAULT_MANIFEST_NAME = "vault.yaml"

#: The slug of a ``VAULT_PATH`` vault that names none: no ``CHROMA_COLLECTION``, no ``chroma_collection``
#: in ``config.json`` and no valid ``slug`` in its own ``vault.yaml``. It belongs to no tenant.
_FALLBACK_SLUG = "vault"
_DEFAULT_EMBEDDING_MODEL = "text-embedding-3-large"
_DEFAULT_MEMORY_FOLDER = "90 - Memory"
_DEFAULT_MEMORY_EXPORT_TYPES: tuple[str, ...] = ("decision", "discovery")
_FALLBACK_REVIEW_THRESHOLDS: Mapping[str, int] = MappingProxyType({"default": 270})
_DEFAULT_PROMOTION_TARGET_FOLDERS: Mapping[str, str] = MappingProxyType(
    {"decision": "Architecture/Decisions", "discovery": "Architecture/Discoveries"}
)


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HttpSettings:
    """Transport and the HTTP surface (bearer, webhook secret, bind address)."""

    transport: str = "stdio"
    host: str = "0.0.0.0"
    port: int = 8000
    path: str = "/mcp"
    api_key: str = field(default="", repr=False)
    webhook_secret: str = field(default="", repr=False)
    #: ``MCP_ADMIN_KEY``: when set, ``api_key`` is read-only and this one holds ``admin``.
    admin_key: str = field(default="", repr=False)


@dataclass(frozen=True)
class BearerSpec:
    """One entry of ``bearers`` in ``vaults.yaml``: who a token belongs to and what it may do.

    ``token_env`` names the variable the token comes from; ``token`` is its
    value at load time, empty when the variable is unset.
    """

    id: str
    token_env: str
    scopes: frozenset[str]
    token: str = field(default="", repr=False)


@dataclass(frozen=True)
class EmbeddingSettings:
    """Which embeddings provider a vault uses and where its key comes from."""

    provider: str = "openai"
    model: str = _DEFAULT_EMBEDDING_MODEL
    key_env: str = "OPENAI_API_KEY"
    api_key: str = field(default="", repr=False)


@dataclass(frozen=True)
class MemorySettings:
    """Where the memory server listens and the bearer it is read and written with.

    ``token`` is empty when ``MEMORY_HTTP_TOKEN`` is not set; a reading client
    never calls the server without one. ``require_token`` governs writes
    only (the memory-bridge): ``False`` when ``KNOWLEDGE_MEMORY_REQUIRE_TOKEN``
    is ``0``, for a loopback memory server that runs without a bearer.
    """

    url: str = DEFAULT_MEMORY_URL
    token: str = field(default="", repr=False)
    require_token: bool = True


@dataclass(frozen=True)
class OutputSettings:
    """How tool results go on the wire.

    The text blocks are the canonical answer of every tool. ``structured``
    (``KNOWLEDGE_STRUCTURED_OUTPUT``, default off) adds ``outputSchema`` to
    the catalog and ``structuredContent`` to every result, a second view of
    the same JSON; off, the tools are registered without them and only the
    text blocks travel. ``text_indent`` (``KNOWLEDGE_TEXT_INDENT``) indents
    the JSON of each text block; ``None``, the default, writes it compact.
    ``profile`` (``KNOWLEDGE_PROFILE``, ``serve --profile``) is the tool
    profile the server registers: ``all``, the default, or ``legacy``.
    """

    structured: bool = False
    text_indent: int | None = None
    profile: str = contract.DEFAULT_PROFILE


#: Largest ``KNOWLEDGE_TEXT_INDENT`` accepted.
MAX_TEXT_INDENT = 8

#: The variable that selects the tool profile; ``serve --profile`` wins over it.
PROFILE_ENV = "KNOWLEDGE_PROFILE"


def _output_settings(env: Mapping[str, str]) -> OutputSettings:
    """``OutputSettings`` from the environment; empty reads as absent."""
    structured = _non_empty(env, "KNOWLEDGE_STRUCTURED_OUTPUT") or "0"
    if structured not in ("0", "1"):
        raise errors.KnowledgeConfigError(
            errors.E_OUTPUT_SETTING_INVALID, "KNOWLEDGE_STRUCTURED_OUTPUT must be 1 or 0"
        )
    raw_indent = _non_empty(env, "KNOWLEDGE_TEXT_INDENT")
    indent: int | None = None
    if raw_indent:
        if not raw_indent.isdigit() or not 1 <= int(raw_indent) <= MAX_TEXT_INDENT:
            raise errors.KnowledgeConfigError(
                errors.E_OUTPUT_SETTING_INVALID,
                f"KNOWLEDGE_TEXT_INDENT must be an integer from 1 to {MAX_TEXT_INDENT}, or unset for compact JSON",
            )
        indent = int(raw_indent)
    profile = _non_empty(env, PROFILE_ENV) or contract.DEFAULT_PROFILE
    if profile not in contract.PUBLIC_PROFILES:
        raise errors.KnowledgeConfigError(
            errors.E_PROFILE_UNKNOWN,
            f"{PROFILE_ENV} (or serve --profile) must be one of: {', '.join(contract.PUBLIC_PROFILES)}",
        )
    return OutputSettings(structured=structured == "1", text_indent=indent, profile=profile)


#: Judges a vault may declare under ``rerank.provider``; ``none`` is the absence of one.
RERANK_PROVIDERS: frozenset[str] = frozenset({"none", "jev", "kev"})
#: Providers that reach a model: the ones ``KNOWLEDGE_RERANK_PROVIDERS`` lists.
RERANK_REMOTE_PROVIDERS: tuple[str, ...] = ("jev", "kev")
#: The word every credential variable of a provider carries as a whole segment
#: (``TYPESAFE_API_KEY``, ``ACME_KEV_API_KEY``). The rest is ``_API_KEY``.
RERANK_KEY_WORDS: Mapping[str, str] = MappingProxyType({"jev": "TYPESAFE", "kev": "KEV"})
RERANK_KEY_SUFFIX = "API_KEY"
#: Where TypeSafe serves Jev; the one host a ``jev`` key may travel to.
TYPESAFE_ORIGIN = "https://api.typesafe.ai"
#: Operator variables (never a vault's): switch, allowed providers, URL and timeout.
RERANK_SWITCH_ENV = "KNOWLEDGE_RERANK"
RERANK_PROVIDERS_ENV = "KNOWLEDGE_RERANK_PROVIDERS"
RERANK_BASE_URL_ENV = "KNOWLEDGE_RERANK_BASE_URL"
RERANK_TIMEOUT_ENV = "KNOWLEDGE_RERANK_TIMEOUT_MS"
#: Bounds of the numeric keys of the ``rerank`` block.
RERANK_CANDIDATES = (1, 12)
RERANK_UNITS_PER_DOC = (1, 3)
RERANK_TIMEOUT_MS = (200, 10_000)
RERANK_TIMEOUT_MS_LOCAL = (200, 60_000)
RERANK_MAX_INPUT_TOKENS = (1_000, 30_000)


@dataclass(frozen=True)
class RerankSettings:
    """The judge a vault declares for ``search``, as its ``vault.yaml`` spells it.

    ``provider`` ``none`` (the default, and what a vault without a ``rerank``
    block or with a malformed one gets) means no judge. ``default_on`` is
    ``rerank.default: on``: ``search`` reorders when the caller omits
    ``rerank``. ``model`` is a pinned version, never an alias. ``key_env`` is
    the NAME of the variable the key comes from and ``api_key`` its value at
    load time, empty when unset. ``candidates`` documents are judged with up to
    ``units_per_doc`` passages each, in one request that may bill at most
    ``max_input_tokens``; ``max_usd_per_day`` is the spending cap per vault
    (0 for none). ``conditional_gap`` reranks only when the two best hybrid
    scores are closer than this value. ``never_send`` lists path prefixes whose
    content never leaves the machine. ``base_url`` points ``kev`` at a loopback
    server.
    """

    provider: str = "none"
    default_on: bool = False
    model: str = ""
    key_env: str = ""
    api_key: str = field(default="", repr=False)
    candidates: int = 10
    units_per_doc: int = 2
    timeout_ms: int = 1500
    max_input_tokens: int = 20_000
    max_usd_per_day: float = 1.0
    conditional_gap: float | None = None
    never_send: tuple[str, ...] = ()
    base_url: str | None = None

    @property
    def configured(self) -> bool:
        """Whether the vault declares a judge at all."""
        return self.provider != "none"


@dataclass(frozen=True)
class RerankOperator:
    """What the operator of this process decides about rerank, whatever the vaults declare.

    ``off`` (``KNOWLEDGE_RERANK=off``) switches every judge off. ``providers``
    (``KNOWLEDGE_RERANK_PROVIDERS``) lists the ones that may run; a vault that
    declares another searches without a judge. ``base_url``
    (``KNOWLEDGE_RERANK_BASE_URL``) replaces the URL of every provider and
    ``timeout_ms`` (``KNOWLEDGE_RERANK_TIMEOUT_MS``) the timeout of every vault.
    """

    off: bool = False
    providers: frozenset[str] = frozenset(RERANK_REMOTE_PROVIDERS)
    base_url: str | None = None
    timeout_ms: int | None = None

    def allows(self, provider: str) -> bool:
        """Whether a vault that declares ``provider`` may use its judge in this process."""
        return not self.off and provider in self.providers


@dataclass(frozen=True)
class ConfluenceSettings:
    url: str = ""
    email: str = ""
    token: str = field(default="", repr=False)


@dataclass(frozen=True)
class PromotionSettings:
    """Memory promotion bridge: where candidates render, and how they are stamped."""

    from_config: bool
    output_dir: Path
    target_folders: Mapping[str, str]
    doc_status: str
    default_owner: str
    branch_prefix: str


@dataclass(frozen=True)
class VaultSpec:
    """One vault this process can serve, and where its index lives.

    ``index_dir`` holds ``index.sqlite`` and its sidecars (link graph, usage
    statistics, index metadata). ``chroma_dir`` is where a Chroma index of the
    same vault lived, the source ``migrate-index`` copies sidecars from; it may
    be ``index_dir`` itself.
    """

    slug: str
    path: Path | None
    is_default: bool
    index_dir: Path
    memory_folder: str
    review_thresholds: Mapping[str, int]
    thresholds_from_config: bool
    embeddings: EmbeddingSettings
    origin: Literal["vault_path", "vaults_yaml"] = "vault_path"
    repo: str | None = None
    git_auto_pull: bool = True
    git_branch: str = "main"
    deploy_key_env: str | None = None
    webhook_secret_env: str | None = None
    features: frozenset[str] = frozenset()
    memory_project: str | None = None
    #: Display name from the vault's own ``vault.yaml`` (``name``), if it declares one.
    name: str | None = None
    chroma_dir: Path | None = None
    #: ``integrations`` of the vault's own ``vault.yaml`` (``jira``, ``confluence``),
    #: frozen and read as declared: the extension that uses a block validates it.
    integrations: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    #: ``memory.hub`` of the vault's own ``vault.yaml``: the vault-relative path
    #: the memory-bridge publishes as the project card's ``knowledge_hub_path``.
    memory_hub: str | None = None
    #: ``rerank`` of the vault's own ``vault.yaml``; ``provider`` ``none`` when it declares no judge.
    rerank: RerankSettings = field(default_factory=RerankSettings)
    #: ``index.exclude`` of the vault's own ``vault.yaml``: glob patterns for files that are not notes,
    #: which the review queue leaves out as ``vault-lint`` leaves them out of the frontmatter rules.
    index_exclude: tuple[str, ...] = ()

    @property
    def skipped_log(self) -> Path:
        """Where a reindex records the files it could not embed: inside this vault's index directory."""
        return self.index_dir / "skipped_files.json"

    @property
    def path_hint(self) -> str:
        """Where an operator declares this vault's directory."""
        if self.origin == "vaults_yaml":
            return "set the vault's path in vaults.yaml"
        return "set VAULT_PATH (or vault_path in config.json)"

    def require_path(self) -> Path:
        """The vault directory, or a coded error when none was declared."""
        if self.path is None:
            raise errors.KnowledgeConfigError(
                errors.E_VAULT_PATH_MISSING,
                f"{self.slug}: no vault directory is declared; {self.path_hint}",
            )
        return self.path


def check_vault_dir(spec: VaultSpec) -> None:
    """Refuse a vault whose directory (or, from ``vaults.yaml``, manifest) is missing."""
    path = spec.require_path()
    if not path.is_dir():
        raise errors.KnowledgeConfigError(
            errors.E_VAULT_PATH_NOT_DIR,
            f"{spec.slug}: {path} is not a directory; {spec.path_hint} to an existing directory",
        )
    if spec.origin == "vaults_yaml" and not (path / VAULT_MANIFEST_NAME).is_file():
        raise errors.KnowledgeConfigError(
            errors.E_VAULT_MANIFEST_MISSING,
            f"{spec.slug}: {path} has no {VAULT_MANIFEST_NAME}; add one that declares "
            f"at least 'slug: {spec.slug}'",
        )


@dataclass(frozen=True)
class Settings:
    """Everything this process was configured with, as one immutable snapshot."""

    project_root: Path
    config_json_path: Path
    config_json_loaded: bool
    raw_config: Mapping[str, Any]
    knowledge_home: Path
    vaults: tuple[VaultSpec, ...]
    http: HttpSettings
    confluence: ConfluenceSettings
    promotion: PromotionSettings
    memory_export_types: tuple[str, ...]
    embedding_model: str
    vaults_file: Path | None = None
    load_warnings: tuple[str, ...] = ()
    memory: MemorySettings = field(default_factory=MemorySettings)
    output: OutputSettings = field(default_factory=OutputSettings)
    #: ``bearers`` of ``vaults.yaml``; empty when it declares none (or there is no ``vaults.yaml``).
    bearers: tuple[BearerSpec, ...] = ()
    #: ``KNOWLEDGE_RERANK*`` of the environment: what the operator allows, over any vault.
    rerank_operator: RerankOperator = field(default_factory=RerankOperator)

    def default_vault(self) -> VaultSpec:
        for spec in self.vaults:
            if spec.is_default:
                return spec
        return self.vaults[0]

    def vault(self, slug: str) -> VaultSpec:
        """The registered vault named ``slug``."""
        for spec in self.vaults:
            if spec.slug == slug:
                return spec
        raise errors.KnowledgeConfigError(
            errors.E_VAULT_UNKNOWN,
            f"no vault named {slug!r} is registered; registered: "
            f"{', '.join(spec.slug for spec in self.vaults)}",
        )

    def validate(self) -> list[str]:
        """Refuse a configuration that cannot serve; return the warnings it can.

        Raises :class:`~knowledge.errors.KnowledgeConfigError` on the first
        fatal problem. Returns ``"<W_CODE>: <message>"`` strings for problems
        the server survives, for the caller to log.
        """
        warnings: list[str] = list(self.load_warnings)
        for spec in self.vaults:
            check_vault_dir(spec)
            emb = spec.embeddings
            if emb.provider not in EMBEDDING_PROVIDERS:
                raise errors.KnowledgeConfigError(
                    errors.E_EMBEDDINGS_PROVIDER_UNKNOWN,
                    f"{spec.slug}: KNOWLEDGE_EMBEDDINGS_PROVIDER must be one of "
                    f"{', '.join(sorted(EMBEDDING_PROVIDERS))}",
                )
            if emb.provider == "openai" and not emb.api_key:
                raise errors.KnowledgeConfigError(
                    errors.E_EMBEDDINGS_KEY_MISSING,
                    f"{spec.slug}: the openai embeddings provider needs {emb.key_env}; "
                    f"export it or add it to .env, or set KNOWLEDGE_EMBEDDINGS_PROVIDER=none",
                )
            if spec.repo is None and _inside_git_checkout(spec.path):
                warnings.append(
                    f"{errors.W_VAULT_GIT_PULL_OFF}: {spec.slug}: the vault sits in a git "
                    "checkout but declares no repository (VAULT_REMOTE or repo in "
                    "vaults.yaml); reindex indexes it without git pull and /webhook/reindex "
                    "is not served for it"
                )
            rr = spec.rerank
            if rr.configured and not rr.api_key and self.rerank_operator.allows(rr.provider):
                warnings.append(
                    f"{errors.W_RERANK_KEY_MISSING}: {spec.slug}: the {rr.provider} rerank provider needs "
                    f"{rr.key_env}; export it and this vault reranks, until then it searches without a judge"
                )
            if not spec.thresholds_from_config:
                warnings.append(
                    f"{errors.W_REVIEW_THRESHOLDS_FALLBACK}: {spec.slug}: no "
                    "review.thresholds_days declared; every document type uses the "
                    f"{_FALLBACK_REVIEW_THRESHOLDS['default']}-day catch-all"
                )

        transport = self.http.transport.lower().strip()
        if transport == "http":
            self.check_bearers()
            # /webhook/reindex exists only for a default vault that declares a
            # repository (extensions.serves_reindex_webhook), so only then does
            # a missing secret matter.
            if not self.http.webhook_secret and self.default_vault().repo is not None:
                warnings.append(
                    f"{errors.W_WEBHOOK_SECRET_MISSING}: MCP_WEBHOOK_SECRET is not set; "
                    "/webhook/reindex only accepts the manual bearer (HMAC mode disabled)"
                )
        elif transport != "stdio":
            warnings.append(
                f"{errors.W_TRANSPORT_UNKNOWN}: MCP_TRANSPORT is neither 'http' nor "
                "'stdio'; serving stdio"
            )
        if transport != "http" and self.bearers:
            warnings.append(
                f"{errors.W_BEARERS_IGNORED_STDIO}: {self.vaults_file}: bearers apply only to "
                "MCP_TRANSPORT=http; over stdio the process runs as the person and no bearer is checked"
            )
        return warnings

    def check_bearers(self) -> None:
        """Refuse an HTTP surface whose bearers cannot tell read from admin.

        Names variables and bearer ids, never a token.
        """
        if self.bearers:
            missing = [b.token_env for b in self.bearers if not b.token]
            if missing:
                raise errors.KnowledgeConfigError(
                    errors.E_SECRET_MISSING,
                    f"{self.vaults_file}: bearers need {', '.join(missing)}; export them or drop "
                    "those entries",
                )
            seen: dict[str, str] = {}
            for bearer in self.bearers:
                if bearer.token in seen:
                    raise _manifest_error(
                        self.vaults_file or Path(VAULTS_FILE_NAME),
                        "bearers",
                        f"has a duplicate bearer value ({seen[bearer.token]} and {bearer.id}); "
                        "every bearer needs its own token",
                    )
                seen[bearer.token] = bearer.id
            return
        if not self.http.api_key and not self.http.admin_key:
            raise errors.KnowledgeConfigError(
                errors.E_HTTP_API_KEY_MISSING,
                "MCP_TRANSPORT=http requires MCP_API_KEY (or MCP_ADMIN_KEY, or bearers in "
                "vaults.yaml); generate one with: openssl rand -hex 32",
            )
        if self.http.admin_key and self.http.admin_key == self.http.api_key:
            raise errors.KnowledgeConfigError(
                errors.E_MANIFEST_INVALID,
                "MCP_ADMIN_KEY equals MCP_API_KEY, which would give the admin scope to every "
                "read-only client; generate a distinct one with: openssl rand -hex 32",
            )


# ---------------------------------------------------------------------------
# config.json
# ---------------------------------------------------------------------------


def load_config_json(path: Path) -> tuple[dict[str, Any], bool]:
    """Read the static settings file, telling "absent" apart from "unreadable".

    Returns ``(settings, found)``. The two failure modes must never collapse
    into the same empty dict:

    * the file is not there — every setting falls back to its built-in
      default, and ``found`` says so out loud, so a caller (or a gate) can
      refuse to trust numbers nobody chose;
    * the file is there but cannot be read, is not JSON, or is not a JSON
      object — that is "I could not look", never "there was nothing to find",
      and it raises. Swallowing it would hand every consumer a default while
      the real configuration sat one unreadable byte away.
    """
    if not path.exists():
        return {}, False
    try:
        loaded = json.loads(path.read_text())
    except OSError as exc:
        raise errors.KnowledgeConfigError(
            errors.E_CONFIG_JSON_INVALID, f"{path} exists but could not be read: {exc}"
        ) from exc
    except ValueError as exc:
        raise errors.KnowledgeConfigError(
            errors.E_CONFIG_JSON_INVALID, f"{path} exists but is not valid JSON: {exc}"
        ) from exc
    if not isinstance(loaded, dict):
        raise errors.KnowledgeConfigError(
            errors.E_CONFIG_JSON_INVALID,
            f"{path} must hold a JSON object, got {type(loaded).__name__}",
        )
    return loaded, True


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _present(env: Mapping[str, str], name: str, default: str) -> str:
    """``os.environ.get`` semantics: a present key wins even when empty."""
    return env[name] if name in env else default


def _non_empty(env: Mapping[str, str], name: str) -> str:
    """A variable where an empty value can only be a mistake reads as absent."""
    return env.get(name, "").strip()


def resolve_relative_path(raw_path: str) -> str:
    """Resolve a path relative to the project root if it is not absolute."""
    p = Path(raw_path)
    return str(p if p.is_absolute() else PROJECT_ROOT / p)


def _anchor(raw: str, root: Path) -> Path:
    path = Path(raw).expanduser()
    return path if path.is_absolute() else root / path


def _section(cfg: Mapping[str, Any], name: str) -> dict[str, Any]:
    value = cfg.get(name)
    return value if isinstance(value, dict) else {}


def _knowledge_home(env: Mapping[str, str], data_dir: Path | None = None) -> tuple[Path, bool]:
    """``KNOWLEDGE_HOME`` > ``data_dir`` of ``vaults.yaml`` > ``$XDG_DATA_HOME/knowledge``
    > ``~/.local/share/knowledge``.

    The flag says whether the operator chose it explicitly; only an explicit
    home relocates the index of a ``VAULT_PATH`` deployment.
    """
    explicit = _non_empty(env, "KNOWLEDGE_HOME")
    if explicit:
        return Path(explicit).expanduser(), True
    if data_dir is not None:
        return data_dir, True
    xdg = _non_empty(env, "XDG_DATA_HOME")
    if xdg:
        return Path(xdg).expanduser() / "knowledge", False
    return Path("~/.local/share/knowledge").expanduser(), False


# ---------------------------------------------------------------------------
# vaults.yaml and vault.yaml
# ---------------------------------------------------------------------------

_TOP_KEYS = frozenset({"version", "data_dir", "vaults", "bearers"})
_BEARER_KEYS = frozenset({"id", "token_env", "scopes"})
_ENTRY_KEYS = frozenset(
    {"slug", "path", "default", "repo", "deploy_key_env", "git", "webhook", "features", "memory", "embeddings"}
)
_GIT_KEYS = frozenset({"auto_pull", "branch"})
_WEBHOOK_KEYS = frozenset({"secret_env"})
_MEMORY_KEYS = frozenset({"project"})
_EMBEDDINGS_KEYS = frozenset({"provider", "model", "key_env"})

#: Keys that may only ever name where a secret lives, never hold one.
_SECRET_KEYS = frozenset({"deploy_key", "token", "secret", "api_key", "password"})
#: A known credential prefix anywhere in a value, including inside a remote URL.
_SECRET_TOKEN = re.compile(r"(?<![A-Za-z0-9])(?:sk-|ghp_|gho_|ghs_|ghu_|github_pat_|xox[bp]-)[A-Za-z0-9_-]{12,}")
_SECRET_RUN = re.compile(r"[A-Za-z0-9+/_-]{40,}={0,2}")
#: Keys holding a path or a remote: only the unambiguous token prefixes apply,
#: a long path is not a base64 run.
_PATH_KEYS = frozenset({"path", "data_dir", "repo", "never_send"})
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CONTRACT_CLAUSE = re.compile(r"^(>=|<=|==|!=|>|<)?\s*(\d+(?:\.\d+)*)$")


@dataclass(frozen=True)
class _VaultsDocument:
    path: Path
    home: Path
    specs: tuple[VaultSpec, ...]
    warnings: tuple[str, ...]
    bearers: tuple[BearerSpec, ...] = ()


def _manifest_error(source: Path, where: str, why: str) -> errors.KnowledgeConfigError:
    return errors.KnowledgeConfigError(errors.E_MANIFEST_INVALID, f"{source}: {where} {why}")


def _load_yaml(path: Path) -> Any:
    """Parse ``path`` as YAML; an error names the file and position, never its text."""
    import yaml

    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise errors.KnowledgeConfigError(
            errors.E_MANIFEST_INVALID, f"{path} could not be read ({exc.__class__.__name__})"
        ) from None
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark is not None else ""
        raise errors.KnowledgeConfigError(
            errors.E_MANIFEST_INVALID, f"{path} is not valid YAML{where}"
        ) from None


def _looks_like_secret(value: str, *, path_like: bool) -> bool:
    text = value.strip()
    if "-----BEGIN" in text or _SECRET_TOKEN.search(text):
        return True
    if path_like or not _SECRET_RUN.fullmatch(text):
        return False
    return any(c.isdigit() for c in text) and any(c.isupper() for c in text) and any(c.islower() for c in text)


def _scan_secrets(node: Any, where: str, source: Path, *, key: str = "") -> None:
    """Refuse a manifest that holds a credential instead of naming its variable."""
    if isinstance(node, dict):
        for raw_key, value in node.items():
            name = str(raw_key)
            child = f"{where}.{name}" if where else name
            if name.lower() in _SECRET_KEYS and value not in (None, ""):
                raise errors.KnowledgeConfigError(
                    errors.E_SECRET_IN_MANIFEST,
                    f"{source}: {child} holds a value; a manifest names the environment "
                    "variable that carries a secret (deploy_key_env, secret_env, key_env), never the secret",
                )
            _scan_secrets(value, child, source, key=name)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _scan_secrets(item, f"{where}[{index}]", source, key=key)
    elif isinstance(node, str) and _looks_like_secret(node, path_like=key in _PATH_KEYS):
        raise errors.KnowledgeConfigError(
            errors.E_SECRET_IN_MANIFEST,
            f"{source}: {where} looks like a credential; move it to an environment variable "
            "and reference that variable by name",
        )


def _expect_keys(mapping: Mapping[str, Any], allowed: frozenset[str], where: str, source: Path) -> None:
    unknown = sorted(str(k) for k in mapping if k not in allowed)
    if unknown:
        raise _manifest_error(
            source, f"{where}.{unknown[0]}", f"is not a known key; allowed: {', '.join(sorted(allowed))}"
        )


def _opt_mapping(mapping: Mapping[str, Any], key: str, where: str, source: Path) -> dict[str, Any] | None:
    value = mapping.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise _manifest_error(source, f"{where}.{key}", "must be a mapping")
    return value


def _opt_str(mapping: Mapping[str, Any], key: str, where: str, source: Path) -> str | None:
    value = mapping.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise _manifest_error(source, f"{where}.{key}", "must be a non-empty string")
    return value.strip()


def _opt_env_name(mapping: Mapping[str, Any], key: str, where: str, source: Path) -> str | None:
    value = _opt_str(mapping, key, where, source)
    if value is not None and not _ENV_NAME.fullmatch(value):
        raise _manifest_error(source, f"{where}.{key}", "must be the name of an environment variable")
    return value


def _opt_bool(mapping: Mapping[str, Any], key: str, where: str, source: Path) -> bool | None:
    value = mapping.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise _manifest_error(source, f"{where}.{key}", "must be true or false")
    return value


def read_vault_manifest(vault_dir: Path | None) -> dict[str, Any]:
    """The vault's own ``vault.yaml`` as a mapping, or ``{}`` when it has none.

    Read leniently: only the fields the server uses are looked at, and the full
    schema belongs to the vault linter.
    """
    if vault_dir is None:
        return {}
    path = vault_dir / VAULT_MANIFEST_NAME
    if not path.is_file():
        return {}
    loaded = _load_yaml(path)
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise _manifest_error(path, "the document", "must be a mapping")
    return loaded


def _sub(mapping: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = mapping.get(key)
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def index_exclude_patterns(manifest: Mapping[str, Any]) -> tuple[str, ...]:
    """The ``index.exclude`` globs of a ``vault.yaml``, or none when the key is absent or malformed.

    The same shape the vault linter accepts (a list of non-blank strings, all
    or nothing). A malformed value is the linter's finding to report
    (``E_MANIFEST_INVALID``); a reader that only needs to know what to leave
    out treats it as declaring nothing.
    """
    index = manifest.get("index")
    declared = index.get("exclude") if isinstance(index, dict) else None
    if isinstance(declared, list) and all(isinstance(g, str) and g.strip() for g in declared):
        return tuple(g.strip() for g in declared)
    return ()


def _manifest_thresholds(manifest: Mapping[str, Any]) -> dict[str, int] | None:
    declared = _sub(manifest, "review").get("thresholds_days")
    if not isinstance(declared, dict):
        return None
    return {str(k): v for k, v in declared.items() if isinstance(v, int) and not isinstance(v, bool)}


def _manifest_features(manifest: Mapping[str, Any]) -> frozenset[str]:
    declared = manifest.get("features")
    if not isinstance(declared, list):
        return frozenset()
    return frozenset(str(f) for f in declared if isinstance(f, str))


def _frozen(node: Any) -> Any:
    """``node`` with every mapping read-only and every list a tuple."""
    if isinstance(node, dict):
        return MappingProxyType({str(k): _frozen(v) for k, v in node.items()})
    if isinstance(node, list):
        return tuple(_frozen(item) for item in node)
    return node


#: Integrations whose ``vault.yaml`` block may name a site and credential
#: variables, with the word every one of those variable names must carry. A
#: vault is curated content, so it may only point an integration at variables
#: of that integration, never at a server secret (``MCP_ADMIN_KEY``,
#: ``OPENAI_API_KEY``, a bearer's ``token_env``) that would then be sent to
#: the site it declares.
INTEGRATION_ENV_WORDS: Mapping[str, str] = MappingProxyType({"confluence": "CONFLUENCE", "jira": "JIRA"})
#: The credential variables an integration block names, with the suffix each must end in.
INTEGRATION_SECRET_SUFFIXES: Mapping[str, str] = MappingProxyType({"email_env": "EMAIL", "token_env": "TOKEN"})


def integration_env_allowed(integration: str, key: str, name: str) -> bool:
    """Whether ``integrations.<integration>.secrets.<key>`` may name the variable ``name``.

    The name is upper snake case, carries the integration's word as a whole
    segment and ends in the key's suffix: ``CONFLUENCE_TOKEN`` or
    ``ACME_CONFLUENCE_TOKEN`` for ``confluence``/``token_env``.
    """
    word = INTEGRATION_ENV_WORDS.get(integration)
    suffix = INTEGRATION_SECRET_SUFFIXES.get(key)
    if word is None or suffix is None:
        return False
    pattern = rf"(?:[A-Z0-9]+_)*{word}_(?:[A-Z0-9]+_)*{suffix}"
    return re.fullmatch(pattern, name) is not None


#: Hosts an integration may call over https without the operator naming them:
#: Atlassian Cloud sites. An entry that starts with a dot matches any host
#: below it; any other entry matches one host exactly.
DEFAULT_INTEGRATION_SITES: tuple[str, ...] = (".atlassian.net",)
#: Variable where the operator lists more hosts an integration may call,
#: comma separated, in the form of :data:`DEFAULT_INTEGRATION_SITES`. A vault
#: never extends the list: it is content, and the list decides where a
#: credential travels.
INTEGRATION_SITES_ENV = "KNOWLEDGE_INTEGRATION_SITES"


def integration_sites(env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """The hosts an integration may call over https: the defaults plus those of ``KNOWLEDGE_INTEGRATION_SITES``."""
    raw = (env or {}).get(INTEGRATION_SITES_ENV) or ""
    extra = tuple(entry.strip().lower().rstrip(".") for entry in raw.split(",") if entry.strip().strip("."))
    return DEFAULT_INTEGRATION_SITES + extra


def _host_listed(host: str, sites: Sequence[str]) -> bool:
    for entry in sites:
        if entry.startswith("."):
            if host.endswith(entry) and len(host) > len(entry):
                return True
        elif host == entry:
            return True
    return False


def integration_site_allowed(site: str, sites: Sequence[str] = DEFAULT_INTEGRATION_SITES) -> bool:
    """Whether an integration may be called at ``site``.

    ``https`` on a host ``sites`` lists (see :func:`integration_sites`), with
    no user name or password in the URL, or plain ``http`` on loopback for a
    test double.
    """
    parsed = urllib.parse.urlsplit(site)
    if parsed.scheme == "https":
        if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
            return False
        host = (parsed.hostname or "").lower().rstrip(".")
        return bool(host) and _host_listed(host, sites)
    return is_loopback_http_url(site)


#: Why a ``site`` is refused, for every message that names the key.
SITE_RULE = (
    "must be an https URL on an Atlassian Cloud host (*.atlassian.net) or on a host the operator lists in "
    f"{INTEGRATION_SITES_ENV} (plain http only on a loopback host)"
)


def env_prefix(slug: str) -> str:
    """The prefix of the credential variables a vault may name: its slug in upper snake case."""
    return slug.upper().replace("-", "_")


def integration_binding_problems(
    integrations: Mapping[str, Any], slug: str, *, is_default: bool
) -> list[tuple[str, str]]:
    """``(key path, why)`` for every credential variable a vault names that belongs to another vault.

    A vault names ``<PREFIX>_<WORD>_..._<SUFFIX>``, where ``<PREFIX>`` is its
    own slug in upper snake case (``ACME_JIRA_TOKEN`` for
    ``acme``); only the default vault may name the bare
    ``<WORD>_..._<SUFFIX>`` (``JIRA_API_TOKEN``). A vault that is not the
    default and declares a ``site`` must name both variables, so it never
    sends the operator's ``CONFLUENCE_*`` credentials to a site of its own.
    One vault's content can therefore never send another vault's
    credentials anywhere.
    """
    prefix = env_prefix(slug)
    problems: list[tuple[str, str]] = []
    for integration, word in INTEGRATION_ENV_WORDS.items():
        block = integrations.get(integration)
        if not isinstance(block, Mapping):
            continue
        where = f"integrations.{integration}.secrets"
        secrets = block.get("secrets")
        named: dict[str, str] = {}
        for key, suffix in INTEGRATION_SECRET_SUFFIXES.items():
            name = secrets.get(key) if isinstance(secrets, Mapping) else None
            if not (isinstance(name, str) and name.strip()):
                continue
            named[key] = name.strip()
            pattern = rf"(?:(?P<prefix>{re.escape(prefix)})_)?{word}_(?:[A-Z0-9]+_)*{suffix}"
            shape = re.fullmatch(pattern, named[key])
            if shape is None or (shape.group("prefix") is None and not is_default):
                hint = f"{prefix}_{word}_{suffix}" + ("" if not is_default else f" or {word}_{suffix}")
                problems.append((
                    f"{where}.{key}",
                    f"must name a variable of this vault, such as {hint}; "
                    "a vault never names another vault's credentials",
                ))
        site = block.get("site")
        declares_site = isinstance(site, str) and bool(site.strip())
        if not is_default and declares_site and len(named) < len(INTEGRATION_SECRET_SUFFIXES):
            problems.append((
                where,
                f"must name both email_env and token_env with the prefix {prefix}_: a vault that is not "
                f"the default never sends the operator's {word}_* credentials to a site it declares",
            ))
    return problems


#: The values ``kind`` takes in a memory task (``httpTaskKindEnum`` of the
#: memory server); a ``kind_map`` of ``integrations.jira`` maps to them.
MEMORY_TASK_KINDS: frozenset[str] = frozenset({"feature", "bugfix", "refactor", "incident", "migration", "spike"})
#: The values ``state`` takes in a memory task (``httpTaskStateEnum``); a
#: ``status_map`` of ``integrations.jira`` maps to them.
MEMORY_TASK_STATES: frozenset[str] = frozenset(
    {"open", "analysis", "in_progress", "review", "verified", "done", "blocked", "cancelled"}
)
#: A Jira project key, as ``integrations.jira.projects`` lists it.
JIRA_PROJECT_PATTERN = re.compile(r"^[A-Z][A-Z0-9]+$")


def jira_content_problems(block: Mapping[str, Any]) -> list[tuple[str, str]]:
    """``(key path, why)`` for the ``integrations.jira`` keys beyond ``site`` and ``secrets`` names.

    Keys it does not know (``boards``, ``groups``) are left alone, as the
    rest of ``vault.yaml`` is read.
    """
    where = "integrations.jira"
    problems: list[tuple[str, str]] = []
    projects = block.get("projects")
    if projects is not None and (
        not isinstance(projects, Sequence)
        or isinstance(projects, str)
        or not projects
        or not all(isinstance(key, str) and JIRA_PROJECT_PATTERN.fullmatch(key) for key in projects)
    ):
        problems.append((f"{where}.projects", "must be a non-empty list of Jira project keys such as PROJ"))
    jql_extra = block.get("jql_extra")
    if jql_extra is not None and not isinstance(jql_extra, str):
        problems.append((f"{where}.jql_extra", "must be a JQL condition in a string"))
    for key, allowed in (("kind_map", MEMORY_TASK_KINDS), ("status_map", MEMORY_TASK_STATES)):
        mapping = block.get(key)
        if mapping is None:
            continue
        if not isinstance(mapping, Mapping) or not all(
            isinstance(name, str) and isinstance(value, str) and value in allowed for name, value in mapping.items()
        ):
            problems.append((f"{where}.{key}", f"must map names to one of {', '.join(sorted(allowed))}"))
    sync = block.get("sync")
    if sync is not None:
        minutes = sync.get("throttle_minutes") if isinstance(sync, Mapping) else None
        if not isinstance(sync, Mapping) or (
            minutes is not None and (isinstance(minutes, bool) or not isinstance(minutes, int) or minutes < 0)
        ):
            problems.append((f"{where}.sync.throttle_minutes", "must be a whole number of minutes, 0 or more"))
    target = block.get("target")
    if target is not None and target != "memory":
        problems.append((f"{where}.target", "must be memory, the only target of the jira extension"))
    return problems


def integration_access_problems(
    integrations: Mapping[str, Any], env: Mapping[str, str] | None = None
) -> list[tuple[str, str]]:
    """``(key path, why)`` for every site or credential variable an ``integrations`` block may not declare.

    Only the integrations of :data:`INTEGRATION_ENV_WORDS` are checked. For
    every one, ``site`` and ``secrets.{email_env,token_env}``: the keys that
    decide where a credential is sent and which variable it is read from.
    ``site`` must be on a host :func:`integration_sites` lists for ``env``.
    Which keys a sync needs (``site``, ``projects``, both secret names) is
    checked when it runs, since the memory-bridge card reads ``projects``
    alone; which vault a variable belongs to is
    :func:`integration_binding_problems`.
    """
    sites = integration_sites(env)
    problems: list[tuple[str, str]] = []
    for integration, word in INTEGRATION_ENV_WORDS.items():
        block = integrations.get(integration)
        if block is None:
            continue
        where = f"integrations.{integration}"
        if not isinstance(block, Mapping):
            problems.append((where, "must be a mapping"))
            continue
        site = block.get("site")
        if site is not None and not (isinstance(site, str) and integration_site_allowed(site.strip(), sites)):
            problems.append((f"{where}.site", SITE_RULE))
        secrets = block.get("secrets")
        if secrets is None:
            continue
        if not isinstance(secrets, Mapping):
            problems.append((f"{where}.secrets", "must be a mapping"))
            continue
        for key, suffix in INTEGRATION_SECRET_SUFFIXES.items():
            name = secrets.get(key)
            if name is None:
                continue
            if not (isinstance(name, str) and integration_env_allowed(integration, key, name.strip())):
                problems.append((
                    f"{where}.secrets.{key}",
                    f"must name a {word} variable in upper snake case ending in _{suffix}, "
                    f"such as {word}_{suffix} or <PREFIX>_{word}_{suffix}",
                ))
    return problems


def _serves_http(env: Mapping[str, str]) -> bool:
    return (env.get("MCP_TRANSPORT") or "").strip().lower() == "http"


def _ignored_over_http(slug: str, extension: str, where: str, why: str) -> str:
    return (
        f"{errors.W_EXTENSION_UNAVAILABLE_HTTP}: {slug}: {where} {why}; the {extension} extension is not "
        "loaded over HTTP, so the key is ignored here and refused over stdio"
    )


def _manifest_integrations(
    manifest: Mapping[str, Any], vault_dir: Path | None, env: Mapping[str, str], slug: str
) -> tuple[Mapping[str, Any], list[str]]:
    """The ``integrations`` block of a ``vault.yaml``, frozen, and the warnings it raised.

    It names the environment variables that carry each integration's
    credentials, never the credentials: a value that looks like one is
    refused with ``E_SECRET_IN_MANIFEST``. A ``site`` off the allowed hosts
    or a credential variable outside the integration's own names (see
    :func:`integration_access_problems`) is ``E_MANIFEST_INVALID``, naming the
    key path, whatever the transport. The keys only the jira sync reads
    (:func:`jira_content_problems`) are ``E_MANIFEST_INVALID`` over stdio and
    a ``W_EXTENSION_UNAVAILABLE_HTTP`` warning over HTTP, which never loads
    the jira extension.
    """
    declared = manifest.get("integrations")
    if not isinstance(declared, dict):
        return MappingProxyType({}), []
    source = (vault_dir / VAULT_MANIFEST_NAME) if vault_dir is not None else Path(VAULT_MANIFEST_NAME)
    _scan_secrets(declared, "integrations", source)
    problems = integration_access_problems(declared, env)
    if problems:
        where, why = problems[0]
        raise _manifest_error(source, where, why)
    warnings: list[str] = []
    jira = declared.get("jira")
    content = jira_content_problems(jira) if isinstance(jira, Mapping) else []
    if content:
        if not _serves_http(env):
            where, why = content[0]
            raise _manifest_error(source, where, why)
        warnings.extend(_ignored_over_http(slug, "jira", where, why) for where, why in content)
    frozen: Mapping[str, Any] = _frozen(declared)
    return frozen, warnings


def _manifest_memory_hub(
    manifest: Mapping[str, Any], vault_dir: Path | None, env: Mapping[str, str], slug: str
) -> tuple[str | None, list[str]]:
    """``memory.hub`` of a ``vault.yaml`` (a vault-relative POSIX path, or ``None``) and the warnings it raised.

    The memory server stores it as a pointer into the vault, so an absolute
    path, a backslash or a ``..`` segment is ``E_MANIFEST_INVALID`` over
    stdio. Over HTTP, which never loads the memory-bridge, it is a
    ``W_EXTENSION_UNAVAILABLE_HTTP`` warning and the hub is dropped.
    """
    declared = _sub(manifest, "memory").get("hub")
    if declared is None:
        return None, []
    source = (vault_dir / VAULT_MANIFEST_NAME) if vault_dir is not None else Path(VAULT_MANIFEST_NAME)
    hub = declared.strip() if isinstance(declared, str) else ""
    parts = hub.split("/")
    if not hub or hub.startswith("/") or "\\" in hub or ".." in parts or "" in parts or ":" in parts[0]:
        why = "must be a path relative to the vault, such as Hub.md"
        if _serves_http(env):
            return None, [_ignored_over_http(slug, "memory-bridge", "memory.hub", why)]
        raise _manifest_error(source, "memory.hub", why)
    return hub, []


class _RerankInvalid(Exception):
    """A key of the ``rerank`` block that is malformed: the vault goes without a judge."""

    def __init__(self, where: str, why: str) -> None:
        super().__init__(f"{where} {why}")
        self.where = where
        self.why = why


_RERANK_KEYS = frozenset({
    "provider",
    "default",
    "model",
    "key_env",
    "candidates",
    "units_per_doc",
    "timeout_ms",
    "max_input_tokens",
    "max_usd_per_day",
    "conditional_gap",
    "never_send",
    "base_url",
})
_JEV_MODEL = re.compile(r"^jev-\d+\.\d+\.\d+$")
_KEV_MODEL = re.compile(r"^kev-(?:latest|\d+\.\d+\.\d+)$")
_UPPER_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _rerank_key_words(provider: str) -> tuple[str, ...]:
    """The word a credential variable of ``provider`` carries; every provider's word for an unknown one."""
    word = RERANK_KEY_WORDS.get(provider)
    return (word,) if word is not None else tuple(RERANK_KEY_WORDS.values())


def rerank_env_allowed(provider: str, name: str) -> bool:
    """Whether ``rerank.key_env`` may name the variable ``name`` for ``provider``.

    The name is upper snake case and ends in the provider's word and
    ``_API_KEY``: ``TYPESAFE_API_KEY`` or ``ACME_TYPESAFE_API_KEY`` for
    ``jev``, ``KEV_API_KEY`` or ``ACME_KEV_API_KEY`` for ``kev``. Anything
    else — ``OPENAI_API_KEY``, ``MCP_ADMIN_KEY``, a bearer's ``token_env`` —
    is refused: a vault is curated content, and the key it names is sent to
    the judge's host.
    """
    words = "|".join(_rerank_key_words(provider))
    return re.fullmatch(rf"(?:[A-Z0-9]+_)*(?:{words})_{RERANK_KEY_SUFFIX}", name) is not None


def rerank_binding_problems(rerank: RerankSettings, slug: str, *, is_default: bool) -> list[tuple[str, str]]:
    """``(key path, why)`` when ``rerank.key_env`` names a variable of another vault.

    A vault names ``<PREFIX>_<WORD>_API_KEY``, where ``<PREFIX>`` is its own
    slug in upper snake case; only the default vault may name the bare
    ``<WORD>_API_KEY``. One vault's content can therefore never send another
    vault's key to its judge.
    """
    if not rerank.key_env:
        return []
    prefix = env_prefix(slug)
    words = _rerank_key_words(rerank.provider)
    pattern = rf"(?:(?P<prefix>{re.escape(prefix)})_)?(?:{'|'.join(words)})_{RERANK_KEY_SUFFIX}"
    shape = re.fullmatch(pattern, rerank.key_env)
    if shape is not None and (shape.group("prefix") is not None or is_default):
        return []
    hints = [f"{prefix}_{word}_{RERANK_KEY_SUFFIX}" for word in words]
    if is_default:
        hints += [f"{word}_{RERANK_KEY_SUFFIX}" for word in words]
    return [(
        "rerank.key_env",
        f"must name a variable of this vault, such as {' or '.join(hints)}; "
        "a vault never names another vault's credentials",
    )]


def _origin_only(url: str) -> bool:
    """Whether ``url`` is a scheme and a host (and port) only: no user name, path, query or fragment."""
    parsed = urllib.parse.urlsplit(url)
    return (
        parsed.username is None
        and parsed.password is None
        and "@" not in parsed.netloc
        and parsed.path in ("", "/")
        and not parsed.query
        and not parsed.fragment
    )


def _rerank_provider_hint(mapping: Mapping[str, Any]) -> str:
    """The provider the block names when it is one the server knows; ``""`` otherwise."""
    value = mapping.get("provider")
    return value if isinstance(value, str) and value in RERANK_REMOTE_PROVIDERS else ""


def _rerank_key_env(mapping: Mapping[str, Any], provider: str, source: Path) -> str:
    """``rerank.key_env`` when it is a variable the vault may name; ``""`` when absent or not a string.

    A name outside the allowed shape stops the server whatever else is wrong
    with the block: the key it points at would travel to the judge's host.
    """
    value = mapping.get("key_env")
    if not isinstance(value, str) or not value.strip():
        return ""
    name = value.strip()
    if not _UPPER_ENV_NAME.fullmatch(name) or not rerank_env_allowed(provider, name):
        names = " or ".join(
            f"{word}_{RERANK_KEY_SUFFIX} or <PREFIX>_{word}_{RERANK_KEY_SUFFIX}" for word in _rerank_key_words(provider)
        )
        raise _manifest_error(
            source,
            "rerank.key_env",
            f"must name a variable in upper snake case such as {names}; "
            "a vault never names a server secret or another service's key",
        )
    return name


def _rerank_base_url(mapping: Mapping[str, Any], provider: str, source: Path) -> str | None:
    """``rerank.base_url`` when it is a loopback server for ``kev``; ``None`` when absent or not a string.

    ``jev`` always calls the fixed TypeSafe URL, and ``kev`` a server on this
    machine, so a vault can never aim a key at a host of its choosing. Both a
    ``base_url`` for ``jev`` and a ``kev`` URL off the loopback interface stop
    the server.
    """
    value = mapping.get("base_url")
    if not isinstance(value, str) or not value.strip() or not provider:
        return None
    if provider != "kev":
        raise _manifest_error(
            source, "rerank.base_url", "is only for the kev provider; jev always calls its fixed URL"
        )
    url = value.strip().rstrip("/")
    if not is_loopback_http_url(url) or not _origin_only(url):
        raise _manifest_error(
            source,
            "rerank.base_url",
            "must be http://127.0.0.1:<port>, http://localhost:<port> or http://[::1]:<port>: "
            "the kev server runs on this machine",
        )
    return url


def _rerank_number(
    mapping: Mapping[str, Any], key: str, default: int, bounds: tuple[int, int]
) -> int:
    value = mapping.get(key)
    if value is None:
        return default
    low, high = bounds
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise _RerankInvalid(f"rerank.{key}", f"must be a whole number from {low} to {high}")
    return value


def _rerank_values(
    mapping: Mapping[str, Any], env: Mapping[str, str], key_env: str, base_url: str | None
) -> RerankSettings:
    """The ``rerank`` block as settings; raises :class:`_RerankInvalid` on the first malformed key."""
    unknown = sorted(str(k) for k in mapping if k not in _RERANK_KEYS)
    if unknown:
        raise _RerankInvalid(f"rerank.{unknown[0]}", f"is not a known key; allowed: {', '.join(sorted(_RERANK_KEYS))}")
    provider = mapping.get("provider")
    if provider is None:
        provider = "none"
    if not isinstance(provider, str) or provider not in RERANK_PROVIDERS:
        raise _RerankInvalid("rerank.provider", f"must be one of {', '.join(sorted(RERANK_PROVIDERS))}")
    if provider == "none":
        return RerankSettings(key_env=key_env)

    # YAML 1.1 reads a bare `on` and `off` as booleans, so both spellings count.
    declared = mapping.get("default", False)
    if isinstance(declared, str) and declared in ("on", "off"):
        declared = declared == "on"
    if not isinstance(declared, bool):
        raise _RerankInvalid("rerank.default", "must be on or off")

    model = mapping.get("model")
    if model is None and provider == "kev":
        model = "kev-latest"
    if provider == "jev":
        if not isinstance(model, str) or not model.strip():
            raise _RerankInvalid("rerank.model", "is required with the jev provider, such as jev-1.13.0")
        if not _JEV_MODEL.fullmatch(model.strip()):
            alias = " is an alias that changes without notice;" if model.strip() == "jev-latest" else ""
            raise _RerankInvalid("rerank.model", f"{alias} must be a pinned version such as jev-1.13.0".strip())
    elif not isinstance(model, str) or not _KEV_MODEL.fullmatch(model.strip()):
        raise _RerankInvalid("rerank.model", "must be kev-latest or a version such as kev-1.0.0")
    model = model.strip()

    if not key_env:
        raise _RerankInvalid(
            "rerank.key_env", f"is required with the {provider} provider: the NAME of the variable the key comes from"
        )

    timeout_bounds = RERANK_TIMEOUT_MS_LOCAL if provider == "kev" else RERANK_TIMEOUT_MS
    max_usd = mapping.get("max_usd_per_day", 1.0)
    if isinstance(max_usd, bool) or not isinstance(max_usd, int | float) or not math.isfinite(max_usd) or max_usd < 0:
        raise _RerankInvalid("rerank.max_usd_per_day", "must be a number of US dollars, 0 or more (0 for no cap)")
    gap = mapping.get("conditional_gap")
    if gap is not None and (
        isinstance(gap, bool) or not isinstance(gap, int | float) or not math.isfinite(gap) or not 0 < gap <= 1
    ):
        raise _RerankInvalid("rerank.conditional_gap", "must be null or a number above 0 and up to 1")
    never_send = mapping.get("never_send")
    prefixes: list[str] = []
    if never_send is not None:
        if not isinstance(never_send, list):
            raise _RerankInvalid("rerank.never_send", "must be a list of vault-relative path prefixes")
        for entry in never_send:
            prefix = entry.strip() if isinstance(entry, str) else ""
            parts = prefix.split("/")
            if not prefix or prefix.startswith("/") or "\\" in prefix or ".." in parts:
                raise _RerankInvalid(
                    "rerank.never_send", "must list non-empty paths relative to the vault, such as Personal/"
                )
            if prefix not in prefixes:
                prefixes.append(prefix)

    return RerankSettings(
        provider=provider,
        default_on=declared,
        model=model,
        key_env=key_env,
        api_key=_non_empty(env, key_env),
        candidates=_rerank_number(mapping, "candidates", 10, RERANK_CANDIDATES),
        units_per_doc=_rerank_number(mapping, "units_per_doc", 2, RERANK_UNITS_PER_DOC),
        timeout_ms=_rerank_number(mapping, "timeout_ms", 1500, timeout_bounds),
        max_input_tokens=_rerank_number(mapping, "max_input_tokens", 20_000, RERANK_MAX_INPUT_TOKENS),
        max_usd_per_day=float(max_usd),
        conditional_gap=None if gap is None else float(gap),
        never_send=tuple(prefixes),
        base_url=base_url,
    )


def _manifest_rerank(
    manifest: Mapping[str, Any], vault_dir: Path | None, env: Mapping[str, str], slug: str
) -> tuple[RerankSettings, list[str]]:
    """The ``rerank`` block of a ``vault.yaml`` as settings, and the warnings it raised.

    The block names the judge and the environment variable its key comes from,
    never the key: a value that looks like a credential is refused with
    ``E_SECRET_IN_MANIFEST``. A ``key_env`` outside the provider's own names or
    a ``base_url`` the key could leak through is ``E_MANIFEST_INVALID``,
    whatever the transport (the prefix of the variable is checked by
    :func:`rerank_binding_problems` once the default vault is known). Every
    other mistake — an unknown provider, an alias model, a number out of
    range — leaves only this vault without a judge, with a
    ``W_RERANK_INVALID`` warning: a typo in one vault's file must not stop a
    process that serves four.
    """
    declared = manifest.get("rerank")
    if declared is None:
        return RerankSettings(), []
    source = (vault_dir / VAULT_MANIFEST_NAME) if vault_dir is not None else Path(VAULT_MANIFEST_NAME)
    _scan_secrets(declared, "rerank", source)
    mapping: Mapping[str, Any] = declared if isinstance(declared, dict) else {}
    provider = _rerank_provider_hint(mapping)
    key_env = _rerank_key_env(mapping, provider, source)
    base_url = _rerank_base_url(mapping, provider, source)
    try:
        if not isinstance(declared, dict):
            raise _RerankInvalid("rerank", "must be a mapping")
        return _rerank_values(mapping, env, key_env, base_url), []
    except _RerankInvalid as exc:
        return RerankSettings(key_env=key_env), [
            f"{errors.W_RERANK_INVALID}: {slug}: {exc.where} {exc.why}; this vault searches without a judge"
        ]


def _rerank_operator(env: Mapping[str, str]) -> tuple[RerankOperator, list[str]]:
    """The operator's ``KNOWLEDGE_RERANK*`` variables, and the warnings a malformed one raised.

    A switch that is not exactly ``off`` still switches rerank off: a typo in
    the variable meant to stop content from leaving the machine must not leave
    it running. The same goes for a providers list with nothing the server
    knows. A URL or timeout it cannot use is ignored.
    """
    warnings: list[str] = []
    raw_switch = _non_empty(env, RERANK_SWITCH_ENV).lower()
    off = bool(raw_switch)
    if raw_switch and raw_switch != "off":
        warnings.append(
            f"{errors.W_RERANK_INVALID}: {RERANK_SWITCH_ENV} must be off or empty; rerank stays off in this process"
        )

    providers = frozenset(RERANK_REMOTE_PROVIDERS)
    raw_providers = _non_empty(env, RERANK_PROVIDERS_ENV)
    if raw_providers:
        named = [name.strip().lower() for name in raw_providers.split(",") if name.strip()]
        providers = frozenset(name for name in named if name in RERANK_REMOTE_PROVIDERS)
        if len(providers) < len(set(named)):
            warnings.append(
                f"{errors.W_RERANK_INVALID}: {RERANK_PROVIDERS_ENV} lists a provider this server does not "
                f"know; allowed: {', '.join(RERANK_REMOTE_PROVIDERS)}"
            )

    base_url: str | None = None
    raw_url = _non_empty(env, RERANK_BASE_URL_ENV).rstrip("/")
    if raw_url:
        if (is_loopback_http_url(raw_url) or raw_url == TYPESAFE_ORIGIN) and _origin_only(raw_url):
            base_url = raw_url
        else:
            warnings.append(
                f"{errors.W_RERANK_INVALID}: {RERANK_BASE_URL_ENV} must be {TYPESAFE_ORIGIN} or an http URL "
                "on a loopback host; it is ignored"
            )

    timeout_ms: int | None = None
    raw_timeout = _non_empty(env, RERANK_TIMEOUT_ENV)
    if raw_timeout:
        low, high = RERANK_TIMEOUT_MS_LOCAL
        if raw_timeout.isdigit() and low <= int(raw_timeout) <= high:
            timeout_ms = int(raw_timeout)
        else:
            warnings.append(
                f"{errors.W_RERANK_INVALID}: {RERANK_TIMEOUT_ENV} must be a whole number of milliseconds "
                f"from {low} to {high}; it is ignored"
            )
    return RerankOperator(off=off, providers=providers, base_url=base_url, timeout_ms=timeout_ms), warnings


def _check_binding(spec: VaultSpec) -> None:
    """Refuse a vault whose ``integrations`` or ``rerank`` name another vault's credential variables.

    The error is ``E_MANIFEST_INVALID``, naming the key path.
    """
    problems = integration_binding_problems(spec.integrations, spec.slug, is_default=spec.is_default)
    problems += rerank_binding_problems(spec.rerank, spec.slug, is_default=spec.is_default)
    if problems:
        where, why = problems[0]
        source = (spec.path / VAULT_MANIFEST_NAME) if spec.path is not None else Path(VAULT_MANIFEST_NAME)
        raise _manifest_error(source, where, why)


def _version(raw: str) -> tuple[int, ...]:
    parts = tuple(int(p) for p in raw.split("."))
    return parts + (0,) * (3 - len(parts))


def contract_satisfied(declared: str, version: Sequence[int] = TOOL_CONTRACT_VERSION) -> bool | None:
    """Whether ``version`` falls inside a ``knowledge_contract`` range such as ``>=1.0,<3``.

    ``None`` when the range cannot be parsed.
    """
    current = _version(".".join(str(v) for v in version))
    clauses = [c.strip() for c in declared.split(",") if c.strip()]
    if not clauses:
        return None
    for clause in clauses:
        match = _CONTRACT_CLAUSE.fullmatch(clause)
        if match is None:
            return None
        op, bound = match.group(1) or "==", _version(match.group(2))
        ok = {
            ">=": current >= bound,
            "<=": current <= bound,
            ">": current > bound,
            "<": current < bound,
            "==": current == bound,
            "!=": current != bound,
        }[op]
        if not ok:
            return False
    return True


def _contract_warning(slug: str, manifest: Mapping[str, Any]) -> str | None:
    declared = manifest.get("knowledge_contract")
    if declared is None:
        return None
    served = ".".join(str(v) for v in TOOL_CONTRACT_VERSION)
    verdict = contract_satisfied(str(declared))
    if verdict is None:
        return (
            f"{errors.W_CONTRACT_RANGE}: {slug}: knowledge_contract in {VAULT_MANIFEST_NAME} "
            "is not a version range such as '>=1.0,<3'"
        )
    if not verdict:
        return (
            f"{errors.W_CONTRACT_RANGE}: {slug}: {VAULT_MANIFEST_NAME} asks for tool contract "
            f"{declared}; this server serves {served}"
        )
    return None


def _embedding_settings(
    env: Mapping[str, str], layers: Sequence[Mapping[str, Any]], fallback_model: str
) -> EmbeddingSettings:
    """env > each layer in order > built-in default, field by field."""

    def pick(field_name: str) -> str | None:
        for layer in layers:
            value = _text(layer.get(field_name))
            if value is not None:
                return value
        return None

    provider = (_non_empty(env, "KNOWLEDGE_EMBEDDINGS_PROVIDER") or pick("provider") or "openai").lower()
    model = _present(env, "EMBEDDING_MODEL", pick("model") or fallback_model)
    key_env = pick("key_env") or "OPENAI_API_KEY"
    return EmbeddingSettings(provider=provider, model=model, key_env=key_env, api_key=env.get(key_env, ""))


def _parse_entry(
    raw: Any, index: int, source: Path, base_dir: Path, home: Path, env: Mapping[str, str]
) -> tuple[VaultSpec, bool | None, list[str]]:
    where = f"vaults[{index}]"
    if not isinstance(raw, dict):
        raise _manifest_error(source, where, "must be a mapping")
    _expect_keys(raw, _ENTRY_KEYS, where, source)

    slug = raw.get("slug")
    if not isinstance(slug, str) or not SLUG_PATTERN.fullmatch(slug):
        raise errors.KnowledgeConfigError(
            errors.E_VAULT_SLUG_INVALID,
            f"{source}: {where}.slug must match {SLUG_PATTERN.pattern} "
            "(lowercase letters, digits and inner hyphens, 3 to 32 characters)",
        )
    raw_path = _opt_str(raw, "path", where, source)
    if raw_path is None:
        raise _manifest_error(source, f"{where}.path", "is required")
    vault_path = _anchor(raw_path, base_dir)
    is_default = _opt_bool(raw, "default", where, source)

    repo = _opt_str(raw, "repo", where, source)
    deploy_key_env = _opt_env_name(raw, "deploy_key_env", where, source)
    git = _opt_mapping(raw, "git", where, source)
    webhook = _opt_mapping(raw, "webhook", where, source)
    for needs_repo, value in (("deploy_key_env", deploy_key_env), ("git", git), ("webhook", webhook)):
        if value is not None and repo is None:
            raise _manifest_error(source, f"{where}.{needs_repo}", "requires repo")
    git = git or {}
    _expect_keys(git, _GIT_KEYS, f"{where}.git", source)
    webhook = webhook or {}
    _expect_keys(webhook, _WEBHOOK_KEYS, f"{where}.webhook", source)

    features_raw = raw.get("features")
    if features_raw is None:
        features_raw = []
    if not isinstance(features_raw, list) or not all(isinstance(f, str) for f in features_raw):
        raise _manifest_error(source, f"{where}.features", "must be a list of feature names")
    unknown = sorted(set(features_raw) - VAULT_FEATURES)
    if unknown:
        known = ", ".join(sorted(VAULT_FEATURES))
        raise _manifest_error(source, f"{where}.features", f"names unknown feature {unknown[0]!r}; known: {known}")

    memory = _opt_mapping(raw, "memory", where, source) or {}
    _expect_keys(memory, _MEMORY_KEYS, f"{where}.memory", source)
    embeddings = _opt_mapping(raw, "embeddings", where, source) or {}
    _expect_keys(embeddings, _EMBEDDINGS_KEYS, f"{where}.embeddings", source)
    provider = _opt_str(embeddings, "provider", f"{where}.embeddings", source)
    if provider is not None and provider not in EMBEDDING_PROVIDERS:
        raise _manifest_error(
            source, f"{where}.embeddings.provider", f"must be one of {', '.join(sorted(EMBEDDING_PROVIDERS))}"
        )
    _opt_str(embeddings, "model", f"{where}.embeddings", source)
    _opt_env_name(embeddings, "key_env", f"{where}.embeddings", source)

    warnings: list[str] = []
    if "git-webhook" in features_raw and repo is None:
        warnings.append(
            f"{errors.W_FEATURE_WITHOUT_REPO}: {slug}: the git-webhook feature needs repo; "
            "no push will ever reach this vault"
        )

    manifest = read_vault_manifest(vault_path if vault_path.is_dir() else None)
    declared_slug = _text(manifest.get("slug"))
    if declared_slug is not None and declared_slug != slug:
        raise errors.KnowledgeConfigError(
            errors.E_VAULT_SLUG_MISMATCH,
            f"{source}: {where} registers {slug!r} but {vault_path / VAULT_MANIFEST_NAME} "
            f"declares {declared_slug!r}; make them agree",
        )
    contract = _contract_warning(slug, manifest)
    if contract is not None:
        warnings.append(contract)

    thresholds = _manifest_thresholds(manifest)
    manifest_memory = _sub(manifest, "memory")
    integrations, integration_warnings = _manifest_integrations(manifest, vault_path, env, slug)
    memory_hub, hub_warnings = _manifest_memory_hub(manifest, vault_path, env, slug)
    rerank, rerank_warnings = _manifest_rerank(manifest, vault_path, env, slug)
    warnings.extend(integration_warnings + hub_warnings + rerank_warnings)
    spec = VaultSpec(
        slug=slug,
        path=vault_path,
        is_default=bool(is_default),
        index_dir=home / slug,
        chroma_dir=home / slug / "chroma",
        memory_folder=_text(manifest_memory.get("export_folder")) or _DEFAULT_MEMORY_FOLDER,
        review_thresholds=MappingProxyType(
            thresholds if thresholds is not None else dict(_FALLBACK_REVIEW_THRESHOLDS)
        ),
        thresholds_from_config=thresholds is not None,
        embeddings=_embedding_settings(env, (embeddings, _sub(manifest, "embedding")), _DEFAULT_EMBEDDING_MODEL),
        origin="vaults_yaml",
        repo=repo,
        git_auto_pull=_opt_bool(git, "auto_pull", f"{where}.git", source) is not False,
        git_branch=_opt_str(git, "branch", f"{where}.git", source) or "main",
        deploy_key_env=deploy_key_env,
        webhook_secret_env=_opt_env_name(webhook, "secret_env", f"{where}.webhook", source),
        features=frozenset(features_raw) or _manifest_features(manifest),
        memory_project=_opt_str(memory, "project", f"{where}.memory", source)
        or _text(manifest_memory.get("project")),
        name=_text(manifest.get("name")),
        integrations=integrations,
        memory_hub=memory_hub,
        rerank=rerank,
        index_exclude=index_exclude_patterns(manifest),
    )
    return spec, is_default, warnings


def _parse_bearers(raw: Any, source: Path, env: Mapping[str, str]) -> tuple[BearerSpec, ...]:
    """The ``bearers`` list of a ``vaults.yaml``; empty when the key is absent.

    Each entry is ``{id, token_env, scopes}``: ``scopes`` is a subset of
    ``read`` and ``admin`` (``read`` when omitted), and ``admin`` requires
    ``read``. The token is read from ``token_env`` here; whether it is set is
    for :meth:`Settings.validate` to decide, since only HTTP needs it.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list) or not raw:
        raise _manifest_error(source, "bearers", "must be a non-empty list of {id, token_env, scopes}")
    out: list[BearerSpec] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        where = f"bearers[{index}]"
        if not isinstance(entry, dict):
            raise _manifest_error(source, where, "must be a mapping")
        _expect_keys(entry, _BEARER_KEYS, where, source)
        bearer_id = entry.get("id")
        if not isinstance(bearer_id, str) or not BEARER_ID_PATTERN.fullmatch(bearer_id):
            raise _manifest_error(source, f"{where}.id", f"must match {BEARER_ID_PATTERN.pattern}")
        if bearer_id in seen:
            raise _manifest_error(source, f"{where}.id", f"{bearer_id!r} is already declared; ids are unique")
        seen.add(bearer_id)
        token_env = _opt_env_name(entry, "token_env", where, source)
        if token_env is None:
            raise _manifest_error(source, f"{where}.token_env", "is required")
        scopes_raw = entry.get("scopes", [SCOPE_READ])
        if not isinstance(scopes_raw, list) or not scopes_raw or not all(isinstance(x, str) for x in scopes_raw):
            raise _manifest_error(source, f"{where}.scopes", "must be a non-empty list of scope names")
        scopes = frozenset(scopes_raw)
        unknown = sorted(scopes - BEARER_SCOPES)
        if unknown:
            known = ", ".join(sorted(BEARER_SCOPES))
            raise _manifest_error(source, f"{where}.scopes", f"names unknown scope {unknown[0]!r}; known: {known}")
        if SCOPE_ADMIN in scopes and SCOPE_READ not in scopes:
            raise _manifest_error(source, f"{where}.scopes", "must include read when it includes admin")
        out.append(BearerSpec(id=bearer_id, token_env=token_env, scopes=scopes, token=_non_empty(env, token_env)))
    return tuple(out)


def _parse_vaults_document(path: Path, env: Mapping[str, str]) -> _VaultsDocument:
    if not path.is_file():
        raise errors.KnowledgeConfigError(
            errors.E_VAULTS_FILE_MISSING,
            f"{path} does not exist; point KNOWLEDGE_VAULTS at an existing {VAULTS_FILE_NAME}",
        )
    doc = _load_yaml(path)
    if not isinstance(doc, dict):
        raise _manifest_error(path, "the document", "must be a mapping with a 'vaults' list")
    _scan_secrets(doc, "", path)
    _expect_keys(doc, _TOP_KEYS, "(top level)", path)
    version = doc.get("version", 1)
    if version != 1 or isinstance(version, bool):
        raise _manifest_error(path, "version", "must be 1, the only schema this server reads")

    base_dir = path.parent
    data_dir = _opt_str(doc, "data_dir", "(top level)", path)
    home, _ = _knowledge_home(env, _anchor(data_dir, base_dir) if data_dir else None)

    entries = doc.get("vaults")
    if not isinstance(entries, list) or not entries:
        raise _manifest_error(path, "vaults", "must be a non-empty list")

    parsed: list[tuple[VaultSpec, bool | None]] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(entries):
        spec, declared_default, entry_warnings = _parse_entry(raw, index, path, base_dir, home, env)
        if spec.slug in seen:
            raise errors.KnowledgeConfigError(
                errors.E_VAULT_SLUG_DUPLICATE,
                f"{path}: vaults[{index}].slug {spec.slug!r} is already registered; every slug must be unique",
            )
        seen.add(spec.slug)
        parsed.append((spec, declared_default))
        warnings.extend(entry_warnings)

    defaults = [spec.slug for spec, declared in parsed if declared]
    if len(defaults) > 1:
        raise errors.KnowledgeConfigError(
            errors.E_VAULT_DEFAULT_AMBIGUOUS,
            f"{path}: {', '.join(defaults)} are all marked default: true; keep exactly one",
        )
    if not defaults and len(parsed) > 1:
        raise errors.KnowledgeConfigError(
            errors.E_VAULT_DEFAULT_MISSING,
            f"{path}: {len(parsed)} vaults are registered and none is marked default: true; mark one",
        )
    default_slug = defaults[0] if defaults else parsed[0][0].slug
    specs = tuple(replace(spec, is_default=spec.slug == default_slug) for spec, _ in parsed)
    for spec in specs:
        _check_binding(spec)
    bearers = _parse_bearers(doc.get("bearers"), path, env)
    return _VaultsDocument(path=path, home=home, specs=specs, warnings=tuple(warnings), bearers=bearers)


def parse_vaults_file(
    path: Path, env: Mapping[str, str] | None = None, *, check_paths: bool = True
) -> tuple[list[VaultSpec], list[str]]:
    """Parse a ``vaults.yaml`` into its vault specs and the warnings it raised.

    Every schema problem raises :class:`~knowledge.errors.KnowledgeConfigError`
    with a stable code and the path of the offending key, never its value.
    With ``check_paths`` each vault directory and its ``vault.yaml`` must also
    exist; without it only the schema is checked (an example file, a manifest
    for another machine).
    """
    document = _parse_vaults_document(path, os.environ if env is None else env)
    if check_paths:
        for spec in document.specs:
            check_vault_dir(spec)
    return list(document.specs), list(document.warnings)


def _user_vaults_file(env: Mapping[str, str]) -> Path | None:
    """``$XDG_CONFIG_HOME/knowledge/vaults.yaml`` or ``$HOME/.config/knowledge/vaults.yaml``.

    Only the given environment is consulted, so a hermetic ``env`` never
    reaches the developer's own configuration.
    """
    xdg = _non_empty(env, "XDG_CONFIG_HOME")
    if xdg:
        base = Path(xdg).expanduser()
    else:
        home = _non_empty(env, "HOME")
        if not home:
            return None
        base = Path(home) / ".config"
    candidate = base / "knowledge" / VAULTS_FILE_NAME
    return candidate if candidate.is_file() else None


def _inside_git_checkout(path: Path | None) -> bool:
    """Whether ``path`` or one of its parents holds a ``.git`` entry.

    A filesystem probe only: no git command runs.
    """
    if path is None:
        return False
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return any((candidate / ".git").exists() for candidate in (resolved, *resolved.parents))


def load_settings(
    env: Mapping[str, str] | None = None,
    *,
    project_root: Path | None = None,
    dotenv: bool = True,
) -> Settings:
    """Build a :class:`Settings` snapshot from ``env`` and ``config.json``.

    With ``env=None`` the process environment is read, after loading
    ``<project_root>/.env`` without overriding anything already set (only when
    ``dotenv`` is true). Passing ``env`` makes the call hermetic: no ``.env``
    is read and ``os.environ`` is never consulted.
    """
    root = project_root if project_root is not None else PROJECT_ROOT
    if env is None:
        if dotenv:
            from dotenv import load_dotenv

            load_dotenv(root / ".env", override=False)
        env = dict(os.environ)

    explicit_json = _non_empty(env, "KNOWLEDGE_CONFIG_JSON")
    config_json_path = _anchor(explicit_json, root) if explicit_json else root / "config.json"
    cfg, loaded = load_config_json(config_json_path)

    # -- http ----------------------------------------------------------------
    raw_port = _present(env, "MCP_HTTP_PORT", "8000")
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise errors.KnowledgeConfigError(
            errors.E_HTTP_PORT_INVALID, "MCP_HTTP_PORT must be an integer port number"
        ) from exc
    http = HttpSettings(
        transport=_present(env, "MCP_TRANSPORT", "stdio"),
        host=_present(env, "MCP_HTTP_HOST", "0.0.0.0"),
        port=port,
        path=_present(env, "MCP_HTTP_PATH", "/mcp"),
        api_key=_present(env, "MCP_API_KEY", ""),
        webhook_secret=_present(env, "MCP_WEBHOOK_SECRET", ""),
        admin_key=_present(env, "MCP_ADMIN_KEY", ""),
    )

    # -- memory export -------------------------------------------------------
    memory_cfg = _section(cfg, "memory")
    export_types = tuple(memory_cfg.get("export_types", _DEFAULT_MEMORY_EXPORT_TYPES))

    # -- promotion -----------------------------------------------------------
    promotion_cfg = _section(memory_cfg, "promotion")
    promotion = PromotionSettings(
        from_config=bool(promotion_cfg),
        output_dir=_anchor(
            _present(env, "PROMOTION_OUTPUT_DIR", str(promotion_cfg.get("output_dir", "build/promotions"))),
            root,
        ),
        target_folders=MappingProxyType(
            dict(promotion_cfg.get("target_folders", _DEFAULT_PROMOTION_TARGET_FOLDERS))
        ),
        doc_status=str(promotion_cfg.get("doc_status", "draft")),
        default_owner=str(promotion_cfg.get("default_owner", "plat")),
        branch_prefix=str(promotion_cfg.get("branch_prefix", "feature/memory-promotion")),
    )

    # -- the vaults --------------------------------------------------------
    vaults_file, named = _discover_vaults_file(env, root, cfg)
    load_warnings: list[str] = []
    vaults: tuple[VaultSpec, ...]
    bearers: tuple[BearerSpec, ...] = ()
    if vaults_file is not None:
        document = _parse_vaults_document(vaults_file, env)
        home, vaults, bearers = document.home, document.specs, document.bearers
        load_warnings.extend(document.warnings)
        ignored = _non_empty(env, "VAULT_PATH")
        default_path = next(spec for spec in vaults if spec.is_default).path
        overridden = default_path is not None and _anchor(ignored, root).resolve() != default_path.resolve()
        if named and ignored and overridden:
            load_warnings.append(
                f"{errors.W_VAULT_PATH_IGNORED}: KNOWLEDGE_VAULTS is set, so VAULT_PATH is ignored; "
                "the default vault of the registry is served"
            )
    else:
        spec, compat_warnings = _compat_vault(env, root, cfg)
        home, _ = _knowledge_home(env)
        vaults = (spec,)
        load_warnings.extend(compat_warnings)
    default_spec = next((spec for spec in vaults if spec.is_default), vaults[0])
    rerank_operator, operator_warnings = _rerank_operator(env)
    load_warnings.extend(operator_warnings)

    confluence_cfg = _section(cfg, "confluence")
    return Settings(
        project_root=root,
        config_json_path=config_json_path,
        config_json_loaded=loaded,
        raw_config=MappingProxyType(cfg),
        knowledge_home=home,
        vaults=vaults,
        http=http,
        confluence=ConfluenceSettings(
            url=_present(env, "CONFLUENCE_URL", str(confluence_cfg.get("url", ""))),
            email=_present(env, "CONFLUENCE_EMAIL", ""),
            token=_present(env, "CONFLUENCE_TOKEN", ""),
        ),
        promotion=promotion,
        memory_export_types=export_types,
        embedding_model=default_spec.embeddings.model,
        vaults_file=vaults_file,
        load_warnings=tuple(load_warnings),
        memory=MemorySettings(
            url=_non_empty(env, "KNOWLEDGE_MEMORY_URL") or DEFAULT_MEMORY_URL,
            token=_non_empty(env, MEMORY_TOKEN_ENV),
            require_token=memory_token_required(env),
        ),
        output=_output_settings(env),
        bearers=bearers,
        rerank_operator=rerank_operator,
    )


def _discover_vaults_file(
    env: Mapping[str, str], root: Path, cfg: Mapping[str, Any]
) -> tuple[Path | None, bool]:
    """The ``vaults.yaml`` to serve, if any, and whether the operator named it.

    ``KNOWLEDGE_VAULTS`` > ``VAULT_PATH`` > ``vault_path`` in ``config.json`` >
    the user's ``vaults.yaml``. A ``VAULT_PATH`` always beats a registry the
    operator did not name, so a process pinned to one vault never picks up the
    vaults of whoever runs it.
    """
    named = _non_empty(env, "KNOWLEDGE_VAULTS")
    if named:
        return _anchor(named, root), True
    if _non_empty(env, "VAULT_PATH") or str(cfg.get("vault_path") or "").strip():
        return None, False
    return _user_vaults_file(env), False


def _compat_slug(env: Mapping[str, str], cfg: Mapping[str, Any], declared: str | None) -> tuple[str, str]:
    """The slug a ``VAULT_PATH`` vault is served under, and what named it.

    In order: ``CHROMA_COLLECTION``, ``chroma_collection`` of ``config.json``, the ``slug`` of the
    vault's own ``vault.yaml`` (when it is a valid slug, since it ends up in a path), then
    :data:`_FALLBACK_SLUG`. A blank value reads as absent: it cannot name an index.
    """
    named = _non_empty(env, "CHROMA_COLLECTION")
    if named:
        return named, "CHROMA_COLLECTION"
    configured = str(cfg.get("chroma_collection") or "").strip()
    if configured:
        return configured, "chroma_collection in config.json"
    if declared is not None and SLUG_PATTERN.fullmatch(declared):
        return declared, VAULT_MANIFEST_NAME
    return _FALLBACK_SLUG, "the fallback"


def _compat_vault(
    env: Mapping[str, str], root: Path, cfg: Mapping[str, Any]
) -> tuple[VaultSpec, list[str]]:
    """The single vault of a ``VAULT_PATH`` (or ``config.json``) deployment.

    Its slug is ``CHROMA_COLLECTION``, else ``chroma_collection`` of ``config.json``, else the
    ``slug`` its own ``vault.yaml`` declares, else ``vault``; its index directory is the first of
    ``KNOWLEDGE_INDEX_DIR``, ``CHROMA_PERSIST_DIR``, ``<KNOWLEDGE_HOME>/<slug>`` (only with an
    explicit ``KNOWLEDGE_HOME``) and ``<root>/.data/<slug>``. ``CHROMA_PERSIST_DIR`` keeps working
    as the index directory, so a deployment that mounts it keeps its sidecars and its volume;
    ``index.sqlite`` lives beside ``chroma.sqlite3``. The ``vault.yaml`` inside the vault also comes
    before ``config.json`` for review thresholds, the memory folder and the embeddings. A slug it
    declares that differs from the one an operator named only warns, so a deployed index is never renamed.
    """
    raw_vault = _non_empty(env, "VAULT_PATH") or str(cfg.get("vault_path") or "").strip()
    vault_path = _anchor(raw_vault, root) if raw_vault else None
    manifest = read_vault_manifest(vault_path if vault_path is not None and vault_path.is_dir() else None)
    declared_slug = _text(manifest.get("slug"))
    slug, slug_source = _compat_slug(env, cfg, declared_slug)
    home, home_explicit = _knowledge_home(env)
    explicit_index = _non_empty(env, "KNOWLEDGE_INDEX_DIR")
    explicit_chroma = _non_empty(env, "CHROMA_PERSIST_DIR")
    if explicit_chroma:
        chroma_dir = Path(explicit_chroma).expanduser()
    elif home_explicit:
        chroma_dir = home / slug / "chroma"
    else:
        chroma_dir = root / ".data" / "chroma"
    if explicit_index:
        index_dir = Path(explicit_index).expanduser()
    elif explicit_chroma:
        index_dir = chroma_dir
    elif home_explicit:
        index_dir = home / slug
    else:
        index_dir = root / ".data" / slug

    warnings: list[str] = []
    if declared_slug is not None and declared_slug != slug:
        why = (
            f"({slug_source}); the index is not renamed"
            if slug_source != "the fallback"
            else "(the declared slug is not a vault slug: lowercase letters, digits and inner hyphens)"
        )
        warnings.append(
            f"{errors.W_VAULT_SLUG_MISMATCH}: {VAULT_MANIFEST_NAME} declares slug {declared_slug!r} "
            f"but the index is served as {slug!r} {why}"
        )
    contract = _contract_warning(slug, manifest)
    if contract is not None:
        warnings.append(contract)

    thresholds = _manifest_thresholds(manifest)
    if thresholds is None:
        configured = _section(cfg, "review").get("thresholds_days")
        thresholds = dict(configured) if isinstance(configured, dict) else None
    manifest_memory = _sub(manifest, "memory")
    memory_folder = _text(manifest_memory.get("export_folder")) or str(
        _section(cfg, "memory").get("vault_folder", _DEFAULT_MEMORY_FOLDER)
    )
    fallback_model = str(cfg.get("embedding_model", _DEFAULT_EMBEDDING_MODEL))
    remote = _non_empty(env, "VAULT_REMOTE")
    integrations, integration_warnings = _manifest_integrations(manifest, vault_path, env, slug)
    memory_hub, hub_warnings = _manifest_memory_hub(manifest, vault_path, env, slug)
    rerank, rerank_warnings = _manifest_rerank(manifest, vault_path, env, slug)
    warnings.extend(integration_warnings + hub_warnings + rerank_warnings)
    spec = VaultSpec(
        slug=slug,
        path=vault_path,
        is_default=True,
        index_dir=index_dir,
        chroma_dir=chroma_dir,
        memory_folder=memory_folder,
        review_thresholds=MappingProxyType(
            thresholds if thresholds is not None else dict(_FALLBACK_REVIEW_THRESHOLDS)
        ),
        thresholds_from_config=thresholds is not None,
        embeddings=_embedding_settings(env, (_sub(manifest, "embedding"),), fallback_model),
        repo=remote or None,
        git_branch=_non_empty(env, "VAULT_BRANCH") or "main",
        features=_manifest_features(manifest),
        memory_project=_text(manifest_memory.get("project")),
        name=_text(manifest.get("name")),
        integrations=integrations,
        memory_hub=memory_hub,
        rerank=rerank,
        index_exclude=index_exclude_patterns(manifest),
    )
    _check_binding(spec)
    return spec, warnings


# ---------------------------------------------------------------------------
# Process-wide snapshot for the legacy facade
# ---------------------------------------------------------------------------

_current: Settings | None = None


def current() -> Settings:
    """The process-wide snapshot, loaded from the environment on first use."""
    global _current
    if _current is None:
        _current = load_settings()
    return _current


def install(settings: Settings) -> None:
    """Pin the process-wide snapshot to ``settings`` (what a server validated)."""
    global _current
    _current = settings


def reset_current() -> None:
    """Drop the snapshot; the next :func:`current` re-reads the environment."""
    global _current
    _current = None
