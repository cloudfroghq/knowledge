"""Compatibility of a live tool response with a frozen golden, as parsed JSON.

The byte-exact golden pins the *current* contract version. A frozen older
version answers a different question: does everything a client of that
version relied on still come back with the same value? So the comparison is
over parsed JSON and over the keys the old version already had:

* a key the frozen golden carries must be present with an equal value;
* a key only the live response carries is allowed (additive growth);
* each ``content`` text block is parsed and held to the same rule, and the
  block count must match;
* ``isError`` must match.

Some orderings were never part of the contract because they depended on the
interpreter's hash seed or on ties. Those are compared as sets, per tool, and
only for the tools in :data:`ORDER_FREE_TOOLS`:

* ``find_path``: only ``from``, ``to``, ``found`` and ``length``; which of
  several equally short paths is reported is free.
* ``get_impact``: each ``depth_N`` list as a set.
* ``get_related``: the rows as a multiset, each live row projected onto the
  keys the frozen rows carry (rows are matched by content, not position).
* ``search`` and ``search_deep``: rows that share a score form a tie block,
  and a tie block is a set; blocks keep their order.

Some values are bound to the store or to the version, not to what a client
was promised, and are relaxed only when the frozen golden predates the live
version in the way each rule names:

* :data:`STORE_BOUND_TOOLS` rank documents through the index. Against a
  frozen version older than :data:`STORE_BOUND_SINCE` served by a version at
  or past it, their scores, row order and row membership are free, and they
  are compared by shape: ``isError``, every row keeps the keys all frozen rows
  carry with the same JSON types, no more rows than ``limit``, at least one row
  where the frozen golden had one, and ``used_tokens <= budget_tokens``.
* :data:`VERSION_BOUND` values spell the contract version itself: they must
  equal the live version, not the frozen one.
* :data:`PRESENCE_BOUND` keys must still be there; their value may change
  across the same boundary as the store. The set is empty: the one key it
  held, ``reindex.export_hint``, is retired by 2.0.0 (rule R11).

Below :data:`STORE_BOUND_SINCE` nothing is relaxed: every value is compared.

Contract 2.0.0 breaks 1.x on purpose, so a frozen 1.4.0 golden is no longer
compared with the live answer as it was recorded. It first goes through
:func:`migrate_1_4_to_2_0`, which applies the declared rules of
:data:`MIGRATIONS_2_0` and nothing else; the result is then held to the rules
above, by value. Each rule carries the id of its row in the «Rompe» table of
``## 2.0.0`` in ``docs/contract/CHANGELOG.md`` (``R<n>``), and a test keeps
both sets equal, so a change that no rule declares fails the replay. Only
1.4.0 is replayed against a 2.x server: the 1.0.0-1.3.0 goldens stay frozen
and hashed, and what they guaranteed reaches 2.0.0 through 1.4.0, which
honoured them and is frozen too.

This module is a helper, not a test module, so pytest never collects it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any

from knowledge import confluence_sync, contract, errors, flows

#: Tools whose row order is compared as described in the module docstring.
ORDER_FREE_TOOLS = frozenset({"find_path", "get_related", "get_impact", "search", "search_deep"})

#: Tools whose scores, order and membership come from the index's ranking.
#: ``test_the_store_bound_set_is_exactly_the_ranked_tools`` fails if it grows:
#: every other tool keeps being compared by value against every frozen version.
STORE_BOUND_TOOLS = frozenset({"search", "search_deep", "context_pack"})

#: First contract version whose store ranks differently from the ones before.
STORE_BOUND_SINCE: tuple[int, int, int] = (1, 2, 0)

#: Values that spell the contract version: ``<golden>.<key>``, with ``[0]`` for
#: the first line of a text value.
VERSION_BOUND = frozenset({"list_vaults.contract", "initialize.instructions[0]"})

#: Keys that must stay present across :data:`STORE_BOUND_SINCE` while their
#: value may change: ``<tool>.<key>``. Empty since 2.0.0, which retires
#: ``reindex.export_hint`` and replays only 1.4.0 (past that boundary).
PRESENCE_BOUND: frozenset[str] = frozenset()

#: Row keys whose value may be ``null`` by contract, whatever the frozen row had.
_NULLABLE_ROW_KEYS = frozenset({"snippet", "chunk_id", "section"})

#: ``context_pack`` keys that echo the request or name sources: compared by value.
_PACK_VALUE_KEYS = ("query", "vault", "budget_tokens", "degraded")

_FIND_PATH_STABLE_KEYS = ("from", "to", "found", "length")
_TIE_TOOLS = frozenset({"search", "search_deep"})
_OBJECT_TOOLS = frozenset({"find_path", "get_impact"})


def project(old: Any, new: Any) -> Any:
    """``new`` restricted to the keys and positions ``old`` has.

    A key missing from ``new`` is left out, so the projection differs from
    ``old`` exactly where ``new`` lost something. Lists of different length
    are returned untouched for the same reason.
    """
    if isinstance(old, dict) and isinstance(new, dict):
        return {key: project(value, new[key]) for key, value in old.items() if key in new}
    if isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
        return [project(o, n) for o, n in zip(old, new, strict=True)]
    return new


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _as_set(rows: Any) -> Any:
    return sorted(rows, key=_canonical) if isinstance(rows, list) else rows


def _tie_blocks(rows: list[Any], sizes: list[int]) -> list[Any]:
    out: list[Any] = []
    start = 0
    for size in sizes:
        out.append(_as_set(rows[start : start + size]))
        start += size
    return out


def _run_sizes(rows: list[Any]) -> list[int]:
    sizes: list[int] = []
    previous: Any = object()
    for row in rows:
        score = row.get("score") if isinstance(row, dict) else None
        if sizes and score == previous:
            sizes[-1] += 1
        else:
            sizes.append(1)
        previous = score
    return sizes


def _project_rows(old: list[Any], new: list[Any]) -> list[Any]:
    """Each row of ``new`` restricted to the keys any row of ``old`` carries.

    Unlike :func:`project` it pairs nothing by position, so it suits rows whose
    order is free: a row that lost a key still differs from every old row.
    """
    keys = {key for row in old if isinstance(row, dict) for key in row}
    return [{k: v for k, v in row.items() if k in keys} if isinstance(row, dict) else row for row in new]


def _align_rows(tool: str, old: list[Any], new: list[Any]) -> tuple[Any, Any]:
    """Comparable forms of two row lists under the per-tool ordering rule."""
    if tool == "get_related":
        return _as_set(old), _as_set(_project_rows(old, new))
    projected = project(old, new)
    if tool in _TIE_TOOLS and isinstance(projected, list) and len(projected) == len(old):
        sizes = _run_sizes(old)
        return _tie_blocks(old, sizes), _tie_blocks(projected, sizes)
    return old, projected


def _align_object(tool: str, old: dict[str, Any], new: dict[str, Any]) -> tuple[Any, Any]:
    if tool == "find_path":
        keys = [k for k in _FIND_PATH_STABLE_KEYS if k in old]
        return {k: old[k] for k in keys}, {k: new[k] for k in keys if k in new}
    projected = project(old, new)
    if tool == "get_impact" and isinstance(projected, dict):
        depth = lambda d: {k: (_as_set(v) if k.startswith("depth_") else v) for k, v in d.items()}  # noqa: E731
        return depth(old), depth(projected)
    return old, projected


def _align(tool: str, old: Any, new: Any) -> tuple[Any, Any]:
    if tool not in ORDER_FREE_TOOLS:
        return old, project(old, new)
    if isinstance(old, list) and isinstance(new, list):
        return _align_rows(tool, old, new)
    if isinstance(old, dict) and isinstance(new, dict):
        return _align_object(tool, old, new)
    return old, new


def _unwrap(tool: str, old: list[Any], new: list[Any]) -> tuple[Any, Any]:
    """A tool that returns one object has one content block: compare the object."""
    if tool in _OBJECT_TOOLS and len(old) == 1 and len(new) == 1 and isinstance(old[0], dict):
        return old[0], new[0]
    return old, new


def _decode(payload: dict[str, Any]) -> dict[str, Any]:
    """A ``tools/call`` result with every ``content`` text block parsed."""
    decoded = dict(payload)
    blocks = []
    for block in payload.get("content", []):
        if block.get("type") == "text":
            try:
                blocks.append({**block, "text": json.loads(block["text"])})
                continue
            except (json.JSONDecodeError, KeyError):
                pass
        blocks.append(block)
    decoded["content"] = blocks
    return decoded


def _version_info(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def crosses_store_boundary(frozen_version: str | None, live_version: str | None) -> bool:
    """Whether ``frozen_version`` predates :data:`STORE_BOUND_SINCE` and ``live_version`` does not."""
    if frozen_version is None or live_version is None:
        return False
    return _version_info(frozen_version) < STORE_BOUND_SINCE <= _version_info(live_version)


def _json_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if value is None:
        return "null"
    return type(value).__name__


def _row_shape_problems(where: str, old_rows: list[Any], new_rows: list[Any]) -> list[str]:
    """Every live row keeps the keys all frozen rows carry, with a frozen JSON type."""
    old_dicts = [row for row in old_rows if isinstance(row, dict)]
    if not old_dicts:
        return []
    required = set.intersection(*(set(row) for row in old_dicts))
    types: dict[str, set[str]] = {}
    for row in old_dicts:
        for key, value in row.items():
            types.setdefault(key, set()).add(_json_type(value))
    problems: list[str] = []
    for index, row in enumerate(new_rows):
        if not isinstance(row, dict):
            problems.append(f"{where}[{index}]: not an object")
            continue
        missing = sorted(required - set(row))
        if missing:
            problems.append(f"{where}[{index}]: lost {missing}")
        for key, value in row.items():
            allowed = types.get(key)
            if allowed is None:
                continue
            kind = _json_type(value)
            if kind not in allowed and not (kind == "null" and key in _NULLABLE_ROW_KEYS):
                problems.append(f"{where}[{index}].{key}: {sorted(allowed)} -> {kind}")
    return problems


def _rows_problems(where: str, old_rows: list[Any], new_rows: list[Any], limit: Any) -> list[str]:
    problems = _row_shape_problems(where, old_rows, new_rows)
    if old_rows and not new_rows:
        problems.append(f"{where}: {len(old_rows)} rows -> none")
    if isinstance(limit, int) and len(new_rows) > limit:
        problems.append(f"{where}: {len(new_rows)} rows over limit {limit}")
    return problems


def _pack_problems(where: str, old: Any, new: Any) -> list[str]:
    if not isinstance(old, dict) or not isinstance(new, dict):
        return [] if type(old) is type(new) else [f"{where}: {_json_type(old)} -> {_json_type(new)}"]
    problems = [f"{where}: lost {key!r}" for key in sorted(set(old) - set(new))]
    for key in _PACK_VALUE_KEYS:
        if key in old and old[key] != new.get(key):
            problems.append(f"{where}.{key}: {old[key]!r} -> {new.get(key)!r}")
    for key in ("items", "dropped"):
        if isinstance(old.get(key), list) and isinstance(new.get(key), list):
            problems += _row_shape_problems(f"{where}.{key}", old[key], new[key])
    if old.get("items") and not new.get("items"):
        problems.append(f"{where}.items: none delivered")
    used, budget = new.get("used_tokens"), new.get("budget_tokens")
    if not isinstance(used, int) or not isinstance(budget, int) or used > budget:
        problems.append(f"{where}: used_tokens {used!r} over budget_tokens {budget!r}")
    return problems


def _shape_problems(tool: str, old_d: dict[str, Any], new_d: dict[str, Any], arguments: Mapping[str, Any]) -> list[str]:
    """Store-bound comparison of two decoded results; see the module docstring."""
    problems: list[str] = []
    old_blocks, new_blocks = old_d.get("content", []), new_d.get("content", [])
    if any(block.get("type") != "text" for block in new_blocks):
        problems.append(f"content block types: {[block.get('type') for block in new_blocks]}")
    old_texts = [block.get("text") for block in old_blocks]
    new_texts = [block.get("text") for block in new_blocks]
    if tool == "context_pack":
        if len(old_texts) != len(new_texts):
            problems.append(f"content: {len(old_texts)} blocks -> {len(new_texts)}")
        for index, (before, after) in enumerate(zip(old_texts, new_texts, strict=False)):
            problems += _pack_problems(f"content[{index}]", before, after)
        if "structuredContent" in old_d:
            problems += _pack_problems("structuredContent", old_d["structuredContent"], new_d.get("structuredContent"))
        return problems
    limit = arguments.get("limit")
    problems += _rows_problems("content", old_texts, new_texts, limit)
    old_s, new_s = old_d.get("structuredContent"), new_d.get("structuredContent")
    if isinstance(old_s, dict) and isinstance(old_s.get("result"), list):
        new_rows = new_s.get("result") if isinstance(new_s, dict) else None
        if not isinstance(new_rows, list):
            problems.append("structuredContent: result is not a list")
        else:
            problems += _rows_problems("structuredContent.result", old_s["result"], new_rows, limit)
    return problems


def _with_live_values(
    tool: str, old_d: dict[str, Any], new_d: dict[str, Any], live_version: str | None, store_crossed: bool
) -> dict[str, Any]:
    """``old_d`` with the version-bound and presence-bound values it may not pin.

    A version-bound key is expected to carry the live version; a presence-bound
    key takes whatever value the live response has, so only its absence differs.
    """
    replacements: dict[str, Any] = {}
    for bound in VERSION_BOUND:
        golden, _, key = bound.partition(".")
        if golden == tool and live_version is not None and "[" not in key:
            replacements[key] = live_version
    if store_crossed:
        for bound in PRESENCE_BOUND:
            name, _, key = bound.partition(".")
            if name == tool:
                live = _first_value(new_d, key)
                if live is not _MISSING:
                    replacements[key] = live
    if not replacements:
        return old_d

    def rewrite(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: (replacements[k] if k in replacements else rewrite(v)) for k, v in value.items()}
        if isinstance(value, list):
            return [rewrite(v) for v in value]
        return value

    rewritten = dict(old_d)
    rewritten["content"] = [{**block, "text": rewrite(block.get("text"))} for block in old_d.get("content", [])]
    if "structuredContent" in old_d:
        rewritten["structuredContent"] = rewrite(old_d["structuredContent"])
    return rewritten


_MISSING = object()


def _first_value(decoded: dict[str, Any], key: str) -> Any:
    for candidate in [decoded.get("structuredContent"), *(b.get("text") for b in decoded.get("content", []))]:
        if isinstance(candidate, dict) and key in candidate:
            return candidate[key]
    return _MISSING


def response_problems(
    tool: str,
    old: dict[str, Any],
    new: dict[str, Any],
    *,
    frozen_version: str | None = None,
    live_version: str | None = None,
    arguments: Mapping[str, Any] | None = None,
) -> list[str]:
    """What a live ``tools/call`` result lost or changed against a frozen one.

    Empty when the live result is compatible. ``tool`` is the tool name, not
    the golden name (``search_by_metadata__tag`` is ``search_by_metadata``).
    ``frozen_version`` and ``live_version`` turn on the store-bound, version-bound
    and presence-bound rules of the module docstring; without them every value
    is compared. ``arguments`` are the call's arguments (``limit`` caps the rows
    of a store-bound comparison).
    """
    problems: list[str] = []
    old_d, new_d = _decode(old), _decode(new)
    store_crossed = crosses_store_boundary(frozen_version, live_version)
    if frozen_version is not None and frozen_version != live_version:
        old_d = _with_live_values(tool, old_d, new_d, live_version, store_crossed)
    elif store_crossed:
        old_d = _with_live_values(tool, old_d, new_d, None, store_crossed)

    if old_d.get("isError") != new_d.get("isError"):
        problems.append(f"isError: {old_d.get('isError')!r} -> {new_d.get('isError')!r}")

    if store_crossed and tool in STORE_BOUND_TOOLS:
        return problems + _shape_problems(tool, old_d, new_d, arguments or {})

    if old_d.get("isError") != new_d.get("isError"):
        problems.append(f"isError: {old_d.get('isError')!r} -> {new_d.get('isError')!r}")

    old_blocks, new_blocks = old_d.get("content", []), new_d.get("content", [])
    if len(old_blocks) != len(new_blocks):
        problems.append(f"content: {len(old_blocks)} blocks -> {len(new_blocks)}")
    else:
        left, right = _align(
            tool, *_unwrap(tool, [b.get("text") for b in old_blocks], [b.get("text") for b in new_blocks])
        )
        if left != right:
            problems.append(f"content differs:\n  frozen {_canonical(left)}\n  live   {_canonical(right)}")
        types_old = [b.get("type") for b in old_blocks]
        types_new = [b.get("type") for b in new_blocks]
        if types_old != types_new:
            problems.append(f"content block types: {types_old} -> {types_new}")

    if "structuredContent" in old_d:
        if "structuredContent" not in new_d:
            problems.append("structuredContent: dropped")
        else:
            old_s, new_s = old_d["structuredContent"], new_d["structuredContent"]
            if isinstance(old_s, dict) and set(old_s) == {"result"} and isinstance(old_s["result"], list):
                left, right = _align(tool, old_s["result"], new_s.get("result") if isinstance(new_s, dict) else None)
            else:
                left, right = _align(tool, old_s, new_s)
            if left != right:
                problems.append(
                    f"structuredContent differs:\n  frozen {_canonical(left)}\n  live   {_canonical(right)}"
                )

    return problems


def initialize_problems(
    old: dict[str, Any], new: dict[str, Any], *, frozen_version: str | None = None, live_version: str | None = None
) -> list[str]:
    """What a live ``initialize`` golden (``instructions`` and ``serverInfo.name``) changed.

    The first line of ``instructions`` is version-bound: across versions it
    must name the live version. Every other line and ``serverInfo`` compare by value.
    """
    problems: list[str] = []
    if old.get("serverInfo") != new.get("serverInfo"):
        problems.append(f"serverInfo: {old.get('serverInfo')!r} -> {new.get('serverInfo')!r}")
    old_lines = str(old.get("instructions", "")).splitlines()
    new_lines = str(new.get("instructions", "")).splitlines()
    if frozen_version is not None and live_version is not None and frozen_version != live_version and old_lines:
        old_lines = [old_lines[0].replace(frozen_version, live_version), *old_lines[1:]]
    if old_lines[:1] != new_lines[:1]:
        problems.append(f"instructions[0]: {old_lines[:1]!r} -> {new_lines[:1]!r}")
    if old_lines[1:] != new_lines[1:]:
        problems.append(f"instructions[1:]: {old_lines[1:]!r} -> {new_lines[1:]!r}")
    return problems


def _strip_titles(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip_titles(v) for k, v in value.items() if k != "title"}
    if isinstance(value, list):
        return [_strip_titles(v) for v in value]
    return value


def catalog_problems(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """What a live ``tools/list`` lost or changed against a frozen one.

    The frozen tools keep their name and position; new tools may follow them.
    Every frozen property keeps its schema (``title`` aside), ``required`` may
    only shrink, and ``outputSchema`` and ``_meta`` are unchanged. Descriptions
    and titles are free.
    """
    problems: list[str] = []
    old_tools, new_tools = old.get("tools", []), new.get("tools", [])
    if [t["name"] for t in new_tools[: len(old_tools)]] != [t["name"] for t in old_tools]:
        problems.append(
            f"tool order: {[t['name'] for t in old_tools]} is not a prefix of {[t['name'] for t in new_tools]}"
        )
        return problems
    for before, after in zip(old_tools, new_tools, strict=False):
        name = before["name"]
        old_in, new_in = before.get("inputSchema", {}), after.get("inputSchema", {})
        old_props, new_props = old_in.get("properties", {}), new_in.get("properties", {})
        for prop, schema in old_props.items():
            if prop not in new_props:
                problems.append(f"{name}.{prop}: parameter removed")
            elif _strip_titles(schema) != _strip_titles(new_props[prop]):
                problems.append(
                    f"{name}.{prop}: schema {_canonical(_strip_titles(schema))} -> "
                    f"{_canonical(_strip_titles(new_props[prop]))}"
                )
        extra_required = set(new_in.get("required", [])) - set(old_in.get("required", []))
        if extra_required:
            problems.append(f"{name}: newly required {sorted(extra_required)}")
        if old_in.get("type") != new_in.get("type"):
            problems.append(f"{name}: inputSchema type {old_in.get('type')!r} -> {new_in.get('type')!r}")
        if before.get("outputSchema") != after.get("outputSchema"):
            problems.append(f"{name}: outputSchema changed")
        if before.get("_meta") != after.get("_meta"):
            problems.append(f"{name}: _meta {before.get('_meta')!r} -> {after.get('_meta')!r}")
    return problems


# --- the declared migration from 1.4.0 to 2.0.0 ----------------------------------


@dataclass(frozen=True)
class ReplaySession:
    """What the live session that replays a frozen 1.4.0 golden serves.

    ``profile`` is the tool profile of the server (``all`` or ``legacy``) and
    ``features`` the extensions its vault declares. A rule that depends on
    the catalog a session serves reads them; every other rule ignores them.
    """

    profile: str = "all"
    features: frozenset[str] = frozenset()


#: ``(tool, frozen result, call arguments, session) -> migrated result``.
ResponseRule = Callable[[str, dict[str, Any], Mapping[str, Any], ReplaySession], dict[str, Any]]
#: ``(frozen tools/list result, session) -> migrated tools/list result``.
CatalogRule = Callable[[dict[str, Any], ReplaySession], dict[str, Any]]
#: ``frozen initialize golden -> migrated initialize golden``.
HandshakeRule = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class Migration:
    """One declared change of 2.0.0: its ``R<n>`` id and how it rewrites a 1.4.0 golden.

    A rule with no callable declares a change no 1.4.0 golden reaches (an
    argument range, an error branch no case calls): the 2.0.0 goldens pin it.
    """

    rule: str
    summary: str
    response: ResponseRule | None = None
    catalog: CatalogRule | None = None
    handshake: HandshakeRule | None = None


def _without_structured_content(
    tool: str, frozen: dict[str, Any], arguments: Mapping[str, Any], session: ReplaySession
) -> dict[str, Any]:
    del tool, arguments, session
    return {key: value for key, value in frozen.items() if key != "structuredContent"}


def _without_output_schema(catalog: dict[str, Any], session: ReplaySession) -> dict[str, Any]:
    del session
    tools = [{key: value for key, value in tool.items() if key != "outputSchema"} for tool in catalog.get("tools", [])]
    return {**catalog, "tools": tools}


def _error_result(text: str) -> dict[str, Any]:
    """A ``tools/call`` result that is a coded error: one text block, ``isError``."""
    return {"content": [{"text": text, "type": "text"}], "isError": True}


def _tool_error_result(tool: str, error: Any) -> dict[str, Any]:
    """The coded error a tool body raises, as the SDK reports it: its own prefix, then ``<code>: <message>``."""
    return _error_result(f"Error executing tool {tool}: {error}")


def _error_object(frozen: dict[str, Any], tool: str, tool_name: str | frozenset[str]) -> str | None:
    """The ``error`` text of a 1.4.0 success that was an ``{"error": ...}`` object of ``tool_name``; else ``None``."""
    names = {tool_name} if isinstance(tool_name, str) else tool_name
    rows = _text_rows(frozen) if tool in names else None
    if not rows or len(rows) != 1 or not isinstance(rows[0], dict) or set(rows[0]) != {"error"}:
        return None
    return str(rows[0]["error"])


def _chain(*rules: ResponseRule) -> ResponseRule:
    """One response rule that applies ``rules`` in order."""

    def rule(tool: str, frozen: dict[str, Any], arguments: Mapping[str, Any], session: ReplaySession) -> dict[str, Any]:
        for each in rules:
            frozen = each(tool, frozen, arguments, session)
        return frozen

    return rule


def _chain_catalog(*rules: CatalogRule) -> CatalogRule:
    """One catalog rule that applies ``rules`` in order."""

    def rule(frozen: dict[str, Any], session: ReplaySession) -> dict[str, Any]:
        for each in rules:
            frozen = each(frozen, session)
        return frozen

    return rule


def _gated_by(extension: str) -> tuple[ResponseRule, CatalogRule]:
    """The response and catalog rules of an extension whose tools 2.0 registers only when a vault declares it.

    Under ``all`` without the extension, each of its tools leaves the catalog
    and a call answers ``E_FEATURE_DISABLED`` (the server's message for a
    tool no served vault turns on). With the extension, or under ``legacy``,
    nothing changes.
    """
    tools = frozenset(name for name, ext in contract.TOOL_EXTENSION.items() if ext == extension)

    def off(session: ReplaySession) -> bool:
        return session.profile != contract.LEGACY_PROFILE and extension not in session.features

    def response(tool: str, frozen: dict[str, Any], arguments: Mapping[str, Any], session: ReplaySession) -> dict[str, Any]:
        del arguments
        if tool in tools and off(session):
            return _error_result(f"E_FEATURE_DISABLED: {contract.unloaded_message(tool)}")
        return frozen

    def catalog(frozen: dict[str, Any], session: ReplaySession) -> dict[str, Any]:
        if not off(session):
            return frozen
        return {**frozen, "tools": [tool for tool in frozen.get("tools", []) if tool["name"] not in tools]}

    return response, catalog


def _retired_response(
    tool: str, frozen: dict[str, Any], arguments: Mapping[str, Any], session: ReplaySession
) -> dict[str, Any]:
    del arguments
    if tool in contract.TOOL_REMOVED and session.profile != contract.LEGACY_PROFILE:
        return _error_result(f"E_TOOL_RETIRED: {contract.retired_message(tool)}")
    return frozen


def _retired_catalog(frozen: dict[str, Any], session: ReplaySession) -> dict[str, Any]:
    if session.profile == contract.LEGACY_PROFILE:
        return frozen
    return {**frozen, "tools": [tool for tool in frozen.get("tools", []) if tool["name"] not in contract.TOOL_REMOVED]}


def _core_then_extensions(frozen: dict[str, Any], session: ReplaySession) -> dict[str, Any]:
    """Under ``all``: the core tools in their frozen order, then each extension tool in registry order."""
    if session.profile == contract.LEGACY_PROFILE:
        return frozen
    tools = frozen.get("tools", [])
    core = [tool for tool in tools if tool["name"] not in contract.TOOL_EXTENSION]
    by_name = {tool["name"]: tool for tool in tools}
    ext = [by_name[name] for name in contract.TOOL_EXTENSION if name in by_name]
    return {**frozen, "tools": [*core, *ext]}


def _removal_announced(frozen: dict[str, Any], session: ReplaySession) -> dict[str, Any]:
    """``search_code`` keeps its ``_meta`` under ``legacy``, with the version that retires that profile."""
    del session
    tools = []
    for tool in frozen.get("tools", []):
        if tool["name"] in contract.TOOL_REMOVED and "_meta" in tool:
            tool = {**tool, "_meta": dict(contract.TOOL_META[tool["name"]])}
        tools.append(tool)
    return {**frozen, "tools": tools}


def _text_rows(frozen: dict[str, Any]) -> list[Any] | None:
    """The parsed JSON of every text block of a result that is not an error; ``None`` for an error."""
    if frozen.get("isError"):
        return None
    return [json.loads(block["text"]) for block in frozen.get("content", [])]


def _with_text_rows(frozen: dict[str, Any], rows: list[Any], *, wrapped: bool) -> dict[str, Any]:
    """``frozen`` answering ``rows``: one text block per row and, if it had one, the structured view.

    ``wrapped`` says whether the structured view is ``{"result": [...]}`` (a
    tool that answers a list) or the only row itself (a tool that answers an
    object).
    """
    out = {**frozen, "content": [{"text": _compact(row), "type": "text"} for row in rows]}
    if "structuredContent" in frozen:
        out["structuredContent"] = {"result": rows} if wrapped else rows[0]
    return out


def _compact(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _stem(file_path: str) -> str:
    return PurePosixPath(file_path).stem


def _pointer_rows(tool_name: str) -> ResponseRule:
    """R1/R2: a default row of ``tool_name`` loses ``excerpt``, and ``title`` when it is the file name.

    Only calls without ``fields`` change: a projected row is the one asked
    for. The pointer keys the 2.0 row adds (``chunk_id``, ``section``) are
    additive and need no rewrite.
    """

    def rule(tool: str, frozen: dict[str, Any], arguments: Mapping[str, Any], session: ReplaySession) -> dict[str, Any]:
        del session
        rows = _text_rows(frozen) if tool == tool_name and arguments.get("fields") is None else None
        if rows is None:
            return frozen
        trimmed = []
        for row in rows:
            row = {key: value for key, value in row.items() if key != "excerpt"}
            title = str(row.get("title") or "").strip()
            if "title" in row and (not title or title == _stem(str(row.get("file_path") or ""))):
                del row["title"]
            trimmed.append(row)
        return frozen if trimmed == rows else _with_text_rows(frozen, trimmed, wrapped=True)

    return rule


#: Characters ``get_document`` returns without ``max_chars`` in 2.0.0.
DOCUMENT_MAX_CHARS_2_0 = 3000
#: Frontmatter keys ``get_document`` keeps under ``metadata`` without ``full_metadata`` in 2.0.0.
DOCUMENT_METADATA_KEYS_2_0 = ("type", "status", "service", "owner", "tags", "last_updated")


def _shorter_document(
    tool: str, frozen: dict[str, Any], arguments: Mapping[str, Any], session: ReplaySession
) -> dict[str, Any]:
    """R4: a 1.4.0 read without ``max_chars`` cut at 3,000 characters, and ``metadata`` cut to the whitelist."""
    del session
    rows = _text_rows(frozen) if tool == "get_document" else None
    if not rows:
        return frozen
    document = json.loads(json.dumps(rows[0]))
    if "metadata" in document:
        document["metadata"] = {k: v for k, v in document["metadata"].items() if k in DOCUMENT_METADATA_KEYS_2_0}
    content = document.get("content")
    if "max_chars" not in arguments and isinstance(content, str) and len(content) > DOCUMENT_MAX_CHARS_2_0:
        document["content"] = content[:DOCUMENT_MAX_CHARS_2_0]
        document["truncated"] = True
        document["next_offset"] = DOCUMENT_MAX_CHARS_2_0
    return frozen if document == rows[0] else _with_text_rows(frozen, [document], wrapped=False)


def _with_properties(tool_name: str, properties: Mapping[str, Any], *, optional: frozenset[str] = frozenset()) -> CatalogRule:
    """A catalog rule that gives ``tool_name`` the 2.0 schema of ``properties`` and drops ``optional`` from ``required``."""

    def rule(frozen: dict[str, Any], session: ReplaySession) -> dict[str, Any]:
        del session
        tools = []
        for tool in frozen.get("tools", []):
            if tool["name"] == tool_name:
                schema = dict(tool.get("inputSchema", {}))
                schema["properties"] = {**schema.get("properties", {}), **{k: dict(v) for k, v in properties.items()}}
                if optional and "required" in schema:
                    schema["required"] = [name for name in schema["required"] if name not in optional]
                tool = {**tool, "inputSchema": schema}
            tools.append(tool)
        return {**frozen, "tools": tools}

    return rule


_NULLABLE_STRING = {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None}

_GET_DOCUMENT_2_0 = _with_properties(
    "get_document",
    {"file_path": _NULLABLE_STRING, "max_chars": {"default": DOCUMENT_MAX_CHARS_2_0, "type": "integer"}},
    optional=frozenset({"file_path"}),
)
_INCLUDE_METADATA_RETIRED = _with_properties(
    "get_document", {"include_metadata": {"anyOf": [{"type": "boolean"}, {"type": "null"}], "default": None}}
)


#: ``limit`` of ``search_by_metadata`` and ``max_items`` of ``get_impact`` when a 2.0.0 call does not say them.
METADATA_LIMIT_2_0 = 20
IMPACT_MAX_ITEMS_2_0 = 20


def _metadata_page(facets: bool) -> ResponseRule:
    """R6 (documents) and R7 (``facets=True``): the 1.4.0 rows become one ``{total, offset, items}`` page.

    A 1.4.0 call without ``limit`` and ``offset`` listed every row, so its
    page is the first 20 (documents by path, facet rows in their order) and
    ``total`` counts them all. A paged 1.4.0 call already holds its page, cut
    from the same order; its ``total`` is not in the golden and is left to the
    2.0.0 one (a key the migration does not write is not compared).
    """

    def rule(tool: str, frozen: dict[str, Any], arguments: Mapping[str, Any], session: ReplaySession) -> dict[str, Any]:
        del session
        rows = _text_rows(frozen) if tool == "search_by_metadata" else None
        if rows is None or _facet_rows(rows, arguments) is not facets:
            return frozen
        offset = int(arguments.get("offset") or 0)
        page: dict[str, Any] = {"offset": offset}
        if arguments.get("limit") is None and not offset:
            ordered = rows if facets else sorted(rows, key=lambda row: str(row["file_path"]))
            page = {"total": len(ordered), "offset": 0, "items": ordered[:METADATA_LIMIT_2_0]}
        else:
            page["items"] = rows
        return _with_text_rows(frozen, [page], wrapped=False)

    return rule


def _facet_rows(rows: list[Any], arguments: Mapping[str, Any]) -> bool:
    """Whether ``rows`` are ``{facet, value, count}`` rows: by their keys, or by the call when there are none."""
    if rows:
        return all(isinstance(row, dict) and "facet" in row for row in rows)
    return bool(arguments.get("facets"))


def _related_pairs(
    tool: str, frozen: dict[str, Any], arguments: Mapping[str, Any], session: ReplaySession
) -> dict[str, Any]:
    """R8: no ``file``; one ``both`` row for a document linked both ways; ``weight`` to 3 decimals."""
    del arguments, session
    rows = _text_rows(frozen) if tool == "get_related" else None
    if rows is None:
        return frozen
    merged: dict[str, dict[str, Any]] = {}
    for row in rows:
        row = {key: value for key, value in row.items() if key != "file"}
        if isinstance(row.get("weight"), float | int):
            row["weight"] = round(row["weight"], 3)
        seen = merged.get(row["file_path"])
        if seen is not None and {seen.get("direction"), row.get("direction")} == {"outgoing", "incoming"}:
            seen["direction"] = "both"
            if "weight" in seen:
                seen["weight"] = max(seen["weight"], row.get("weight", seen["weight"]))
            continue
        merged[row["file_path"]] = row
    return _with_text_rows(frozen, list(merged.values()), wrapped=True)


def _impact_capped(
    tool: str, frozen: dict[str, Any], arguments: Mapping[str, Any], session: ReplaySession
) -> dict[str, Any]:
    """R9: a 1.4.0 call without ``max_items`` keeps 20 paths per depth and says how many it left out."""
    del session
    rows = _text_rows(frozen) if tool == "get_impact" and arguments.get("max_items") is None else None
    if not rows:
        return frozen
    impact = dict(rows[0])
    omitted = {
        key: len(value) - IMPACT_MAX_ITEMS_2_0
        for key, value in impact.items()
        if key.startswith("depth_") and isinstance(value, list) and len(value) > IMPACT_MAX_ITEMS_2_0
    }
    if not omitted:
        return frozen
    for key in omitted:
        impact[key] = impact[key][:IMPACT_MAX_ITEMS_2_0]
    impact["omitted"] = omitted
    return _with_text_rows(frozen, [impact], wrapped=False)


_METADATA_LIMIT_2_0 = _with_properties(
    "search_by_metadata", {"limit": {"default": METADATA_LIMIT_2_0, "type": "integer"}}
)
_IMPACT_MAX_ITEMS_2_0 = _with_properties("get_impact", {"max_items": {"default": IMPACT_MAX_ITEMS_2_0, "type": "integer"}})


def _reading_protocol(frozen: dict[str, Any]) -> dict[str, Any]:
    """R17: the lines after the version line are the 2.0 reading protocol."""
    lines = str(frozen.get("instructions", "")).splitlines()
    return {**frozen, "instructions": "\n".join([*lines[:1], *contract.INSTRUCTIONS.splitlines()[1:]])}


def _reindex_without_pull(
    tool: str, frozen: dict[str, Any], arguments: Mapping[str, Any], session: ReplaySession
) -> dict[str, Any]:
    """R11: no ``export_hint``; a call without ``pull=true`` skips the git pull, and ``vault_sync`` is ``skipped_pull_false``."""
    del session
    rows = _text_rows(frozen) if tool == "reindex" else None
    if not rows:
        return frozen
    stats = {key: value for key, value in rows[0].items() if key != "export_hint"}
    if "pull" not in arguments and "vault_sync" in stats:
        stats["vault_sync"] = SYNC_SKIPPED_PULL_FALSE
    return frozen if stats == rows[0] else _with_text_rows(frozen, [stats], wrapped=False)


#: ``vault_sync`` of a reindex that did not pull (``ports.SYNC_SKIPPED_PULL_FALSE``).
SYNC_SKIPPED_PULL_FALSE = "skipped_pull_false"

_REINDEX_2_0 = _with_properties("reindex", {"pull": {"default": False, "type": "boolean"}})

#: The 1.4.0 text of ``sync_confluence`` without credentials: which settings it named, and where.
_UNCONFIGURED_1_4 = re.compile(r"^Confluence credentials not configured\. Set (?P<names>.+?) in (?P<where>your \.env file|the environment)\.$")


def _confluence_coded(
    tool: str, frozen: dict[str, Any], arguments: Mapping[str, Any], session: ReplaySession
) -> dict[str, Any]:
    """R12: the 1.4.0 ``{"error": ...}`` of a call without credentials is ``E_SECRET_MISSING``.

    The 1.4.0 text named every default variable whichever were empty; the
    2.0 message names the empty ones, so a default-credentials golden (all
    three empty in every recording) names all three.
    """
    del arguments, session
    text = _error_object(frozen, tool, "sync_confluence")
    match = _UNCONFIGURED_1_4.match(text) if text is not None else None
    if match is None:
        return frozen
    names = [name.strip() for name in re.split(r",\s*(?:and\s+)?|\s+and\s+", match["names"]) if name.strip()]
    default = match["where"] == "your .env file"
    sources = {} if default else _named_sources(names)
    empty = {"url": "url_source", "email": "email_env", "token": "token_env"}
    # A value 1.4.0 did not name was set; only the named ones (all three, by default) were empty.
    values = {key: "" if default or source in sources else "set" for key, source in empty.items()}
    creds = confluence_sync.Credentials(**values, **sources)
    return _tool_error_result(tool, errors.KnowledgeToolError(errors.E_SECRET_MISSING, confluence_sync.unconfigured_message(creds)))


def _named_sources(names: list[str]) -> dict[str, str]:
    """The ``Credentials`` sources a non-default 1.4.0 message named, URL first, then email and token."""
    sources: dict[str, str] = {}
    for name in names:
        if name.endswith("_URL") or name == "integrations.confluence.site":
            sources["url_source"] = name
        elif "EMAIL" in name:
            sources["email_env"] = name
        elif "TOKEN" in name:
            sources["token_env"] = name
    return sources


def _without_properties(tool_name: str, names: frozenset[str]) -> CatalogRule:
    """A catalog rule that removes ``names`` from the input schema of ``tool_name``."""

    def rule(frozen: dict[str, Any], session: ReplaySession) -> dict[str, Any]:
        del session
        tools = []
        for tool in frozen.get("tools", []):
            if tool["name"] == tool_name:
                schema = dict(tool.get("inputSchema", {}))
                schema["properties"] = {k: v for k, v in schema.get("properties", {}).items() if k not in names}
                if "required" in schema:
                    schema["required"] = [name for name in schema["required"] if name not in names]
                tool = {**tool, "inputSchema": schema}
            tools.append(tool)
        return {**frozen, "tools": tools}

    return rule


_CONFLUENCE_PARAMETERS_2_0 = _without_properties("sync_confluence", frozenset({"space_key", "page_id"}))

#: The 1.4.0 text of a flow tool for a provider without a flow document.
_NO_FLOW_1_4 = re.compile(r"^(?:Provider [12]: )?No flow file found for provider '(?P<provider>.*)'\.$")


def _flow_coded(
    tool: str, frozen: dict[str, Any], arguments: Mapping[str, Any], session: ReplaySession
) -> dict[str, Any]:
    """R13: the 1.4.0 ``{"error": ...}`` of a provider without a flow document is ``E_FLOW_NOT_FOUND``.

    ``get_flow`` names its provider. 1.4.0's ``compare_flows`` named only the
    first provider it missed; 2.0 names every one, so the call's arguments
    say which providers the replay vault has no flow for.
    """
    del session
    text = _error_object(frozen, tool, frozenset({"get_flow", "compare_flows"}))
    match = _NO_FLOW_1_4.match(text) if text is not None else None
    if match is None:
        return frozen
    if tool == "compare_flows":
        missing = [str(arguments.get(key)) for key in ("provider1", "provider2") if arguments.get(key) is not None]
        providers = missing if match["provider"] in missing else [match["provider"]]
    else:
        providers = [match["provider"]]
    return _tool_error_result(tool, flows.flow_not_found(providers))


_CONFLUENCE_RESPONSE, _CONFLUENCE_CATALOG = _gated_by("confluence")
_FLOWS_RESPONSE, _FLOWS_CATALOG = _gated_by("flows")
_GOVERNANCE_RESPONSE, _GOVERNANCE_CATALOG = _gated_by("governance")


#: Every rule 2.0.0 declares, by id. ``test_every_2_0_migration_is_declared_in_the_changelog``
#: keeps the ids equal to the rows of «Rompe» under ``## 2.0.0``.
MIGRATIONS_2_0: Mapping[str, Migration] = MappingProxyType(
    {
        "R1": Migration(
            rule="R1",
            summary="a default search row points (chunk_id, section) and drops excerpt and a title that is the file name",
            response=_pointer_rows("search"),
        ),
        "R2": Migration(
            rule="R2",
            summary="a default search_deep row is the search row plus discovered_via",
            response=_pointer_rows("search_deep"),
        ),
        "R3": Migration(
            rule="R3",
            summary="every limit is bounded (search 1-50, search_deep 1-20, suggest_links 1-200, get_stale_docs 1-500)",
        ),
        "R4": Migration(
            rule="R4",
            summary="get_document reads 3000 characters by default, metadata is a whitelist, file_path is optional",
            response=_shorter_document,
            catalog=_GET_DOCUMENT_2_0,
        ),
        "R5": Migration(
            rule="R5",
            summary="get_document answers a missing file or section with E_FILE_NOT_FOUND or E_SECTION_NOT_FOUND",
        ),
        "R6": Migration(
            rule="R6",
            summary="search_by_metadata answers {total, offset, items}: 20 rows by path unless limit (1-200) says",
            response=_metadata_page(facets=False),
            catalog=_METADATA_LIMIT_2_0,
        ),
        "R7": Migration(
            rule="R7",
            summary="search_by_metadata(facets=True) answers the same page of facet rows",
            response=_metadata_page(facets=True),
        ),
        "R8": Migration(
            rule="R8",
            summary="get_related drops file, merges a two-way link into direction both and rounds weight to 3",
            response=_related_pairs,
        ),
        "R9": Migration(
            rule="R9",
            summary="get_impact keeps 20 paths per depth unless max_items (0-1000) says, and reports omitted",
            response=_impact_capped,
            catalog=_IMPACT_MAX_ITEMS_2_0,
        ),
        "R10": Migration(
            rule="R10",
            summary="find_path answers an end outside the link graph with E_FILE_NOT_IN_GRAPH",
        ),
        "R11": Migration(
            rule="R11",
            summary="reindex does not pull unless pull=True says so, and carries no export_hint",
            response=_reindex_without_pull,
            catalog=_REINDEX_2_0,
        ),
        "R12": Migration(
            rule="R12",
            summary=(
                "sync_confluence is registered only with features: [confluence], drops space_key and page_id, "
                "and answers E_SECRET_MISSING or E_SCOPE_EMPTY instead of an error object"
            ),
            response=_chain(_CONFLUENCE_RESPONSE, _confluence_coded),
            catalog=_chain_catalog(_CONFLUENCE_CATALOG, _CONFLUENCE_PARAMETERS_2_0),
        ),
        "R13": Migration(
            rule="R13",
            summary=(
                "get_flow and compare_flows are registered only with features: [flows] and answer a provider "
                "without a flow document with E_FLOW_NOT_FOUND"
            ),
            response=_chain(_FLOWS_RESPONSE, _flow_coded),
            catalog=_FLOWS_CATALOG,
        ),
        "R14": Migration(
            rule="R14",
            summary="get_stale_docs and suggest_links are registered only with features: [governance]",
            response=_GOVERNANCE_RESPONSE,
            catalog=_GOVERNANCE_CATALOG,
        ),
        "R15": Migration(
            rule="R15",
            summary="search_code leaves the all profile: E_TOOL_RETIRED with the redirect; legacy still lists it",
            response=_retired_response,
            catalog=_retired_catalog,
        ),
        "R16": Migration(
            rule="R16",
            summary="the catalog lists the core tools first, then the extension tools a vault turns on",
            catalog=_core_then_extensions,
        ),
        "R17": Migration(
            rule="R17",
            summary="the instructions teach search(query) -> get_document(chunk_id=<chunk_id>)",
            handshake=_reading_protocol,
        ),
        "R18": Migration(
            rule="R18",
            summary="the outputSchema of search_by_metadata is the page object; R19 keeps outputSchema off by default",
        ),
        "R19": Migration(
            rule="R19",
            summary="KNOWLEDGE_STRUCTURED_OUTPUT defaults to 0: no structuredContent, no outputSchema",
            response=_without_structured_content,
            catalog=_without_output_schema,
        ),
        "R20": Migration(
            rule="R20",
            summary="include_metadata is retired: any value is E_ARGUMENT_RETIRED; full_metadata=True reads it all",
            catalog=_INCLUDE_METADATA_RETIRED,
        ),
        "R21": Migration(
            rule="R21",
            summary="search_code's _meta announces its removal with the legacy profile, in 2.1.0",
            catalog=_removal_announced,
        ),
    }
)


def _rule_number(rule: str) -> int:
    return int(rule.removeprefix("R"))


def migrate_1_4_to_2_0(
    name: str,
    frozen: dict[str, Any],
    arguments: Mapping[str, Any] | None = None,
    *,
    session: ReplaySession = ReplaySession(),
) -> dict[str, Any]:
    """The 1.4.0 golden ``name`` rewritten by every rule of :data:`MIGRATIONS_2_0`, in rule order.

    ``tools_list`` goldens go through the catalog rules, ``initialize`` through
    the handshake rules (its version line stays: it is version-bound), and
    every other golden through the response rules of its tool
    (``search__pointers`` is ``search``).
    """
    rules = sorted(MIGRATIONS_2_0.values(), key=lambda m: _rule_number(m.rule))
    migrated = json.loads(json.dumps(frozen))
    if name == "initialize":
        for migration in rules:
            if migration.handshake is not None:
                migrated = migration.handshake(migrated)
        return migrated
    if name == "tools_list" or name.startswith("tools_list__"):
        for migration in rules:
            if migration.catalog is not None:
                migrated = migration.catalog(migrated, session)
        return migrated
    tool = name.split("__", 1)[0]
    for migration in rules:
        if migration.response is not None:
            migrated = migration.response(tool, migrated, arguments or {}, session)
    return migrated
