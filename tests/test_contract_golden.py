"""Golden master of the tool contract, byte for byte.

The response-shape freeze (``test_response_schemas``) pins which keys a tool
emits and with what types. This module pins the *bytes*: the tool catalog a
client gets from ``tools/list`` and the full ``tools/call`` result every
registered tool returns for one fixed input over the fixture vault. The two
guards answer different questions. The freeze catches a field that appears or
disappears; the golden catches a description that was reworded, a default
that moved, a score that changed in the third decimal, an input schema that
gained a constraint — anything a consumer could have come to depend on.

How the catalog and the responses are obtained matters more than what they
contain, so this is deliberately not an in-process call on the tool
functions. ``tests/golden_stdio_server.py`` is launched as a subprocess over
the stdio transport (the transport ``MCP_TRANSPORT`` defaults to and the one
``python -m knowledge.server`` serves) and driven with the official SDK
client: a real ``initialize`` handshake, a real ``tools/list``, a real
``tools/call`` per tool. The bytes recorded are the wire result after the
server's own serialization — the ``content`` text blocks, ``isError`` and,
in the session that turns it on, the ``structuredContent`` FastMCP derives
from the return annotation — which is exactly what a client parses. Each result is dumped as compact JSON
with sorted keys, so a diff is a diff in content and never in formatting.

What is pinned to make the bytes reproducible, and why each pin is honest:

* The embeddings provider is the suite's bag-of-words double, so vectors and
  therefore scores are a pure function of the fixture text. The index is the
  real SQLite store with sqlite-vec loaded: the server runs with
  ``KNOWLEDGE_REQUIRE_SQLITE_VEC=1``, and in a Python where the extension does
  not load the module is skipped (failed, when the suite itself runs with that
  variable), because the standard-library fallback may round a score
  differently.
* The calendar inside the server is frozen (``golden_stdio_server.FROZEN_TODAY``):
  ``age_days`` is a distance to today, and a golden that aged with the
  calendar would turn red on its own every morning. It is a documented clock,
  not a scrubbed field; every recorded ``age_days`` is true for that date.
* ``PYTHONHASHSEED`` is fixed for the server process, and nothing may depend
  on it. Every order a tool emits is part of the contract and is sorted by a
  declared key (``get_impact``'s per-depth lists by path, ``get_related`` by
  weight, direction and path, ``find_path`` the lexicographically smallest of
  the shortest paths, ``search`` and ``search_deep`` ties by path), never left
  to the walk order of a Python ``set``. The whole session is replayed under
  a second seed and must come back byte for byte, catalog included
  (``test_every_golden_is_independent_of_the_hash_seed``); a third replay
  under a random seed, printed on failure and repeatable through
  ``KNOWLEDGE_GOLDEN_SEED``, looks for a dependence the two fixed seeds miss
  by chance.
* Usage counters and Hebbian weights start empty in the fresh server and only
  move through the calls this module makes, in the order written below.
* The interpreter is the one ``requires-python`` pins in ``pyproject.toml``.
  The tool descriptions in the catalog come from ``contract.DESCRIPTIONS``,
  not from the function docstrings, so the indentation an interpreter keeps
  or strips in a docstring no longer reaches ``tools_list.json``.
* The output switches (``KNOWLEDGE_STRUCTURED_OUTPUT``,
  ``KNOWLEDGE_TEXT_INDENT``) and the tool profile (``KNOWLEDGE_PROFILE``) are
  pinned to their defaults, so the goldens are the compact text blocks alone,
  the canonical answer of contract 2.0. Two more sessions flip one switch
  each: the ``structured`` session (``KNOWLEDGE_STRUCTURED_OUTPUT=1``) records
  its catalog as ``tools_list__structured`` and must add only
  ``structuredContent`` and ``outputSchema``, the same JSON as the text; the
  indented one must differ only in the whitespace of the text.

Goldens are versioned: ``tests/golden/v<CONTRACT_VERSION>/`` holds the bytes of
the contract version ``knowledge.contract`` declares, and every older version
stays beside it, frozen. A frozen directory carries a ``SHA256SUMS`` file and
``tests/test_contract_frozen.py`` fails when any of its files changes, in any
Python (it only hashes files, so it runs where this module is skipped), so an
old version can neither be re-recorded nor edited by hand. Contract 2.0.0
breaks 1.x on purpose, so the frozen 1.4.0 goldens are replayed through the
declared migration of ``golden_compat`` (``migrate_1_4_to_2_0``: only the
``R<n>`` rules of the changelog's "Breaking" table) and then compared on parsed
JSON, key by key and value by value, in a session of their own so the cases
of newer versions never move their counters. That session runs every 1.4.0
case in its order over the vault of the ``features`` session (the fixture
with ``flows``, ``governance`` and ``confluence`` declared), so no 1.4.0 case
is split off into another session. The 1.0.0-1.3.0 goldens are no
longer replayed against a 2.x server: they stay frozen and hashed, and what
they guaranteed reaches 2.0.0 through 1.4.0, which honoured every one of them.

Re-recording is explicit: ``KNOWLEDGE_GOLDEN_RECORD=1 uv run pytest
tests/test_contract_golden.py`` rewrites every file of the current version's
directory, and refuses when that directory is frozen or when the changelog
(``docs/contract/CHANGELOG.md``) does not open with that version: a golden is
never recorded for a version whose changes are not written down. Without the variable,
any byte difference fails with a readable diff. A re-record is a contract
change and is reviewed as one.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import os
import random
import re
import shutil
import sys
from collections.abc import Iterable
from datetime import timedelta
from pathlib import Path
from typing import Any, NamedTuple

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from knowledge import contract, stores

from golden_compat import (
    MIGRATIONS_2_0,
    ORDER_FREE_TOOLS,
    PRESENCE_BOUND,
    STORE_BOUND_SINCE,
    STORE_BOUND_TOOLS,
    VERSION_BOUND,
    ReplaySession,
    catalog_problems,
    initialize_problems,
    migrate_1_4_to_2_0,
    response_problems,
)

TESTS_DIR = Path(__file__).parent
PROJECT_ROOT = TESTS_DIR.parent
FIXTURE_VAULT = TESTS_DIR / "fixtures" / "vault"
GOLDEN_ROOT = TESTS_DIR / "golden"
GOLDEN_DIR = GOLDEN_ROOT / f"v{contract.CONTRACT_VERSION}"
FROZEN_SUMS = "SHA256SUMS"
SERVER_SCRIPT = TESTS_DIR / "golden_stdio_server.py"

RECORD_ENV = "KNOWLEDGE_GOLDEN_RECORD"
RECORDING = os.environ.get(RECORD_ENV) == "1"

# ``PYTHONHASHSEED`` the goldens are recorded under, and a second seed the
# whole session is replayed under to find out which responses depend on it.
RECORDING_SEED = "0"
SECOND_SEED = "7"

# A third replay runs under a seed drawn per run. ``KNOWLEDGE_GOLDEN_SEED``
# pins it, so a failure reported with its seed can be replayed exactly.
RANDOM_SEED_ENV = "KNOWLEDGE_GOLDEN_SEED"
# ``PYTHONHASHSEED`` accepts integers in [0, 4294967295]; 0 disables hash
# randomisation, so the draw starts at 1.
_MAX_HASH_SEED = 4_294_967_295

# Tools whose bytes may change with the hash seed. Empty: every order a tool
# emits is sorted by a declared key, so any golden whose bytes change under
# another seed fails ``test_every_golden_is_independent_of_the_hash_seed``.
SEED_DEPENDENT_TOOLS: frozenset[str] = frozenset()

CHANGELOG = PROJECT_ROOT / "docs" / "contract" / "CHANGELOG.md"

if stores.probe_vector_backend() != stores.BACKEND_SQLITE_VEC:
    if stores.sqlite_vec_required():
        raise RuntimeError(f"{stores.REQUIRE_SQLITE_VEC_ENV}=1 and sqlite-vec does not load in this Python")
    pytest.skip("the goldens are recorded and compared with sqlite-vec, which does not load here", allow_module_level=True)

# The same synthetic key ``conftest.py`` hands the in-process server. It only
# has to be non-empty for ``config.validate()``; the embeddings call that
# would use it is replaced in the subprocess. It must never land in a golden.
PLACEHOLDER_KEY = "placeholder-not-a-credential"

# Name of the golden that holds the ``tools/list`` catalog. Not a tool name,
# so it cannot collide with one.
CATALOG = "tools_list"

# Name of the golden that holds the contract part of the ``initialize`` result:
# ``serverInfo.name`` and ``instructions``. ``serverInfo.version``, the
# negotiated ``protocolVersion`` and ``capabilities`` belong to the ``mcp``
# SDK, not to the contract, so they stay out of it; the version is checked
# against the installed SDK instead (``test_server_info_carries_the_sdk_version``).
INITIALIZE = "initialize"

# Key of the captured ``serverInfo.version``. Not a golden: it is never written.
SERVER_VERSION = "serverInfo.version"

# Key of the captured ``serverInfo.icons``, as wire JSON. Not a golden either:
# the icon is presentation, not contract.
SERVER_ICONS = "serverInfo.icons"

RUNBOOK = "Runbooks/Runbook - Middleware Outage.md"
MIDDLEWARE = "Services/Middleware Service.md"
PORTAL = "Services/Portal Service.md"


class Case(NamedTuple):
    """One fixed ``tools/call`` and the golden its result is recorded in.

    The golden of a tool's first case carries the bare tool name; any further
    case for the same tool adds a variant, ``<tool>__<variant>``. The double
    underscore cannot appear in a registered tool name, so a variant never
    collides with a tool.
    """

    tool: str
    arguments: dict[str, Any]
    variant: str | None = None

    @property
    def golden(self) -> str:
        return self.tool if self.variant is None else f"{self.tool}__{self.variant}"


# One fixed invocation per registered tool, plus variants where one input does
# not cover the tool, in the order they are made. The order is part of the
# recording: read-only tools go first, the two mutating ones close the run so
# nothing after them reads a state they changed.
#
# ``search_by_metadata`` is called twice because its filters reach the index
# by different routes: ``type`` is a scalar equality, ``tag`` a membership test
# over the stored tag list. ``service/middleware`` is a tag several fixture
# documents carry, so an empty result for it is a regression, never a valid
# recording (``test_tag_filter_golden_is_not_empty``).
#
# ``sync_confluence`` runs with no credentials on purpose: its structured
# "not configured" error is the response every deployment without Confluence
# gets, and it is as much contract as a report. ``reindex`` is the vault
# target only (ADR-039) over an index the earlier calls already built, so it
# reports every file unchanged.
V1_0_0_CASES: tuple[Case, ...] = (
    Case("get_document", {"file_path": RUNBOOK}),
    Case("search", {"query": "middleware outage", "limit": 5}),
    Case("search_by_metadata", {"type": "service"}),
    Case("search_by_metadata", {"tag": "service/middleware"}, variant="tag"),
    Case("search_deep", {"query": "middleware outage", "depth": 2, "limit": 5}),
    Case("find_path", {"from_path": PORTAL, "to_path": RUNBOOK}),
    Case("get_related", {"file_path": MIDDLEWARE, "limit": 10}),
    Case("suggest_links", {}),
    Case("get_stale_docs", {}),
    Case("get_flow", {"provider": "Telvia"}),
    Case("compare_flows", {"provider1": "Telvia", "provider2": "Comet Argentina"}),
    Case("get_impact", {"file_path": RUNBOOK, "max_depth": 2}),
    Case("search_code", {"query": "anything"}),
    Case("sync_confluence", {"dry_run": True}),
    Case("reindex", {"target": "vault"}),
)

# ``chunk_id`` of the runbook's first heading: ``sha1(path)[:10]`` plus the
# ordinal. Written out, not computed, so the case reads the pointer a client
# copies from the ``search__pointers`` row
# (``test_the_chunk_case_reads_the_pointer_search_returned`` keeps both equal).
RUNBOOK_CHUNK = "d9bc623bc4:1"

#: ``budget_tokens`` of the ``context_pack`` cases.
PACK_BUDGET = 600

# Read-only variants of the parameters 1.1.0 adds. They run after the 1.0.0
# read-only cases and before the two mutating ones, so the 1.0.0 cases keep
# the usage counters and link weights they were recorded with, and nothing
# these variants record reaches a response after them.
V1_1_0_READ_CASES: tuple[Case, ...] = (
    Case(
        "search",
        {"query": "middleware outage", "limit": 5, "fields": ["file_path", "doc_id", "chunk_id", "section", "score"]},
        variant="pointers",
    ),
    Case("search", {"query": "otp session", "limit": 2, "snippet": True}, variant="snippet"),
    Case(
        "search_deep",
        {"query": "middleware outage", "depth": 2, "limit": 3, "fields": ["file_path", "discovered_via", "chunk_id", "section"]},
        variant="pointers",
    ),
    Case("get_document", {"file_path": RUNBOOK, "chunk_id": RUNBOOK_CHUNK}, variant="chunk"),
    Case(
        "get_document",
        {"file_path": MIDDLEWARE, "fields": ["file_path", "doc_id", "sections", "truncated"]},
        variant="sections",
    ),
    Case(
        "get_document",
        {"file_path": RUNBOOK, "chunk_id": RUNBOOK_CHUNK, "section": "Runbook - Middleware Outage"},
        variant="argument_conflict",
    ),
    Case("search", {"query": "middleware outage", "vault": "no-such-vault"}, variant="unknown_vault"),
    Case("search_by_metadata", {"limit": 2, "offset": 1}, variant="page"),
    Case("search_by_metadata", {"tag": "service/middleware", "facets": True}, variant="facets"),
    Case(
        "get_related",
        {"file_path": MIDDLEWARE, "limit": 10, "fields": ["file_path", "doc_id", "direction", "weight"]},
        variant="fields",
    ),
    Case("get_impact", {"file_path": RUNBOOK, "max_depth": 2, "max_items": 1, "counts": True}, variant="counts"),
    Case("get_stale_docs", {"missing_last_updated": True}, variant="missing"),
    Case("list_vaults", {}),
    # A small budget over the fixture, so the pack both takes and drops
    # sections. The memory variant asks for memory with no bearer pinned in
    # the server's environment: its ``degraded`` answer is deterministic and
    # no memory server is ever read into a golden.
    Case("context_pack", {"query": "middleware outage", "budget_tokens": PACK_BUDGET}),
    Case(
        "context_pack",
        {"query": "middleware outage", "budget_tokens": PACK_BUDGET, "include_memories": True},
        variant="memory_unconfigured",
    ),
)

# Mutating variants of 1.1.0. They close the session after the 1.0.0
# mutating cases, for the same reason those close it.
V1_1_0_MUTATING_CASES: tuple[Case, ...] = (
    Case("reindex", {"target": "vault", "pull": False}, variant="no_pull"),
)

#: Tools that change state: once one runs, only these may follow it.
MUTATING_TOOLS = frozenset({"sync_confluence", "reindex"})

# The cases of 1.1.0, in the order they were recorded. Frozen with the 1.1.0
# goldens: the compatibility session replays exactly these against
# ``tests/golden/v1.1.0/``.
V1_1_0_CASES: tuple[Case, ...] = (
    *V1_0_0_CASES[:-2],
    *V1_1_0_READ_CASES,
    *V1_0_0_CASES[-2:],
    *V1_1_0_MUTATING_CASES,
)

# The cases of 1.2.0: the same session as 1.1.0, recorded against the SQLite
# store. Frozen with the 1.2.0 goldens: the compatibility session replays
# exactly these against ``tests/golden/v1.2.0/``.
V1_2_0_CASES: tuple[Case, ...] = V1_1_0_CASES

# Read-only variant of 1.3.0: ``sync_confluence`` asked to write. The fence
# answers ``E_WRITE_FENCED`` before any credential or file is read, so the
# error is deterministic and nothing changes; it runs right before the
# mutating cases, after every 1.2.0 read.
V1_3_0_READ_CASES: tuple[Case, ...] = (Case("sync_confluence", {"dry_run": False}, variant="fenced"),)

# The cases of 1.3.0: the 1.2.0 session with the fenced write before the
# three mutating cases that close it. Frozen with the 1.3.0 goldens, together
# with the session that declares ``jira`` (``JIRA_CASES``).
V1_3_0_CASES: tuple[Case, ...] = (*V1_2_0_CASES[:-3], *V1_3_0_READ_CASES, *V1_2_0_CASES[-3:])

# Read-only variant of 1.4.0: ``search(rerank="on")`` over the fixture vault,
# which declares no judge. The argument and the vault are checked before any
# search runs, so the ``E_RERANK_NOT_CONFIGURED`` error moves no counter; it
# runs after every 1.3.0 read and before the fenced write and the mutating
# cases that close the session.
V1_4_0_READ_CASES: tuple[Case, ...] = (
    Case("search", {"query": "middleware outage", "limit": 5, "rerank": "on"}, variant="rerank_unconfigured"),
)

# The cases of 1.4.0: the 1.3.0 session with the unconfigured rerank before
# the fenced write and the three mutating cases that close it.
V1_4_0_CASES: tuple[Case, ...] = (*V1_3_0_CASES[:-4], *V1_4_0_READ_CASES, *V1_3_0_CASES[-4:])

#: Tools of the extensions a vault turns on with ``features``, and the retired
#: ones: the main session (the fixture vault, no ``vault.yaml``) does not
#: register them, so their 1.4.0 cases move to the ``features`` and ``legacy``
#: sessions below.
OFF_THE_MAIN_SESSION = frozenset(
    {"get_flow", "compare_flows", "get_stale_docs", "suggest_links", "sync_confluence", "search_code"}
)

# Read-only variants of 2.0.0 in the main session: what the new defaults and
# arguments answer, and the coded errors that replace the 1.x error objects.
# They run after every 1.4.0 read, so the usage counters and link weights the
# 1.4.0 cases meet are the ones 1.4.0 recorded them with; the reads among them
# only move counters that the cases after them do not rank by.
V2_0_0_READ_CASES: tuple[Case, ...] = (
    Case(
        "search",
        {"query": "middleware outage", "limit": 5, "fields": ["file_path", "title", "score", "excerpt"]},
        variant="excerpt",
    ),
    Case("search", {"query": "middleware outage", "limit": 51}, variant="limit_out_of_range"),
    Case("get_document", {"chunk_id": RUNBOOK_CHUNK}, variant="by_chunk"),
    Case("get_document", {"file_path": RUNBOOK, "full_metadata": True}, variant="full_metadata"),
    Case("get_document", {"file_path": "Nowhere/Ghost.md"}, variant="missing"),
    Case("get_document", {"file_path": RUNBOOK, "section": "No Such Section"}, variant="section_missing"),
    Case("get_document", {}, variant="no_target"),
    Case("get_document", {"file_path": RUNBOOK, "include_metadata": True}, variant="include_metadata"),
    Case("search_by_metadata", {}, variant="all"),
    Case("get_related", {"file_path": PORTAL, "limit": 6, "include_suggested": True}, variant="suggested"),
    Case("find_path", {"from_path": "Nowhere/Ghost.md", "to_path": RUNBOOK}, variant="missing"),
)

# The calls a client with a cached 1.x catalog still makes. The server refuses
# both before any tool runs, so no counter moves; they run after every other
# read and before the two mutating cases that close the session.
V2_0_0_REFUSED_CASES: tuple[Case, ...] = (
    Case("search_code", {"query": "anything"}, variant="retired"),
    Case("get_flow", {"provider": "Telvia"}, variant="feature_disabled"),
)

_MAIN_1_4_0: tuple[Case, ...] = tuple(case for case in V1_4_0_CASES if case.tool not in OFF_THE_MAIN_SESSION)

# Mutating variant of 2.0.0: ``reindex`` pulls only with ``pull=true``, so the
# pull is asked for; the fixture vault declares no repository and the answer
# says so. It closes the session after the two mutating 1.4.0 cases.
V2_0_0_MUTATING_CASES: tuple[Case, ...] = (Case("reindex", {"target": "vault", "pull": True}, variant="pull"),)

# The cases of 2.0.0's main session: the 1.4.0 cases of the core tools, in
# their order, then the 2.0.0 reads and the refused calls before the mutating
# cases.
V2_0_0_CASES: tuple[Case, ...] = (
    *_MAIN_1_4_0[:-2],
    *V2_0_0_READ_CASES,
    *V2_0_0_REFUSED_CASES,
    *_MAIN_1_4_0[-2:],
    *V2_0_0_MUTATING_CASES,
)

# The cases of the current contract version. Newer versions add to the frozen
# cases and never edit them: the compatibility session replays exactly
# ``V1_4_0_CASES`` (with ``JIRA_CASES`` and ``RERANK_CASES``) against
# ``tests/golden/v1.4.0/``, each golden migrated by the declared 2.0 rules.
CASES: tuple[Case, ...] = V2_0_0_CASES

FROZEN_V1_0_0 = GOLDEN_ROOT / "v1.0.0"
V1_0_0_NAMES: tuple[str, ...] = (CATALOG, *(case.golden for case in V1_0_0_CASES))

FROZEN_V1_1_0 = GOLDEN_ROOT / "v1.1.0"
V1_1_0_NAMES: tuple[str, ...] = (INITIALIZE, CATALOG, *(case.golden for case in V1_1_0_CASES))

FROZEN_V1_2_0 = GOLDEN_ROOT / "v1.2.0"
V1_2_0_NAMES: tuple[str, ...] = (INITIALIZE, CATALOG, *(case.golden for case in V1_2_0_CASES))

FROZEN_V1_3_0 = GOLDEN_ROOT / "v1.3.0"
V1_3_0_NAMES: tuple[str, ...] = (INITIALIZE, CATALOG, *(case.golden for case in V1_3_0_CASES))

FROZEN_V1_4_0 = GOLDEN_ROOT / "v1.4.0"
V1_4_0_NAMES: tuple[str, ...] = (INITIALIZE, CATALOG, *(case.golden for case in V1_4_0_CASES))

GOLDEN_NAMES: tuple[str, ...] = (INITIALIZE, CATALOG, *(case.golden for case in CASES))

# The session with a gated extension on. The suite vault declares no
# extension, so the main session proves ``sync_jira`` stays out of
# ``tools_list``; this second session serves a copy of the fixture vault whose
# ``vault.yaml`` declares ``jira``, with the credential variables it names
# pinned empty, and records the catalog with the tool and its answer without
# credentials. Nothing is read from Jira or memory: the answer is the
# ``E_SECRET_MISSING`` every person without the variables gets, and names
# them. A recording against a fake Jira is left out on purpose: its ``since``
# and ``next_since`` are date-times, which ``_CLOCK_BOUND`` refuses in a
# golden; ``tests/test_jira_sync.py`` and the shape freeze cover that answer.
JIRA_CATALOG = "tools_list__jira"
JIRA_MANIFEST = (
    "slug: cloudfrog\n"
    "features: [jira]\n"
    "memory:\n  project: cloudfrog\n"
    "integrations:\n"
    "  jira:\n"
    "    site: https://jira.invalid\n"
    "    projects: [EXMP]\n"
    "    secrets: {email_env: CLOUDFROG_JIRA_EMAIL, token_env: CLOUDFROG_JIRA_TOKEN}\n"
)
JIRA_CASES: tuple[Case, ...] = (Case("sync_jira", {}, variant="unconfigured"),)
JIRA_GOLDEN_NAMES: tuple[str, ...] = (JIRA_CATALOG, *(case.golden for case in JIRA_CASES))

# The session with a judge. It serves a copy of the fixture vault whose
# ``vault.yaml`` declares a ``kev`` judge at a loopback double of System One
# (``tests/systemone_fakes.py``) that answers each passage with the Jaccard
# similarity of its terms and the query's: deterministic, no network, no key
# of anyone's. What the double answers is chosen per query by a marker
# (``[529]``), never by state shared between requests, and no case uses its
# ``delay`` mode, so no clock enters a golden.
#
# The session opens with the 1.0.0 ``get_document`` read (not recorded
# again), so the usage counters ``search__rerank_off`` meets are the ones the
# main ``search`` met and both return the same rows. ``search__rerank`` and
# ``search__rerank_fields`` share their query on purpose: the second is
# answered from the cache (``cache_hits`` equals ``units``) and names the
# block the judge chose. ``search__rerank_down`` asks a query no case judged
# before, so the cache cannot hide the 529 the double answers.
RERANK_QUERY = "middleware outage"
RERANK_WARMUP: tuple[Case, ...] = (Case("get_document", {"file_path": RUNBOOK}),)
RERANK_CASES: tuple[Case, ...] = (
    Case("search", {"query": RERANK_QUERY, "limit": 5, "rerank": "off"}, variant="rerank_off"),
    Case("search", {"query": RERANK_QUERY, "limit": 5, "rerank": "on"}, variant="rerank"),
    Case(
        "search",
        {
            "query": RERANK_QUERY,
            "limit": 5,
            "rerank": "on",
            "fields": ["file_path", "score", "chunk_id", "section", "snippet", "rerank_score"],
            "snippet": True,
        },
        variant="rerank_fields",
    ),
    Case("search", {"query": "otp session [529]", "limit": 3, "rerank": "on"}, variant="rerank_down"),
)
RERANK_GOLDEN_NAMES: tuple[str, ...] = tuple(case.golden for case in RERANK_CASES)
#: The ``rerank`` block of the session's ``vault.yaml``; ``{base_url}`` is the double's origin.
RERANK_MANIFEST = (
    "rerank:\n"
    "  provider: kev\n"
    "  model: kev-latest\n"
    "  key_env: KEV_API_KEY\n"
    "  default: off\n"
    "  candidates: 5\n"
    "  base_url: {base_url}\n"
)

# The ``structured`` session: the main cases with ``KNOWLEDGE_STRUCTURED_OUTPUT=1``.
# Only its catalog is recorded, as ``tools_list__structured`` (the bytes of the
# catalog with ``outputSchema``); its responses are checked against the main
# goldens, which they must equal once ``structuredContent`` is set aside.
STRUCTURED_CATALOG = "tools_list__structured"

# The ``features`` session: a copy of the fixture vault whose ``vault.yaml``
# declares the three extensions that bring tools over HTTP and stdio alike,
# with the same slug and no ``review`` block, so nothing but the catalog
# differs from the main session. Confluence credentials stay empty: over stdio
# that only warns, and ``sync_confluence`` answers ``E_SECRET_MISSING``. Its
# cases are the 1.4.0 cases of those tools, in their order, with the flow
# tools asked for a provider the vault has no flow for before the two
# ``sync_confluence`` cases that close it.
FEATURES = frozenset({"flows", "governance", "confluence"})
FEATURES_MANIFEST = "slug: cloudfrog\nfeatures: [flows, governance, confluence]\n"
FEATURES_CATALOG = "tools_list__features"
_FEATURES_1_4_0: tuple[Case, ...] = tuple(
    case for case in V1_4_0_CASES if case.tool in OFF_THE_MAIN_SESSION - {"search_code"}
)
FEATURES_2_0_READ_CASES: tuple[Case, ...] = (
    Case("get_flow", {"provider": "Nobody"}, variant="missing"),
    Case("compare_flows", {"provider1": "Telvia", "provider2": "Nobody"}, variant="missing"),
)
FEATURES_CASES: tuple[Case, ...] = (
    *(case for case in _FEATURES_1_4_0 if case.tool not in MUTATING_TOOLS),
    *FEATURES_2_0_READ_CASES,
    *(case for case in _FEATURES_1_4_0 if case.tool in MUTATING_TOOLS),
)
FEATURES_GOLDEN_NAMES: tuple[str, ...] = (FEATURES_CATALOG, *(case.golden for case in FEATURES_CASES))

# The ``legacy`` session: the fixture vault with ``KNOWLEDGE_PROFILE=legacy``.
# Its catalog lists the tools of 1.4.0 by name and in order; ``search_code``
# answers its redirect and ``get_flow`` answers with no ``features`` at all.
LEGACY_CATALOG = "tools_list__legacy"
LEGACY_CASES: tuple[Case, ...] = (
    Case("search_code", {"query": "anything"}),
    Case("get_flow", {"provider": "Telvia"}, variant="legacy"),
)
LEGACY_GOLDEN_NAMES: tuple[str, ...] = (LEGACY_CATALOG, *(case.golden for case in LEGACY_CASES))

#: Every golden of the current version, all six sessions.
ALL_GOLDEN_NAMES: tuple[str, ...] = (
    *GOLDEN_NAMES,
    STRUCTURED_CATALOG,
    *FEATURES_GOLDEN_NAMES,
    *LEGACY_GOLDEN_NAMES,
    *JIRA_GOLDEN_NAMES,
    *RERANK_GOLDEN_NAMES,
)

# Anything that would tie a golden to the machine that recorded it. Absolute
# path roots on the three platforms the suite runs on, plus the placeholder
# key above: a golden is reviewed in a pull request and read by every clone.
_MACHINE_BOUND = (
    re.compile(re.escape(PLACEHOLDER_KEY)),
    re.compile(r"(?<![A-Za-z0-9_./-])/(?:Users|home|private|tmp|var|opt)/"),
    re.compile(r"\b[A-Za-z]:\\"),
)

# Anything that would tie a golden to the moment it was recorded. The frozen
# calendar makes a bare date legitimate (``2026-06-01``); a time of day or a
# Unix timestamp can only come from a live clock the recording did not pin.
#
# * ISO-8601 date and time, ``T`` or space separated (``str(datetime)`` uses
#   the space).
# * Unix epoch in seconds with a fraction (10 digits, a point, digits) or in
#   milliseconds (13 digits). Neither may be glued to a word character or a
#   point, so the fractional digits of a score such as ``0.1234567890123``
#   are not mistaken for a timestamp.
_CLOCK_BOUND = (
    re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}"),
    re.compile(r"(?<![\w.])(?:\d{10}\.\d+|\d{13})(?![\w.])"),
)


# --- capture ---------------------------------------------------------------


def _dump(payload: Any) -> bytes:
    """Compact JSON, sorted keys, UTF-8, one trailing newline. The golden format."""
    return (json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def _wire(model: Any) -> Any:
    """A result as the SDK puts it on the wire: aliases on, unset fields off."""
    return model.model_dump(mode="json", by_alias=True, exclude_none=True)


def _server_environment(chroma_dir: Path, seed: str) -> dict[str, str]:
    return {
        "VAULT_PATH": str(FIXTURE_VAULT),
        "CHROMA_PERSIST_DIR": str(chroma_dir),
        "OPENAI_API_KEY": PLACEHOLDER_KEY,
        "MCP_TRANSPORT": "stdio",
        # Explicitly empty, not merely absent: ``config`` loads a ``.env`` from
        # the project root when one exists, and dotenv never overrides a
        # variable that is already set. Without this a developer's local
        # Confluence credentials would turn the recorded error into a report.
        "CONFLUENCE_URL": "",
        "CONFLUENCE_EMAIL": "",
        "CONFLUENCE_TOKEN": "",
        # The same reason for memory: a developer's ``MEMORY_HTTP_TOKEN`` would
        # let ``context_pack`` read a real memory server into a golden.
        "MEMORY_HTTP_TOKEN": "",
        "KNOWLEDGE_MEMORY_URL": "",
        # The output switches and the tool profile, empty so a ``.env`` cannot
        # flip them: the goldens are the defaults (compact text alone, the
        # ``all`` profile). A session that needs another value overrides it.
        "KNOWLEDGE_STRUCTURED_OUTPUT": "",
        "KNOWLEDGE_TEXT_INDENT": "",
        "KNOWLEDGE_PROFILE": "",
        # Set-walk order is part of several responses; see the module docstring.
        "PYTHONHASHSEED": seed,
        # The vectors are ranked by sqlite-vec or the server does not start.
        stores.REQUIRE_SQLITE_VEC_ENV: "1",
    }


async def _capture(
    chroma_dir: Path, seed: str, cases: tuple[Case, ...] = CASES, env: dict[str, str] | None = None
) -> dict[str, bytes]:
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER_SCRIPT)],
        env={**_server_environment(chroma_dir, seed), **(env or {})},
        cwd=str(PROJECT_ROOT),
    )
    captured: dict[str, bytes] = {}
    async with stdio_client(params) as (read, write):
        # The first tool call indexes the fixture vault from scratch; generous
        # on purpose so a slow CI runner fails on content, never on a clock.
        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=180)) as session:
            init = await session.initialize()
            captured[INITIALIZE] = _dump({"instructions": init.instructions, "serverInfo": {"name": init.serverInfo.name}})
            captured[SERVER_VERSION] = init.serverInfo.version.encode("utf-8")
            captured[SERVER_ICONS] = _dump([icon.model_dump(mode="json", exclude_none=True) for icon in init.serverInfo.icons or []])
            captured[CATALOG] = _dump(_wire(await session.list_tools()))
            for case in cases:
                result = await session.call_tool(case.tool, arguments=case.arguments)
                captured[case.golden] = _dump(_wire(result))
    return captured


@pytest.fixture(scope="module")
def recorded(tmp_path_factory: pytest.TempPathFactory) -> dict[str, bytes]:
    """Every golden's live bytes, from one server process and one session."""
    chroma_dir = tmp_path_factory.mktemp("golden-chroma")
    return asyncio.run(_capture(chroma_dir, RECORDING_SEED))


