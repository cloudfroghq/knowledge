"""Deterministic work counters for the performance budgets: exact counts, never a clock.

A budget test states how much work an operation may do (SQL statements, rows
fetched, files opened, YAML parses, calls of a function) and asserts the exact
number. Nothing here measures time, so the same run gives the same numbers on a
laptop and on the Linux pipeline.

Nothing is patched. Each helper uses an observation point the platform provides:

* ``sql_trace(store)``: ``SQLiteStore.observe`` attaches SQLite's own trace
  callback and a ``row_factory`` that returns every row unchanged to each
  connection of the store (``src/knowledge/stores.py``).
* ``file_opens(root)``: a PEP 578 audit hook (``open`` events), installed once per
  process and idle outside a block.
* ``count_calls(fn, ...)`` and ``yaml_parses()``: ``sys.monitoring`` (Python 3.12)
  ``PY_START`` events on the code object of each function, with no wrapper.

Import them explicitly, in the style of ``golden_compat``::

    from perf_counters import count_calls, file_opens, sql_trace, yaml_parses

    with sql_trace(runtime.indexer.store) as trace:
        search(...)
    assert len(trace.statements) == 3
    assert trace.rows_fetched <= 60
    assert trace.selects_column("docs", "body") == 0
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# SQL: statements executed and rows fetched
# ---------------------------------------------------------------------------

_BLOB = re.compile(r"[xX]'[0-9a-fA-F]*'")
_STRING = re.compile(r"'(?:[^']|'')*'")
_NUMBER = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?(?![\w.])")
_PLACEHOLDERS = re.compile(r"\(\s*\?(?:\s*,\s*\?)+\s*\)")
_SPACES = re.compile(r"\s+")
#: What SQLite traces for itself, not for the caller: virtual-table and trigger
#: statements (``-- ...``) and everything addressed to ``'main'.`` (FTS5's
#: shadow tables, ``PRAGMA data_version``).
_INTERNAL = re.compile(r"^\s*--|'main'\.")
_DML = re.compile(r"^\s*(?:INSERT|UPDATE|DELETE|REPLACE)\b", re.IGNORECASE)


def normalize_sql(statement: str) -> str:
    """The shape of ``statement``: whitespace collapsed, literals and parameter lists replaced by ``?``.

    The trace callback receives the statement with its parameters expanded, so
    two runs of one query with different arguments only compare equal here.
    """
    text = _BLOB.sub("?", statement)
    text = _STRING.sub("?", text)
    text = _NUMBER.sub("?", text)
    text = _PLACEHOLDERS.sub("(?...)", text)
    return _SPACES.sub(" ", text).strip()


@dataclass
class SqlTrace:
    """Everything the observed connections ran and returned while a :func:`sql_trace` block was open.

    ``statements`` holds the statements the application executed; ``internal``
    holds what SQLite ran for itself (FTS5 shadow-table reads, ``data_version``
    polls), which a budget should not depend on. Both are in execution order.
    """

    statements: list[str] = field(default_factory=list)
    internal: list[str] = field(default_factory=list)
    #: Trace events dropped as the repeat of a DML statement (see :meth:`_on_statement`).
    collapsed: int = 0
    #: Rows handed to a caller by ``fetchone``/``fetchall``/iteration, in total.
    rows_fetched: int = 0
    #: Rows fetched, by the column names of the cursor that produced them.
    rows_by_columns: Counter[tuple[str, ...]] = field(default_factory=Counter)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    _last: dict[int, str] = field(default_factory=dict, repr=False, compare=False)

    # -- observers (called by the store's connections, from any thread) --------

    def _on_statement(self, statement: str) -> None:
        """Record one trace event.

        SQLite reports a trigger firing as one more event carrying the outer
        statement's text, and CPython shows no way to tell it from a real
        execution (``chunks`` has one ``AFTER INSERT`` trigger and each insert
        is traced three times). So an ``INSERT``, ``UPDATE``, ``DELETE`` or
        ``REPLACE`` identical to the previous event of the same thread is not
        counted again; ``collapsed`` says how many were dropped. A real
        back-to-back repeat of one DML statement with identical parameters is
        collapsed too, which no code of the server does. ``SELECT`` events are
        exact: reads fire no trigger.
        """
        with self._lock:
            if _INTERNAL.search(statement):
                self.internal.append(statement)
                return
            thread = threading.get_ident()
            if _DML.match(statement) and self._last.get(thread) == statement:
                self.collapsed += 1
                return
            self._last[thread] = statement
            self.statements.append(statement)

    def _on_row(self, cursor: sqlite3.Cursor, row: tuple[Any, ...]) -> tuple[Any, ...]:
        names = tuple(column[0] for column in (cursor.description or ()))
        with self._lock:
            self.rows_fetched += 1
            self.rows_by_columns[names] += 1
        return row

    # -- queries ---------------------------------------------------------------

    def shapes(self) -> Counter[str]:
        """How many times each normalized statement ran."""
        return Counter(normalize_sql(statement) for statement in self.statements)

    def count(self, regex: str, *, flags: int = re.IGNORECASE) -> int:
        """Statements whose text matches ``regex`` (searched, case-insensitive by default)."""
        pattern = re.compile(regex, flags)
        return sum(1 for statement in self.statements if pattern.search(statement))

    @property
    def selects(self) -> int:
        return self.count(r"^\s*(?:SELECT|WITH)\b")

    @property
    def writes(self) -> int:
        """``INSERT``, ``UPDATE``, ``DELETE`` and ``REPLACE`` statements."""
        return self.count(r"^\s*(?:INSERT|UPDATE|DELETE|REPLACE)\b")

    @property
    def commits(self) -> int:
        return self.count(r"^\s*(?:COMMIT|END)\b")

    def selects_column(self, table: str, column: str) -> int:
        """Statements that read ``column`` of ``table``: a ``SELECT`` that names the column (or ``*``) and the table.

        A textual check on the select list, enough for a budget such as "no
        search reads ``docs.body``"; it does not parse SQL.
        """
        named = re.compile(rf"(?<![\w]){re.escape(column)}(?![\w])", re.IGNORECASE)
        from_table = re.compile(rf"\b(?:FROM|JOIN)\s+{re.escape(table)}\b", re.IGNORECASE)
        total = 0
        for statement in self.statements:
            head = re.match(r"\s*(?:SELECT|WITH)\b(.*?)\bFROM\b", statement, re.IGNORECASE | re.DOTALL)
            if head is None or not from_table.search(statement):
                continue
            select_list = head.group(1)
            if named.search(select_list) or re.search(r"(?:^|[\s,.])\*", select_list):
                total += 1
        return total

    def rows_with_column(self, column: str) -> int:
        """Rows fetched by cursors that returned a column named ``column``."""
        return sum(n for names, n in self.rows_by_columns.items() if column in names)


def _resolve_store(target: Any) -> Any:
    """The ``SQLiteStore`` behind ``target``: a store, an indexer (``.store``) or a runtime (``.indexer.store``)."""
    for candidate in (target, getattr(target, "store", None), getattr(getattr(target, "indexer", None), "store", None)):
        if candidate is not None and callable(getattr(candidate, "observe", None)):
            return candidate
    raise TypeError(f"{type(target).__name__} has no SQLite connections to observe (is it a SQLiteStore?)")


@contextmanager
def sql_trace(target: Any) -> Iterator[SqlTrace]:
    """Count the SQL of every connection of ``target``'s store while the block runs.

    ``target`` is a ``SQLiteStore``, a ``VaultIndexer`` or a ``VaultRuntime``.
    Reading threads that open a connection inside the block are observed too,
    and a connection's own setup pragmas are not counted.
    """
    store = _resolve_store(target)
    trace = SqlTrace()
    store.observe(trace=trace._on_statement, row=trace._on_row)
    try:
        yield trace
    finally:
        store.observe()


# ---------------------------------------------------------------------------
# Files: opens under a root
# ---------------------------------------------------------------------------

_O_WRITE = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND


@dataclass
class FileOpens:
    """Files opened under a root while a :func:`file_opens` block was open."""

    root: str
    #: ``(path relative to root, "r" | "w", thread name)`` in order. A read-write open counts as a write.
    events: list[tuple[str, str, str]] = field(default_factory=list)

    def count(self, name_glob: str = "*", *, kind: str | None = None) -> int:
        return sum(1 for rel, k, _ in self.events if fnmatch.fnmatch(rel, name_glob) and kind in (None, k))

    def reads(self, name_glob: str = "*") -> int:
        """Opens for reading of paths matching ``name_glob`` (``fnmatch`` on the posix relative path)."""
        return self.count(name_glob, kind="r")

    def writes(self, name_glob: str = "*") -> list[str]:
        """Thread names of the opens for writing of paths matching ``name_glob``, in order."""
        return [thread for rel, k, thread in self.events if k == "w" and fnmatch.fnmatch(rel, name_glob)]

    def paths(self, kind: str = "r") -> list[str]:
        return [rel for rel, k, _ in self.events if k == kind]


_collectors: list[tuple[tuple[str, ...], FileOpens]] = []
_collectors_lock = threading.Lock()
_hook_installed = False


def _audit(event: str, args: tuple[Any, ...]) -> None:
    if event != "open" or not _collectors:
        return
    raw, flags = args[0], args[2]
    if not isinstance(raw, (str, bytes, os.PathLike)):
        return  # a file descriptor
    try:
        path = os.path.abspath(os.fsdecode(raw))
    except (TypeError, ValueError):
        return
    kind = "w" if isinstance(flags, int) and flags & _O_WRITE else "r"
    thread = threading.current_thread().name
    for prefixes, sink in tuple(_collectors):
        for prefix in prefixes:
            if path == prefix or path.startswith(prefix + os.sep):
                rel = Path(path).relative_to(prefix).as_posix()
                sink.events.append((rel, kind, thread))
                break


@contextmanager
def file_opens(root: Path) -> Iterator[FileOpens]:
    """Record every file opened under ``root`` by any thread of this process while the block runs.

    Uses an audit hook (``sys.addaudithook`` cannot be removed, so one hook is
    installed per process and does nothing while no block is open).
    """
    global _hook_installed
    absolute = os.path.abspath(root)
    prefixes = tuple(dict.fromkeys((absolute, os.path.realpath(root))))
    sink = FileOpens(root=absolute)
    with _collectors_lock:
        if not _hook_installed:
            sys.addaudithook(_audit)
            _hook_installed = True
        _collectors.append((prefixes, sink))
    try:
        yield sink
    finally:
        with _collectors_lock:
            _collectors.remove((prefixes, sink))


# ---------------------------------------------------------------------------
# Calls of a Python function, without wrapping it
# ---------------------------------------------------------------------------


@dataclass
class CallCount:
    """Calls of the watched functions while a :func:`count_calls` block was open."""

    #: ``(qualified name, thread name)`` per call, in order.
    calls: list[tuple[str, str]] = field(default_factory=list)

    def count(self, name: str | None = None) -> int:
        """Total calls, or the calls of the function whose qualified name ends with ``name``."""
        return sum(1 for qualname, _ in self.calls if name is None or qualname.endswith(name))

    def threads(self, name: str | None = None) -> set[str]:
        return {thread for qualname, thread in self.calls if name is None or qualname.endswith(name)}


@contextmanager
def count_calls(*targets: Callable[..., Any]) -> Iterator[CallCount]:
    """Count the calls of each pure-Python function or method in ``targets`` (``sys.monitoring``, 3.12+).

    The functions are not replaced or wrapped: the interpreter reports each
    start of their code objects. A bound method, a function or an unbound
    method works; a builtin or C function does not (``TypeError``). Watch the
    function where it is defined: a call through ``functools.partial`` or a
    subclass override reaches the code object it resolves to.
    """
    monitoring = sys.monitoring
    codes = {}
    for target in targets:
        function = getattr(target, "__func__", target)
        code = getattr(function, "__code__", None)
        if code is None:
            raise TypeError(f"{target!r} is not a pure-Python function; sys.monitoring cannot watch it")
        codes[code] = getattr(function, "__qualname__", code.co_name)

    tool = next((t for t in (3, 4, 2) if monitoring.get_tool(t) is None), None)
    if tool is None:
        raise RuntimeError("no sys.monitoring tool id is free (is a debugger or profiler attached?)")
    counter = CallCount()

    def on_start(code: Any, offset: int) -> None:
        del offset
        counter.calls.append((codes[code], threading.current_thread().name))

    monitoring.use_tool_id(tool, "knowledge-perf-counters")
    try:
        monitoring.register_callback(tool, monitoring.events.PY_START, on_start)
        for code in codes:
            monitoring.set_local_events(tool, code, monitoring.events.PY_START)
        yield counter
    finally:
        for code in codes:
            monitoring.set_local_events(tool, code, 0)
        monitoring.register_callback(tool, monitoring.events.PY_START, None)
        monitoring.free_tool_id(tool)


def yaml_parses() -> Any:
    """``count_calls`` on PyYAML's single-document parse: one count per YAML document the process loads.

    ``yaml.load``, ``yaml.safe_load`` and python-frontmatter's handler all end in
    ``BaseConstructor.get_single_data``. A loader built on libyaml (``CSafeLoader``)
    would bypass it; the indexer and ``vault-lint`` use the pure-Python ``SafeLoader``.
    """
    import yaml.constructor

    return count_calls(yaml.constructor.BaseConstructor.get_single_data)


# ---------------------------------------------------------------------------
# Imports of a command, in a clean process
# ---------------------------------------------------------------------------

_MODULES_SCRIPT = """
import json, sys
out, argv = sys.argv[1], sys.argv[2:]
try:
    from knowledge import cli
    cli.main(argv)
except SystemExit:
    pass
with open(out, "w", encoding="utf-8") as handle:
    json.dump(sorted(sys.modules), handle)
"""


def modules_after_cli(argv: list[str], env: Mapping[str, str] | None = None) -> set[str]:
    """The ``sys.modules`` keys of a fresh interpreter after ``knowledge.cli.main(argv)`` returns.

    Runs a subprocess (``-I`` is not used: the project is importable from the
    environment), with ``env`` as its whole environment when given. The
    command's own output is discarded.
    """
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "modules.json"
        subprocess.run(
            [sys.executable, "-c", _MODULES_SCRIPT, str(out), *argv],
            env=dict(env) if env is not None else None,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=120,
        )
        return set(json.loads(out.read_text(encoding="utf-8")))
