"""Configuration errors with stable codes.

Every failure a deployment can fix by changing its environment or its files
carries a code that never changes between releases, plus a message that names
the variable or file to touch. Messages name settings, never their values: a
key, a bearer or a token must not reach a log line, a traceback or a client.

``KnowledgeConfigError`` subclasses ``RuntimeError`` so callers that caught the
untyped configuration errors keep catching these.
"""

from __future__ import annotations

E_VAULT_PATH_MISSING = "E_VAULT_PATH_MISSING"
E_VAULT_PATH_NOT_DIR = "E_VAULT_PATH_NOT_DIR"
E_EMBEDDINGS_KEY_MISSING = "E_EMBEDDINGS_KEY_MISSING"
E_EMBEDDINGS_PROVIDER_UNKNOWN = "E_EMBEDDINGS_PROVIDER_UNKNOWN"
E_HTTP_API_KEY_MISSING = "E_HTTP_API_KEY_MISSING"
E_HTTP_PORT_INVALID = "E_HTTP_PORT_INVALID"
E_CONFIG_JSON_INVALID = "E_CONFIG_JSON_INVALID"
E_INDEX_PROVIDER_MISMATCH = "E_INDEX_PROVIDER_MISMATCH"
# The index file was written by a newer layout than this package reads.
E_INDEX_VERSION_NEWER = "E_INDEX_VERSION_NEWER"
# The index file is not a SQLite database, or SQLite cannot read it.
E_INDEX_UNREADABLE = "E_INDEX_UNREADABLE"
# ``KNOWLEDGE_REQUIRE_SQLITE_VEC=1`` and the sqlite-vec extension does not load.
E_SQLITE_VEC_UNAVAILABLE = "E_SQLITE_VEC_UNAVAILABLE"
# ``migrate-index --prune-chroma`` refuses a directory it must not delete from.
E_PRUNE_REFUSED = "E_PRUNE_REFUSED"
E_VAULTS_FILE_MISSING = "E_VAULTS_FILE_MISSING"
E_MANIFEST_INVALID = "E_MANIFEST_INVALID"
E_SECRET_IN_MANIFEST = "E_SECRET_IN_MANIFEST"
E_VAULT_SLUG_INVALID = "E_VAULT_SLUG_INVALID"
E_VAULT_SLUG_DUPLICATE = "E_VAULT_SLUG_DUPLICATE"
E_VAULT_SLUG_MISMATCH = "E_VAULT_SLUG_MISMATCH"
E_VAULT_DEFAULT_AMBIGUOUS = "E_VAULT_DEFAULT_AMBIGUOUS"
E_VAULT_DEFAULT_MISSING = "E_VAULT_DEFAULT_MISSING"
E_VAULT_MANIFEST_MISSING = "E_VAULT_MANIFEST_MISSING"
E_VAULT_UNKNOWN = "E_VAULT_UNKNOWN"
E_EVAL_UNAVAILABLE = "E_EVAL_UNAVAILABLE"
E_LINT_PATH_NOT_DIR = "E_LINT_PATH_NOT_DIR"
E_FRONTMATTER_UNPARSEABLE = "E_FRONTMATTER_UNPARSEABLE"
E_FRONTMATTER_NOT_MAPPING = "E_FRONTMATTER_NOT_MAPPING"
E_WIKILINK_UNRESOLVED = "E_WIKILINK_UNRESOLVED"
# ``vault-lint`` was asked for a rule set it does not have.
E_LINT_RULES_UNKNOWN = "E_LINT_RULES_UNKNOWN"
# ``vault-lint`` rules of the 1.3 set. A note without a readable ISO
# ``last_updated`` is invisible to ``get_stale_docs``; ``--fix`` can move the
# date from ``updated``, ``last_review`` or ``last_verified.date``.
E_FRONTMATTER_LAST_UPDATED = "E_FRONTMATTER_LAST_UPDATED"
# A ``scope`` that is neither the vault's slug, ``global`` nor one of the
# vault's declared ``scopes``: a convention of another vault.
E_SCOPE_FOREIGN = "E_SCOPE_FOREIGN"
# Two note paths that differ only by letter case or Unicode normal form, or a
# path that is not NFC: they collide on a case-sensitive or normalising disk.
E_PATH_CASE_COLLISION = "E_PATH_CASE_COLLISION"
E_PATH_NOT_NFC = "E_PATH_NOT_NFC"
# A runbook (``type: runbook`` under ``runbooks.dir``, not a template) whose
# ``id`` is not ``RB-NNN``, whose ``status`` is outside ``draft``, ``verified``
# and ``outdated``, or whose ``service`` the services map does not know:
# the memory server skips it when it rescans the vault.
E_RUNBOOK_ID_INVALID = "E_RUNBOOK_ID_INVALID"
E_RUNBOOK_STATUS_INVALID = "E_RUNBOOK_STATUS_INVALID"
E_RUNBOOK_SERVICE_UNKNOWN = "E_RUNBOOK_SERVICE_UNKNOWN"
# ``services.json`` exists and is not ``{"services": [{"slug", "aliases"}]}``.
E_SERVICES_MAP_INVALID = "E_SERVICES_MAP_INVALID"
E_PROFILE_UNKNOWN = "E_PROFILE_UNKNOWN"
E_OUTPUT_SETTING_INVALID = "E_OUTPUT_SETTING_INVALID"
# ``doctor`` only: the data directory exists (or would be created) where this
# process cannot write.
E_KNOWLEDGE_HOME_NOT_WRITABLE = "E_KNOWLEDGE_HOME_NOT_WRITABLE"
# An HTTP server whose vault declares an extension it can serve, without the
# environment variables that extension reads its credentials from; and a call
# that needs those credentials without them (``sync_confluence``,
# ``confluence export``).
E_SECRET_MISSING = "E_SECRET_MISSING"
# An HTTP caller authenticated with a bearer that lacks the scope the tool or
# route requires (``admin`` for ``reindex``, ``sync_confluence`` and
# ``POST /webhook/reindex``).
E_SCOPE_REQUIRED = "E_SCOPE_REQUIRED"
# ``confluence export`` only: reading the vault or preparing ``--out`` failed
# outside a single page (a page that fails is reported in the JSON instead).
E_EXPORT_FAILED = "E_EXPORT_FAILED"

