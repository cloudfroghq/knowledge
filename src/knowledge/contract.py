"""The tool contract this package serves: its version and when each tool appeared.

A leaf module on purpose: it imports nothing from the package, so ``settings``,
the server and the scripts can all read the version without a cycle. It is the
single source of the contract number; nothing else in the repository spells it
out as a literal.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

#: Contract version as published to clients and compared against a vault's
#: ``knowledge_contract`` range.
CONTRACT_VERSION = "2.0.0"
CONTRACT_VERSION_INFO: tuple[int, int, int] = (2, 0, 0)

#: Prefix of the first line of :data:`INSTRUCTIONS`; the rest of that line is
#: :data:`CONTRACT_VERSION`.
INSTRUCTIONS_VERSION_PREFIX = "knowledge-contract: "

#: The ``instructions`` field of the ``initialize`` result. Its first line
#: names the contract version, so a client learns it in the handshake without
#: calling a tool; clients may truncate the field, so the version never moves
#: off the first line. The rest is the reading protocol the contract's token budget is
#: measured with: search with its default rows, then read the one block a row
#: points at. It stays under 400 characters.
INSTRUCTIONS = "\n".join(
    (
        f"{INSTRUCTIONS_VERSION_PREFIX}{CONTRACT_VERSION}",
        "Find, then read one section: search(query) -> get_document(chunk_id=<chunk_id>), "
        "or file_path when chunk_id is null. Never page whole files.",
        "Task context in one call: context_pack(query, budget_tokens=3000).",
        "Several vaults: list_vaults, then vault=<slug> on any tool.",
        "file_path is vault-relative. Every error is isError with a stable E_ code.",
    )
)

#: The catalog description of each tool, as ``tools/list`` serves it. The
#: function docstrings document the code; these strings are what a model reads
#: to choose a tool and its arguments, so each one names the parameters whose
#: meaning is not obvious from their name. At most 250 characters each, and 80
#: for the retired ``search_code``, which only the ``legacy`` profile lists.
DESCRIPTIONS: Mapping[str, str] = MappingProxyType(
    {
        "get_document": (
            "Read a vault doc: chunk_id (from a search row) reads one heading's block, file_path optional; "
            "section= a heading, start_line/end_line a range. max_chars=3000 caps text; truncated=True: narrow. "
            "full_metadata=True: all frontmatter. fields= trims keys."
        ),
        "search": (
            "Hybrid search (mode hybrid|semantic|keyword, limit 1-50); rows: file_path, score (ordinal, not a "
            "probability), chunk_id, section to read. fields= adds excerpt; snippet=True; include_memories=True. "
            "rerank='on': vault judge, rerank_score a probability."
        ),
        "search_by_metadata": (
            "Filter docs by frontmatter type, service, tag (e.g. service/middleware) and status, AND-ed. Answers "
            "{total, offset, items}, items by path: limit (20, max 200) and offset page them; facets=True counts "
            "values instead. fields= trims items."
        ),
        "search_deep": (
            "search expanded through wiki-links up to depth hops (max 3), limit 1-20; discovered_via says how each "
            "row was reached. Same rows, fields= and snippet= as search."
        ),
        "find_path": (
            "Shortest wiki-link path between two docs, links in either direction, up to max_depth hops (1-6). "
            "weighted=True prefers learned links. Ties resolve to the lexicographically smallest path."
        ),
        "get_related": (
            "Docs linked from or to a doc (direction outgoing, incoming or both), strongest learned links first, "
            "ties by path; limit 1-200. include_suggested=True adds unlinked docs sharing two or more neighbors. "
            "fields= trims rows."
        ),
        "suggest_links": (
            "Curation report: unlinked doc pairs that share graph neighbors, by confidence. Pure graph math, no "
            "API cost."
        ),
        "get_stale_docs": (
            "Docs due for revalidation and the owning team (owner, owner_source). reason: overdue (age_days past "
            "type threshold), last_updated_invalid (include_unreadable=False drops it) or, with "
            "missing_last_updated=True, last_updated_missing. owner= filters."
        ),
        "get_flow": "Auth-flow steps for a provider plus the shared login steps.",
        "compare_flows": "Diff two providers' auth flows: common steps, steps only in each, shared login steps.",
        "get_impact": (
            "Docs that reference this one through backlinks, per depth, sorted by path. max_items (20, max 1000) "
            "caps each list and omitted says how many it left out; counts=True adds per-depth totals."
        ),
        "sync_confluence": (
            "Report vault files under scope_prefix (default Conventions/) whose Confluence page is newer; files "
            "outside it are listed. Read-only: dry_run=False is E_WRITE_FENCED; use `cloudfrog-knowledge "
            "confluence export`. Without credentials: E_SECRET_MISSING."
        ),
        "search_code": "Retired (ADR-039): returns a redirect to graphify, rg and search.",
        "reindex": (
            "Re-index the vault by file hash; only target='vault'. force=True re-embeds every file (a paid full "
            "embedding run; needed to backfill new metadata). pull=True runs git pull first (the push webhook "
            "already pulls); vault_sync says how it went."
        ),
        "context_pack": (
            "Best sections for query under a hard token budget (budget_tokens 500-12000), deduplicated; dropped "
            "lists what did not fit. include_memories=True adds memory-folder docs and, when configured, the "
            "memory server."
        ),
        "list_vaults": (
            "Vaults this server serves (slug, name, default, features) and the tool contract version. Pass a "
            "slug as vault= to any tool."
        ),
        "sync_jira": (
            "Admin. Mirror the issues of the vault's integrations.jira projects into memory tasks (upsert by "
            "jira_key). dry_run=True (default) only reads Jira; since= ISO date or -7d."
        ),
    }
)

#: ``_meta`` of the tools that carry one in the catalog. Only the retired
#: ``search_code`` does, and only the ``legacy`` profile lists it: deprecated,
#: and gone with that profile in the version that names its removal.
TOOL_META: Mapping[str, Mapping[str, object]] = MappingProxyType(
    {
        "search_code": MappingProxyType({"knowledge/deprecated": True, "knowledge/removal": "2.1.0"}),
    }
)

#: The tool profile every server serves unless asked otherwise.
DEFAULT_PROFILE = "all"
#: The profile that serves the catalog of 1.4.0 (names and order) with the
#: 2.0 answers, for one release.
LEGACY_PROFILE = "legacy"
#: The profiles ``serve --profile`` and ``KNOWLEDGE_PROFILE`` may select.
PUBLIC_PROFILES: tuple[str, ...] = (DEFAULT_PROFILE, LEGACY_PROFILE)

#: Tools the contract retired, by the version that retired them. Under the
#: ``all`` profile a call to one answers ``E_TOOL_RETIRED`` with
#: :func:`retired_message`; the ``legacy`` profile still lists them.
TOOL_REMOVED: Mapping[str, str] = MappingProxyType({"search_code": "2.0.0"})

#: Where to go instead of a retired tool.
TOOL_REDIRECTS: Mapping[str, str] = MappingProxyType(
    {
        "search_code": (
            "source code is not indexed here (ADR-039). Use `graphify explain <symbol>` and "
            "`graphify affected <file>` for relationships and impact, `graphify query <symbol>` only with a "
            "real symbol name, `rg` for literal text, and `search` or `search_deep` for intent in the vault"
        ),
    }
)

#: The extension (``features: [<name>]`` of a ``vault.yaml``) each extension
#: tool belongs to. These tools are registered only when a served vault
#: declares their extension and the transport loads it.
TOOL_EXTENSION: Mapping[str, str] = MappingProxyType(
    {
        "get_flow": "flows",
        "compare_flows": "flows",
        "get_stale_docs": "governance",
        "suggest_links": "governance",
        "sync_confluence": "confluence",
        "sync_jira": "jira",
    }
)


def retired_message(name: str) -> str:
    """The message of ``E_TOOL_RETIRED`` for the retired tool ``name``."""
    return f"{name} was retired in contract {TOOL_REMOVED[name]}; {TOOL_REDIRECTS[name]}."


def unloaded_message(name: str) -> str:
    """The message of ``E_FEATURE_DISABLED`` for extension tool ``name`` when no served vault declares its extension."""
    return (
        f"no vault this server serves declares features: [{TOOL_EXTENSION[name]}], which brings {name}; "
        "add it to a vault.yaml by pull request"
    )


#: Contract version in which each tool first appeared, for every tool of the
#: contract (retired ones are in :data:`TOOL_REMOVED`). The registered set is
#: these keys minus the extension tools no served vault turns on, plus the
#: retired tools under the ``legacy`` profile; a test keeps both equal.
TOOL_SINCE: Mapping[str, str] = MappingProxyType(
    {
        "get_document": "1.0.0",
        "search": "1.0.0",
        "search_by_metadata": "1.0.0",
        "search_deep": "1.0.0",
        "find_path": "1.0.0",
        "get_related": "1.0.0",
        "suggest_links": "1.0.0",
        "get_stale_docs": "1.0.0",
        "get_flow": "1.0.0",
        "compare_flows": "1.0.0",
        "get_impact": "1.0.0",
        "sync_confluence": "1.0.0",
        "reindex": "1.0.0",
        "context_pack": "1.1.0",
        "list_vaults": "1.1.0",
        "sync_jira": "1.3.0",
    }
)
