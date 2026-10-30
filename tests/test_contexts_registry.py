"""The repository registry a vault declares in ``Contexts/config.json``.

What a checkout IS travels with the vault; where it IS on this machine lives
in ``<config home>/knowledge/<slug>/repos.local.json`` (written by
``cloudfrog-knowledge init``) or in the environment. These cases pin:

* the loader reads a vault's declaration, its packages directory and its
  ``graph.targets``;
* the resolution order: ``KNOWLEDGE_*`` variables, then the deprecated
  ``CD_*`` aliases (reported), then ``repos.local.json``, then a root's
  ``default`` only where that directory exists;
* which declaration is read when the caller names none: the only initialized
  vault, an error naming them when there are several, the legacy
  ``config.json`` only when no vault is initialized and it exists;
* ``python -m knowledge.repos`` still runs with the standard library alone.

Every case uses the fixture vault or ``tmp_path``, with an explicit ``env``
(or a patched ``HOME``/``XDG_CONFIG_HOME``): none reads the developer's home.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from knowledge import repos

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTEXTS_VAULT = REPO_ROOT / "tests" / "fixtures" / "vaults" / "contexts"


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    copy = tmp_path / "vault"
    shutil.copytree(CONTEXTS_VAULT, copy)
    return copy


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {"HOME": str(tmp_path / "home"), "XDG_CONFIG_HOME": str(tmp_path / "xdg"), **extra}


def _overlay(tmp_path: Path, vault: Path, slug: str = "contexts", **blocks: dict[str, str]) -> Path:
    path = tmp_path / "xdg" / "knowledge" / slug / repos.OVERLAY_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"slug": slug, "vault": str(vault), "roots": {}, "repos": {}, **blocks}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# --- the declaration ----------------------------------------------------------


def test_the_loader_reads_a_vaults_declaration(vault: Path, tmp_path: Path) -> None:
    registry = repos.load_registry(vault_dir=vault, env=_env(tmp_path))

    assert registry.source == "vault"
    assert registry.declaration_path == vault / "Contexts" / "config.json"
    assert list(registry.roots) == ["work", "side"]
    assert list(registry.entries) == ["ledger-api", "ledger-web", "notes-cli"]
    assert registry.entries["ledger-web"].install_mode == repos.INSTALL_MODE_COPY
    assert registry.context_dir("ledger-api") == vault / "Contexts" / "ledger-api"
    assert [t["repo"] for t in registry.graph_targets] == ["ledger-api", "ledger-web"]
    assert registry.overlay_found is False
    assert all(r.status == repos.STATUS_UNCONFIGURED for r in registry.resolve_all())


def test_an_older_declaration_without_default_still_loads(tmp_path: Path) -> None:
    """``env`` is optional now and ``default`` is new: a declaration that only has ``env`` loads as it is."""
    older = _declaration(tmp_path, [{"id": "work", "env": "CD_WORK_ROOT"}, {"id": "side", "env": "CD_SIDE_ROOT"}])
    registry = repos.load_registry(declaration_path=older, overlay_path=tmp_path / "absent.json", env={})
    assert registry.source == "vault"
    assert registry.vault_dir == older.parent.parent
    assert registry.contexts_root == older.parent
    assert all(root.env for root in registry.roots.values())
    assert all(root.default is None for root in registry.roots.values())


def test_a_declaration_file_outside_a_vault_keeps_its_packages_beside_it(tmp_path: Path) -> None:
    declaration = tmp_path / "elsewhere" / "config.json"
    declaration.parent.mkdir()
    declaration.write_text(
        json.dumps(
            {
                "repos": {
                    "roots": [{"id": "work"}],
                    "entries": [{"id": "svc", "remote": "svc", "group": "standalone", "root": "work", "path": "svc"}],
                }
            }
        ),
        encoding="utf-8",
    )

    registry = repos.load_registry(declaration_path=declaration, overlay={}, env={})

    assert registry.source == "file"
    assert registry.vault_dir is None
    assert registry.contexts_root == declaration.parent


def _declaration(tmp_path: Path, roots: list[dict], entries: list[dict] | None = None) -> Path:
    path = tmp_path / "decl" / "Contexts" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = entries or [
        {"id": "svc", "remote": "svc", "group": "standalone", "root": roots[0]["id"], "path": "svc"}
    ]
    path.write_text(json.dumps({"repos": {"roots": roots, "entries": entries}}), encoding="utf-8")
    return path


def test_a_relative_default_is_refused(tmp_path: Path) -> None:
    declaration = _declaration(tmp_path, [{"id": "work", "default": "Projects/work"}])
    with pytest.raises(repos.RegistryError, match="default"):
        repos.load_registry(declaration_path=declaration, overlay={}, env={})


@pytest.mark.parametrize("context", ["../escape", "a/b", ".."])
def test_a_context_that_is_a_path_is_refused(tmp_path: Path, context: str) -> None:
    entries = [
        {
            "id": "svc",
            "remote": "svc",
            "group": "standalone",
            "root": "work",
            "path": "svc",
            "context": context,
            "install_mode": "link",
        }
    ]
    declaration = _declaration(tmp_path, [{"id": "work"}], entries)
    with pytest.raises(repos.RegistryError, match="never a path"):
        repos.load_registry(declaration_path=declaration, overlay={}, env={})


def test_two_ids_pinned_by_one_variable_are_refused(tmp_path: Path) -> None:
    """Root ``repo-x`` and checkout ``x-root`` would both read ``KNOWLEDGE_REPO_X_ROOT``."""
    entries = [{"id": "x-root", "remote": "x", "group": "standalone", "root": "repo-x", "path": "x"}]
    declaration = _declaration(tmp_path, [{"id": "repo-x"}], entries)
    with pytest.raises(repos.RegistryError, match="KNOWLEDGE_REPO_X_ROOT"):
        repos.load_registry(declaration_path=declaration, overlay={}, env={})


# --- a vault that declares no apps ----------------------------------------------


def _vault_declaration(vault: Path, repos_block: object, **extra: object) -> Path:
    path = vault / "Contexts" / "config.json"
    path.write_text(json.dumps({"repos": repos_block, **extra}), encoding="utf-8")
    return path


def test_a_vault_may_declare_no_apps(vault: Path, tmp_path: Path) -> None:
    _vault_declaration(vault, {"roots": [], "groups": {}, "entries": []}, graph={"targets": []})

    registry = repos.load_registry(vault_dir=vault, env=_env(tmp_path))

    assert registry.source == "vault"
    assert registry.roots == {} and registry.entries == {}
    assert registry.graph_targets == ()
    assert registry.resolve_all() == []


def test_roots_may_be_declared_without_any_app(vault: Path, tmp_path: Path) -> None:
    _vault_declaration(vault, {"roots": [{"id": "work"}], "entries": []})

    registry = repos.load_registry(vault_dir=vault, env=_env(tmp_path))

    assert list(registry.roots) == ["work"] and registry.entries == {}


@pytest.mark.parametrize(
    ("block", "message"),
    [
        ({"roots": {}, "entries": []}, "repos.roots must be a JSON array"),
        ({"roots": [], "entries": {}}, "repos.entries must be a JSON array"),
        ({"roots": [], "entries": None}, "repos.entries must be a JSON array"),
        ({"roots": [{"description": "no id"}], "entries": []}, "needs an 'id'"),
        ({"roots": [], "entries": ["svc"]}, "non-object"),
        ({"roots": [], "entries": [{"id": "svc"}]}, "missing 'remote'"),
        (
            {"roots": [], "entries": [{"id": "s", "remote": "s", "group": "g", "root": "work", "path": "s"}]},
            "repos.roots does not declare",
        ),
    ],
)
def test_declaring_no_apps_does_not_excuse_a_malformed_declaration(
    vault: Path, tmp_path: Path, block: dict, message: str
) -> None:
    _vault_declaration(vault, block)

    with pytest.raises(repos.RegistryError, match=message):
        repos.load_registry(vault_dir=vault, env=_env(tmp_path))


@pytest.mark.parametrize("block", [{"roots": [], "entries": []}, {"roots": [{"id": "work"}], "entries": []}])
def test_a_declaration_file_named_outright_still_has_to_declare_something(tmp_path: Path, block: dict) -> None:
    named = tmp_path / "elsewhere" / "config.json"
    named.parent.mkdir()
    named.write_text(json.dumps({"repos": block}), encoding="utf-8")

    with pytest.raises(repos.RegistryError, match="is empty"):
        repos.load_registry(declaration_path=named, overlay={}, env={})


# --- a declared path stays inside its root --------------------------------------------


@pytest.mark.parametrize("declared", ["../escaped", "a/../../b", "..", "a/..", "a\\..\\b", "x/./../y", "./.."])
def test_a_declared_path_that_leaves_its_root_is_refused(tmp_path: Path, declared: str) -> None:
    entries = [{"id": "svc", "remote": "svc", "group": "standalone", "root": "work", "path": declared}]
    declaration = _declaration(tmp_path, [{"id": "work"}], entries)

    with pytest.raises(repos.RegistryError, match="leaves its root"):
        repos.load_registry(declaration_path=declaration, overlay={}, env={})


@pytest.mark.parametrize("declared", [".", "a", "a/b", "..hidden", "a/..b/c", "a/b.."])
def test_a_declared_path_that_only_looks_like_a_parent_is_kept(tmp_path: Path, declared: str) -> None:
    entries = [{"id": "svc", "remote": "svc", "group": "standalone", "root": "work", "path": declared}]
    declaration = _declaration(tmp_path, [{"id": "work"}], entries)

    registry = repos.load_registry(declaration_path=declaration, overlay={}, env={"HOME": str(tmp_path)})

    assert registry.entries["svc"].path == declared


# --- the resolution order -------------------------------------------------------


def test_the_overlay_init_wrote_locates_the_roots(vault: Path, tmp_path: Path) -> None:
    work = tmp_path / "work"
    (work / "ledger" / "api").mkdir(parents=True)
    _overlay(tmp_path, vault, roots={"work": str(work)})

    registry = repos.load_registry(vault_dir=vault, env=_env(tmp_path))
    api = registry.resolve("ledger-api")
    web = registry.resolve("ledger-web")

    assert registry.overlay_found is True
    assert registry.slug == "contexts"
    assert (api.status, api.path) == (repos.STATUS_RESOLVED, str(work / "ledger" / "api"))
    assert api.source.endswith("repos.local.json:roots.work + declared path")
    assert web.status == repos.STATUS_MISSING, "a located root whose checkout is absent is missing"


def test_the_generic_variable_beats_the_deprecated_alias_and_the_overlay(vault: Path, tmp_path: Path) -> None:
    for name in ("from-overlay", "from-alias", "from-env"):
        (tmp_path / name / "ledger" / "api").mkdir(parents=True)
    _overlay(tmp_path, vault, roots={"work": str(tmp_path / "from-overlay")})

    aliased = repos.load_registry(
        vault_dir=vault, env=_env(tmp_path, CD_WORK_ROOT=str(tmp_path / "from-alias"))
    )
    generic = repos.load_registry(
        vault_dir=vault,
        env=_env(
            tmp_path,
            CD_WORK_ROOT=str(tmp_path / "from-alias"),
            KNOWLEDGE_WORK_ROOT=str(tmp_path / "from-env"),
        ),
    )

    assert aliased.resolve("ledger-api").path == str(tmp_path / "from-alias" / "ledger" / "api")
    assert aliased.warnings == ["CD_WORK_ROOT is deprecated; export KNOWLEDGE_WORK_ROOT instead"]
    assert generic.resolve("ledger-api").path == str(tmp_path / "from-env" / "ledger" / "api")
    assert generic.resolve("ledger-api").source.startswith("KNOWLEDGE_WORK_ROOT")
    assert generic.warnings == []


def test_a_checkout_pin_beats_every_root_and_its_alias_is_reported(vault: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere" / "api-clone"
    elsewhere.mkdir(parents=True)

    pinned = repos.load_registry(
        vault_dir=vault,
        env=_env(
            tmp_path,
            KNOWLEDGE_WORK_ROOT=str(tmp_path / "work"),
            CD_REPO_LEDGER_API=str(elsewhere),
        ),
    )

    resolution = pinned.resolve("ledger-api")
    assert (resolution.status, resolution.path, resolution.source) == (
        repos.STATUS_RESOLVED,
        str(elsewhere),
        "CD_REPO_LEDGER_API",
    )
    assert pinned.warnings == ["CD_REPO_LEDGER_API is deprecated; export KNOWLEDGE_REPO_LEDGER_API instead"]


def test_a_default_counts_only_where_its_directory_exists(vault: Path, tmp_path: Path) -> None:
    """``side`` suggests ``~/side-projects``: unconfigured until that directory is there."""
    env = _env(tmp_path)

    before = repos.load_registry(vault_dir=vault, env=env).resolve("notes-cli")
    (tmp_path / "home" / "side-projects").mkdir(parents=True)
    after = repos.load_registry(vault_dir=vault, env=env).resolve("notes-cli")

    assert before.status == repos.STATUS_UNCONFIGURED, "a default nobody followed is not a location"
    assert after.status == repos.STATUS_MISSING
    assert after.path == str(tmp_path / "home" / "side-projects" / "notes-cli")
    assert after.source.endswith("config.json:roots.side.default + declared path")


def test_the_overlay_beats_the_default(vault: Path, tmp_path: Path) -> None:
    (tmp_path / "home" / "side-projects").mkdir(parents=True)
    _overlay(tmp_path, vault, roots={"side": str(tmp_path / "mine")})

    resolution = repos.load_registry(vault_dir=vault, env=_env(tmp_path)).resolve("notes-cli")

    assert resolution.path == str(tmp_path / "mine" / "notes-cli")


# --- which declaration ----------------------------------------------------------


def test_the_only_initialized_vault_is_read_without_naming_it(vault: Path, tmp_path: Path) -> None:
    _overlay(tmp_path, vault)

    registry = repos.load_registry(env=_env(tmp_path))

    assert registry.vault_dir == vault
    assert registry.slug == "contexts"
    assert [b.slug for b in repos.initialized_vaults(_env(tmp_path))] == ["contexts"]


def test_several_initialized_vaults_must_be_named(vault: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    shutil.copytree(CONTEXTS_VAULT, other)
    _overlay(tmp_path, vault)
    _overlay(tmp_path, other, slug="other-vault")

    with pytest.raises(repos.RegistryError, match="contexts, other-vault"):
        repos.load_registry(env=_env(tmp_path))
    assert repos.load_registry(vault="other-vault", env=_env(tmp_path)).vault_dir == other


def test_a_vault_named_by_slug_must_be_initialized(tmp_path: Path) -> None:
    with pytest.raises(repos.RegistryError, match="run: cloudfrog-knowledge init"):
        repos.load_registry(vault="contexts", env=_env(tmp_path))


def test_a_vault_named_by_directory_finds_the_overlay_init_wrote(vault: Path, tmp_path: Path) -> None:
    overlay = _overlay(tmp_path, vault)

    registry = repos.load_registry(vault_dir=vault, env=_env(tmp_path))

    assert registry.overlay_path == overlay


def test_an_overlay_without_its_vault_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "xdg" / "knowledge" / "contexts" / repos.OVERLAY_NAME
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"roots": {}}), encoding="utf-8")

    with pytest.raises(repos.RegistryError, match="names no 'vault'"):
        repos.initialized_vaults(_env(tmp_path))


def test_without_any_declaration_the_answer_is_run_init(tmp_path: Path) -> None:
    with pytest.raises(repos.RegistryError, match="no vault is initialized"):
        repos.load_registry(env=_env(tmp_path))


def test_the_config_home_follows_xdg_then_home() -> None:
    assert repos.config_home({"XDG_CONFIG_HOME": "/x", "HOME": "/h"}) == Path("/x/knowledge")
    assert repos.config_home({"HOME": "/h"}) == Path("/h/.config/knowledge")
    assert repos.config_home({}) is None
    assert repos.overlay_path_for("contexts", {"HOME": "/h"}) == Path("/h/.config/knowledge/contexts/repos.local.json")
    with pytest.raises(repos.RegistryError):
        repos.overlay_path_for("../escape", {"HOME": "/h"})


@pytest.mark.parametrize(
    ("url", "expected", "matches"),
    [
        ("git@github.com:team/ledger-api.git", "ledger-api", True),
        ("https://github.com/team/ledger-api", "ledger-api", True),
        ("https://github.com/team/ledger-api/", "ledger-api", True),
        ("git@github.com:team/ledger-api-bk.git", "ledger-api", False),
        ("git@github.com:team/old-ledger-api.git", "ledger-api", False),
    ],
)
def test_a_remote_matches_only_its_whole_repository_name(url: str, expected: str, matches: bool) -> None:
    assert repos.remote_matches(url, expected) is matches


# --- the shell reader -----------------------------------------------------------


def test_the_registry_module_runs_on_the_standard_library_alone(vault: Path, tmp_path: Path) -> None:
    """``-S`` drops site-packages: what still imports is the standard library and ``src``."""
    work = tmp_path / "work"
    (work / "ledger" / "api").mkdir(parents=True)
    _overlay(tmp_path, vault, roots={"work": str(work)})
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": str(REPO_ROOT / "src"),
        **_env(tmp_path),
    }

    path = subprocess.run(
        [sys.executable, "-S", "-m", "knowledge.repos", "--vault", "contexts", "--path", "ledger-api"],
        env=env, capture_output=True, text=True, timeout=30, check=False,
    )
    package = subprocess.run(
        [sys.executable, "-S", "-m", "knowledge.repos", "--vault-dir", str(vault), "--context-dir", "ledger-web"],
        env=env, capture_output=True, text=True, timeout=30, check=False,
    )
    report = subprocess.run(
        [sys.executable, "-S", "-m", "knowledge.repos", "--json"],
        env=env, capture_output=True, text=True, timeout=30, check=False,
    )

    assert (path.returncode, path.stdout.strip()) == (0, str(work / "ledger" / "api")), path.stderr
    assert package.stdout.strip() == str(vault / "Contexts" / "ledger-web")
    payload = json.loads(report.stdout)
    assert (payload["source"], payload["slug"]) == ("vault", "contexts")
    assert {e["id"]: e["status"] for e in payload["entries"]} == {
        "ledger-api": "resolved",
        "ledger-web": "missing",
        "notes-cli": "unconfigured",
    }
    assert report.returncode == 1, "a checkout that did not resolve is never a pass"
