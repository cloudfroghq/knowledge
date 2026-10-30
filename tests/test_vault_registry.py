"""Several vaults behind one process: ``vaults.yaml``, sidecars per slug, one default.

These cases pin the registry contract:

* ``vaults.yaml`` has one schema; every way to get it wrong fails with a stable
  code and the path of the offending key, never the value it held;
* which vaults are served is decided once, and a ``VAULT_PATH`` always beats a
  ``vaults.yaml`` the operator did not name;
* a ``VAULT_PATH`` deployment is a registry of one vault whose index directory
  and served files are exactly the ones it had before;
* each registered vault keeps its own sidecar and is opened only when asked
  for, and nothing indexed, searched or counted for one vault reaches another;
* the tools serve the default vault, with no new parameter.

Every case builds its settings from an explicit ``env`` and works on copies of
``tests/fixtures/vaults`` under ``tmp_path``: none reads the real vault, the
real ``config.json`` or the developer's own ``~/.config``.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from knowledge import contract, errors, extensions, review, server, settings
from knowledge.errors import KnowledgeConfigError, KnowledgeToolError
from knowledge.ports import Judge, NullJudge
from knowledge.registry import VaultRegistry, parse_vaults_file

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_VAULT = REPO_ROOT / "tests" / "fixtures" / "vault"
FIXTURE_VAULTS = REPO_ROOT / "tests" / "fixtures" / "vaults"

#: A value shaped like a real credential. It must never reach a message.
_TOKEN_SHAPED = "sk-proj-THISISNOTAREALKEYbutlookslikeone0123456789"
_BASE64_SHAPED = "QWxhZGRpbjpvcGVuIHNlc2FtZQ9xK3Zz8bH0aW5nT2Zm"

_ALPHA_FILES = {"Services/Gateway Service.md", "Runbooks/Login Runbook.md"}
_BETA_FILES = {"Services/Portal Dashboard.md", "Architecture/Lookup Architecture.md"}


def _call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    result = fn(*args, **kwargs)
    return asyncio.run(result) if inspect.isawaitable(result) else result


def _vaults(tmp_path: Path) -> Path:
    """A writable copy of the alpha and beta fixture vaults."""
    target = tmp_path / "vaults"
    if not target.exists():
        shutil.copytree(FIXTURE_VAULTS, target)
    return target


def _root(tmp_path: Path, cfg: dict[str, Any] | None = None) -> Path:
    root = tmp_path / "root"
    root.mkdir(exist_ok=True)
    if cfg is not None:
        (root / "config.json").write_text(json.dumps(cfg))
    return root


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {"KNOWLEDGE_HOME": str(tmp_path / "home"), "KNOWLEDGE_EMBEDDINGS_PROVIDER": "bag-of-words"}
    env.update(extra)
    return env


def _write(tmp_path: Path, document: Any, name: str = "vaults.yaml") -> Path:
    path = tmp_path / name
    path.write_text(document if isinstance(document, str) else yaml.safe_dump(document, sort_keys=False))
    return path


def _two_vaults(tmp_path: Path, *, default: str = "beta", **entries: dict[str, Any]) -> Path:
    vaults = _vaults(tmp_path)
    alpha: dict[str, Any] = {"slug": "alpha", "path": str(vaults / "alpha")}
    beta: dict[str, Any] = {"slug": "beta", "path": str(vaults / "beta")}
    {"alpha": alpha, "beta": beta}[default]["default"] = True
    alpha.update(entries.get("alpha", {}))
    beta.update(entries.get("beta", {}))
    return _write(tmp_path, {"version": 1, "vaults": [alpha, beta]})


def _load(tmp_path: Path, env: dict[str, str], cfg: dict[str, Any] | None = None) -> settings.Settings:
    return settings.load_settings(env, project_root=_root(tmp_path, cfg))


def _registry_settings(tmp_path: Path, *, default: str = "beta") -> settings.Settings:
    vaults_file = _two_vaults(tmp_path, default=default)
    return _load(tmp_path, _env(tmp_path, KNOWLEDGE_VAULTS=str(vaults_file)))


@pytest.fixture()
def serve_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Install a registry on the live server module for one test, then restore it."""

    def install(s: settings.Settings) -> VaultRegistry:
        reg = VaultRegistry.from_settings(s, sync_factory=server.vault_sync_for)
        monkeypatch.setattr(server, "_registry", reg)
        return reg

    yield install


# --- the vaults.yaml schema ---------------------------------------------------


