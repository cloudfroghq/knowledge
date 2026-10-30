"""The Claude Code plugin under ``plugin/claude-code``: manifests and session hook.

No network and no real ``uvx``: the manifests are read as JSON, and the
SessionStart hook runs against a stub that stands in for ``uvx``. The
``@v<version>`` tag the ``.mcp.json`` names is checked against ``plugin.json``
only; whether that tag already exists in the remote is the release check's job
(``scripts/check-version.sh``), so a branch can carry the next version before
its tag is cut.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import time
import tomllib
from pathlib import Path
from typing import Any

import pytest

from knowledge import cli

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN = REPO_ROOT / "plugin" / "claude-code"
MANIFEST = PLUGIN / ".claude-plugin" / "plugin.json"
MCP_JSON = PLUGIN / ".mcp.json"
HOOKS = PLUGIN / "hooks" / "hooks.json"
HOOK_SCRIPT = PLUGIN / "scripts" / "session-start.sh"

PACKAGE_SOURCE = "git+ssh://git@github.com/cloudfroghq/knowledge.git"
TOOL_PREFIX = "mcp__plugin_knowledge_knowledge__"
MEMORY_PREFIX = "mcp__plugin_memory_memory__"
#: The prefix of the retired ``cloudfrog@cloudfrog`` install of memory. Nothing
#: the plugin ships may name it; ``memory@cloudfroghq`` is the only install.
RETIRED_MEMORY_PREFIX = "mcp__plugin_cloudfrog_memory__"
#: Bound on the line the hook prints, the one ``doctor --brief`` keeps. The line
#: break is not counted: stdout is at most this many bytes plus one.
LINE_MAX_BYTES = 200
_USER_CONFIG_REF = re.compile(r"\$\{user_config\.([A-Za-z0-9_]+)\}")
_CREDENTIAL_SHAPE = re.compile(r"(?:sk-|ghp_|gho_|github_pat_|xox[bp]-)[A-Za-z0-9_-]{8,}|[A-Za-z0-9+/_-]{32,}")


def _json(path: Path) -> Any:
    assert path.is_file(), f"missing: {path.relative_to(REPO_ROOT)}"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def manifest() -> dict[str, Any]:
    return _json(MANIFEST)


@pytest.fixture(scope="module")
def server() -> dict[str, Any]:
    servers = _json(MCP_JSON)["mcpServers"]
    assert list(servers) == ["knowledge"], "the server key sets the tool prefix; it stays 'knowledge'"
    return servers["knowledge"]


# ---------------------------------------------------------------------------
# plugin.json
# ---------------------------------------------------------------------------


def test_the_manifest_names_the_plugin_knowledge(manifest: dict[str, Any]) -> None:
    # The manifest name, not the marketplace entry, namespaces the tools.
    assert manifest["name"] == "knowledge"
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"])


def test_the_user_config_declares_the_two_options(manifest: dict[str, Any]) -> None:
    options = manifest["userConfig"]
    assert set(options) == {"vaults_config", "embeddings_provider"}
    assert options["vaults_config"]["type"] == "file"
    assert options["embeddings_provider"]["type"] == "string"
    for name, option in options.items():
        assert option.get("title") and option.get("description"), name
        assert not option.get("sensitive"), f"{name}: the plugin stores no secret"
    for provider in ("openai", "none", "bag-of-words"):
        assert provider in options["embeddings_provider"]["description"]


# ---------------------------------------------------------------------------
# .mcp.json
# ---------------------------------------------------------------------------


def test_the_server_runs_the_tagged_package_over_stdio(
    server: dict[str, Any], manifest: dict[str, Any]
) -> None:
    assert server["type"] == "stdio"
    assert server["command"] in ("uvx", "${UVX_BIN:-uvx}")
    args = server["args"]
    # A uv-managed CPython loads SQLite extensions; a system Python may not,
    # and would rank vectors with the slower standard-library fallback.
    assert args[:3] == ["--managed-python", "--python", "3.12"]
    assert args[3] == "--from"
    assert args[4] == f"{PACKAGE_SOURCE}@v{manifest['version']}"
    assert args[5:] == ["cloudfrog-knowledge", "serve", "--transport", "stdio"]


def test_the_source_pins_the_package_version() -> None:
    # The release this plugin ships with, the one pyproject.toml declares; the tag may not exist yet.
    package = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = package["project"]["version"]
    assert f"{PACKAGE_SOURCE}@v{version}" in MCP_JSON.read_text(encoding="utf-8")


def test_the_options_map_to_the_server_environment(server: dict[str, Any]) -> None:
    assert server["env"] == {
        "KNOWLEDGE_VAULTS": "${user_config.vaults_config}",
        "KNOWLEDGE_EMBEDDINGS_PROVIDER": "${user_config.embeddings_provider}",
    }


def test_no_home_override_and_no_key_mapping() -> None:
    # KNOWLEDGE_HOME would move the indexes into plugin data that an uninstall
    # deletes; an OPENAI_API_KEY mapped from an unset option would export an
    # empty string over the inherited key.
    for path in (MCP_JSON, MANIFEST, HOOKS):
        text = path.read_text(encoding="utf-8")
        assert "KNOWLEDGE_HOME" not in text, path.name
        assert "OPENAI_API_KEY" not in text, path.name
        assert "CLAUDE_PLUGIN_DATA" not in text, path.name


def test_every_user_config_reference_is_declared(manifest: dict[str, Any]) -> None:
    referenced = set(_USER_CONFIG_REF.findall(MCP_JSON.read_text(encoding="utf-8")))
    assert referenced
    assert referenced <= set(manifest["userConfig"])


def test_no_literal_credential_in_the_server_environment(server: dict[str, Any]) -> None:
    for name, value in server["env"].items():
        literal = _USER_CONFIG_REF.sub("", value)
        assert not _CREDENTIAL_SHAPE.search(literal), name


# ---------------------------------------------------------------------------
# skills
# ---------------------------------------------------------------------------


def _frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), path
    head = text.split("---\n", 2)[1]
    return dict(line.split(": ", 1) for line in head.splitlines() if ": " in line)


def test_skills_reach_knowledge_through_the_plugin_prefix() -> None:
    skills = sorted(PLUGIN.glob("skills/*/SKILL.md"))
    assert [p.parent.name for p in skills] == ["code-question", "repair-mcp-tool", "search-first"]
    for path in skills:
        meta = _frontmatter(path)
        assert meta["name"] == path.parent.name
        tools = [t.strip() for t in meta["allowed-tools"].split(",")]
        assert any(t.startswith(TOOL_PREFIX) for t in tools), path
        assert not any(t.startswith("mcp__cloudfrog-knowledge__") for t in tools), path


def test_memory_tools_are_listed_under_the_memory_plugin_prefix() -> None:
    """``repair-mcp-tool`` saves its fix to memory through ``memory@cloudfroghq``."""
    tools = [t.strip() for t in _frontmatter(PLUGIN / "skills" / "repair-mcp-tool" / "SKILL.md")["allowed-tools"].split(",")]

    assert f"{MEMORY_PREFIX}mem_save" in tools


def test_no_plugin_file_names_the_retired_memory_prefix() -> None:
    """The plugin lists memory tools under one prefix; the retired one appears in no file it ships.

    The skills under ``.claude/`` are held to the same rule once their
    protected-path change lands; until then this covers ``plugin/`` only.
    """
    named = [
        str(path.relative_to(REPO_ROOT))
        for path in sorted(PLUGIN.rglob("*"))
        if path.is_file() and RETIRED_MEMORY_PREFIX in path.read_text(encoding="utf-8", errors="replace")
    ]

    assert named == []


def test_search_first_teaches_the_pointer_protocol() -> None:
    """The skill follows the two-step protocol of the 2.0 ``instructions``: default rows, then the chunk."""
    body = (PLUGIN / "skills" / "search-first" / "SKILL.md").read_text(encoding="utf-8")
    assert 'search(query="$ARGUMENTS")\n' in body
    assert "get_document(chunk_id=<chunk_id>)\n" in body
    assert "fields=" not in body and "max_chars=2000" not in body
    assert "`search_code` se retiró" in body and "full_metadata=true" in body


def test_search_first_explains_the_rerank_of_the_vault() -> None:
    body = (PLUGIN / "skills" / "search-first" / "SKILL.md").read_text(encoding="utf-8")
    assert 'rerank="on"' in body and "rerank_score" in body and "E_RERANK_NOT_CONFIGURED" in body
    assert "`code-question`" in body


def test_code_question_runs_jg_on_a_narrow_root_with_a_byte_cap_and_never_on_a_vault() -> None:
    path = PLUGIN / "skills" / "code-question" / "SKILL.md"
    meta = _frontmatter(path)
    body = path.read_text(encoding="utf-8")
    tools = [t.strip() for t in meta["allowed-tools"].split(",")]
    assert "Bash(jg:*)" in tools
    assert 'jg "$ARGUMENTS" <carpeta estrecha> --max-source-bytes 20000' in body
    assert "## Nunca sobre un vault" in body
    assert "jg auth" in body and "Nunca pidas una clave en el chat" in body


def test_the_plugin_skills_carry_no_team_specifics() -> None:
    for path in PLUGIN.glob("skills/*/SKILL.md"):
        text = path.read_text(encoding="utf-8")
        for word in ("EXMP", "bkt ", "cd-knowledge-mcp", "Bitbucket"):
            assert word not in text, f"{path.parent.name}: {word}"


# ---------------------------------------------------------------------------
# SessionStart hook
# ---------------------------------------------------------------------------


def test_the_hook_runs_the_session_start_script() -> None:
    [entry] = _json(HOOKS)["hooks"]["SessionStart"]
    [hook] = entry["hooks"]
    assert hook["type"] == "command"
    assert hook["command"] == '"${CLAUDE_PLUGIN_ROOT}"/scripts/session-start.sh'
    assert hook["timeout"] > 8
    assert HOOK_SCRIPT.stat().st_mode & stat.S_IXUSR


def _stub_uvx(tmp_path: Path, body: str) -> Path:
    stub = tmp_path / "uvx"
    stub.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    stub.chmod(0o755)
    return stub


def _line(stdout: str) -> bytes:
    """The single line the hook printed, without its line break."""
    assert stdout.endswith("\n") and stdout.count("\n") == 1, repr(stdout)
    return stdout[:-1].encode("utf-8")


def _calls(path: Path) -> list[list[str]]:
    """The uvx argument lists a stub recorded, one per invocation."""
    return [line.split() for line in path.read_text(encoding="utf-8").splitlines()]


def _run_hook(uvx: Path | str, tmp_path: Path, **extra: str) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "CLAUDE_PLUGIN_ROOT": str(PLUGIN),
        "UVX_BIN": str(uvx),
        # Off unless a test turns it on: the background Jira sync is a second
        # uvx call the doctor tests do not expect.
        "KNOWLEDGE_JIRA_AUTOSYNC": "0",
        **extra,
    }
    return subprocess.run(
        [str(HOOK_SCRIPT)], env=env, capture_output=True, text=True, timeout=30, check=False
    )


def test_the_hook_is_silent_when_the_doctor_is_happy(tmp_path: Path) -> None:
    record = tmp_path / "record"
    stub = _stub_uvx(
        tmp_path,
        f'printf "%s\\n" "$@" > "{record}"\n'
        f'printf "vaults=%s provider=%s\\n" "$KNOWLEDGE_VAULTS" "$KNOWLEDGE_EMBEDDINGS_PROVIDER" >> "{record}"\n'
        'echo "knowledge doctor: ok"\n',
    )

    result = _run_hook(
        stub,
        tmp_path,
        CLAUDE_PLUGIN_OPTION_VAULTS_CONFIG="/srv/vaults.yaml",
        CLAUDE_PLUGIN_OPTION_EMBEDDINGS_PROVIDER="none",
    )

    assert result.returncode == 0
    assert result.stdout == ""
    seen = record.read_text(encoding="utf-8").splitlines()
    spec = _json(MCP_JSON)["mcpServers"]["knowledge"]["args"][4]
    assert seen[:-1] == [
        "--quiet", "--managed-python", "--python", "3.12", "--from", spec, "cloudfrog-knowledge", "doctor", "--brief"
    ]
    assert seen[-1] == "vaults=/srv/vaults.yaml provider=none"


def test_the_hook_line_bound_is_the_doctor_brief_bound() -> None:
    declared = re.search(r"^MAX_BYTES=(\d+)$", HOOK_SCRIPT.read_text(encoding="utf-8"), re.MULTILINE)
    assert declared is not None
    assert int(declared.group(1)) == cli.BRIEF_MAX_BYTES == LINE_MAX_BYTES


def test_the_hook_forwards_one_bounded_line_on_a_problem(tmp_path: Path) -> None:
    stub = _stub_uvx(tmp_path, 'printf "knowledge doctor: %0300d\\n" 0\nexit 1\n')

    result = _run_hook(stub, tmp_path)

    assert result.returncode == 0
    line = _line(result.stdout)
    assert len(line) == LINE_MAX_BYTES  # 318 bytes in, cut at the bound, line break excluded
    assert len(result.stdout.encode("utf-8")) == LINE_MAX_BYTES + 1
    assert line.startswith(b"knowledge doctor: 000")


_PACKAGE_FAILS = "echo 'git operation failed' >&2\nexit 2\n"
_FAILED_PREFIX = (
    "knowledge: server package failed (uvx rc 2); diagnose: uvx --managed-python --python 3.12 --from 'git+ssh://"
)


def _marker(cache_home: Path) -> Path:
    return cache_home / "knowledge" / "session-start-package-failed"


def test_the_hook_names_the_doctor_when_the_package_does_not_start(tmp_path: Path) -> None:
    stub = _stub_uvx(tmp_path, _PACKAGE_FAILS)

    result = _run_hook(stub, tmp_path)

    assert result.returncode == 0
    line = _line(result.stdout)
    assert len(line) <= LINE_MAX_BYTES
    assert line.decode("utf-8").startswith(_FAILED_PREFIX)
    assert line.endswith(b"cloudfrog-knowledge doctor")
    assert b"git operation failed" not in line


def test_a_package_that_does_not_start_is_reported_once_per_day(tmp_path: Path) -> None:
    stub = _stub_uvx(tmp_path, _PACKAGE_FAILS)

    first = _run_hook(stub, tmp_path)
    second = _run_hook(stub, tmp_path)
    third = _run_hook(stub, tmp_path)

    assert first.stdout.startswith("knowledge: server package failed")
    assert (second.stdout, third.stdout) == ("", "")
    assert [first.returncode, second.returncode, third.returncode] == [0, 0, 0]
    # The marker lives under $HOME/.cache when XDG_CACHE_HOME is not set.
    stamp = _marker(tmp_path / ".cache").read_text(encoding="utf-8")
    assert stamp.endswith(f" {_json(MCP_JSON)['mcpServers']['knowledge']['args'][4]}\n")


def test_the_marker_follows_xdg_cache_home(tmp_path: Path) -> None:
    stub = _stub_uvx(tmp_path, _PACKAGE_FAILS)
    cache = tmp_path / "xdg"

    first = _run_hook(stub, tmp_path, XDG_CACHE_HOME=str(cache))
    second = _run_hook(stub, tmp_path, XDG_CACHE_HOME=str(cache))

    assert first.stdout and second.stdout == ""
    assert _marker(cache).is_file()
    assert not (tmp_path / ".cache").exists()


@pytest.mark.parametrize(
    "stale",
    ["19990101 {spec}", "{today} git+ssh://git@github.com/cloudfroghq/knowledge.git@v0.0.1", ""],
    ids=["another-day", "another-release", "empty"],
)
def test_a_stale_marker_reports_again(tmp_path: Path, stale: str) -> None:
    stub = _stub_uvx(tmp_path, _PACKAGE_FAILS)
    spec = _json(MCP_JSON)["mcpServers"]["knowledge"]["args"][4]
    marker = _marker(tmp_path / ".cache")
    marker.parent.mkdir(parents=True)
    marker.write_text(stale.format(spec=spec, today=time.strftime("%Y%m%d")) + "\n", encoding="utf-8")

    result = _run_hook(stub, tmp_path)

    assert result.stdout.startswith("knowledge: server package failed")


def test_an_unwritable_marker_never_silences_the_report(tmp_path: Path) -> None:
    stub = _stub_uvx(tmp_path, _PACKAGE_FAILS)
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("", encoding="utf-8")

    first = _run_hook(stub, tmp_path, XDG_CACHE_HOME=str(blocker))
    second = _run_hook(stub, tmp_path, XDG_CACHE_HOME=str(blocker))

    assert first.returncode == second.returncode == 0
    assert first.stdout == second.stdout != ""
    assert first.stderr == second.stderr == ""


def test_a_doctor_problem_is_not_rate_limited(tmp_path: Path) -> None:
    stub = _stub_uvx(tmp_path, 'echo "knowledge doctor: 1 vault problem"\nexit 1\n')

    first = _run_hook(stub, tmp_path)
    second = _run_hook(stub, tmp_path)

    assert first.stdout == second.stdout == "knowledge doctor: 1 vault problem\n"
    assert not _marker(tmp_path / ".cache").exists()


def test_the_hook_reports_a_missing_uvx(tmp_path: Path) -> None:
    result = _run_hook(tmp_path / "no-such-uvx", tmp_path)

    assert result.returncode == 0
    assert _line(result.stdout).startswith(b"knowledge: uvx not found")
    assert len(_line(result.stdout)) <= LINE_MAX_BYTES


def test_the_hook_gives_up_quietly_on_a_cold_cache(tmp_path: Path) -> None:
    # The stub keeps a child alive, like uvx keeps the interpreter it spawned:
    # the budget has to end both, or the output pipe stays open.
    calls = tmp_path / "calls"
    stub = _stub_uvx(tmp_path, f'echo "$*" >> "{calls}"\nsleep 30\necho late\n')

    started = time.monotonic()
    result = _run_hook(stub, tmp_path, KNOWLEDGE_DOCTOR_BUDGET="1")
    elapsed = time.monotonic() - started

    assert result.returncode == 0
    assert result.stdout == ""
    assert elapsed < 10
    assert len(_calls(calls)) == 1  # a doctor the budget killed is not retried


def _doctor_then_slow_sync(tmp_path: Path, doctor: str) -> tuple[Path, Path]:
    """A uvx stub whose doctor answers ``doctor`` at once and whose ``jira sync`` takes five seconds."""
    calls, done = tmp_path / "calls", tmp_path / "sync-done"
    stub = _stub_uvx(
        tmp_path,
        f'echo "$*" >> "{calls}"\n'
        'case "$*" in\n'
        f'  *"jira sync --auto"*) sleep 5; echo done > "{done}" ;;\n'
        f"  *) {doctor} ;;\n"
        "esac\n",
    )
    return stub, calls


def _wait_for(path: Path, needle: str, seconds: float = 5.0) -> list[str]:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        if any(needle in line for line in lines):
            return lines
        time.sleep(0.05)
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def test_a_happy_doctor_starts_the_jira_sync_detached(tmp_path: Path) -> None:
    stub, calls = _doctor_then_slow_sync(tmp_path, 'echo "knowledge doctor: ok"')

    started = time.monotonic()
    result = _run_hook(stub, tmp_path, KNOWLEDGE_JIRA_AUTOSYNC="")
    elapsed = time.monotonic() - started

    assert result.returncode == 0
    assert result.stdout == "" and result.stderr == ""
    assert elapsed < 4, "the hook never waits for the sync"
    lines = _wait_for(calls, "jira sync --auto")
    spec = _json(MCP_JSON)["mcpServers"]["knowledge"]["args"][4]
    assert lines[0].endswith("cloudfrog-knowledge doctor --brief")
    assert lines[1] == f"--quiet --managed-python --python 3.12 --from {spec} cloudfrog-knowledge jira sync --auto"


def test_a_pending_init_reaches_the_session_and_the_sync_still_starts(tmp_path: Path) -> None:
    """``doctor`` exits 0 with a line that is not the ``ok`` one: the hook forwards it and carries on."""
    pending = "knowledge doctor: init pending for vault(s) acme; run: cloudfrog-knowledge init"
    stub, calls = _doctor_then_slow_sync(tmp_path, f'echo "{pending}"')

    result = _run_hook(stub, tmp_path, KNOWLEDGE_JIRA_AUTOSYNC="")

    assert result.returncode == 0
    assert result.stdout == pending + "\n"
    assert any("jira sync --auto" in line for line in _wait_for(calls, "jira sync --auto"))


@pytest.mark.parametrize(
    ("doctor", "extra"),
    [
        pytest.param('echo "knowledge doctor: E_VAULT_PATH_MISSING"; exit 1', {"KNOWLEDGE_JIRA_AUTOSYNC": ""}, id="doctor-problem"),
        pytest.param('echo "knowledge doctor: ok"', {"KNOWLEDGE_JIRA_AUTOSYNC": "0"}, id="turned-off"),
    ],
)
def test_no_jira_sync_without_a_happy_doctor_or_when_turned_off(
    tmp_path: Path, doctor: str, extra: dict[str, str]
) -> None:
    stub, calls = _doctor_then_slow_sync(tmp_path, doctor)

    result = _run_hook(stub, tmp_path, **extra)
    time.sleep(0.3)

    assert result.returncode == 0
    assert len(_calls(calls)) == 1


# ---------------------------------------------------------------------------
# claude plugin validate
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("claude") is None, reason="the claude binary is not installed")
def test_claude_plugin_validate_strict_passes() -> None:
    result = subprocess.run(
        ["claude", "plugin", "validate", "--strict", str(PLUGIN)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