@pytest.fixture(scope="module")
def recorded_jira(tmp_path_factory: pytest.TempPathFactory) -> dict[str, bytes]:
    """The session with ``jira`` declared: its catalog as ``tools_list__jira`` and its cases."""
    vault = tmp_path_factory.mktemp("golden-jira") / "vault"
    shutil.copytree(FIXTURE_VAULT, vault)
    (vault / "vault.yaml").write_text(JIRA_MANIFEST, encoding="utf-8")
    env = {
        "VAULT_PATH": str(vault),
        "CLOUDFROG_JIRA_EMAIL": "",
        "CLOUDFROG_JIRA_TOKEN": "",
        # jira.invalid never resolves; the operator's list lets the manifest name it.
        "KNOWLEDGE_INTEGRATION_SITES": "jira.invalid",
        "KNOWLEDGE_MEMORY_REQUIRE_TOKEN": "",
    }
    captured = asyncio.run(_capture(tmp_path_factory.mktemp("golden-chroma-jira"), RECORDING_SEED, JIRA_CASES, env))
    return {JIRA_CATALOG: captured[CATALOG], **{case.golden: captured[case.golden] for case in JIRA_CASES}}


def _capture_rerank(tmp_path_factory: pytest.TempPathFactory, seed: str) -> dict[str, bytes]:
    """The session with a judge, against a loopback double started for it; only its own goldens."""
    from systemone_fakes import API_KEY, FakeSystemOne, systemone_server

    vault = tmp_path_factory.mktemp("golden-rerank") / "vault"
    shutil.copytree(FIXTURE_VAULT, vault)
    with systemone_server(FakeSystemOne()) as (origin, _state):
        (vault / "vault.yaml").write_text(RERANK_MANIFEST.format(base_url=origin), encoding="utf-8")
        env = {"VAULT_PATH": str(vault), "KNOWLEDGE_GOLDEN_KEV_API_KEY": API_KEY}
        captured = asyncio.run(
            _capture(tmp_path_factory.mktemp("golden-chroma-rerank"), seed, (*RERANK_WARMUP, *RERANK_CASES), env)
        )
    return {name: captured[name] for name in RERANK_GOLDEN_NAMES}


