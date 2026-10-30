"""The published artifacts carry the package and nothing else.

The vault, the deployment files and the operational scripts live in the
repository but are content or deployment material, not part of the
distribution. The sdist and the wheel are built for real and inspected, so a
change to the build configuration that starts shipping any of them fails here.
"""

from __future__ import annotations

import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.slow

FORBIDDEN_SDIST_PREFIXES = (
    "vault",
    ".mcp.json",
    "scripts",
    "infra",
    "lab",
    ".claude",
)


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    out_dir = tmp_path_factory.mktemp("dist")
    subprocess.run(
        ["uv", "build", "--out-dir", str(out_dir)],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    sdists = list(out_dir.glob("*.tar.gz"))
    wheels = list(out_dir.glob("*.whl"))
    assert len(sdists) == 1, sdists
    assert len(wheels) == 1, wheels
    return sdists[0], wheels[0]


def _sdist_relative_paths(sdist: Path) -> list[str]:
    with tarfile.open(sdist, "r:gz") as tar:
        names = tar.getnames()
    # Every member sits under a single "<name>-<version>/" root directory.
    return [name.split("/", 1)[1] for name in names if "/" in name]


def test_sdist_excludes_repository_content(built: tuple[Path, Path]) -> None:
    sdist, _ = built
    leaked = [
        path
        for path in _sdist_relative_paths(sdist)
        if path.split("/", 1)[0] in FORBIDDEN_SDIST_PREFIXES
    ]
    assert leaked == []


def test_sdist_only_carries_package_and_metadata(built: tuple[Path, Path]) -> None:
    sdist, _ = built
    allowed_top_level = {"src", "pyproject.toml", "README.md", "LICENSE", "PKG-INFO", ".gitignore"}
    unexpected = [
        path for path in _sdist_relative_paths(sdist) if path.split("/", 1)[0] not in allowed_top_level
    ]
    assert unexpected == []


def test_wheel_only_carries_package_and_dist_info(built: tuple[Path, Path]) -> None:
    _, wheel = built
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
    unexpected = [
        name
        for name in names
        if not (name.startswith("knowledge/") or name.split("/", 1)[0].endswith(".dist-info"))
    ]
    assert unexpected == []
    assert any(name.startswith("knowledge/") for name in names)