# Tool arguments a caller can fix. FastMCP reports them as an ``isError``
# result whose text carries ``<code>: <message>`` after its own prefix.
E_FIELD_UNKNOWN = "E_FIELD_UNKNOWN"
E_ARGUMENT_CONFLICT = "E_ARGUMENT_CONFLICT"
E_ARGUMENT_OUT_OF_RANGE = "E_ARGUMENT_OUT_OF_RANGE"
E_DOC_ID_UNKNOWN = "E_DOC_ID_UNKNOWN"
E_DOC_ID_AMBIGUOUS = "E_DOC_ID_AMBIGUOUS"
E_CHUNK_ID_INVALID = "E_CHUNK_ID_INVALID"
# A call that names nothing to act on: ``get_document`` with neither
# ``file_path`` nor ``chunk_id``.
E_ARGUMENT_MISSING = "E_ARGUMENT_MISSING"
# A parameter the contract retired but the schema still declares, so a call
# written for an older contract is refused instead of silently ignored:
# ``include_metadata`` of ``get_document``. The message names the replacement.
E_ARGUMENT_RETIRED = "E_ARGUMENT_RETIRED"
# ``get_document`` on a path that is not a file of the vault, and on a
# ``section`` the document has no heading for (the message lists its headings).
E_FILE_NOT_FOUND = "E_FILE_NOT_FOUND"
E_SECTION_NOT_FOUND = "E_SECTION_NOT_FOUND"
# ``find_path`` with an end no document of the link graph is: no wiki-link
# reaches it and it links nowhere, or the path is not a vault document.
E_FILE_NOT_IN_GRAPH = "E_FILE_NOT_IN_GRAPH"
# ``get_flow`` or ``compare_flows`` for a provider the vault has no flow
# document for (no note named ``Flow - <provider>.md``).
E_FLOW_NOT_FOUND = "E_FLOW_NOT_FOUND"
# ``sync_confluence`` over a ``scope_prefix`` that holds no file with a
# ``confluence_id``: a misconfigured scope, never an empty success.
E_SCOPE_EMPTY = "E_SCOPE_EMPTY"
# A write the server refuses because it would land inside a vault: the
# vault changes only through a pull request a person reviews.
# ``sync_confluence(dry_run=False)`` and ``confluence export --out`` pointing
# into a served vault both answer with it.
E_WRITE_FENCED = "E_WRITE_FENCED"
# ``search(rerank="on")`` on a vault whose ``vault.yaml`` declares no
# ``rerank.provider``: the caller asked for a judge the vault does not have.
E_RERANK_NOT_CONFIGURED = "E_RERANK_NOT_CONFIGURED"

