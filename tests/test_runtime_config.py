"""How the static configuration file is read, and how graph targets stay portable.

Two failures must never look alike to a caller, and that is the shape of a gate
passing for the wrong reason:

* ``config.json`` is absent — every setting falls back to its built-in default;
* ``config.json`` is there but unreadable or malformed — nothing applies, and
  the caller cannot tell.

``config._load_config_json`` separates them: absent returns ``found=False``,
unreadable raises. These tests pin both with files they write themselves, and
read the ``graph.targets`` portability rule off the fixture vault's
``Contexts/config.json``. No repository file named ``config.json`` exists: a
vault keeps its registry in ``Contexts/``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from knowledge import config

# --- "not found" is never "could not look" ----------------------------------


def test_a_missing_config_file_is_reported_as_not_found(tmp_path: Path) -> None:
    settings, found = config._load_config_json(tmp_path / "config.json")

    assert (settings, found) == ({}, False)


def test_a_present_config_file_is_reported_as_found(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"review": {"thresholds_days": {"runbook": 7}}}))

    settings, found = config._load_config_json(path)

    assert found is True
    assert settings["review"]["thresholds_days"]["runbook"] == 7


def test_malformed_json_raises_instead_of_degrading_to_defaults(tmp_path: Path) -> None:
    """The failure this exists for: a truncated write, a bad merge conflict.

    Swallowing it hands every consumer a default while the real configuration
    sits one unreadable byte away.
    """
    path = tmp_path / "config.json"
    path.write_text('{"review": {"thresholds_days": {"runbook": 90,}}')

    with pytest.raises(RuntimeError, match="not valid JSON"):
        config._load_config_json(path)


def test_a_json_document_that_is_not_an_object_raises(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("[1, 2, 3]")

    with pytest.raises(RuntimeError, match="must hold a JSON object"):
        config._load_config_json(path)


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores the permission bits this test relies on",
)
def test_an_unreadable_config_file_raises(tmp_path: Path) -> None:
    """"I could not look" is the case a silent ``{}`` hides best."""
    path = tmp_path / "config.json"
    path.write_text("{}")
    path.chmod(0o000)
    try:
        with pytest.raises(RuntimeError, match="could not be read"):
            config._load_config_json(path)
    finally:
        path.chmod(0o600)


# ---------------------------------------------------------------------------
# graph.targets portability
#
# The entries in a vault's Contexts/config.json graph.targets name a registry
# repository and a relative subpath under it. A revert to absolute paths would
# still pass a check that reads the resolved value or touches the filesystem,
# because on the machine where the revert gets written those absolute paths
# exist. So the case below asserts the *shape* of what the vault declares,
# which is false on every machine the moment someone writes a home directory
# into it.
# ---------------------------------------------------------------------------

# Prefixes that pin a config to one machine. An absolute POSIX path under a
# user's home, or a Windows drive letter; "~" is not expanded by the resolver
# either, so it would land as a literal directory name.
_MACHINE_SPECIFIC = ("/Users/", "/home/", "/root/", "~")

CONTEXTS_FIXTURE = Path(__file__).parent / "fixtures" / "vaults" / "contexts" / "Contexts" / "config.json"


def test_resolve_relative_path_anchors_a_relative_path_to_the_project_root() -> None:
    resolved = Path(config._resolve_relative_path("../sibling-checkout/apps/service"))
    assert resolved.is_absolute(), "a resolved path must be absolute"
    assert resolved == config._PROJECT_ROOT / "../sibling-checkout/apps/service"


def test_resolve_relative_path_leaves_an_absolute_path_alone() -> None:
    # The control. Without it a resolver that joined unconditionally would still
    # satisfy the case above, and this file would be asserting nothing about the
    # branch that makes the config portable.
    absolute = "/opt/somewhere/middleware"
    assert config._resolve_relative_path(absolute) == absolute


def test_a_vault_declares_no_machine_specific_graph_targets() -> None:
    """A vault's graph targets must not pin themselves to one machine's filesystem.

    This reads the declaration rather than ``graph_targets.GRAPH_TARGETS``: the
    resolved value necessarily contains whatever directory the checkout lives
    in, so it cannot tell a portable declaration from an absolute one. The raw
    declaration can.

    Nothing here is asserted about paths existing on disk. That is the trap a
    battery falls into — on the machine where absolute paths get written, they
    resolve to real directories and the check passes.
    """
    raw = json.loads(CONTEXTS_FIXTURE.read_text(encoding="utf-8"))
    entries = raw.get("graph", {}).get("targets", [])

    # An empty list would satisfy every assertion below while checking nothing.
    # "I looked and found no bad path" and "there was nothing to look at" are
    # different answers, and only the first one is a pass.
    assert entries, f"{CONTEXTS_FIXTURE} declares no graph.targets — this test cannot certify a file it did not read"

    offenders = [
        (entry.get("repo", "?"), entry["path"])
        for entry in entries
        if str(entry["path"]).startswith(_MACHINE_SPECIFIC) or Path(str(entry["path"])).is_absolute()
    ]
    assert not offenders, (
        "graph.targets must be relative so the same declaration works on every machine; "
        f"these are pinned to one filesystem: {offenders}"
    )
