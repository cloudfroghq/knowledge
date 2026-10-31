"""The npm wrapper under ``npm/`` agrees with the rest of the repository.

Its own behaviour is tested with ``node --test`` inside ``npm/``; this file
checks what only the repository can see: the license it ships is this one, the
uv it downloads is the one the workflows install, and the Python it asks uv
for is the one the package supports and the plugin requests. The version is
``scripts/check-version.sh``'s job (``test_version_scripts.py``).
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
NPM = REPO_ROOT / "npm"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"


def test_the_package_ships_the_repository_license_unchanged() -> None:
    assert (NPM / "LICENSE").read_bytes() == (REPO_ROOT / "LICENSE").read_bytes()
    assert json.loads((NPM / "package.json").read_text(encoding="utf-8"))["license"] == "MIT"


def test_the_wrapper_and_the_workflows_pin_the_same_uv() -> None:
    release = json.loads((NPM / "lib" / "uv-release.json").read_text(encoding="utf-8"))
    pinned = {
        version
        for workflow in ("ring1.yml", "release-check.yml")
        for version in re.findall(r"astral\.sh/uv/(\d+\.\d+\.\d+)/install\.sh", (WORKFLOWS / workflow).read_text())
    }
    assert pinned == {release["version"]}, f"workflows install uv {sorted(pinned)}, the wrapper {release['version']}"


def test_the_wrapper_asks_uv_for_the_supported_python() -> None:
    requires = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["requires-python"]
    floor = re.search(r">=\s*(\d+\.\d+)", requires)
    assert floor, requires
    run = (NPM / "lib" / "run.js").read_text(encoding="utf-8")
    assert f"const PYTHON = ['--managed-python', '--python', '{floor.group(1)}'];" in run
    plugin = json.loads((REPO_ROOT / "plugin" / "claude-code" / ".mcp.json").read_text(encoding="utf-8"))
    assert plugin["mcpServers"]["knowledge"]["args"][:3] == ["--managed-python", "--python", floor.group(1)]