# Why a read of the memory server did not happen or failed. ``context_pack``
# reports them under ``degraded``; none of them fails the tool.
E_MEMORY_UNAVAILABLE = "E_MEMORY_UNAVAILABLE"
E_MEMORY_TOKEN_MISSING = "E_MEMORY_TOKEN_MISSING"
E_MEMORY_PROJECT_MISSING = "E_MEMORY_PROJECT_MISSING"
E_MEMORY_URL_NOT_LOOPBACK = "E_MEMORY_URL_NOT_LOOPBACK"
# A write to the memory server (project card, task, runbook index) that the
# server answered with an error status or a body this client cannot read.
E_MEMORY_WRITE_FAILED = "E_MEMORY_WRITE_FAILED"
# Jira could not answer a search: a refused query, a server error, a timeout
# or a body that is not a page of issues. Nothing is written to memory.
E_JIRA_UNAVAILABLE = "E_JIRA_UNAVAILABLE"
# Jira refused the account e-mail and API token the vault's
# ``integrations.jira.secrets`` name (HTTP 401 or 403).
E_JIRA_AUTH_FAILED = "E_JIRA_AUTH_FAILED"
# A truncated jira sync that cannot move its cursor: every issue the run may
# read was updated in the minute it starts from, and JQL compares minutes.
E_JIRA_SYNC_STALLED = "E_JIRA_SYNC_STALLED"
# A command or a tool that belongs to an extension the vault does not
# declare under ``features`` (or that no served vault declares).
E_FEATURE_DISABLED = "E_FEATURE_DISABLED"
# A call to a tool the contract retired; the message says where to go instead.
E_TOOL_RETIRED = "E_TOOL_RETIRED"
# ``memory-bridge promote stamp``: the ``--knowledge-ref`` is not the
# vault-relative path of a Markdown document that exists in the vault.
E_KNOWLEDGE_REF_INVALID = "E_KNOWLEDGE_REF_INVALID"
# ``init``: a vault's ``Contexts/config.json``, or this machine's
# ``repos.local.json`` for it, cannot be read or is not a repository registry.
E_CONTEXTS_INVALID = "E_CONTEXTS_INVALID"
# ``init --root NAME=DIR``: no vault being initialized declares root ``NAME``.
E_ROOT_UNKNOWN = "E_ROOT_UNKNOWN"
# ``doctor --client``: a checkout's package cannot be installed because the
# checkout holds an untracked file, directory or link of its own there.
E_CONTEXTS_CONFLICT = "E_CONTEXTS_CONFLICT"
# ``doctor --client``: a checkout is not the declared repository, its origin
# cannot be read, or its package cannot be installed into it.
E_CONTEXTS_CHECKOUT_INVALID = "E_CONTEXTS_CHECKOUT_INVALID"
# ``init --repo ID`` or ``init --drop ID``: no vault being initialized declares
# checkout ``ID`` in its ``Contexts/config.json``.
E_CONTEXTS_REPO_UNKNOWN = "E_CONTEXTS_REPO_UNKNOWN"

