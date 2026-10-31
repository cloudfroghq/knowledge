"""The declared tool contract version and where it is recorded."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest

from knowledge import contract, extensions, settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = PROJECT_ROOT / "docs" / "contract" / "CHANGELOG.md"
GOLDEN_ROOT = PROJECT_ROOT / "tests" / "golden"


def test_the_version_string_and_its_tuple_agree() -> None:
    assert tuple(int(p) for p in contract.CONTRACT_VERSION.split(".")) == contract.CONTRACT_VERSION_INFO
    assert len(contract.CONTRACT_VERSION_INFO) == 3


def test_settings_serves_the_declared_version() -> None:
    assert settings.TOOL_CONTRACT_VERSION == contract.CONTRACT_VERSION_INFO
    assert settings.contract_satisfied(f"=={contract.CONTRACT_VERSION}") is True
    assert settings.contract_satisfied(f">{contract.CONTRACT_VERSION}") is False


@pytest.mark.parametrize("profile", contract.PUBLIC_PROFILES)
def test_every_registered_tool_has_a_since_version_and_no_other_does(profile: str) -> None:
    """The registered catalog is the contract (plus the retired tools under legacy) minus the gated tools turned off."""
    from mcp.server.fastmcp import FastMCP

    from knowledge import server

    s = settings.current()
    app = FastMCP("probe")
    server.register_tools(app, structured=True, profile=profile)
    server.register_gated_tools(app, s, structured=True)
    registered = {tool.name for tool in asyncio.run(app.list_tools())}
    retired = set(contract.TOOL_REMOVED) if profile == contract.LEGACY_PROFILE else set()

    assert (set(contract.TOOL_SINCE) | retired) - extensions.gated_off_tools(s.vaults, s.http.transport, profile) == registered
    assert extensions.gated_tools() <= set(contract.TOOL_SINCE)


def test_retired_and_extension_tools_are_declared_consistently() -> None:
    """A retired tool leaves ``TOOL_SINCE``; every extension tool keeps its since version."""
    assert not set(contract.TOOL_REMOVED) & set(contract.TOOL_SINCE)
    assert set(contract.TOOL_EXTENSION) <= set(contract.TOOL_SINCE)
    assert set(contract.TOOL_EXTENSION.values()) <= set(extensions.features())
    for name, version in contract.TOOL_REMOVED.items():
        assert tuple(int(p) for p in version.split(".")) <= contract.CONTRACT_VERSION_INFO
        assert name in contract.TOOL_REDIRECTS and name in contract.DESCRIPTIONS
        assert contract.retired_message(name).startswith(f"{name} was retired in contract {version}; ")


def test_since_versions_are_not_newer_than_the_contract() -> None:
    version = contract.CONTRACT_VERSION_INFO
    for name, since in contract.TOOL_SINCE.items():
        assert tuple(int(p) for p in since.split(".")) <= version, name


def test_the_goldens_of_the_declared_version_exist() -> None:
    assert (GOLDEN_ROOT / f"v{contract.CONTRACT_VERSION}").is_dir()


def test_the_changelog_opens_with_the_declared_version() -> None:
    headings = re.findall(r"^## (\S+)\s*$", CHANGELOG.read_text(encoding="utf-8"), flags=re.MULTILINE)

    assert headings, "docs/contract/CHANGELOG.md has no version heading"
    assert headings[0] == contract.CONTRACT_VERSION
    assert "1.0.0" in headings


def test_every_frozen_golden_directory_has_a_changelog_entry() -> None:
    text = CHANGELOG.read_text(encoding="utf-8")
    for directory in sorted(GOLDEN_ROOT.glob("v*")):
        assert f"## {directory.name.removeprefix('v')}" in text, directory.name


# --- the handshake ------------------------------------------------------------

#: Clients truncate long ``instructions`` (Claude Code cuts them), so the field
#: stays short and the version never leaves the first line.
INSTRUCTIONS_LIMIT = 400


def test_the_instructions_open_with_the_contract_version() -> None:
    from knowledge import server

    assert server.mcp.instructions == contract.INSTRUCTIONS
    assert contract.INSTRUCTIONS.splitlines()[0] == f"knowledge-contract: {contract.CONTRACT_VERSION}"
    assert len(contract.INSTRUCTIONS) <= INSTRUCTIONS_LIMIT


# --- the changelog declares what the goldens show ------------------------------
#
# Each version's section declares what changed against the version before it:
# its new parameters, tools and goldens, and every golden whose bytes differ.

CURRENT = GOLDEN_ROOT / f"v{contract.CONTRACT_VERSION}"
CATALOG = "tools_list"


def _version_info(name: str) -> tuple[int, ...]:
    return tuple(int(part) for part in name.removeprefix("v").split("."))


#: The newest golden directory older than the declared version: what the
#: declared version's changelog section declares its changes against.
PREVIOUS = max(
    (d for d in GOLDEN_ROOT.glob("v*") if _version_info(d.name) < contract.CONTRACT_VERSION_INFO),
    key=lambda d: _version_info(d.name),
    default=GOLDEN_ROOT / "v1.0.0",
)

#: Subsections of a version that may declare a golden whose bytes changed.
#: A breaking version declares its changes under "Breaking".
DECLARING_SUBSECTIONS = ("Added", "Changed (non-additive)", "Breaking")

needs_a_newer_version = pytest.mark.skipif(
    contract.CONTRACT_VERSION == "1.0.0", reason="the baseline version declares no change against itself"
)


def _version_section(version: str) -> str:
    """The text under ``## <version>`` up to the next version heading."""
    text = CHANGELOG.read_text(encoding="utf-8")
    match = re.search(rf"^## {re.escape(version)}\s*$(.*?)(?=^## |\Z)", text, flags=re.MULTILINE | re.DOTALL)
    assert match, f"docs/contract/CHANGELOG.md has no ## {version}"
    return match.group(1)