@pytest.fixture(scope="module")
def recorded_rerank(tmp_path_factory: pytest.TempPathFactory) -> dict[str, bytes]:
    """The session with a judge: ``search`` with ``rerank`` off, on, with pointers and with the judge down."""
    return _capture_rerank(tmp_path_factory, RECORDING_SEED)


@pytest.fixture(scope="module")
def recorded_structured(tmp_path_factory: pytest.TempPathFactory) -> dict[str, bytes]:
    """The main session with ``KNOWLEDGE_STRUCTURED_OUTPUT=1``: text plus its structured view."""
    chroma_dir = tmp_path_factory.mktemp("golden-chroma-structured")
    return asyncio.run(_capture(chroma_dir, RECORDING_SEED, env={"KNOWLEDGE_STRUCTURED_OUTPUT": "1"}))


def _features_vault(tmp_path_factory: pytest.TempPathFactory, name: str) -> Path:
    """A copy of the fixture vault whose ``vault.yaml`` declares :data:`FEATURES`."""
    vault = tmp_path_factory.mktemp(name) / "vault"
    shutil.copytree(FIXTURE_VAULT, vault)
    (vault / "vault.yaml").write_text(FEATURES_MANIFEST, encoding="utf-8")
    return vault


def _capture_features(tmp_path_factory: pytest.TempPathFactory, seed: str) -> dict[str, bytes]:
    vault = _features_vault(tmp_path_factory, "golden-features")
    captured = asyncio.run(
        _capture(tmp_path_factory.mktemp("golden-chroma-features"), seed, FEATURES_CASES, {"VAULT_PATH": str(vault)})
    )
    return {FEATURES_CATALOG: captured[CATALOG], **{case.golden: captured[case.golden] for case in FEATURES_CASES}}