W_WIKILINK_AMBIGUOUS_STEM = "W_WIKILINK_AMBIGUOUS_STEM"
# ``type`` outside the closed set of document types of the 1.3 rules.
W_TYPE_UNKNOWN = "W_TYPE_UNKNOWN"
# The vault has runbooks to check and no ``services.json`` to check their
# ``service`` against.
W_SERVICES_MAP_MISSING = "W_SERVICES_MAP_MISSING"
W_TRANSPORT_UNKNOWN = "W_TRANSPORT_UNKNOWN"
W_WEBHOOK_SECRET_MISSING = "W_WEBHOOK_SECRET_MISSING"
W_REVIEW_THRESHOLDS_FALLBACK = "W_REVIEW_THRESHOLDS_FALLBACK"
W_VAULT_PATH_IGNORED = "W_VAULT_PATH_IGNORED"
W_VAULT_SLUG_MISMATCH = "W_VAULT_SLUG_MISMATCH"
W_FEATURE_WITHOUT_REPO = "W_FEATURE_WITHOUT_REPO"
W_VAULT_GIT_PULL_OFF = "W_VAULT_GIT_PULL_OFF"
W_CONTRACT_RANGE = "W_CONTRACT_RANGE"
# A stdio server whose vault declares an extension without its credentials:
# the server starts and only that extension cannot answer.
W_EXTENSION_UNCONFIGURED = "W_EXTENSION_UNCONFIGURED"
# A vault declares an extension the HTTP transport does not load (it writes to
# a memory server, which is only reachable on the person's own loopback).
W_EXTENSION_UNAVAILABLE_HTTP = "W_EXTENSION_UNAVAILABLE_HTTP"
# ``vaults.yaml`` declares ``bearers`` and the server runs over stdio, where
# no bearer is ever presented: the process runs as the person.
W_BEARERS_IGNORED_STDIO = "W_BEARERS_IGNORED_STDIO"
# A vault declares a rerank provider and the variable its key comes from is
# empty: the server starts and that vault searches without a judge.
W_RERANK_KEY_MISSING = "W_RERANK_KEY_MISSING"
# The ``rerank`` block of a ``vault.yaml`` (or a ``KNOWLEDGE_RERANK*`` operator
# variable) is malformed: that vault searches without a judge and the server
# starts. Secret-bearing keys are not forgiven; they stop the server instead.
W_RERANK_INVALID = "W_RERANK_INVALID"
# A served vault declares ``Contexts/config.json`` and this machine has no
# ``repos.local.json`` for it: its context packages are not linked into the
# checkouts yet. The server serves the vault all the same.
W_CONTEXTS_INIT_PENDING = "W_CONTEXTS_INIT_PENDING"
W_CONTEXTS_GITIGNORE = "W_CONTEXTS_GITIGNORE"
# ``doctor --client``: a declared checkout is a git working tree with no origin
# remote, so it cannot be verified; ``init`` installs into it only with ``--force``.
W_CONTEXTS_CHECKOUT_UNVERIFIED = "W_CONTEXTS_CHECKOUT_UNVERIFIED"
# ``init`` and ``doctor --client``: a checkout holds links into its package and
# is not in the ``apply`` list of this machine's ``repos.local.json``.
W_CONTEXTS_LINKED_NOT_SELECTED = "W_CONTEXTS_LINKED_NOT_SELECTED"
# ``doctor --client``: this machine's ``repos.local.json`` has no ``apply`` list,
# so it does not say which checkouts receive a package; the next ``init`` sets it.
W_CONTEXTS_SELECTION_MISSING = "W_CONTEXTS_SELECTION_MISSING"


class KnowledgeConfigError(RuntimeError):
    """A configuration problem the operator can fix, identified by ``code``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class KnowledgeToolError(ValueError):
    """A tool argument the caller can fix, identified by ``code``.

    Raised from a tool body; the MCP layer turns it into an ``isError`` result.
    Like :class:`KnowledgeConfigError`, the message names arguments and vault
    paths, never a credential.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class MemoryUnavailable(RuntimeError):
    """A memory read that did not happen or did not succeed, identified by ``code``.

    Raised by a :class:`~knowledge.ports.MemoryClient`; ``code`` is one of the
    ``E_MEMORY_*`` codes. The message names the setting or the failure class,
    never the bearer, never the request URL.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class MemoryWriteFailed(RuntimeError):
    """A memory write that did not happen or did not succeed, identified by ``code``.

    ``code`` is ``E_MEMORY_WRITE_FAILED`` when the memory server answered
    with an error, or one of the ``E_MEMORY_*`` codes when no write could be
    attempted (no bearer, a non-loopback URL, no project, the server down).
    ``status`` is the HTTP status when there was one; ``reason`` is the
    memory server's own error code (``invalid_enum``, ``no_card``,
    ``payload_too_large``...) or the class of the transport failure. Neither
    the message nor ``reason`` ever carries the bearer, the request URL or
    the body the server sent back.
    """

    def __init__(self, code: str, message: str, *, status: int | None = None, reason: str | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status
        self.reason = reason

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"