def _subsections(section: str) -> dict[str, str]:
    """``### <name>`` blocks of one version section, by name."""
    parts = re.split(r"^### (.+?)\s*$", section, flags=re.MULTILINE)
    return {parts[i].strip(): parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def _named(name: str, text: str) -> bool:
    return f"`{name}`" in text


def _properties(catalog_path: Path) -> dict[str, set[str]]:
    tools = json.loads(catalog_path.read_text(encoding="utf-8"))["tools"]
    return {tool["name"]: set(tool.get("inputSchema", {}).get("properties", {})) for tool in tools}


@needs_a_newer_version
def test_every_new_parameter_and_tool_is_named_in_the_changelog() -> None:
    section = _version_section(contract.CONTRACT_VERSION)
    old, new = _properties(PREVIOUS / f"{CATALOG}.json"), _properties(CURRENT / f"{CATALOG}.json")

    unnamed = [
        f"{tool}.{param}"
        for tool, params in new.items()
        for param in sorted(params - old.get(tool, set()))
        if not _named(param, section)
    ]
    unnamed += [
        tool for tool, since in contract.TOOL_SINCE.items() if since == contract.CONTRACT_VERSION and not _named(tool, section)
    ]

    assert unnamed == [], f"new in {contract.CONTRACT_VERSION} but not in its changelog section: {unnamed}"


@needs_a_newer_version
def test_every_new_golden_is_named_in_the_changelog() -> None:
    section = _version_section(contract.CONTRACT_VERSION)
    added = sorted({p.stem for p in CURRENT.glob("*.json")} - {p.stem for p in PREVIOUS.glob("*.json")})

    assert [name for name in added if not _named(name, section)] == []


def _decoded(payload: Any) -> Any:
    """A golden with every JSON text block parsed, so a version can be compared inside it."""
    if not isinstance(payload, dict) or "content" not in payload:
        return payload
    blocks = []
    for block in payload["content"]:
        try:
            blocks.append({**block, "text": json.loads(block["text"])})
        except (json.JSONDecodeError, KeyError, TypeError):
            blocks.append(block)
    return {**payload, "content": blocks}


def _explained_by_the_migration(frozen: Path, live: Path) -> bool:
    """Whether a 1.4.0 golden, migrated by the declared 2.0 rules, is the live one but for the version.

    Only a breaking version replays its predecessor through a migration; the
    rules of that migration are the rows of "Breaking" (``golden_compat``), so a
    golden they fully explain is declared by them. The contract version
    itself (``list_vaults.contract`` and the first line of ``instructions``)
    is version-bound.
    """
    if contract.CONTRACT_VERSION_INFO[0] != 2 or PREVIOUS.name != "v1.4.0":
        return False
    from golden_compat import ReplaySession, migrate_1_4_to_2_0

    old, new = PREVIOUS.name.removeprefix("v"), contract.CONTRACT_VERSION
    current = json.loads(live.read_bytes())
    # The goldens of every recorded session share one directory: a golden is
    # explained when the migration of the session it was recorded in explains it.
    sessions = (
        ReplaySession(),
        ReplaySession(features=frozenset({"flows", "governance", "confluence"})),
        ReplaySession(features=frozenset({"jira"})),
        ReplaySession(profile=contract.LEGACY_PROFILE),
    )
    for session in sessions:
        migrated = migrate_1_4_to_2_0(frozen.stem, json.loads(frozen.read_bytes()), session=session)
        if frozen.stem == "initialize":
            migrated["instructions"] = migrated["instructions"].replace(
                f"knowledge-contract: {old}", f"knowledge-contract: {new}", 1
            )
            if migrated == current:
                return True
            continue
        decoded = _decoded(migrated)
        if frozen.stem == "list_vaults":
            for block in decoded["content"]:
                if isinstance(block.get("text"), dict) and block["text"].get("contract") == old:
                    block["text"]["contract"] = new
        if decoded == _decoded(current):
            return True
    return False


@needs_a_newer_version
def test_every_golden_whose_bytes_changed_is_declared() -> None:
    """A golden that differs from the previous version names its tool under Added, Changed (non-additive) or Breaking.

    The catalog is declared as ``tools/list``; the handshake as ``initialize``.
    A breaking version also declares a golden through the migration its
    "Breaking" rules define, when that migration explains every byte.
    """
    subsections = _subsections(_version_section(contract.CONTRACT_VERSION))
    declaring = "".join(subsections.get(name, "") for name in DECLARING_SUBSECTIONS)
    undeclared: list[str] = []
    for frozen in sorted(PREVIOUS.glob("*.json")):
        live = CURRENT / frozen.name
        if not live.is_file() or live.read_bytes() == frozen.read_bytes():
            continue
        if _explained_by_the_migration(frozen, live):
            continue
        declared_as = "tools/list" if frozen.stem == CATALOG else frozen.stem.split("__", 1)[0]
        if not _named(declared_as, declaring):
            undeclared.append(frozen.stem)

    assert undeclared == [], (
        f"goldens that differ from {PREVIOUS.name} without a row under {DECLARING_SUBSECTIONS}: {undeclared}"
    )


def test_subsections_are_parsed_by_name() -> None:
    section = "\n### Added\n\n- `a`\n\n### Changed (non-additive)\n\n- `b`\n"

    parsed = _subsections(section)

    assert set(parsed) == {"Added", "Changed (non-additive)"}
    assert "`b`" in parsed["Changed (non-additive)"]
    assert "`b`" not in parsed["Added"]