def _capture_legacy(tmp_path_factory: pytest.TempPathFactory, seed: str) -> dict[str, bytes]:
    captured = asyncio.run(
        _capture(tmp_path_factory.mktemp("golden-chroma-legacy"), seed, LEGACY_CASES, {"KNOWLEDGE_PROFILE": "legacy"})
    )
    return {LEGACY_CATALOG: captured[CATALOG], **{case.golden: captured[case.golden] for case in LEGACY_CASES}}


@pytest.fixture(scope="module")
def recorded_features(tmp_path_factory: pytest.TempPathFactory) -> dict[str, bytes]:
    """The session whose vault declares ``flows``, ``governance`` and ``confluence``."""
    return _capture_features(tmp_path_factory, RECORDING_SEED)


@pytest.fixture(scope="module")
def recorded_legacy(tmp_path_factory: pytest.TempPathFactory) -> dict[str, bytes]:
    """The session under ``KNOWLEDGE_PROFILE=legacy``."""
    return _capture_legacy(tmp_path_factory, RECORDING_SEED)


#: What the replay of the 1.4.0 main session serves: the vault of the
#: ``features`` session, so every 1.4.0 case runs, in one session and in its
#: order, with the usage counters and link weights it was recorded with.
COMPAT_SESSION = ReplaySession(features=FEATURES)
#: The 1.4.0 ``jira`` session declares ``jira`` alone; the judge's declares no feature.
JIRA_SESSION = ReplaySession(features=frozenset({"jira"}))
RERANK_SESSION = ReplaySession()


@pytest.fixture(scope="module")
def compat_recorded_1_4_0(tmp_path_factory: pytest.TempPathFactory) -> dict[str, bytes]:
    """The 1.4.0 main session replayed whole, in its order, in a process of its own.

    It serves the vault of the ``features`` session (the fixture with
    ``flows``, ``governance`` and ``confluence`` declared, same slug, no
    ``review`` block), so no case is split off into another session: every
    ``search`` meets the usage counters 1.4.0 recorded it with, and
    ``search_code`` answers ``E_TOOL_RETIRED`` (R15).
    """
    vault = _features_vault(tmp_path_factory, "golden-compat-1-4-0")
    chroma_dir = tmp_path_factory.mktemp("golden-chroma-compat-1-4-0")
    return asyncio.run(_capture(chroma_dir, RECORDING_SEED, V1_4_0_CASES, {"VAULT_PATH": str(vault)}))


@pytest.fixture(scope="module")
def replayed_under_second_seed(tmp_path_factory: pytest.TempPathFactory) -> dict[str, bytes]:
    """The same session replayed by a fresh process under :data:`SECOND_SEED`.

    It indexes into its own directory, so nothing the recording session
    persisted can leak into the replay and mask a difference.
    """
    chroma_dir = tmp_path_factory.mktemp("golden-chroma-second-seed")
    return asyncio.run(_capture(chroma_dir, SECOND_SEED))


def _random_seed() -> str:
    """The seed of the random replay: :data:`RANDOM_SEED_ENV` when set, else a fresh draw."""
    pinned = os.environ.get(RANDOM_SEED_ENV, "").strip()
    if pinned:
        return pinned
    return str(random.SystemRandom().randint(1, _MAX_HASH_SEED))


# --- comparison ------------------------------------------------------------


def _readable(blob: bytes) -> list[str]:
    """Pretty-print a golden for the diff only; comparison stays byte-exact."""
    return json.dumps(json.loads(blob), indent=2, sort_keys=True, ensure_ascii=False).splitlines(keepends=True)


def _diff(expected: bytes, actual: bytes, *, fromfile: str, tofile: str) -> str:
    return "".join(difflib.unified_diff(_readable(expected), _readable(actual), fromfile=fromfile, tofile=tofile))


def _golden_path(name: str) -> Path:
    return GOLDEN_DIR / f"{name}.json"


def _texts(name: str) -> list[Any]:
    """The parsed JSON of every text block of a recorded ``tools/call`` result: the canonical answer."""
    payload = json.loads(_golden_path(name).read_bytes())
    return [json.loads(block["text"]) for block in payload["content"]]


def _rows(name: str) -> list[dict[str, Any]]:
    """The rows of a tool that answers a list: one text block per row."""
    rows: list[dict[str, Any]] = _texts(name)
    return rows


def _object_result(name: str) -> dict[str, Any]:
    """The answer of a tool that returns one object: its only text block."""
    blocks = _texts(name)
    assert len(blocks) == 1, f"{name}: {len(blocks)} text blocks for an object answer"
    result: dict[str, Any] = blocks[0]
    return result


def _changelog_versions() -> list[str]:
    """The ``## <version>`` headings of the contract changelog, top to bottom."""
    return re.findall(r"^## (\S+)\s*$", CHANGELOG.read_text(encoding="utf-8"), flags=re.MULTILINE)


@pytest.mark.parametrize("name", ALL_GOLDEN_NAMES)
def test_golden_matches_the_live_contract(
    recorded: dict[str, bytes],
    recorded_structured: dict[str, bytes],
    recorded_features: dict[str, bytes],
    recorded_legacy: dict[str, bytes],
    recorded_jira: dict[str, bytes],
    recorded_rerank: dict[str, bytes],
    name: str,
) -> None:
    path = _golden_path(name)
    if name == STRUCTURED_CATALOG:
        actual = recorded_structured[CATALOG]
    elif name in FEATURES_GOLDEN_NAMES:
        actual = recorded_features[name]
    elif name in LEGACY_GOLDEN_NAMES:
        actual = recorded_legacy[name]
    elif name in JIRA_GOLDEN_NAMES:
        actual = recorded_jira[name]
    elif name in RERANK_GOLDEN_NAMES:
        actual = recorded_rerank[name]
    else:
        actual = recorded[name]

    if RECORDING:
        assert not (GOLDEN_DIR / FROZEN_SUMS).exists(), (
            f"{GOLDEN_DIR.relative_to(PROJECT_ROOT)} is frozen ({FROZEN_SUMS}); a frozen version is never re-recorded"
        )
        opening = _changelog_versions()[:1]
        assert opening == [contract.CONTRACT_VERSION], (
            f"{CHANGELOG.relative_to(PROJECT_ROOT)} opens with {opening}, not "
            f"## {contract.CONTRACT_VERSION}; write the change down before recording its goldens"
        )
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        path.write_bytes(actual)

    assert path.is_file(), (
        f"no golden recorded for {name!r} at {path.relative_to(PROJECT_ROOT)}; "
        f"record it with {RECORD_ENV}=1"
    )
    expected = path.read_bytes()
    if actual == expected:
        return

    diff = _diff(expected, actual, fromfile=f"{path.relative_to(PROJECT_ROOT)} (recorded)", tofile=f"{name} (live)")
    pytest.fail(
        f"the live {name!r} contract differs from its golden master. "
        f"If the change is intended, re-record with {RECORD_ENV}=1 and review the "
        f"diff as a contract change.\n{diff}",
        pytrace=False,
    )


