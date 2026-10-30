"""Tool profiles: which tools a server instance registers.

A profile names a subset of the tool contract. ``core`` is the tools that only
need an indexed vault (``context_pack`` included: memory is an optional source
it degrades without); ``ext`` the tools of the extensions a vault turns on with
``features`` (flow files, governance reports, Confluence, Jira), each
registered only when a served vault declares its extension; ``all`` is both and
is what a server serves by default. ``legacy`` is ``all`` plus the tools the
contract retired, registered under the names and in the order of contract
1.4.0 (:data:`LEGACY_ORDER`), with the extension tools of 1.4.0 registered
whatever the vaults declare; it lives for one release.

Only ``all`` and ``legacy`` can be selected from outside, with ``serve
--profile`` or ``KNOWLEDGE_PROFILE`` (the flag wins). Selecting never reorders
the tools, so the registration order is preserved.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from knowledge.contract import DEFAULT_PROFILE, LEGACY_PROFILE, PUBLIC_PROFILES
from knowledge.errors import E_PROFILE_UNKNOWN, KnowledgeConfigError

__all__ = [
    "DEFAULT_PROFILE",
    "LEGACY_ORDER",
    "LEGACY_PROFILE",
    "PROFILES",
    "PUBLIC_PROFILES",
    "public",
    "select",
]

_CORE: frozenset[str] = frozenset(
    {
        "get_document",
        "search",
        "search_by_metadata",
        "search_deep",
        "find_path",
        "get_related",
        "get_impact",
        "reindex",
        "context_pack",
        "list_vaults",
    }
)
_EXT: frozenset[str] = frozenset(
    {"get_flow", "compare_flows", "get_stale_docs", "suggest_links", "sync_confluence", "sync_jira"}
)
_RETIRED: frozenset[str] = frozenset({"search_code"})

PROFILES: Mapping[str, frozenset[str]] = {
    "all": _CORE | _EXT,
    "core": _CORE,
    "ext": _EXT,
    LEGACY_PROFILE: _CORE | _EXT | _RETIRED,
}

#: The catalog of contract 1.4.0, name by name and in its order: what the
#: ``legacy`` profile registers before any gated extension a vault turns on.
LEGACY_ORDER: tuple[str, ...] = (
    "get_document",
    "search",
    "search_by_metadata",
    "search_deep",
    "find_path",
    "get_related",
    "suggest_links",
    "get_stale_docs",
    "get_flow",
    "compare_flows",
    "get_impact",
    "sync_confluence",
    "search_code",
    "reindex",
    "context_pack",
    "list_vaults",
)


def _unknown() -> KnowledgeConfigError:
    return KnowledgeConfigError(
        E_PROFILE_UNKNOWN,
        f"unknown tool profile; use one of: {', '.join(PUBLIC_PROFILES)}",
    )


def public(profile: str) -> str:
    """``profile`` when it is one a server may be asked to serve.

    Raises :class:`~knowledge.errors.KnowledgeConfigError` with
    ``E_PROFILE_UNKNOWN`` otherwise; the message lists the valid names and
    never echoes the one given.
    """
    if profile not in PUBLIC_PROFILES:
        raise _unknown()
    return profile


def select(profile: str, registered_in_order: Sequence[str]) -> list[str]:
    """Return the names of ``registered_in_order`` that ``profile`` keeps, in order.

    Raises :class:`~knowledge.errors.KnowledgeConfigError` with
    ``E_PROFILE_UNKNOWN`` when ``profile`` is not declared.
    """
    try:
        keep = PROFILES[profile]
    except KeyError:
        raise KnowledgeConfigError(
            E_PROFILE_UNKNOWN,
            f"unknown tool profile; use one of: {', '.join(sorted(PROFILES))}",
        ) from None
    return [name for name in registered_in_order if name in keep]
