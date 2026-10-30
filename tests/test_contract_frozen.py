"""The frozen goldens, byte for byte, in any Python.

A golden directory with a ``SHA256SUMS`` file is frozen: its files are exactly
the ones listed, with exactly those digests. This module only hashes files,
so it runs in every interpreter, including one that cannot load sqlite-vec
and therefore skips ``tests/test_contract_golden.py``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

GOLDEN_ROOT = Path(__file__).resolve().parent / "golden"
FROZEN_SUMS = "SHA256SUMS"

#: Versions that must stay frozen; a version that loses its ``SHA256SUMS`` fails here.
FROZEN_VERSIONS = ("v1.0.0", "v1.1.0", "v1.2.0", "v1.3.0", "v1.4.0", "v2.0.0")


def _frozen_dirs() -> list[Path]:
    return sorted(d for d in GOLDEN_ROOT.iterdir() if (d / FROZEN_SUMS).is_file())


def _sums(directory: Path) -> dict[str, str]:
    sums: dict[str, str] = {}
    for line in (directory / FROZEN_SUMS).read_text(encoding="utf-8").splitlines():
        digest, _, name = line.partition("  ")
        sums[name.strip()] = digest.strip()
    return sums


def test_the_released_versions_are_frozen() -> None:
    assert set(FROZEN_VERSIONS) <= {d.name for d in _frozen_dirs()}


@pytest.mark.parametrize("directory", _frozen_dirs(), ids=lambda d: d.name)
def test_a_frozen_version_is_exactly_the_bytes_it_lists(directory: Path) -> None:
    """A re-record, a hand edit or a stray file fails here, and the failure names the file."""
    sums = _sums(directory)
    present = {p.name for p in directory.glob("*.json")}

    assert present == set(sums), f"unlisted: {sorted(present - set(sums))}; missing: {sorted(set(sums) - present)}"
    changed = [
        name for name, digest in sorted(sums.items())
        if hashlib.sha256((directory / name).read_bytes()).hexdigest() != digest
    ]
    assert changed == [], f"frozen {directory.name} goldens changed: {changed}"


@pytest.mark.parametrize("directory", _frozen_dirs(), ids=lambda d: d.name)
def test_frozen_sums_are_bound_to_no_machine(directory: Path) -> None:
    assert "/" not in (directory / FROZEN_SUMS).read_text(encoding="utf-8"), "SHA256SUMS lists bare file names"
