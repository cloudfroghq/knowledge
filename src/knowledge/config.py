"""Compatibility facade over :mod:`knowledge.settings`.

The upper-case names this module always exposed (``VAULT_PATH``,
``CHROMA_PERSIST_DIR``, ``MCP_API_KEY``, ``REVIEW_THRESHOLDS_DAYS``…) still
resolve here, but importing it has no effect: no ``.env`` is read, no
``config.json`` is parsed and no registry is loaded until one of those names
is read, and then each one is answered from :func:`settings.current`, the
process-wide snapshot (its default vault for the per-vault values).

Assigning a name (``config.VAULT_PATH = path``, ``monkeypatch.setattr``)
creates a real attribute that shadows the lazy one, which is what callers that
patch this module expect. Reloading the module drops those assignments and the
snapshot, so the next read re-reads the environment.

The repository registry and the graphify targets no longer live here: see
:mod:`knowledge.graph_targets`.
"""

from __future__ import annotations

from typing import Any

from knowledge import settings as _settings

_PROJECT_ROOT = _settings.PROJECT_ROOT
_load_config_json = _settings.load_config_json
_resolve_relative_path = _settings.resolve_relative_path

#: Every name the facade resolves lazily.
_LEGACY: frozenset[str] = frozenset(
    {
        "CONFIG_JSON_PATH",
        "CONFIG_JSON_LOADED",
        "OPENAI_API_KEY",
        "CONFLUENCE_EMAIL",
        "CONFLUENCE_TOKEN",
        "CONFLUENCE_URL",
        "MCP_TRANSPORT",
        "MCP_API_KEY",
        "MCP_ADMIN_KEY",
        "MCP_WEBHOOK_SECRET",
        "MCP_HTTP_HOST",
        "MCP_HTTP_PORT",
        "MCP_HTTP_PATH",
        "EMBEDDING_MODEL",
        "REVIEW_THRESHOLDS_FROM_CONFIG",
        "REVIEW_THRESHOLDS_DAYS",
        "MEMORY_FOLDER",
        "MEMORY_EXPORT_TYPES",
        "PROMOTION_FROM_CONFIG",
        "PROMOTION_OUTPUT_DIR",
        "PROMOTION_TARGET_FOLDERS",
        "PROMOTION_DOC_STATUS",
        "PROMOTION_DEFAULT_OWNER",
        "PROMOTION_BRANCH_PREFIX",
        "VAULT_PATH",
        "CHROMA_PERSIST_DIR",
        "CHROMA_COLLECTION",
    }
)

# A reload means "re-read the environment": drop any value a caller assigned
# over a lazy name, and the snapshot the lazy names were answered from.
for _name in _LEGACY:
    globals().pop(_name, None)
_settings.reset_current()


def _resolve(name: str, s: _settings.Settings) -> Any:
    vault = s.default_vault()
    values: dict[str, Any] = {
        "CONFIG_JSON_PATH": lambda: s.config_json_path,
        "CONFIG_JSON_LOADED": lambda: s.config_json_loaded,
        "OPENAI_API_KEY": lambda: vault.embeddings.api_key,
        "CONFLUENCE_EMAIL": lambda: s.confluence.email,
        "CONFLUENCE_TOKEN": lambda: s.confluence.token,
        "CONFLUENCE_URL": lambda: s.confluence.url,
        "MCP_TRANSPORT": lambda: s.http.transport,
        "MCP_API_KEY": lambda: s.http.api_key,
        "MCP_ADMIN_KEY": lambda: s.http.admin_key,
        "MCP_WEBHOOK_SECRET": lambda: s.http.webhook_secret,
        "MCP_HTTP_HOST": lambda: s.http.host,
        "MCP_HTTP_PORT": lambda: s.http.port,
        "MCP_HTTP_PATH": lambda: s.http.path,
        "EMBEDDING_MODEL": lambda: vault.embeddings.model,
        "REVIEW_THRESHOLDS_FROM_CONFIG": lambda: vault.thresholds_from_config,
        "REVIEW_THRESHOLDS_DAYS": lambda: dict(vault.review_thresholds),
        "MEMORY_FOLDER": lambda: vault.memory_folder,
        "MEMORY_EXPORT_TYPES": lambda: list(s.memory_export_types),
        "PROMOTION_FROM_CONFIG": lambda: s.promotion.from_config,
        "PROMOTION_OUTPUT_DIR": lambda: s.promotion.output_dir,
        "PROMOTION_TARGET_FOLDERS": lambda: dict(s.promotion.target_folders),
        "PROMOTION_DOC_STATUS": lambda: s.promotion.doc_status,
        "PROMOTION_DEFAULT_OWNER": lambda: s.promotion.default_owner,
        "PROMOTION_BRANCH_PREFIX": lambda: s.promotion.branch_prefix,
        "VAULT_PATH": vault.require_path,
        "CHROMA_PERSIST_DIR": lambda: vault.index_dir,
        "CHROMA_COLLECTION": lambda: vault.slug,
    }
    return values[name]()


def __getattr__(name: str) -> Any:
    """Answer a legacy name from the process-wide settings snapshot."""
    if name in _LEGACY:
        return _resolve(name, _settings.current())
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def validate() -> None:
    """Raise if the process-wide configuration cannot serve."""
    _settings.current().validate()