@pytest.mark.parametrize(
    ("catalog", "cases", "refused"),
    [
        (CATALOG, CASES, V2_0_0_REFUSED_CASES),
        (FEATURES_CATALOG, (*_MAIN_1_4_0, *FEATURES_CASES), ()),
        (LEGACY_CATALOG, (*_MAIN_1_4_0, *FEATURES_CASES, *LEGACY_CASES), ()),
        (JIRA_CATALOG, (*CASES, *JIRA_CASES), V2_0_0_REFUSED_CASES),
    ],
    ids=["main", "features", "legacy", "jira"],
)
def test_every_registered_tool_has_a_golden_case(catalog: str, cases: tuple[Case, ...], refused: tuple[Case, ...]) -> None:
    """Each session's catalog and the cases recorded for it are the same set of tools.

    A tool that reaches a registry without a case would ship with its
    response unrecorded; a case naming a tool the session does not serve is
    a golden for nothing, unless it records the refusal itself.
    """
    advertised = {tool["name"] for tool in json.loads(_golden_path(catalog).read_bytes())["tools"]}
    cased = {case.tool for case in cases if case not in refused}

    assert advertised == cased, (
        f"registered without a golden case: {sorted(advertised - cased)}; "
        f"cased but not registered: {sorted(cased - advertised)}"
    )
    assert not {case.tool for case in refused} & advertised


def test_golden_directory_holds_exactly_the_recorded_set() -> None:
    """No orphan file survives a tool's removal, and no golden is missing."""
    present = {p.stem for p in GOLDEN_DIR.glob("*.json")}
    expected = set(ALL_GOLDEN_NAMES)

    assert present == expected, (
        f"orphaned goldens: {sorted(present - expected)}; missing goldens: {sorted(expected - present)}"
    )


def test_goldens_are_compact_sorted_json() -> None:
    """A hand edit that re-indents or reorders a golden is not a contract change."""
    for name in ALL_GOLDEN_NAMES:
        blob = _golden_path(name).read_bytes()
        assert blob == _dump(json.loads(blob)), f"{name}.json is not in the canonical golden format"


def test_goldens_carry_nothing_machine_bound() -> None:
    """A golden is shared through version control, so it is bound to no machine.

    Neither the absolute path of the clone that recorded it, nor the
    throwaway index directory, nor the placeholder key the server was
    started with may appear in it.
    """
    offenders: list[str] = []
    for name in ALL_GOLDEN_NAMES:
        text = _golden_path(name).read_text(encoding="utf-8")
        for pattern in _MACHINE_BOUND:
            for match in pattern.finditer(text):
                offenders.append(f"{name}.json: {match.group(0)!r}")

    assert offenders == [], f"machine-bound content in goldens: {offenders}"


def _clock_bound_offenders(paths: Iterable[Path]) -> list[str]:
    """Every live-clock value in ``paths``, as ``"<file>: '<text>'"``."""
    offenders: list[str] = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for pattern in _CLOCK_BOUND:
            for match in pattern.finditer(text):
                offenders.append(f"{path.name}: {match.group(0)!r}")
    return offenders


def test_goldens_carry_nothing_clock_bound() -> None:
    """A golden is bound to no moment: no time of day, no Unix timestamp.

    The server's calendar is frozen, so a date alone is reproducible. A
    datetime or an epoch is not: it is read from a clock the recording does
    not control, and the golden would turn red on the next run.
    """
    offenders = _clock_bound_offenders(_golden_path(name) for name in ALL_GOLDEN_NAMES)

    assert offenders == [], f"clock-bound content in goldens: {offenders}"


@pytest.mark.parametrize(
    ("payload", "leaked"),
    [
        ({"result": {"indexed_at": "2026-06-01T09:30:00Z"}}, "2026-06-01T09:30"),
        ({"result": {"indexed_at": "2026-06-01 09:30:00.123456"}}, "2026-06-01 09:30"),
        ({"result": {"indexed_at": 1780306200.25}}, "1780306200.25"),
        ({"result": {"indexed_at": 1780306200250}}, "1780306200250"),
    ],
    ids=["iso-datetime", "iso-datetime-space", "epoch-seconds-fraction", "epoch-milliseconds"],
)
def test_clock_guard_detects_a_live_clock(tmp_path: Path, payload: dict[str, Any], leaked: str) -> None:
    golden = tmp_path / "synthetic.json"
    golden.write_bytes(_dump(payload))

    offenders = _clock_bound_offenders([golden])

    assert len(offenders) == 1, offenders
    assert offenders[0].startswith("synthetic.json: ")
    assert leaked in offenders[0]


def test_clock_guard_lets_dates_and_scores_through(tmp_path: Path) -> None:
    """The values a golden legitimately carries are not mistaken for a clock."""
    golden = tmp_path / "synthetic.json"
    golden.write_bytes(
        _dump(
            {
                "result": {
                    "last_reviewed": "2026-06-01",
                    "score": 0.1234567890123,
                    "small_score": 1.2345678901234e-05,
                    "count": 1780306200,
                    "file_path": "Runbooks/Runbook - 2026-06-01 Outage.md",
                }
            }
        )
    )

    assert _clock_bound_offenders([golden]) == []


def test_tag_filter_golden_is_not_empty() -> None:
    """The ``tag`` filter of ``search_by_metadata`` is pinned on real matches.

    An empty list is a valid response shape, so the byte comparison alone
    would pin a broken filter just as readily. Every document returned must
    carry the requested tag.
    """
    case = next(case for case in CASES if case.golden == "search_by_metadata__tag")
    tag = case.arguments["tag"]
    response = json.loads(_golden_path(case.golden).read_bytes())

    assert response["isError"] is False
    documents = _object_result(case.golden)["items"]
    assert documents, f"search_by_metadata(tag={tag!r}) is recorded with no documents"
    assert all(tag in document["tags"] for document in documents), documents


def _seed_differences(replayed: dict[str, bytes], seed: str) -> tuple[list[str], list[str]]:
    """Goldens whose replay under ``seed`` differs from the recording, with diffs."""
    differing: list[str] = []
    diffs: list[str] = []
    for name in GOLDEN_NAMES:
        if name.split("__", 1)[0] in SEED_DEPENDENT_TOOLS:
            continue
        expected = _golden_path(name).read_bytes()
        actual = replayed[name]
        if actual != expected:
            differing.append(name)
            diffs.append(
                _diff(
                    expected,
                    actual,
                    fromfile=f"{name}.json (PYTHONHASHSEED={RECORDING_SEED})",
                    tofile=f"{name} (PYTHONHASHSEED={seed})",
                )
            )
    return differing, diffs


