"""Auth-flow documents: the steps of one provider's flow and the diff of two.

The ``flows`` extension. A flow document is any Markdown file of the vault
whose stem matches ``flow <provider>`` (``Flow - Comet Argentina.md``); its
steps are its numbered list items and its ``Step N`` headings. The shared
``Successful Login Flow`` document, when the vault has one, is appended to
every flow. The vault's memory folder is never read: consolidated memories may
mention a flow by name without being one.

Everything the functions read comes in as arguments, so this module depends on
nothing of the server; ``knowledge.server`` loads it the first time a flow
tool runs.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from knowledge import errors

log = logging.getLogger(__name__)


def extract_flow_steps(content: str) -> list[str]:
    """Extract ordered list items or header-based steps from markdown."""
    steps: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        # Numbered list items: 1. Step description
        if re.match(r"^\d+\.\s+", stripped):
            steps.append(re.sub(r"^\d+\.\s+", "", stripped))
        # Or heading-based steps: ### Step 1 — …
        elif stripped.startswith("#") and re.search(r"step\s*\d", stripped, re.IGNORECASE):
            steps.append(stripped.lstrip("#").strip())
    return steps


def _flow_file(vault: Path, memory_folder: str, provider: str) -> Path | None:
    """The first flow document of ``provider`` in ``vault``, outside the memory folder; ``None`` without one."""
    pattern = re.compile(rf"flow\s*-?\s*{re.escape(provider)}", re.IGNORECASE)
    for md in vault.rglob("*.md"):
        # Consolidated memories may mention flows by name — never treat them as flow docs.
        if md.relative_to(vault).parts[0] == memory_folder:
            continue
        if pattern.search(md.stem):
            return md
    return None


def flow_not_found(providers: list[str]) -> errors.KnowledgeToolError:
    """``E_FLOW_NOT_FOUND`` naming every provider the vault has no flow document for."""
    names = ", ".join(repr(provider) for provider in providers)
    return errors.KnowledgeToolError(
        errors.E_FLOW_NOT_FOUND,
        f"the vault has no flow document for provider {names}: a flow is a note named "
        "'Flow - <provider>.md'; search(query='flow') lists the ones it has",
    )


def get_flow(vault_path: Path, memory_folder: str, provider: str) -> dict[str, Any]:
    """The steps of ``provider``'s flow in the vault at ``vault_path``, plus the shared login steps.

    ``memory_folder`` is the vault's top-level memory folder, never searched.
    A provider without a flow document raises ``E_FLOW_NOT_FOUND``.
    """
    vault = vault_path
    flow_file = _flow_file(vault, memory_folder, provider)
    if flow_file is None:
        raise flow_not_found([provider])

    content = flow_file.read_text(errors="replace")
    steps = extract_flow_steps(content)

    # Append shared Successful Login Flow if it exists
    shared_steps: list[str] = []
    for md in vault.rglob("*.md"):
        if md.relative_to(vault).parts[0] == memory_folder:
            continue
        if "successful login" in md.stem.lower():
            shared_content = md.read_text(errors="replace")
            shared_steps = extract_flow_steps(shared_content)
            break

    return {
        "provider": provider,
        "file": str(flow_file.relative_to(vault)),
        "steps": steps,
        "shared_login_steps": shared_steps,
    }


def compare_flows(vault_path: Path, memory_folder: str, provider1: str, provider2: str) -> dict[str, Any]:
    """Diff two flows of the vault at ``vault_path``: common, only-in-1, only-in-2, plus shared login steps.

    A provider without a flow document raises ``E_FLOW_NOT_FOUND``, naming
    every provider that has none.
    """
    missing = [p for p in (provider1, provider2) if _flow_file(vault_path, memory_folder, p) is None]
    if missing:
        raise flow_not_found(missing)
    flow1 = get_flow(vault_path, memory_folder, provider1)
    flow2 = get_flow(vault_path, memory_folder, provider2)

    steps1 = flow1.get("steps", [])
    steps2 = flow2.get("steps", [])

    # Normalise for comparison (lowercase, strip provider-specific prefixes)
    def _normalise(step: str) -> str:
        s = step.lower().strip()
        for prefix in (provider1.lower(), provider2.lower()):
            s = s.replace(prefix, "").strip(" -—:")
        return s

    norm1 = {_normalise(s): s for s in steps1}
    norm2 = {_normalise(s): s for s in steps2}

    common_keys = set(norm1.keys()) & set(norm2.keys())
    only1_keys = set(norm1.keys()) - set(norm2.keys())
    only2_keys = set(norm2.keys()) - set(norm1.keys())

    # Both flows pull shared_login_steps from the same "Successful Login Flow"
    # file, so they should be byte-identical. If they ever diverge (e.g. two
    # files match the heuristic in get_flow), fall back to a deduped union and
    # warn so we notice the data drift.
    shared1 = flow1.get("shared_login_steps", [])
    shared2 = flow2.get("shared_login_steps", [])
    if shared1 == shared2:
        shared_login_steps = shared1
    else:
        log.warning(
            "compare_flows: shared_login_steps differ between %s and %s; "
            "merging deduped union as fallback.",
            provider1,
            provider2,
        )
        seen: set[str] = set()
        shared_login_steps = []
        for s in shared1 + shared2:
            if s not in seen:
                seen.add(s)
                shared_login_steps.append(s)

    return {
        "provider1": provider1,
        "provider2": provider2,
        "files": {
            provider1: flow1.get("file", ""),
            provider2: flow2.get("file", ""),
        },
        "total_steps": {
            provider1: len(steps1),
            provider2: len(steps2),
        },
        "common_steps": [
            {provider1: norm1[k], provider2: norm2[k]}
            for k in sorted(common_keys)
        ],
        "only_in_provider1": [norm1[k] for k in sorted(only1_keys)],
        "only_in_provider2": [norm2[k] for k in sorted(only2_keys)],
        "shared_login_steps": shared_login_steps,
    }
