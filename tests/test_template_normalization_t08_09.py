"""Regression tests for the vault template normalization.

The five runbook templates under ``<vault>/Runbooks/Templates`` carry
``type: template`` with a ``verified`` status, and the two hidden duplicate
copies are absent from the vault. These cases fail if either is reverted.

The vault root is read from ``knowledge.config.VAULT_PATH``, which ``conftest``
points at ``tests/fixtures/vault/`` before anything imports the package, and
``_anchor_vault`` then proves that is where it landed. That keeps this module on
the same vault as the rest of the suite, and it keeps the path anchored to a
checkout instead of to one developer's home directory: an absolute path
elsewhere is a silent failure everywhere else, and the two cases that assert an
*absence* go green without inspecting anything when their root does not exist.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from knowledge import config

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_ROOT = Path(__file__).resolve().parent / "fixtures"
VAULT_BASE = Path(config.VAULT_PATH)
TEMPLATES_DIR = VAULT_BASE / "Runbooks" / "Templates"

TEMPLATES = (
    "Auth Issue Template.md",
    "File Save Failure Template.md",
    "Missing Files Issue Template.md",
    "Registration Issue Template.md",
    "Sync Issue Template.md",
)

# The normalization validator ships with the sibling ai-architecture checkout,
# which is not guaranteed on any given machine. AI_ARCH_ROOT is the override the
# create-runbook scripts already use to name that same checkout.
VALIDATOR_REL = Path(
    "ai-platform-plan/05-runbooks/migracion/plantillas/scripts/validar-plantillas.sh"
)

# File that identifies the memory fork checkout: the validator's stage 8 reads
# it to confirm the runbook index filter still selects these templates.
MEMORY_MARKER = Path("internal/store/projects_runbooks.go")

# Exit codes the validator reserves for "this could not run": 77 when a root it
# needs did not resolve, 2 when a tool it needs is missing. Neither is a pass
# and neither is a finding about the templates.
VALIDATOR_SKIP_EXITS = frozenset({2, 77})


def _anchor_vault() -> None:
    """Fail when the vault root is not a real directory under the fixtures.

    Call this before any check that asserts a file is *gone*. Such an assertion
    is satisfied by a path that was never there, so a root pointing at nothing
    turns the whole battery green without reading a single file. Anchoring the
    root first makes a broken root a failure instead of a false pass.

    Existing is not enough, though. ``VAULT_BASE`` follows
    ``knowledge.config.VAULT_PATH``, which outside the suite names the real
    Obsidian vault -- a directory that does exist, so an ``is_dir`` check alone
    waves it through and the absence cases go green over the owner's own
    documents. No test in this repository reads that vault, and this is where
    that rule is enforced rather than assumed. The bound is derived from this
    file's own location, so moving the test tree moves the guard with it.
    """
    vault = VAULT_BASE.resolve()
    fixtures = FIXTURES_ROOT.resolve()
    assert vault.is_relative_to(fixtures), (
        f"vault root points outside the test fixtures: {vault}. It must live "
        f"under {fixtures} -- conftest sets VAULT_PATH to the fixture vault "
        f"before knowledge.config is imported, and no case in this module may "
        f"read any other vault"
    )
    assert VAULT_BASE.is_dir(), (
        f"vault root not found at {VAULT_BASE}: the absence checks in this "
        f"module would pass without inspecting anything"
    )
    assert TEMPLATES_DIR.is_dir(), (
        f"templates directory not found at {TEMPLATES_DIR}: the absence checks "
        f"in this module would pass without inspecting anything"
    )


def _find_checkout(marker: Path, env_var: str) -> Path | None:
    """Locate a sibling checkout by a file it contains, or None when absent.

    ``env_var`` wins when it is set. Otherwise the siblings of this repository
    are scanned for one carrying ``marker``, so the lookup never depends on what
    a directory happens to be named on a given machine.
    """
    declared = os.environ.get(env_var)
    if declared:
        root = Path(declared)
        return root if (root / marker).is_file() else None

    for sibling in sorted(REPO_ROOT.parent.iterdir()):
        if sibling.is_dir() and (sibling / marker).is_file():
            return sibling
    return None


def test_templates_exist() -> None:
    """The five normalized templates are present in the vault."""
    _anchor_vault()

    missing = [tpl for tpl in TEMPLATES if not (TEMPLATES_DIR / tpl).is_file()]
    assert not missing, f"templates missing from {TEMPLATES_DIR}: {missing}"


def test_duplicates_removed() -> None:
    """The two hidden duplicate copies are gone from the vault."""
    _anchor_vault()

    paths_to_check = [
        TEMPLATES_DIR / ".space" / "templates" / "Auth Issue Template1.md",
        VAULT_BASE / ".trash" / "Auth Issue Template1.md",
    ]

    present = [str(path.relative_to(VAULT_BASE)) for path in paths_to_check if path.exists()]
    assert not present, f"duplicates should have been removed but exist: {present}"


def test_templates_are_type_template() -> None:
    """Each template declares type: template and a verified status."""
    _anchor_vault()

    for tpl in TEMPLATES:
        path = TEMPLATES_DIR / tpl
        assert path.is_file(), f"template {tpl} not found at {path}"

        content = path.read_text(encoding="utf-8")
        parts = content.split("---")
        assert len(parts) >= 2, f"template {tpl} has no frontmatter block"
        frontmatter = parts[1]

        assert "type: template" in frontmatter, (
            f"template {tpl} does not declare 'type: template' in its frontmatter"
        )
        assert "status: verified" in frontmatter, (
            f"template {tpl} does not declare 'status: verified' in its frontmatter"
        )
        assert "PENDIENTE-FIRMA-OWNER" not in content, (
            f"template {tpl} still carries the PENDIENTE-FIRMA-OWNER marker"
        )


def test_validator_passes() -> None:
    """The normalization validator reports nine passing stages and no failures."""
    _anchor_vault()

    arch_root = _find_checkout(VALIDATOR_REL, "AI_ARCH_ROOT")
    if arch_root is None:
        pytest.skip(
            f"validator not found at <ai-architecture>/{VALIDATOR_REL}; "
            f"export AI_ARCH_ROOT to point at that checkout"
        )
    validator = arch_root / VALIDATOR_REL

    # The validator resolves the vault through MEMORY_VAULT_ROOT and exits with
    # its SKIP code when that is unset, so declare it rather than inheriting
    # whatever the caller's shell happens to carry. --vault only steers the
    # coverage stage; both point at this checkout's vault.
    env = {**os.environ, "MEMORY_VAULT_ROOT": str(VAULT_BASE)}

    # Stage 8 reads the memory fork's source to confirm the runbook index filter
    # still matches these templates. Declare it when the checkout is on this
    # machine; when it is not, the validator reports its own SKIP and the case
    # below turns that into a visible skip.
    memory_root = _find_checkout(MEMORY_MARKER, "CD_MEMORY_ROOT")
    if memory_root is not None:
        env["CD_MEMORY_ROOT"] = str(memory_root)

    result = subprocess.run(
        ["bash", str(validator), "--vault", str(VAULT_BASE)],
        capture_output=True,
        text=True,
        cwd=validator.parent.parent,
        env=env,
        check=False,
    )

    report = f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"

    # The validator's own three-state contract: a dependency it needs is absent,
    # which is neither a pass nor a failure of what it inspects. Report that as
    # a skip so it stays visible in the summary, never as green.
    if result.returncode in VALIDATOR_SKIP_EXITS:
        pytest.skip(f"validator could not run. {report}")

    assert "PASS: 9" in result.stdout, f"validator did not report nine passes. {report}"
    assert "FAIL: 0" in result.stdout, f"validator reported failures. {report}"
    assert result.returncode == 0, f"validator exited with {result.returncode}. {report}"