def test_the_features_and_legacy_sessions_are_independent_of_the_hash_seed(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """The two sessions that only 2.0 records, replayed under :data:`SECOND_SEED`, give the same bytes."""
    replayed = {**_capture_features(tmp_path_factory, SECOND_SEED), **_capture_legacy(tmp_path_factory, SECOND_SEED)}

    differing = [name for name in (*FEATURES_GOLDEN_NAMES, *LEGACY_GOLDEN_NAMES) if replayed[name] != _golden_path(name).read_bytes()]
    assert differing == [], f"{differing} depend on PYTHONHASHSEED"


def test_every_golden_is_independent_of_the_hash_seed(replayed_under_second_seed: dict[str, bytes]) -> None:
    """Replaying the session under another hash seed changes no byte.

    Every golden, the catalog included, must come back identical under
    :data:`SECOND_SEED`. A difference is a response that follows the walk
    order of a Python ``set`` instead of a declared sort key.
    """
    differing, diffs = _seed_differences(replayed_under_second_seed, SECOND_SEED)

    assert not differing, f"{differing} depend on PYTHONHASHSEED:\n" + "\n".join(diffs)


@pytest.mark.slow
def test_every_golden_is_independent_of_a_random_hash_seed(
    tmp_path_factory: pytest.TempPathFactory, record_property: Any
) -> None:
    """The same check under a seed drawn per run, so 0 and 7 cannot agree by chance.

    The seed is recorded as a test property and printed on failure; setting
    ``KNOWLEDGE_GOLDEN_SEED`` to it replays exactly that run.
    """
    seed = _random_seed()
    record_property("pythonhashseed", seed)
    chroma_dir = tmp_path_factory.mktemp("golden-chroma-random-seed")
    replayed = asyncio.run(_capture(chroma_dir, seed))

    differing, diffs = _seed_differences(replayed, seed)

    assert not differing, (
        f"{differing} depend on PYTHONHASHSEED={seed}; replay with {RANDOM_SEED_ENV}={seed}:\n" + "\n".join(diffs)
    )


def test_the_random_seed_can_be_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(RANDOM_SEED_ENV, "12345")

    assert _random_seed() == "12345"


def test_the_random_seed_is_a_valid_hash_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(RANDOM_SEED_ENV, raising=False)

    assert 1 <= int(_random_seed()) <= _MAX_HASH_SEED


def test_no_tool_is_allowed_to_depend_on_the_hash_seed() -> None:
    assert SEED_DEPENDENT_TOOLS == frozenset()


# --- frozen versions ---------------------------------------------------------


def _frozen_sums(directory: Path) -> dict[str, str]:
    """``SHA256SUMS`` of a frozen directory: file name -> hex digest."""
    sums: dict[str, str] = {}
    for line in (directory / FROZEN_SUMS).read_text(encoding="utf-8").splitlines():
        digest, _, name = line.partition("  ")
        sums[name.strip()] = digest.strip()
    return sums


@pytest.mark.parametrize(
    ("directory", "names"),
    [
        (FROZEN_V1_0_0, V1_0_0_NAMES),
        (FROZEN_V1_1_0, V1_1_0_NAMES),
        (FROZEN_V1_2_0, V1_2_0_NAMES),
        (FROZEN_V1_3_0, (*V1_3_0_NAMES, *JIRA_GOLDEN_NAMES)),
        (FROZEN_V1_4_0, (*V1_4_0_NAMES, *JIRA_GOLDEN_NAMES, *RERANK_GOLDEN_NAMES)),
    ],
    ids=["v1.0.0", "v1.1.0", "v1.2.0", "v1.3.0", "v1.4.0"],
)
def test_each_frozen_version_lists_exactly_its_cases(directory: Path, names: tuple[str, ...]) -> None:
    """Every case of a frozen version has its golden in ``SHA256SUMS``, and nothing else does.

    The bytes themselves are checked by ``tests/test_contract_frozen.py``,
    which runs in any Python, with or without sqlite-vec.
    """
    assert set(_frozen_sums(directory)) == {f"{name}.json" for name in names}


def test_every_1_4_0_golden_migrates_to_2_0(
    compat_recorded_1_4_0: dict[str, bytes], recorded_jira: dict[str, bytes], recorded_rerank: dict[str, bytes]
) -> None:
    """Every 1.4.0 golden, rewritten by the declared 2.0 rules, is what 2.0 answers.

    The three 1.4.0 sessions are replayed against the live server. Each frozen
    golden first goes through ``migrate_1_4_to_2_0`` (the ``R<n>`` rules of
    ``MIGRATIONS_2_0`` and nothing else) and is then compared under the rules
    of ``golden_compat``: every key it keeps comes back with the same value.
    """
    arguments = {case.golden: case.arguments for case in (*V1_4_0_CASES, *JIRA_CASES, *RERANK_CASES)}
    problems: dict[str, list[str]] = {}
    for name in (*V1_4_0_NAMES, *JIRA_GOLDEN_NAMES, *RERANK_GOLDEN_NAMES):
        if name in JIRA_GOLDEN_NAMES:
            session, live = JIRA_SESSION, json.loads(recorded_jira[name])
        elif name in RERANK_GOLDEN_NAMES:
            session, live = RERANK_SESSION, json.loads(recorded_rerank[name])
        else:
            session, live = COMPAT_SESSION, json.loads(compat_recorded_1_4_0[name])
        frozen = migrate_1_4_to_2_0(
            name, json.loads((FROZEN_V1_4_0 / f"{name}.json").read_bytes()), arguments.get(name), session=session
        )
        if name == "list_vaults":
            frozen = _with_session_features(frozen, session)
        if name in (CATALOG, JIRA_CATALOG):
            found = catalog_problems(frozen, live)
        elif name == INITIALIZE:
            found = initialize_problems(frozen, live, frozen_version="1.4.0", live_version=contract.CONTRACT_VERSION)
        else:
            found = response_problems(
                name.split("__", 1)[0],
                frozen,
                live,
                frozen_version="1.4.0",
                live_version=contract.CONTRACT_VERSION,
                arguments=arguments[name],
            )
        if found:
            problems[name] = found

    assert problems == {}, "\n".join(f"{name}: {found}" for name, found in problems.items())


def _with_session_features(frozen: dict[str, Any], session: ReplaySession) -> dict[str, Any]:
    """A frozen ``list_vaults`` with the ``features`` the replay vault declares.

    Not a contract rule: the replay serves a vault that declares
    :data:`FEATURES` and the 1.4.0 recording served one that declared none,
    so the value that names them is set from the session before comparing.
    """
    blocks = []
    for block in frozen["content"]:
        listing = json.loads(block["text"])
        for row in listing["vaults"]:
            row["features"] = sorted(session.features)
        blocks.append({**block, "text": json.dumps(listing, separators=(",", ":"))})
    return {**frozen, "content": blocks}


def test_the_legacy_catalog_is_the_1_4_0_catalog_through_the_migration(recorded_legacy: dict[str, bytes]) -> None:
    """Under ``legacy`` the catalog is 1.4.0's, name by name and in order, and differs only where a rule says.

    Gate of the profile: the names and their order are those of
    ``tests/golden/v1.4.0/tools_list.json``; schemas, ``required`` and
    ``_meta`` compare by value once R19 (no ``outputSchema``) and R21 (the
    removal ``_meta`` of ``search_code``) are applied.
    """
    frozen = json.loads((FROZEN_V1_4_0 / f"{CATALOG}.json").read_bytes())
    live = json.loads(recorded_legacy[LEGACY_CATALOG])
    migrated = migrate_1_4_to_2_0(CATALOG, frozen, session=ReplaySession(profile="legacy"))

    assert [t["name"] for t in live["tools"]] == [t["name"] for t in frozen["tools"]]
    assert catalog_problems(migrated, live) == []
    assert len(live["tools"]) == len(frozen["tools"])


def test_the_main_catalog_is_the_core_of_1_4_0_in_its_order() -> None:
    """Under ``all`` the fixture vault serves the core tools, in the order 1.4.0 listed them (R16)."""
    frozen = [t["name"] for t in json.loads((FROZEN_V1_4_0 / f"{CATALOG}.json").read_bytes())["tools"]]
    main = [t["name"] for t in json.loads(_golden_path(CATALOG).read_bytes())["tools"]]
    features = [t["name"] for t in json.loads(_golden_path(FEATURES_CATALOG).read_bytes())["tools"]]

    assert main == [name for name in frozen if name not in OFF_THE_MAIN_SESSION]
    assert features == [*main, "get_flow", "compare_flows", "get_stale_docs", "suggest_links", "sync_confluence"]


def _changelog_section(version: str) -> str:
    """The text of ``## <version>`` in the contract changelog, up to the next version heading."""
    text = CHANGELOG.read_text(encoding="utf-8")
    heading = f"\n## {version}\n"
    body = text.index(heading) + len(heading)
    following = re.search(r"^## \S+\s*$", text[body:], flags=re.MULTILINE)
    return text[body : body + following.start()] if following else text[body:]


def _breaking_rules(section: str) -> list[str]:
    """The ``R<n>`` ids of the rows of the "Breaking" table of a changelog section, in order."""
    breaking = section.split("### Breaking", 1)[1].split("\n### ", 1)[0]
    return re.findall(r"^\| (R\d+) \|", breaking, flags=re.MULTILINE)


def test_every_2_0_migration_is_declared_in_the_changelog() -> None:
    """The rules the replay applies and the rows "Breaking" declares are the same set, once each."""
    declared = _breaking_rules(_changelog_section("2.0.0"))

    assert len(declared) == len(set(declared)), f"a rule is declared twice: {declared}"
    assert set(declared) == set(MIGRATIONS_2_0), (
        f"declared without a migration: {sorted(set(declared) - set(MIGRATIONS_2_0))}; "
        f"migrated without a declaration: {sorted(set(MIGRATIONS_2_0) - set(declared))}"
    )
    assert all(migration.rule == rule for rule, migration in MIGRATIONS_2_0.items())


def test_the_2_0_migration_only_touches_what_its_rules_name() -> None:
    """A golden no rule applies to comes out of the migration unchanged; the handshake only through R17."""
    frozen = {"content": [{"type": "text", "text": "{}"}], "isError": False}
    error = {"content": [{"type": "text", "text": "E_FIELD_UNKNOWN: x"}], "isError": True}

    assert migrate_1_4_to_2_0("find_path", frozen, {}) == frozen
    assert migrate_1_4_to_2_0("search", error, {}) == error
    assert migrate_1_4_to_2_0(INITIALIZE, {"instructions": "x\nold protocol"}) == {
        "instructions": "\n".join(["x", *contract.INSTRUCTIONS.splitlines()[1:]])
    }


def test_the_pointer_rules_rewrite_only_default_rows() -> None:
    """R1 drops ``excerpt`` and a title that is the file name from a call without ``fields``, and only from it."""
    row = {"file_path": "A/Note.md", "title": "Note", "score": 1.0, "excerpt": "x"}
    titled = {"file_path": "A/n.md", "title": "Note", "score": 0.5, "excerpt": "y"}
    frozen = {"content": [{"type": "text", "text": json.dumps(r)} for r in (row, titled)], "isError": False}

    default = migrate_1_4_to_2_0("search", frozen, {"query": "q"})
    projected = migrate_1_4_to_2_0("search", frozen, {"query": "q", "fields": ["file_path", "excerpt"]})

    assert [json.loads(b["text"]) for b in default["content"]] == [
        {"file_path": "A/Note.md", "score": 1.0},
        {"file_path": "A/n.md", "title": "Note", "score": 0.5},
    ]
    assert projected == frozen


def test_the_store_bound_set_is_exactly_the_ranked_tools() -> None:
    """Only the three tools that rank through the index may be compared by shape.

    Growing any of these sets weakens what every frozen version guarantees, so
    it is a contract decision and fails here until this test says so.
    """
    assert STORE_BOUND_TOOLS == frozenset({"search", "search_deep", "context_pack"})
    assert STORE_BOUND_SINCE == (1, 2, 0)
    assert VERSION_BOUND == frozenset({"list_vaults.contract", "initialize.instructions[0]"})
    assert PRESENCE_BOUND == frozenset()


@pytest.mark.parametrize("cases", [CASES, FEATURES_CASES, V1_4_0_CASES], ids=["main", "features", "replay"])
def test_the_mutating_cases_close_the_session(cases: tuple[Case, ...]) -> None:
    first = next(i for i, case in enumerate(cases) if case.tool in MUTATING_TOOLS)

    assert all(case.tool in MUTATING_TOOLS for case in cases[first:])


def test_the_main_session_keeps_the_1_4_0_order_of_its_tools() -> None:
    """Splitting the extension cases off moves no core case: they keep the 1.4.0 order and counters."""
    added = (*V2_0_0_READ_CASES, *V2_0_0_REFUSED_CASES, *V2_0_0_MUTATING_CASES)
    assert tuple(case for case in CASES if case not in added) == _MAIN_1_4_0
    first_added = min(CASES.index(case) for case in added)
    assert all(CASES.index(case) < first_added for case in _MAIN_1_4_0[:-2])
    assert [case.tool for case in CASES[-3:]] == ["reindex", "reindex", "reindex"]


def test_the_chunk_case_reads_the_pointer_search_returned() -> None:
    rows = _rows("search__pointers")
    runbook = next(row for row in rows if row["file_path"] == RUNBOOK)

    assert runbook["chunk_id"] == RUNBOOK_CHUNK
    chunk = _object_result("get_document__chunk")
    assert (chunk["chunk_id"], chunk["section"]) == (RUNBOOK_CHUNK, runbook["section"])


def test_the_default_search_row_is_a_pointer_the_chunk_read_follows() -> None:
    """R1 and R4: the default row carries the ``chunk_id`` that ``get_document(chunk_id=...)`` reads alone."""
    rows = _rows("search")
    runbook = next(row for row in rows if row["file_path"] == RUNBOOK)
    by_chunk = _object_result("get_document__by_chunk")

    assert all("excerpt" not in row and "title" not in row for row in rows)
    assert all({"file_path", "score", "chunk_id", "section"} <= set(row) for row in rows)
    assert runbook["chunk_id"] == RUNBOOK_CHUNK
    assert by_chunk == _object_result("get_document__chunk")
    assert [set(row) for row in _rows("search__excerpt")] == [{"file_path", "title", "score", "excerpt"}] * 5


#: Documents of ``tests/fixtures/vault/``: what ``search_by_metadata()`` counts with no filter.
FIXTURE_DOCUMENTS = 14


def test_the_listing_goldens_are_bounded_pages() -> None:
    """R6-R9: one page with the total, no ``file`` and no repeated document, weights to 3 decimals."""
    every = _object_result("search_by_metadata__all")
    paths = [row["file_path"] for row in every["items"]]

    assert (every["total"], every["offset"]) == (FIXTURE_DOCUMENTS, 0)
    assert len(paths) <= 20 and paths == sorted(paths)
    for name in ("search_by_metadata", "search_by_metadata__tag", "search_by_metadata__page", "search_by_metadata__facets"):
        assert set(_object_result(name)) == {"total", "offset", "items"}
    for name in ("get_related", "get_related__fields", "get_related__suggested"):
        rows = _rows(name)
        assert rows and all("file" not in row for row in rows)
        assert len({row["file_path"] for row in rows}) == len(rows)
        assert all(row["weight"] == round(row["weight"], 3) for row in rows if "weight" in row)
    suggested = [row for row in _rows("get_related__suggested") if row["direction"] == "suggested"]
    assert [row["file_path"] for row in suggested] == [RUNBOOK]


def test_the_metadata_goldens_hold_the_whitelist_and_the_whole_frontmatter() -> None:
    from knowledge import server

    default = _object_result("get_document")["metadata"]
    full = _object_result("get_document__full_metadata")["metadata"]

    assert set(default) <= set(server.DOCUMENT_METADATA_KEYS) and default
    assert {key: full[key] for key in default} == default and set(full) - set(default) == {"title", "severity"}


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("search__unknown_vault", "E_VAULT_UNKNOWN"),
        ("get_document__argument_conflict", "E_ARGUMENT_CONFLICT"),
        ("sync_confluence__fenced", "E_WRITE_FENCED"),
        ("search__rerank_unconfigured", "E_RERANK_NOT_CONFIGURED"),
        ("search_code__retired", "E_TOOL_RETIRED"),
        ("get_flow__feature_disabled", "E_FEATURE_DISABLED"),
        ("search__limit_out_of_range", "E_ARGUMENT_OUT_OF_RANGE"),
        ("get_document__missing", "E_FILE_NOT_FOUND"),
        ("get_document__section_missing", "E_SECTION_NOT_FOUND"),
        ("get_document__no_target", "E_ARGUMENT_MISSING"),
        ("get_document__include_metadata", "E_ARGUMENT_RETIRED"),
        ("find_path__missing", "E_FILE_NOT_IN_GRAPH"),
        ("sync_confluence", "E_SECRET_MISSING"),
        ("get_flow__missing", "E_FLOW_NOT_FOUND"),
        ("compare_flows__missing", "E_FLOW_NOT_FOUND"),
    ],
)
def test_error_goldens_carry_a_stable_code_and_no_trace(name: str, code: str) -> None:
    response = json.loads(_golden_path(name).read_bytes())
    text = response["content"][0]["text"]

    assert response["isError"] is True
    assert re.search(rf"\b{code}: ", text), text
    assert "Traceback" not in text