def test_a_two_vault_file_registers_both_with_their_own_sidecar(tmp_path: Path) -> None:
    specs, warnings = parse_vaults_file(_two_vaults(tmp_path), _env(tmp_path))
    home = tmp_path / "home"

    assert [s.slug for s in specs] == ["alpha", "beta"]
    assert [s.is_default for s in specs] == [False, True]
    assert warnings == []
    for spec in specs:
        assert spec.origin == "vaults_yaml"
        assert spec.index_dir == home / spec.slug
        assert spec.chroma_dir == home / spec.slug / "chroma"
        assert spec.skipped_log == home / spec.slug / "skipped_files.json"
        assert spec.repo is None


def test_a_single_entry_is_the_default_without_saying_so(tmp_path: Path) -> None:
    vaults = _vaults(tmp_path)
    specs, _ = parse_vaults_file(_write(tmp_path, {"vaults": [{"slug": "alpha", "path": str(vaults / "alpha")}]}))
    assert specs[0].is_default is True


def test_a_relative_path_is_relative_to_the_vaults_file_and_tilde_expands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _vaults(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    vaults_file = _write(
        tmp_path,
        {
            "data_dir": "state",
            "vaults": [
                {"slug": "alpha", "path": "vaults/alpha", "default": True},
                {"slug": "beta", "path": "~/vaults/beta"},
            ],
        },
    )
    specs, _ = parse_vaults_file(vaults_file, {"KNOWLEDGE_EMBEDDINGS_PROVIDER": "bag-of-words"})

    assert specs[0].path == tmp_path / "vaults" / "alpha"
    assert specs[1].path == tmp_path / "vaults" / "beta"
    assert specs[0].index_dir == tmp_path / "state" / "alpha"


def test_knowledge_home_beats_the_data_dir_of_the_file(tmp_path: Path) -> None:
    vaults = _vaults(tmp_path)
    vaults_file = _write(tmp_path, {"data_dir": "/never/used", "vaults": [{"slug": "alpha", "path": str(vaults / "alpha")}]})
    specs, _ = parse_vaults_file(vaults_file, _env(tmp_path))
    assert specs[0].index_dir == tmp_path / "home" / "alpha"


def test_an_entry_declares_repo_git_webhook_memory_and_embeddings(tmp_path: Path) -> None:
    vaults = _vaults(tmp_path)
    entry = {
        "slug": "alpha",
        "path": str(vaults / "alpha"),
        "repo": "git@github.com:example/alpha-vault.git",
        "deploy_key_env": "ALPHA_DEPLOY_KEY_B64",
        "git": {"auto_pull": False, "branch": "trunk"},
        "webhook": {"secret_env": "ALPHA_WEBHOOK_SECRET"},
        "features": ["flows", "git-webhook"],
        "memory": {"project": "alpha"},
        "embeddings": {"provider": "openai", "model": "text-embedding-3-small", "key_env": "ALPHA_OPENAI_KEY"},
    }
    env = {"ALPHA_OPENAI_KEY": "placeholder-not-a-credential"}
    (spec,), warnings = parse_vaults_file(_write(tmp_path, {"vaults": [entry]}), env)

    assert warnings == []
    assert spec.repo == "git@github.com:example/alpha-vault.git"
    assert (spec.git_auto_pull, spec.git_branch) == (False, "trunk")
    assert spec.deploy_key_env == "ALPHA_DEPLOY_KEY_B64"
    assert spec.webhook_secret_env == "ALPHA_WEBHOOK_SECRET"
    assert spec.features == frozenset({"flows", "git-webhook"})
    assert spec.memory_project == "alpha"
    assert (spec.embeddings.provider, spec.embeddings.model, spec.embeddings.key_env) == (
        "openai",
        "text-embedding-3-small",
        "ALPHA_OPENAI_KEY",
    )
    assert spec.embeddings.api_key == "placeholder-not-a-credential"


def test_the_environment_beats_the_entry_for_the_embeddings_provider(tmp_path: Path) -> None:
    vaults = _vaults(tmp_path)
    entry = {"slug": "alpha", "path": str(vaults / "alpha"), "embeddings": {"provider": "openai"}}
    (spec,), _ = parse_vaults_file(_write(tmp_path, {"vaults": [entry]}), {"KNOWLEDGE_EMBEDDINGS_PROVIDER": "none"})
    assert spec.embeddings.provider == "none"


_A = {"slug": "alpha", "path": "/nowhere/alpha"}
_B = {"slug": "beta", "path": "/nowhere/beta"}


@pytest.mark.parametrize(
    ("document", "code", "where"),
    [
        pytest.param("vaults: [unclosed", errors.E_MANIFEST_INVALID, "line", id="unparseable"),
        pytest.param([_A], errors.E_MANIFEST_INVALID, "mapping", id="not-a-mapping"),
        pytest.param({"vaults": []}, errors.E_MANIFEST_INVALID, "vaults", id="no-vaults"),
        pytest.param({"vaults": [_A], "colour": "red"}, errors.E_MANIFEST_INVALID, "colour", id="unknown-top-key"),
        pytest.param({"vaults": [{**_A, "colour": "red"}]}, errors.E_MANIFEST_INVALID, "vaults[0].colour", id="unknown-key"),
        pytest.param({"version": 2, "vaults": [_A]}, errors.E_MANIFEST_INVALID, "version", id="version"),
        pytest.param({"vaults": [{"slug": "alpha"}]}, errors.E_MANIFEST_INVALID, "vaults[0].path", id="no-path"),
        pytest.param({"vaults": [{**_A, "default": "yes"}]}, errors.E_MANIFEST_INVALID, "vaults[0].default", id="default-type"),
        pytest.param({"vaults": [{**_A, "git": {"branch": "main"}}]}, errors.E_MANIFEST_INVALID, "vaults[0].git", id="git-no-repo"),
        pytest.param(
            {"vaults": [{**_A, "webhook": {"secret_env": "X"}}]}, errors.E_MANIFEST_INVALID, "vaults[0].webhook", id="webhook-no-repo"
        ),
        pytest.param(
            {"vaults": [{**_A, "deploy_key_env": "KEY"}]}, errors.E_MANIFEST_INVALID, "deploy_key_env", id="deploy-key-no-repo"
        ),
        pytest.param(
            {"vaults": [{**_A, "repo": "git@x:y.git", "git": {"rebase": True}}]},
            errors.E_MANIFEST_INVALID,
            "vaults[0].git.rebase",
            id="unknown-git-key",
        ),
        pytest.param({"vaults": [{**_A, "features": ["teleport"]}]}, errors.E_MANIFEST_INVALID, "features", id="unknown-feature"),
        pytest.param(
            {"vaults": [{**_A, "embeddings": {"provider": "ollama"}}]},
            errors.E_MANIFEST_INVALID,
            "embeddings.provider",
            id="unknown-provider",
        ),
        pytest.param(
            {"vaults": [{**_A, "repo": "git@x:y.git", "deploy_key_env": "not a name"}]},
            errors.E_MANIFEST_INVALID,
            "deploy_key_env",
            id="env-name",
        ),
        pytest.param({"vaults": [{**_A, "slug": "Alpha_1"}]}, errors.E_VAULT_SLUG_INVALID, "vaults[0].slug", id="slug-shape"),
        pytest.param({"vaults": [{**_A, "slug": "ab"}]}, errors.E_VAULT_SLUG_INVALID, "vaults[0].slug", id="slug-short"),
        pytest.param({"vaults": [_A, {**_B, "slug": "alpha"}]}, errors.E_VAULT_SLUG_DUPLICATE, "vaults[1].slug", id="duplicate"),
        pytest.param(
            {"vaults": [{**_A, "default": True}, {**_B, "default": True}]},
            errors.E_VAULT_DEFAULT_AMBIGUOUS,
            "alpha, beta",
            id="two-defaults",
        ),
        pytest.param({"vaults": [_A, _B]}, errors.E_VAULT_DEFAULT_MISSING, "default", id="no-default"),
        pytest.param({"vaults": [{**_A, "token": _TOKEN_SHAPED}]}, errors.E_SECRET_IN_MANIFEST, "vaults[0].token", id="secret-key"),
        pytest.param(
            {"vaults": [{**_A, "embeddings": {"key_env": _TOKEN_SHAPED}}]},
            errors.E_SECRET_IN_MANIFEST,
            "vaults[0].embeddings.key_env",
            id="secret-in-env-name",
        ),
        pytest.param(
            {"vaults": [{**_A, "repo": "https://ghp_abcdefghijklmnopqrstuvwxyz0123456789@github.com/x.git"}]},
            errors.E_SECRET_IN_MANIFEST,
            "vaults[0].repo",
            id="secret-in-remote",
        ),
        pytest.param(
            {"vaults": [{**_A, "memory": {"project": _BASE64_SHAPED}}]},
            errors.E_SECRET_IN_MANIFEST,
            "vaults[0].memory.project",
            id="base64-run",
        ),
        pytest.param(
            {"vaults": [{**_A, "memory": {"project": "-----BEGIN OPENSSH PRIVATE KEY-----"}}]},
            errors.E_SECRET_IN_MANIFEST,
            "vaults[0].memory.project",
            id="private-key",
        ),
    ],
)
def test_every_schema_violation_has_a_stable_code(tmp_path: Path, document: Any, code: str, where: str) -> None:
    path = _write(tmp_path, document)
    with pytest.raises(KnowledgeConfigError) as exc_info:
        parse_vaults_file(path, {}, check_paths=False)

    assert exc_info.value.code == code
    message = str(exc_info.value)
    assert where in message
    assert str(path) in message
    for secret in (_TOKEN_SHAPED, _BASE64_SHAPED, "ghp_abcdefghijklmnopqrstuvwxyz0123456789", "-----BEGIN"):
        assert secret not in message


def test_a_named_vaults_file_that_does_not_exist_is_refused(tmp_path: Path) -> None:
    with pytest.raises(KnowledgeConfigError) as exc_info:
        _load(tmp_path, _env(tmp_path, KNOWLEDGE_VAULTS=str(tmp_path / "missing.yaml")))
    assert exc_info.value.code == errors.E_VAULTS_FILE_MISSING
    assert "KNOWLEDGE_VAULTS" in str(exc_info.value)


def test_check_paths_requires_the_directory_and_its_vault_yaml(tmp_path: Path) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()
    missing = _write(tmp_path, {"vaults": [{"slug": "alpha", "path": str(tmp_path / "nowhere")}]}, "missing.yaml")
    no_manifest = _write(tmp_path, {"vaults": [{"slug": "alpha", "path": str(bare)}]}, "bare.yaml")

    with pytest.raises(KnowledgeConfigError) as not_dir:
        parse_vaults_file(missing, {})
    with pytest.raises(KnowledgeConfigError) as no_yaml:
        parse_vaults_file(no_manifest, {})

    assert not_dir.value.code == errors.E_VAULT_PATH_NOT_DIR
    assert "vaults.yaml" in str(not_dir.value)
    assert no_yaml.value.code == errors.E_VAULT_MANIFEST_MISSING
    assert parse_vaults_file(missing, {}, check_paths=False)[0][0].slug == "alpha"
    assert parse_vaults_file(no_manifest, {}, check_paths=False)[0][0].slug == "alpha"


def test_validate_requires_the_vault_yaml_of_a_registered_vault(tmp_path: Path) -> None:
    vaults = _vaults(tmp_path)
    (vaults / "beta" / "vault.yaml").unlink()
    s = _load(tmp_path, _env(tmp_path, KNOWLEDGE_VAULTS=str(_two_vaults(tmp_path))))
    with pytest.raises(KnowledgeConfigError) as exc_info:
        s.validate()
    assert exc_info.value.code == errors.E_VAULT_MANIFEST_MISSING
    assert "beta" in str(exc_info.value)


def test_a_vault_yaml_slug_that_disagrees_with_its_entry_is_refused(tmp_path: Path) -> None:
    vaults = _vaults(tmp_path)
    path = _write(tmp_path, {"vaults": [{"slug": "gamma", "path": str(vaults / "alpha")}]})
    with pytest.raises(KnowledgeConfigError) as exc_info:
        parse_vaults_file(path, {})
    assert exc_info.value.code == errors.E_VAULT_SLUG_MISMATCH


def test_survivable_registry_problems_come_back_as_warnings(tmp_path: Path) -> None:
    vaults = _vaults(tmp_path)
    (vaults / "alpha" / "vault.yaml").write_text("slug: alpha\nknowledge_contract: '>=3.0'\n")
    path = _write(tmp_path, {"vaults": [{"slug": "alpha", "path": str(vaults / "alpha"), "features": ["git-webhook"]}]})
    _, warnings = parse_vaults_file(path, {})
    assert sorted(w.split(":")[0] for w in warnings) == [errors.W_CONTRACT_RANGE, errors.W_FEATURE_WITHOUT_REPO]


@pytest.mark.parametrize(
    ("declared", "expected"),
    [(">=1.0,<3", True), (">=1.1", False), ("<1", False), ("==1.0", True), ("1", True), ("!=1.0", False), ("latest", None)],
)
def test_the_contract_range_is_checked_against_the_served_contract(declared: str, expected: bool | None) -> None:
    assert settings.contract_satisfied(declared, (1, 0)) is expected


def test_a_vault_yaml_supplies_thresholds_and_the_memory_folder(tmp_path: Path) -> None:
    s = _registry_settings(tmp_path)
    alpha = s.vault("alpha")

    assert alpha.thresholds_from_config is True
    assert review.threshold_for("runbook", thresholds=alpha.review_thresholds) == 30
    assert review.threshold_for("service", thresholds=alpha.review_thresholds) == 200
    assert alpha.memory_folder == "90 - Memory"
    assert s.vault("beta").thresholds_from_config is False


def test_an_unknown_slug_is_a_coded_error(tmp_path: Path) -> None:
    s = _registry_settings(tmp_path)
    with pytest.raises(KnowledgeConfigError) as exc_info:
        s.vault("gamma")
    assert exc_info.value.code == errors.E_VAULT_UNKNOWN


# --- which vaults are served ---------------------------------------------------


def _user_vaults_file(config_home: Path, vaults: Path) -> Path:
    path = config_home / "knowledge" / "vaults.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump({"vaults": [{"slug": "alpha", "path": str(vaults / "alpha")}]}))
    return path


def test_knowledge_vaults_wins_over_vault_path_and_says_so(tmp_path: Path) -> None:
    vaults_file = _two_vaults(tmp_path)
    s = _load(tmp_path, _env(tmp_path, KNOWLEDGE_VAULTS=str(vaults_file), VAULT_PATH=str(FIXTURE_VAULT)))

    assert s.vaults_file == vaults_file
    assert [v.slug for v in s.vaults] == ["alpha", "beta"]
    assert errors.W_VAULT_PATH_IGNORED in [w.split(":")[0] for w in s.load_warnings]


def test_vault_path_wins_over_the_users_vaults_yaml(tmp_path: Path) -> None:
    vaults = _vaults(tmp_path)
    _user_vaults_file(tmp_path / "xdg", vaults)
    _user_vaults_file(tmp_path / "home-dir" / ".config", vaults)
    env = _env(tmp_path, VAULT_PATH=str(FIXTURE_VAULT), XDG_CONFIG_HOME=str(tmp_path / "xdg"), HOME=str(tmp_path / "home-dir"))

    s = _load(tmp_path, env)

    assert s.vaults_file is None
    assert [v.origin for v in s.vaults] == ["vault_path"]
    assert s.default_vault().path == FIXTURE_VAULT


def test_config_json_vault_path_wins_over_the_users_vaults_yaml(tmp_path: Path) -> None:
    _user_vaults_file(tmp_path / "xdg", _vaults(tmp_path))
    s = _load(tmp_path, _env(tmp_path, XDG_CONFIG_HOME=str(tmp_path / "xdg")), {"vault_path": str(FIXTURE_VAULT)})
    assert s.vaults_file is None
    assert s.default_vault().path == FIXTURE_VAULT


@pytest.mark.parametrize("via", ["XDG_CONFIG_HOME", "HOME"])
def test_without_vault_path_the_users_vaults_yaml_is_served(tmp_path: Path, via: str) -> None:
    vaults = _vaults(tmp_path)
    if via == "XDG_CONFIG_HOME":
        expected = _user_vaults_file(tmp_path / "xdg", vaults)
        env = _env(tmp_path, XDG_CONFIG_HOME=str(tmp_path / "xdg"))
    else:
        expected = _user_vaults_file(tmp_path / "home-dir" / ".config", vaults)
        env = _env(tmp_path, HOME=str(tmp_path / "home-dir"))

    s = _load(tmp_path, env)

    assert s.vaults_file == expected
    assert s.default_vault().slug == "alpha"
    assert s.default_vault().origin == "vaults_yaml"


def test_with_nothing_declared_validation_names_the_missing_vault(tmp_path: Path) -> None:
    with pytest.raises(KnowledgeConfigError) as exc_info:
        _load(tmp_path, _env(tmp_path)).validate()
    assert exc_info.value.code == errors.E_VAULT_PATH_MISSING


# --- VAULT_PATH is a registry of one -------------------------------------------


def test_vault_path_alone_keeps_the_index_directory_it_had(tmp_path: Path) -> None:
    env = {"VAULT_PATH": str(FIXTURE_VAULT), "CHROMA_PERSIST_DIR": str(tmp_path / "chroma")}
    s = _load(tmp_path, env)
    (spec,) = s.vaults

    assert (spec.slug, spec.origin, spec.is_default) == ("vault", "vault_path", True)
    assert spec.path == FIXTURE_VAULT
    assert spec.index_dir == tmp_path / "chroma"
    assert spec.skipped_log == tmp_path / "chroma" / "skipped_files.json"
    assert spec.repo is None
    assert server.vault_sync_for(spec) is None
    assert s.vaults_file is None and s.load_warnings == ()

    reg = VaultRegistry.from_settings(s)
    assert reg.slugs() == ("vault",)
    assert reg.opened() == ()


def test_without_chroma_persist_dir_the_index_stays_under_the_checkout(tmp_path: Path) -> None:
    s = _load(tmp_path, {"VAULT_PATH": str(FIXTURE_VAULT), "XDG_DATA_HOME": str(tmp_path / "xdg")})
    assert s.default_vault().index_dir == tmp_path / "root" / ".data" / "vault"
    assert s.default_vault().chroma_dir == tmp_path / "root" / ".data" / "chroma"


def _vault_with_manifest(tmp_path: Path, manifest: str, name: str = "vault-dir") -> Path:
    vault = tmp_path / name
    vault.mkdir(parents=True)
    (vault / "vault.yaml").write_text(manifest, encoding="utf-8")
    return vault


PERSONAL_MANIFEST = """\
slug: personal
rerank:
  provider: jev
  default: off
  model: jev-1.13.0
  key_env: PERSONAL_TYPESAFE_API_KEY
"""


def test_vault_path_serves_the_slug_its_vault_yaml_declares(tmp_path: Path) -> None:
    """The input that failed: a vault named ``personal`` whose rerank key is ``PERSONAL_TYPESAFE_API_KEY``.

    The served slug used to be ``CHROMA_COLLECTION`` or a tenant's name, and the binding check then
    refused the vault's own credential variable as another vault's.
    """
    vault = _vault_with_manifest(tmp_path, PERSONAL_MANIFEST)

    s = _load(tmp_path, {"VAULT_PATH": str(vault)})
    spec = s.default_vault()

    assert (spec.slug, spec.origin, spec.is_default) == ("personal", "vault_path", True)
    assert spec.rerank.key_env == "PERSONAL_TYPESAFE_API_KEY"
    assert spec.index_dir == tmp_path / "root" / ".data" / "personal"
    assert [w for w in s.load_warnings if w.startswith(errors.W_VAULT_SLUG_MISMATCH)] == []


def test_an_explicit_chroma_collection_beats_the_slug_of_the_vault_yaml(tmp_path: Path) -> None:
    vault = _vault_with_manifest(tmp_path, "slug: personal\n")

    s = _load(tmp_path, {"VAULT_PATH": str(vault), "CHROMA_COLLECTION": "team"})
    spec = s.default_vault()

    assert spec.slug == "team" and spec.index_dir == tmp_path / "root" / ".data" / "team"
    assert [w.split(":")[0] for w in s.load_warnings] == [errors.W_VAULT_SLUG_MISMATCH]


def test_an_explicit_chroma_collection_keeps_a_deployed_index_where_it_was(tmp_path: Path) -> None:
    """The deployment that names its index keeps it, whatever the vault says about itself."""
    vault = _vault_with_manifest(tmp_path, "slug: personal\n")
    env = {"VAULT_PATH": str(vault), "CHROMA_COLLECTION": "acme", "KNOWLEDGE_HOME": str(tmp_path / "home")}

    spec = _load(tmp_path, env).default_vault()

    assert (spec.slug, spec.index_dir) == ("acme", tmp_path / "home" / "acme")


def test_config_json_chroma_collection_comes_before_the_slug_of_the_vault_yaml(tmp_path: Path) -> None:
    vault = _vault_with_manifest(tmp_path, "slug: personal\n")

    spec = _load(tmp_path, {"VAULT_PATH": str(vault)}, {"chroma_collection": "team"}).default_vault()

    assert spec.slug == "team"


def test_a_blank_chroma_collection_reads_as_absent(tmp_path: Path) -> None:
    vault = _vault_with_manifest(tmp_path, "slug: personal\n")

    spec = _load(tmp_path, {"VAULT_PATH": str(vault), "CHROMA_COLLECTION": "  "}).default_vault()

    assert spec.slug == "personal"


def test_a_vault_without_a_slug_anywhere_is_served_under_a_name_no_tenant_owns(tmp_path: Path) -> None:
    for vault in (FIXTURE_VAULT, _vault_with_manifest(tmp_path, "name: Anonymous\n")):
        s = _load(tmp_path, {"VAULT_PATH": str(vault)})
        spec = s.default_vault()

        assert spec.slug == "vault"
        assert spec.index_dir == tmp_path / "root" / ".data" / "vault"
        assert s.load_warnings == ()


@pytest.mark.parametrize("declared", ["../../etc", "Has Spaces", "UPPER", "a", "x" * 40])
def test_a_slug_that_is_not_a_slug_is_never_used_to_build_a_path(tmp_path: Path, declared: str) -> None:
    vault = _vault_with_manifest(tmp_path, f"slug: '{declared}'\n")

    s = _load(tmp_path, {"VAULT_PATH": str(vault)})
    spec = s.default_vault()

    assert spec.slug == "vault" and spec.index_dir == tmp_path / "root" / ".data" / "vault"
    assert [w.split(":")[0] for w in s.load_warnings] == [errors.W_VAULT_SLUG_MISMATCH]


def test_the_cloud_frog_shape_resolves_the_same_slug_and_the_same_index_directory(tmp_path: Path) -> None:
    """VAULT_PATH, no CHROMA_COLLECTION, ``slug: acme`` in the vault and an explicit index directory."""
    vault = _vault_with_manifest(tmp_path, "slug: acme\nreview:\n  thresholds_days:\n    default: 90\n")
    index = tmp_path / "data" / "index" / "acme"
    deployed = {"VAULT_PATH": str(vault), "KNOWLEDGE_INDEX_DIR": str(index)}

    spec = _load(tmp_path, deployed).default_vault()
    named = _load(tmp_path, {**deployed, "CHROMA_COLLECTION": "acme"}).default_vault()

    assert (spec.slug, spec.index_dir) == ("acme", index)
    assert (spec.slug, spec.index_dir, spec.chroma_dir) == (named.slug, named.index_dir, named.chroma_dir)


def test_vault_remote_declares_the_repository_of_a_vault_path_deployment(tmp_path: Path) -> None:
    env = {"VAULT_PATH": str(FIXTURE_VAULT), "VAULT_REMOTE": "git@github.com:example/vault.git", "VAULT_BRANCH": "trunk"}
    spec = _load(tmp_path, env).default_vault()
    assert (spec.repo, spec.git_branch) == ("git@github.com:example/vault.git", "trunk")


def test_a_vault_yaml_inside_vault_path_comes_before_config_json(tmp_path: Path) -> None:
    alpha = _vaults(tmp_path) / "alpha"
    cfg = {"review": {"thresholds_days": {"runbook": 90, "default": 270}}, "memory": {"vault_folder": "99 - Other"}}
    s = _load(tmp_path, {"VAULT_PATH": str(alpha), "CHROMA_PERSIST_DIR": str(tmp_path / "chroma")}, cfg)
    spec = s.default_vault()

    assert dict(spec.review_thresholds) == {"runbook": 30, "default": 200}
    assert spec.memory_folder == "90 - Memory"
    # The vault names itself, the index directory is the one the deployment mounted, nothing to warn about.
    assert (spec.slug, spec.index_dir) == ("alpha", tmp_path / "chroma")
    assert s.load_warnings == ()


def test_the_suite_server_serves_the_fixture_vault_through_a_registry_of_one(indexed_server: Any) -> None:
    """With only ``VAULT_PATH``, every tool reads the vault it always read.

    The golden master (``test_contract_golden``) replays every tool over a
    ``VAULT_PATH``-only stdio server byte for byte; this pins the same thing
    from inside the process: one registered vault, the fixture vault, its usual
    sidecar, no git, and the tools answering with that vault's files.
    """
    reg = indexed_server.registry()
    rt = indexed_server.default_runtime()

    assert reg.slugs() == ("vault",)
    assert rt is reg.get("vault")
    assert rt.vault_path == FIXTURE_VAULT
    assert rt.spec.origin == "vault_path"
    assert rt.spec.index_dir == Path(settings.current().default_vault().index_dir)
    assert indexed_server.vault_sync_for(rt.spec) is None
    off = extensions.gated_off_tools(settings.current().vaults, settings.current().http.transport, contract.DEFAULT_PROFILE)
    assert {tool.name for tool in asyncio.run(indexed_server.mcp.list_tools())} == set(contract.TOOL_SINCE) - off

    served = [r["file_path"] for r in _call(indexed_server.search, "middleware outage", limit=5)]
    served += [r["file_path"] for r in _call(indexed_server.search_by_metadata, type="service")["items"]]
    served += [r["file_path"] for r in _call(indexed_server.get_stale_docs)]
    assert served
    assert all((FIXTURE_VAULT / fp).is_file() for fp in served)
    doc = _call(indexed_server.get_document, "Services/Middleware Service.md")
    assert "error" not in doc


# --- the registry ---------------------------------------------------------------


def test_runtimes_open_lazily_and_only_on_request(tmp_path: Path) -> None:
    reg = VaultRegistry.from_settings(_registry_settings(tmp_path))
    home = tmp_path / "home"

    assert reg.slugs() == ("alpha", "beta")
    assert reg.opened() == ()
    assert reg.default.spec.slug == "beta"
    assert reg.opened() == ("beta",)
    assert not (home / "alpha").exists()
    assert reg.get("alpha") is reg.get("alpha")
    assert reg.opened() == ("alpha", "beta")
    with pytest.raises(KnowledgeConfigError) as exc_info:
        reg.get("gamma")
    assert exc_info.value.code == errors.E_VAULT_UNKNOWN


def test_a_vault_without_a_judge_gets_the_null_judge(tmp_path: Path) -> None:
    reg = VaultRegistry.from_settings(_registry_settings(tmp_path))

    assert isinstance(reg.get("alpha").judge, NullJudge)
    assert isinstance(reg.get("beta").judge, Judge)


def test_the_judge_factory_gets_each_spec_when_its_vault_opens(tmp_path: Path) -> None:
    judges: dict[str, NullJudge] = {}
    seen: list[str] = []

    def factory(spec: settings.VaultSpec) -> NullJudge:
        seen.append(spec.slug)
        return judges.setdefault(spec.slug, NullJudge())

    reg = VaultRegistry.from_settings(_registry_settings(tmp_path), judge_factory=factory)

    assert seen == [], "no vault is opened, so no judge is built"
    assert reg.get("alpha").judge is judges["alpha"]
    assert seen == ["alpha"]
    assert reg.get("alpha").judge is judges["alpha"]
    assert seen == ["alpha"], "a judge is built once per opened vault"


def test_closing_the_registry_checkpoints_every_opened_store(tmp_path: Path) -> None:
    reg = VaultRegistry.from_settings(_registry_settings(tmp_path))
    store = reg.default.indexer.store
    store.set_meta({"probe": "1"})
    index_dir = reg.default.spec.index_dir

    reg.close()

    wal = index_dir / "index.sqlite-wal"
    assert not wal.exists() or wal.stat().st_size == 0
    assert reg.opened() == ("beta",)
    assert not (tmp_path / "home" / "alpha").exists(), "closing never opens a vault"


def test_the_tools_serve_the_default_vault(tmp_path: Path, serve_registry: Any) -> None:
    reg = serve_registry(_registry_settings(tmp_path, default="beta"))

    hits = {r["file_path"] for r in _call(server.search, "portal dashboard comet", limit=5)}
    doc = _call(server.get_document, "Services/Portal Dashboard.md")
    with pytest.raises(KnowledgeToolError) as missing:
        _call(server.get_document, "Services/Gateway Service.md")

    assert hits and hits <= _BETA_FILES
    assert doc["file_path"] == "Services/Portal Dashboard.md"
    assert missing.value.code == errors.E_FILE_NOT_FOUND and "'beta'" in str(missing.value)
    assert reg.opened() == ("beta",)


def test_vault_isolation(tmp_path: Path, serve_registry: Any) -> None:
    """Nothing indexed, searched or counted for one vault reaches the other."""
    reg = serve_registry(_registry_settings(tmp_path, default="beta"))
    home = tmp_path / "home"
    alpha, beta = reg.get("alpha"), reg.get("beta")

    alpha.indexer.index(force=False, target="vault")
    assert beta.indexer.store.count() == 0
    assert (home / "alpha" / "index_meta.json").is_file()
    assert not (home / "beta" / "index_meta.json").exists()
    assert (home / "alpha" / "index.sqlite").is_file() and (home / "beta" / "index.sqlite").is_file()
    assert alpha.spec.index_dir != beta.spec.index_dir

    beta.indexer.index(force=False, target="vault")

    def indexed(rt: Any) -> set[str]:
        return set(rt.indexer.store.file_hashes())

    assert indexed(alpha) == _ALPHA_FILES
    assert indexed(beta) == _BETA_FILES

    alpha.usage.record_uses(sorted(_ALPHA_FILES))
    alpha.usage.flush()
    assert (home / "alpha" / "usage_stats.json").is_file()
    assert not (home / "beta" / "usage_stats.json").exists()

    # beta is the default: words that only alpha uses find nothing of alpha's.
    hits = {r["file_path"] for r in _call(server.search, "gateway telvia token login otp session", limit=10)}
    assert not hits & _ALPHA_FILES
    keyword_hits = {fp for fp, _ in beta.indexer.keyword_index.search("gateway telvia token", limit=10)}
    assert not keyword_hits & _ALPHA_FILES
