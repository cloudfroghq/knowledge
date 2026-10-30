"""No module of the core imports anything by name at run time.

import-linter and grimp only see ``import`` statements, so a module that
imports another through ``importlib`` or ``__import__`` crosses a layer without
any contract noticing. The core describes the optional extensions as dotted
paths in strings (``knowledge.extensions``) precisely so that it never has to
import one; ``knowledge.server`` is the one module that turns those strings
into modules. This reads the source of each module (AST, no import) and fails
on any dynamic import outside the server.

The core is read from the ``core-isolated`` contract of ``.importlinter``, so a
module added to the core is covered the moment it joins the contract.
"""

from __future__ import annotations

import ast
import configparser
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE = REPO_ROOT / "src" / "knowledge"

#: The only module allowed to call ``importlib.import_module``.
LOADER = "server"

#: Names that resolve a module from a string.
_DYNAMIC_CALLS = frozenset({"import_module", "__import__"})


def _core_modules() -> list[str]:
    parser = configparser.ConfigParser()
    parser.read(REPO_ROOT / ".importlinter")
    raw = parser["importlinter:contract:core-isolated"]["source_modules"]
    return sorted(line.strip().removeprefix("knowledge.") for line in raw.splitlines() if line.strip())


def _all_modules() -> list[str]:
    return sorted(path.stem for path in PACKAGE.glob("*.py") if path.stem not in {"__init__", "__main__"})


def _tree(module: str) -> ast.Module:
    path = PACKAGE / f"{module}.py"
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _importlib_uses(tree: ast.Module) -> list[str]:
    """Every ``import importlib…`` / ``from importlib… import …`` in ``tree``."""
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [f"line {node.lineno}: import {a.name}" for a in node.names if a.name.split(".")[0] == "importlib"]
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "importlib":
            found.append(f"line {node.lineno}: from {node.module} import …")
    return found


def _dynamic_calls(tree: ast.Module) -> list[str]:
    """Every call or reference to ``import_module`` or ``__import__`` in ``tree``."""
    found: list[str] = []
    for node in ast.walk(tree):
        name = None
        if isinstance(node, ast.Name) and node.id in _DYNAMIC_CALLS:
            name = node.id
        elif isinstance(node, ast.Attribute) and node.attr in _DYNAMIC_CALLS:
            name = node.attr
        elif isinstance(node, ast.alias) and node.name in _DYNAMIC_CALLS:
            name = node.name
        if name is not None:
            found.append(f"line {getattr(node, 'lineno', '?')}: {name}")
    return found


def test_the_core_is_read_from_the_contract() -> None:
    core = _core_modules()

    assert "extensions" in core and "settings" in core
    assert LOADER not in core


@pytest.mark.parametrize("module", _core_modules())
def test_no_core_module_uses_importlib(module: str) -> None:
    tree = _tree(module)

    assert _importlib_uses(tree) == [], f"knowledge.{module} imports importlib"
    assert _dynamic_calls(tree) == [], f"knowledge.{module} imports a module by name"


@pytest.mark.parametrize("module", [m for m in _all_modules() if m != LOADER])
def test_only_the_server_imports_a_module_by_name(module: str) -> None:
    """``importlib.metadata`` (the CLI's version lookup) is fine; ``import_module`` is not."""
    assert _dynamic_calls(_tree(module)) == [], f"knowledge.{module} imports a module by name"


def test_the_server_is_where_extensions_load() -> None:
    assert _dynamic_calls(_tree(LOADER)), "knowledge.server does not load any extension by name"


def test_the_check_catches_each_form() -> None:
    """The detectors see every spelling, so a green run means something."""
    sources = {
        "import importlib": 1,
        "import importlib.util": 1,
        "from importlib import import_module": 1,
    }
    for source, expected in sources.items():
        assert len(_importlib_uses(ast.parse(source))) == expected, source

    for source in (
        "importlib.import_module('x')",
        "from importlib import import_module",
        "__import__('x')",
        "m = import_module",
    ):
        assert _dynamic_calls(ast.parse(source)), source