def test_order_free_tools_are_registered_tools() -> None:
    assert ORDER_FREE_TOOLS <= {case.tool for case in V1_0_0_CASES}


@pytest.mark.parametrize(
    ("name", "outcome"),
    [("reindex", "skipped_pull_false"), ("reindex__no_pull", "skipped_pull_false"), ("reindex__pull", "skipped_no_repo")],
)
def test_reindex_goldens_report_the_vault_sync_and_carry_no_export_hint(name: str, outcome: str) -> None:
    """R11: ``reindex`` pulls only with ``pull=True``, and then the fixture vault declares no repository.

    ``export_hint`` is gone; ``index_version`` names the layout of ``index.sqlite``.
    """
    result = _object_result(name)

    assert result["vault_sync"] == outcome
    assert "export_hint" not in result
    assert result["index_version"] == stores.SCHEMA_VERSION
    assert result["validation"] == {"ok": True, "errors": 0, "warnings": 0}


@pytest.mark.parametrize("name", ["context_pack", "context_pack__memory_unconfigured"])
def test_context_pack_goldens_stay_inside_their_budget(name: str) -> None:
    from knowledge import server

    pack = _object_result(name)

    assert pack["budget_tokens"] == PACK_BUDGET
    assert pack["items"] and pack["dropped"], "the case is sized to take and to drop"
    assert pack["used_tokens"] == sum(server.estimate_tokens(item) for item in pack["items"])
    assert pack["used_tokens"] <= PACK_BUDGET
    assert {item["source"] for item in pack["items"]} == {"vault"}


def test_the_memory_golden_says_why_memory_is_missing() -> None:
    pack = _object_result("context_pack__memory_unconfigured")
    plain = _object_result("context_pack")

    assert pack["degraded"] == [{"source": "memory", "code": "E_MEMORY_TOKEN_MISSING"}]
    assert pack["sources_used"] == ["vault"]
    assert "degraded" not in plain


def test_the_list_vaults_golden_names_no_machine_detail() -> None:
    listing = _object_result("list_vaults")

    assert listing["contract"] == contract.CONTRACT_VERSION
    assert [set(row) for row in listing["vaults"]] == [{"slug", "name", "default", "features"}]


# --- the handshake -------------------------------------------------------------


def test_server_info_carries_the_sdk_version(recorded: dict[str, bytes]) -> None:
    """The contract version travels in ``instructions``; ``serverInfo.version`` is the SDK's."""
    import importlib.metadata

    assert recorded[SERVER_VERSION].decode("utf-8") == importlib.metadata.version("mcp")


def test_server_info_carries_the_mark_inline(recorded: dict[str, bytes]) -> None:
    """One SVG icon, sent as a data: URI whose bytes are the packaged ``icon.svg``."""
    import base64

    from knowledge import server

    (icon,) = json.loads(recorded[SERVER_ICONS])
    prefix = "data:image/svg+xml;base64,"

    assert icon == {"src": icon["src"], "mimeType": "image/svg+xml", "sizes": ["any"]}
    assert icon["src"].startswith(prefix)
    assert base64.b64decode(icon["src"].removeprefix(prefix)) == (Path(server.__file__).parent / "icon.svg").read_bytes()


def test_the_initialize_golden_holds_only_the_contract_part() -> None:
    init = json.loads(_golden_path(INITIALIZE).read_bytes())

    assert init == {"instructions": contract.INSTRUCTIONS, "serverInfo": {"name": "Knowledge"}}
    assert init["instructions"].splitlines()[0] == f"knowledge-contract: {contract.CONTRACT_VERSION}"


# --- the wire form of a result ---------------------------------------------------

#: Goldens that hold a ``tools/call`` result.
RESPONSE_NAMES: tuple[str, ...] = tuple(case.golden for case in CASES)


def _parsed_text(block: dict[str, Any]) -> Any:
    """A text block's JSON, or ``None`` when the block is not JSON (an error message)."""
    try:
        return json.loads(block["text"])
    except json.JSONDecodeError:
        return None


def _reindented(golden: bytes, indent: int) -> bytes:
    """A golden whose JSON text blocks are re-serialized with ``indent``, as FastMCP writes them."""
    import pydantic_core

    payload = json.loads(golden)
    for block in payload.get("content", []):
        parsed = _parsed_text(block)
        if block.get("type") == "text" and parsed is not None:
            block["text"] = pydantic_core.to_json(parsed, fallback=str, indent=indent).decode()
    return _dump(payload)


def test_text_blocks_are_compact_json() -> None:
    """Every JSON text block is written compact, in the key order the tool returned."""
    import pydantic_core

    problems: list[str] = []
    for name in RESPONSE_NAMES:
        payload = json.loads(_golden_path(name).read_bytes())
        if payload["isError"]:
            continue
        texts = [block["text"] for block in payload["content"]]
        compact = [pydantic_core.to_json(json.loads(text), fallback=str).decode() for text in texts]
        if texts != compact:
            problems.append(name)

    assert problems == [], f"text blocks that are not compact JSON: {problems}"


def test_the_goldens_carry_no_structured_view() -> None:
    """The text is the canonical answer: no golden of the main session carries the structured view."""
    carrying = [name for name in RESPONSE_NAMES if "structuredContent" in json.loads(_golden_path(name).read_bytes())]
    with_schema = [t["name"] for t in json.loads(_golden_path(CATALOG).read_bytes())["tools"] if "outputSchema" in t]

    assert carrying == [] and with_schema == []


@pytest.fixture(scope="module")
def replayed_with_indented_text(tmp_path_factory: pytest.TempPathFactory) -> dict[str, bytes]:
    """The session replayed with ``KNOWLEDGE_TEXT_INDENT=2``, the text the SDK writes by default."""
    chroma_dir = tmp_path_factory.mktemp("golden-chroma-indent")
    return asyncio.run(_capture(chroma_dir, RECORDING_SEED, env={"KNOWLEDGE_TEXT_INDENT": "2"}))


def test_the_text_indent_switch_only_reindents_the_text(replayed_with_indented_text: dict[str, bytes]) -> None:
    """``KNOWLEDGE_TEXT_INDENT=2`` changes nothing but the whitespace of the JSON text blocks.

    The goldens re-indented by 2 are the replay byte for byte: the compact text
    is the same JSON, and the switch gives back the indented text clients got
    before the compact one, structured content and error texts untouched.
    """
    differing = [
        name
        for name in RESPONSE_NAMES
        if replayed_with_indented_text[name] != _reindented(_golden_path(name).read_bytes(), 2)
    ]
    for name in (INITIALIZE, CATALOG):
        if replayed_with_indented_text[name] != _golden_path(name).read_bytes():
            differing.append(name)

    assert differing == [], f"the indented replay differs from the re-indented goldens: {differing}"


def _without(payload: dict[str, Any], key: str) -> dict[str, Any]:
    return {k: v for k, v in payload.items() if k != key}


def test_structured_output_on_only_adds_the_structured_view(recorded_structured: dict[str, bytes]) -> None:
    """With the switch at 1 the session is the goldens plus ``structuredContent`` and ``outputSchema``.

    The catalog keeps every tool in the same order, with the same
    descriptions, schemas and ``_meta``; every response keeps the same text
    blocks, and its ``structuredContent`` is the JSON of those blocks: a
    client that reads either one reads the same answer.
    """
    live = recorded_structured
    catalog = json.loads(_golden_path(CATALOG).read_bytes())
    structured_catalog = json.loads(live[CATALOG])

    assert {**structured_catalog, "tools": [_without(t, "outputSchema") for t in structured_catalog["tools"]]} == catalog
    assert all("outputSchema" in tool for tool in structured_catalog["tools"])
    assert live[INITIALIZE] == _golden_path(INITIALIZE).read_bytes()
    differing: list[str] = []
    for name in RESPONSE_NAMES:
        payload = json.loads(live[name])
        golden = json.loads(_golden_path(name).read_bytes())
        if _without(payload, "structuredContent") != golden:
            differing.append(f"{name}: text")
            continue
        if payload["isError"]:
            continue
        structured = payload["structuredContent"]
        elements = structured["result"] if set(structured) == {"result"} else [structured]
        if [json.loads(block["text"]) for block in payload["content"]] != elements:
            differing.append(f"{name}: structuredContent")

    assert differing == [], f"responses whose structured view is not their text: {differing}"


