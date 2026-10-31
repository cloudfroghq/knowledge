"""Import budget of the command line: what each subcommand loads, counted in a fresh interpreter.

``doctor --brief`` runs on every Claude Code SessionStart, so a subcommand that
only reads a vault or a registry must not pay for the MCP SDK. The budget is a
set of module names in ``sys.modules`` after ``knowledge.cli.main`` returns (a
subprocess, nothing patched), never a clock.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from perf_counters import modules_after_cli

from knowledge import contract

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_VAULT = REPO_ROOT / "tests" / "fixtures" / "vault"
CONTEXTS_VAULT = REPO_ROOT / "tests" / "fixtures" / "vaults" / "contexts"

#: Top-level packages the server pulls in and no read-only subcommand needs.
HEAVY = frozenset({"mcp", "starlette", "uvicorn"})


def _cli_env(tmp_path: Path, vault: Path = FIXTURE_VAULT) -> dict[str, str]:
    """A hermetic environment for a CLI subprocess: its own HOME, one vault, no judge, no memory."""
    env = dict(os.environ)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env.update(
        HOME=str(home),
        XDG_CONFIG_HOME=str(home / ".config"),
        KNOWLEDGE_HOME=str(tmp_path / "knowledge-home"),
        KNOWLEDGE_EMBEDDINGS_PROVIDER="bag-of-words",
        VAULT_PATH=str(vault),
        KNOWLEDGE_VAULTS="",
        CHROMA_PERSIST_DIR=str(tmp_path / "index"),
        MEMORY_HTTP_TOKEN="",
        KNOWLEDGE_MEMORY_URL="",
    )
    return env


def _heavy(modules: set[str]) -> list[str]:
    return sorted(name for name in modules if name.split(".")[0] in HEAVY)


COMMANDS = [
    pytest.param(["doctor"], id="doctor"),
    pytest.param(["doctor", "--brief"], id="doctor-brief"),
    pytest.param(["doctor", "--client"], id="doctor-client"),
    pytest.param(["--version"], id="version"),
    pytest.param(["vault-lint", str(FIXTURE_VAULT)], id="vault-lint"),
    pytest.param(["init", "--dry-run"], id="init-dry-run"),
]


@pytest.mark.parametrize("argv", COMMANDS)
def test_read_only_subcommands_do_not_load_the_server_or_the_mcp_sdk(argv: list[str], tmp_path: Path) -> None:
    vault = CONTEXTS_VAULT if argv[0] == "init" else FIXTURE_VAULT

    modules = modules_after_cli(argv, _cli_env(tmp_path, vault))

    assert "knowledge.cli" in modules, "the subprocess did run the command line"
    assert "knowledge.server" not in modules
    assert _heavy(modules) == []


def test_importing_the_command_line_alone_does_not_load_the_server(tmp_path: Path) -> None:
    # `main([])` would serve, so the bare import is measured with its own script.
    script = (
        "import json, sys\n"
        "from knowledge import cli\n"
        "print(json.dumps(sorted(m for m in sys.modules if m == 'knowledge.server' "
        "or m.split('.')[0] in ('mcp', 'starlette', 'uvicorn'))))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], env=_cli_env(tmp_path), capture_output=True, text=True, timeout=120, check=True
    )
    assert result.stdout.strip() == "[]"


def test_serve_does_load_the_server(tmp_path: Path) -> None:
    """The counter above is not blind: a subcommand that serves imports the server and the SDK.

    ``--profile`` with an unknown name stops in the configuration check, after
    ``serve`` imported the server and before it starts listening, so nothing blocks.
    """
    modules = modules_after_cli(["serve", "--transport", "stdio", "--profile", "no-such-profile"], _cli_env(tmp_path))

    assert "knowledge.server" in modules
    assert "mcp" in modules


def test_doctor_brief_output_is_unchanged_by_the_lazy_import(tmp_path: Path) -> None:
    """Characterization: the line a SessionStart hook forwards, from a real process."""
    result = subprocess.run(
        [sys.executable, "-m", "knowledge", "doctor", "--brief"],
        env=_cli_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=120,
        cwd=REPO_ROOT,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"knowledge doctor: ok, contract {contract.CONTRACT_VERSION}, 1 vault(s), default vault\n"
    assert result.stderr == ""
