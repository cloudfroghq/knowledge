"""``scripts/bump-version.sh`` and ``scripts/check-version.sh`` on a throwaway checkout.

Each case builds a minimal tree (``pyproject.toml``, ``uv.lock``, a copy of
the plugin and the npm wrapper's ``package.json``) under ``tmp_path`` and points the scripts at it through
``KNOWLEDGE_ROOT``. ``bump-version.sh`` runs with ``--no-lock``: refreshing the
lock is ``uv lock``'s job and needs the package index.

``test_the_real_tree_agrees_on_the_package_version`` is the exception: it runs
``check-version.sh`` on this repository, so a version bump that misses one of the
places that name the version fails the suite.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BUMP = REPO_ROOT / "scripts" / "bump-version.sh"
CHECK = REPO_ROOT / "scripts" / "check-version.sh"
PLUGIN = REPO_ROOT / "plugin" / "claude-code"
NPM_MANIFEST = REPO_ROOT / "npm" / "package.json"

_PYPROJECT = """[project]
name = "cloudfrog-knowledge"
version = "{version}"

[tool.other]
version = "9.9.9"
"""

_LOCK = """version = 1

[[package]]
name = "anyio"
version = "4.0.0"

[[package]]
name = "cloudfrog-knowledge"
version = "{version}"
source = {{ editable = "." }}
"""


def _tree(
    tmp_path: Path, *, version: str = "0.2.0", plugin: str = "0.2.0", npm: str | None = None, lock: bool = True
) -> Path:
    root = tmp_path / "checkout"
    shutil.copytree(PLUGIN, root / "plugin" / "claude-code")
    (root / "npm").mkdir()
    package = json.loads(NPM_MANIFEST.read_text(encoding="utf-8"))
    package["version"] = plugin if npm is None else npm
    (root / "npm" / "package.json").write_text(json.dumps(package, indent=2) + "\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(_PYPROJECT.format(version=version), encoding="utf-8")
    if lock:
        (root / "uv.lock").write_text(_LOCK.format(version=version), encoding="utf-8")
    manifest = root / "plugin" / "claude-code" / ".claude-plugin" / "plugin.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["version"] = plugin
    manifest.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    mcp = root / "plugin" / "claude-code" / ".mcp.json"
    text = mcp.read_text(encoding="utf-8")
    current = json.loads(text)["mcpServers"]["knowledge"]["args"][4].rsplit("==", 1)[1]
    mcp.write_text(text.replace(f"cloudfrog-knowledge=={current}", f"cloudfrog-knowledge=={plugin}"), encoding="utf-8")
    return root


def _run(script: Path, root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "KNOWLEDGE_ROOT": str(root)}
    return subprocess.run(
        [str(script), *args], env=env, cwd=root, capture_output=True, text=True, timeout=60, check=False
    )


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
         "-c", "tag.gpgsign=false", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


def test_check_passes_when_every_place_agrees(tmp_path: Path) -> None:
    result = _run(CHECK, _tree(tmp_path))

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "pyproject.toml: 0.2.0",
        "plugin.json: 0.2.0",
        ".mcp.json: 0.2.0",
        "npm/package.json: 0.2.0",
        "uv.lock: 0.2.0",
        "check-version: ok (0.2.0)",
    ]


def test_the_real_tree_agrees_on_the_package_version() -> None:
    # No copy and no KNOWLEDGE_ROOT: the script resolves the repository it lives in.
    env = {k: v for k, v in os.environ.items() if k != "KNOWLEDGE_ROOT"}
    result = subprocess.run(
        [str(CHECK)], env=env, cwd=REPO_ROOT, capture_output=True, text=True, timeout=60, check=False
    )

    assert result.returncode == 0, result.stdout + result.stderr
    sources = dict(line.split(": ", 1) for line in result.stdout.splitlines() if ": " in line and "check-version" not in line)
    assert {"pyproject.toml", "plugin.json", ".mcp.json", "npm/package.json", "uv.lock"} <= set(sources)
    assert len(set(sources.values())) == 1, sources


def test_check_fails_when_the_plugin_drifts(tmp_path: Path) -> None:
    result = _run(CHECK, _tree(tmp_path, version="0.1.0", plugin="0.2.0"))

    assert result.returncode == 1
    assert "versions disagree" in result.stderr


def test_check_fails_when_the_npm_wrapper_drifts(tmp_path: Path) -> None:
    result = _run(CHECK, _tree(tmp_path, npm="0.1.0"))

    assert result.returncode == 1
    assert "npm/package.json: 0.1.0" in result.stdout
    assert "versions disagree" in result.stderr


def test_check_without_the_npm_manifest_is_a_usage_error(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    (root / "npm" / "package.json").unlink()

    result = _run(CHECK, root)
    assert result.returncode == 2
    assert "cannot read npm/package.json" in result.stderr


def test_check_without_a_lock_compares_the_rest(tmp_path: Path) -> None:
    result = _run(CHECK, _tree(tmp_path, lock=False))

    assert result.returncode == 0
    assert "uv.lock" not in result.stdout


def test_check_compares_a_given_tag(tmp_path: Path) -> None:
    root = _tree(tmp_path)

    assert _run(CHECK, root, "v0.2.0").returncode == 0
    mismatch = _run(CHECK, root, "v0.3.0")
    assert mismatch.returncode == 1
    assert "tag: 0.3.0" in mismatch.stdout
    assert _run(CHECK, root, "0.2.0").returncode == 2
    assert _run(CHECK, root, "v0.2.0", "extra").returncode == 2


def test_check_reads_the_tag_on_head_and_skips_it_when_absent(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")

    untagged = _run(CHECK, root)
    assert untagged.returncode == 0
    assert "tag:" not in untagged.stdout

    _git(root, "tag", "v0.9.0")
    tagged = _run(CHECK, root)
    assert tagged.returncode == 1
    assert "tag: 0.9.0" in tagged.stdout


def test_bump_moves_every_place_and_nothing_else(tmp_path: Path) -> None:
    root = _tree(tmp_path, version="0.1.0", plugin="0.1.0")
    before_lock = (root / "uv.lock").read_text(encoding="utf-8")

    result = _run(BUMP, root, "0.3.1", "--no-lock")

    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert 'version = "0.3.1"' in pyproject
    assert 'version = "9.9.9"' in pyproject
    manifest = json.loads((root / "plugin" / "claude-code" / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["version"] == "0.3.1"
    assert manifest["userConfig"]  # the rest of the manifest survives
    mcp = json.loads((root / "plugin" / "claude-code" / ".mcp.json").read_text())
    assert mcp["mcpServers"]["knowledge"]["args"][4] == "cloudfrog-knowledge==0.3.1"
    package = json.loads((root / "npm" / "package.json").read_text(encoding="utf-8"))
    assert package["version"] == "0.3.1"
    assert package["bin"]  # the rest of the manifest survives
    # --no-lock leaves uv.lock alone, so the closing check reports it.
    assert (root / "uv.lock").read_text(encoding="utf-8") == before_lock
    assert result.returncode == 1
    assert "uv.lock: 0.1.0" in result.stdout

    (root / "uv.lock").unlink()
    assert _run(CHECK, root).returncode == 0


@pytest.mark.parametrize("version", ["v0.3.0", "0.3", "0.3.0-rc1", ""])
def test_bump_refuses_a_malformed_version_without_touching_anything(tmp_path: Path, version: str) -> None:
    root = _tree(tmp_path)
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}

    result = _run(BUMP, root, version, "--no-lock")

    assert result.returncode == 2
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


def test_bump_rejects_an_unknown_flag(tmp_path: Path) -> None:
    assert _run(BUMP, _tree(tmp_path), "0.3.0", "--lock-later").returncode == 2


def test_the_scripts_are_executable() -> None:
    for script in (BUMP, CHECK):
        assert os.access(script, os.X_OK), script.name