def test_search_code_is_listed_only_by_legacy_and_marked_deprecated_there() -> None:
    legacy = {tool["name"]: tool for tool in json.loads(_golden_path(LEGACY_CATALOG).read_bytes())["tools"]}
    main = {tool["name"]: tool for tool in json.loads(_golden_path(CATALOG).read_bytes())["tools"]}

    assert legacy["search_code"]["_meta"] == {"knowledge/deprecated": True, "knowledge/removal": "2.1.0"}
    assert [name for name, tool in legacy.items() if "_meta" in tool] == ["search_code"]
    assert "search_code" not in main and not [name for name, tool in main.items() if "_meta" in tool]


def test_the_refused_calls_say_what_to_do() -> None:
    """``search_code`` under ``all`` redirects as ADR-039 does; ``get_flow`` names the missing extension."""
    retired = json.loads(_golden_path("search_code__retired").read_bytes())["content"][0]["text"]
    disabled = json.loads(_golden_path("get_flow__feature_disabled").read_bytes())["content"][0]["text"]
    served = _rows("search_code")

    assert retired == f"E_TOOL_RETIRED: {contract.retired_message('search_code')}"
    assert "graphify explain <symbol>" in retired and "`rg`" in retired
    assert disabled == f"E_FEATURE_DISABLED: {contract.unloaded_message('get_flow')}"
    assert served and "graphify explain <symbol>" in served[0]["info"]
    assert _object_result("get_flow__legacy") == _object_result("get_flow")


def test_input_schemas_carry_no_titles() -> None:
    """``title`` is dropped from every input schema node; ``outputSchema`` keeps the 1.0.0 bytes."""
    tools = json.loads(_golden_path(CATALOG).read_bytes())["tools"]

    assert ['"title"' in json.dumps(tool["inputSchema"]) for tool in tools] == [False] * len(tools)


#: The goldens recorded as a tool error on purpose; every other one is a result.
ERROR_GOLDENS = frozenset(
    {
        "search__unknown_vault",
        "get_document__argument_conflict",
        "search__rerank_unconfigured",
        "search_code__retired",
        "get_flow__feature_disabled",
        "sync_confluence__fenced",
        "search__limit_out_of_range",
        "get_document__missing",
        "get_document__section_missing",
        "get_document__no_target",
        "get_document__include_metadata",
        "find_path__missing",
        "sync_confluence",
        "get_flow__missing",
        "compare_flows__missing",
    }
)

#: Every golden that records a ``tools/call`` result, all sessions.
ALL_RESPONSE_NAMES: tuple[str, ...] = tuple(
    name for name in ALL_GOLDEN_NAMES if name not in (INITIALIZE, CATALOG) and not name.startswith("tools_list__")
)


def test_only_the_error_cases_are_recorded_as_errors() -> None:
    """A re-record never pins a broken tool: an unexpected ``isError`` fails here."""
    errored = {name for name in ALL_RESPONSE_NAMES if json.loads(_golden_path(name).read_bytes())["isError"]}

    assert errored == ERROR_GOLDENS | {"sync_jira__unconfigured"}


#: Bytes of the 1.0.0 ``tools/list`` in compact JSON.
V1_0_0_CATALOG_BYTES = 11_566
#: Bytes of the 1.4.0 ``tools/list`` in compact JSON: 16 tools with ``outputSchema``.
V1_4_0_CATALOG_BYTES = 11_094

#: The most bytes each recorded catalog may take. The main one (10 tools) and
#: its structured twin stay under 8,000; with ``jira`` under 8,650; with every
#: extension under the 1.4.0 catalog; ``legacy`` under the 1.0.0 one.
CATALOG_BOUNDS: dict[str, int] = {
    CATALOG: 8_000,
    STRUCTURED_CATALOG: 8_000,
    JIRA_CATALOG: 8_650,
    FEATURES_CATALOG: V1_4_0_CATALOG_BYTES,
    LEGACY_CATALOG: V1_0_0_CATALOG_BYTES,
}


@pytest.mark.parametrize("name", sorted(CATALOG_BOUNDS))
def test_each_catalog_stays_inside_its_bound(name: str) -> None:
    size = len(_golden_path(name).read_bytes().rstrip(b"\n"))

    assert size <= CATALOG_BOUNDS[name]


def test_the_catalog_bounds_name_every_recorded_catalog() -> None:
    assert set(CATALOG_BOUNDS) == {name for name in ALL_GOLDEN_NAMES if name == CATALOG or name.startswith("tools_list__")}
    assert len((FROZEN_V1_0_0 / f"{CATALOG}.json").read_bytes().rstrip(b"\n")) == V1_0_0_CATALOG_BYTES
    assert len((FROZEN_V1_4_0 / f"{CATALOG}.json").read_bytes().rstrip(b"\n")) == V1_4_0_CATALOG_BYTES


# --- the session with a gated extension -----------------------------------------

def test_the_jira_catalog_appends_sync_jira_to_the_main_one() -> None:
    main = json.loads(_golden_path(CATALOG).read_bytes())["tools"]
    jira = json.loads(_golden_path(JIRA_CATALOG).read_bytes())["tools"]

    assert len(jira) == len(main) + 1
    assert jira[:-1] == main
    assert jira[-1]["name"] == "sync_jira"
    assert "sync_jira" not in {tool["name"] for tool in main}
    assert set(jira[-1]["inputSchema"]["properties"]) == {"vault", "dry_run", "since"}
    assert jira[-1]["inputSchema"]["properties"]["dry_run"]["default"] is True
    assert len(jira[-1]["description"]) <= 200


def test_the_unconfigured_jira_golden_names_the_variables_and_nothing_else() -> None:
    response = json.loads(_golden_path("sync_jira__unconfigured").read_bytes())
    text = response["content"][0]["text"]

    assert response["isError"] is True
    assert re.search(r"\bE_SECRET_MISSING: ", text), text
    assert "CLOUDFROG_JIRA_EMAIL, CLOUDFROG_JIRA_TOKEN" in text
    assert "Traceback" not in text and "jira.invalid" not in text


# --- the session with a judge -----------------------------------------------------

#: Keys of ``_meta["knowledge/rerank"]``: deterministic, no latency.
RERANK_META_KEYS = {"provider", "model", "applied", "reason", "candidates", "units", "cache_hits"}


def _rerank_meta(name: str) -> dict[str, Any]:
    payload = json.loads(_golden_path(name).read_bytes())
    assert set(payload["_meta"]) == {"knowledge/rerank"}, payload.get("_meta")
    meta: dict[str, Any] = payload["_meta"]["knowledge/rerank"]
    assert set(meta) == RERANK_META_KEYS
    return meta


@pytest.mark.parametrize("name", ["search", "search__pointers", "search__snippet", "search__unknown_vault"])
def test_search_without_rerank_is_the_1_3_0_answer_through_the_2_0_migration(name: str) -> None:
    """No ``rerank`` and no vault default: the 1.3.0 answer under the 2.0 rules, with no ``_meta`` at all."""
    frozen = json.loads((FROZEN_V1_3_0 / f"{name}.json").read_bytes())
    case = next(case for case in CASES if case.golden == name)
    live = json.loads(_golden_path(name).read_bytes())

    assert response_problems("search", migrate_1_4_to_2_0(name, frozen, case.arguments), live) == []
    assert "_meta" not in live


def test_rerank_off_returns_the_plain_rows_and_says_so() -> None:
    plain = json.loads(_golden_path("search").read_bytes())
    off = json.loads(_golden_path("search__rerank_off").read_bytes())

    assert off["content"] == plain["content"]
    assert "structuredContent" not in off
    assert _rerank_meta("search__rerank_off") == {
        "provider": "kev",
        "model": "kev-latest",
        "applied": False,
        "reason": "off",
        "candidates": 0,
        "units": 0,
        "cache_hits": 0,
    }


def test_rerank_on_judges_and_fuses_with_a_bounded_multiplier() -> None:
    meta = _rerank_meta("search__rerank")
    rows = _rows("search__rerank")

    assert (meta["provider"], meta["model"], meta["applied"], meta["reason"]) == ("kev", "kev-latest", True, None)
    assert meta["cache_hits"] == 0
    assert 1 <= meta["candidates"] <= 5 and meta["candidates"] <= meta["units"] <= 2 * meta["candidates"]
    judged = [row for row in rows if "rerank_score" in row]
    assert judged, "the fused rows carry the probability of the block the judge chose"
    assert all(0.0 <= row["rerank_score"] <= 1.0 for row in judged)
    assert [row["score"] for row in rows] == sorted((row["score"] for row in rows), reverse=True)


def test_the_second_identical_rerank_is_served_from_the_cache_and_names_the_chosen_block() -> None:
    meta = _rerank_meta("search__rerank_fields")
    rows = _rows("search__rerank_fields")

    assert meta["applied"] is True and meta["cache_hits"] == meta["units"] > 0
    for row in rows:
        assert set(row) <= {"file_path", "score", "chunk_id", "section", "snippet", "rerank_score"}
        assert row["chunk_id"] is not None


def test_a_judge_that_answers_529_leaves_the_plain_ranking() -> None:
    meta = _rerank_meta("search__rerank_down")
    rows = _rows("search__rerank_down")

    assert (meta["applied"], meta["reason"]) == (False, "unavailable")
    assert rows and all("rerank_score" not in row for row in rows)


def test_the_rerank_goldens_replay_identically(tmp_path_factory: pytest.TempPathFactory) -> None:
    """A second session with a judge, under another hash seed, gives the same bytes."""
    replayed = _capture_rerank(tmp_path_factory, SECOND_SEED)

    differing = [name for name in RERANK_GOLDEN_NAMES if replayed[name] != _golden_path(name).read_bytes()]
    assert differing == [], f"the session with a judge is not reproducible: {differing}"


def test_the_catalog_declares_rerank_in_search() -> None:
    tools = {tool["name"]: tool for tool in json.loads(_golden_path(CATALOG).read_bytes())["tools"]}
    search = tools["search"]

    assert search["inputSchema"]["properties"]["rerank"] == {
        "anyOf": [{"type": "string"}, {"type": "null"}],
        "default": None,
    }
    assert "not a probability" in search["description"] and "rerank_score" in search["description"]
    assert len(search["description"]) <= 250
