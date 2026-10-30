"""Deployment files: Dockerfile, the composes, the bootstrap and the multi-vault resolver."""

from __future__ import annotations

import base64
import functools
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from knowledge import contexts, errors, vault_lint
from knowledge.settings import load_settings, parse_vaults_file, read_vault_manifest

ROOT = Path(__file__).resolve().parent.parent
INFRA = ROOT / "infra"
BOOTSTRAP = INFRA / "bootstrap-vault.sh"
LIVE_COMPOSE = INFRA / "dokploy-compose.yml"
GENERIC = INFRA / "compose.yml"

SECRET_NAME = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD)", re.IGNORECASE)
PLACEHOLDER = re.compile(r"^\$\{[A-Z0-9_]+(:-[^}]*)?\}$")
SEEDED_KEY = "SEEDED-DEPLOY-KEY-VALUE"


def _load(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text())
    assert isinstance(loaded, dict)
    return loaded


def test_dockerfile_ships_no_config_json_and_runs_the_bootstrap() -> None:
    text = (ROOT / "Dockerfile").read_text()
    assert not re.search(r"^COPY\s+config\.json", text, re.MULTILINE)
    assert "COPY infra/bootstrap-vault.sh" in text
    assert 'CMD ["knowledge-bootstrap"]' in text


def test_dockerfile_installs_the_project_so_the_cli_is_on_path() -> None:
    """The builder installs the project, non-editable, after copying its source.

    Dependencies keep their own cached layer (``--no-install-project`` before the
    source is copied); the second sync puts ``cloudfrog-knowledge`` in
    ``/app/.venv/bin``, which the runtime stage copies and puts on ``PATH``.
    """
    text = (ROOT / "Dockerfile").read_text()
    builder, runtime = text.split(" AS runtime", 1)
    deps = builder.index("uv sync --frozen --no-dev --no-install-project")
    source = builder.index("COPY src ./src")
    project = re.search(r"^RUN uv sync --frozen --no-dev --no-editable$", builder, re.MULTILINE)
    assert project is not None, "the builder installs the project non-editable"
    assert deps < source < project.start()
    assert "COPY --from=builder /app/.venv /app/.venv" in runtime
    assert re.search(r"^\s*PATH=/app/\.venv/bin:\$PATH\b", runtime, re.MULTILINE)
    assert 'cloudfrog-knowledge = "knowledge.cli:main"' in (ROOT / "pyproject.toml").read_text()


def test_dockerfile_puts_the_data_root_under_data_index() -> None:
    """``KNOWLEDGE_HOME`` is the index volume's mount point, never ``/data`` itself.

    ``CHROMA_PERSIST_DIR`` keeps its value for deployments whose compose is not
    versioned here: without ``KNOWLEDGE_INDEX_DIR`` the index is written beside
    ``chroma.sqlite3``, inside the volume those composes already mount.
    """
    text = (ROOT / "Dockerfile").read_text()
    assert re.search(r"^\s*KNOWLEDGE_HOME=/data/index\b", text, re.MULTILINE)
    assert re.search(r"^\s*CHROMA_PERSIST_DIR=/data/chroma\b", text, re.MULTILINE)
    assert "mkdir -p /data/index" in text


def _index_mounts(service: dict[str, Any]) -> tuple[str, list[str], list[str]]:
    env = {k: str(v) for k, v in service["environment"].items()}
    mounts = [str(m) for m in service["volumes"]]
    index = [m for m in mounts if m.split(":")[1] == "/data/index"]
    chroma = [m for m in mounts if m.split(":")[1] == "/data/chroma"]
    return env.get("KNOWLEDGE_INDEX_DIR", ""), index, chroma


def test_the_live_compose_keeps_the_index_in_its_own_volume() -> None:
    """The index lives under ``/data/index`` on a named volume; Chroma's stays only as the source.

    ``KNOWLEDGE_INDEX_DIR`` has to be set: the image keeps ``CHROMA_PERSIST_DIR``,
    and without the explicit directory the index would land in the Chroma volume.
    """
    doc = _load(LIVE_COMPOSE)
    index_dir, index, chroma = _index_mounts(doc["services"]["knowledge-mcp"])
    assert index_dir.startswith("/data/index/"), "KNOWLEDGE_INDEX_DIR must be under /data/index"
    assert len(index) == 1, "one volume mounted at /data/index"
    volume = index[0].split(":")[0]
    assert volume in doc.get("volumes", {}), "/data/index must be a named volume"
    assert len(chroma) == 1, "the Chroma volume stays mounted as the migration source"


def test_the_live_compose_serves_a_vault_repository_with_no_config_json() -> None:
    """The deployed compose clones a vault repository and serves its root, with no config.json mounted.

    The review thresholds come from the vault.yaml at the root of that clone,
    so the compose names no KNOWLEDGE_CONFIG_JSON. The clone lives in a volume
    of its own, never the one that held the clone of this code repository. The
    file is the live deployment's wiring until the generic compose replaces it,
    and this case goes with it.
    """
    compose = _load(LIVE_COMPOSE)
    service = compose["services"]["knowledge-mcp"]
    env = {k: str(v) for k, v in service["environment"].items()}
    assert env["VAULT_REMOTE"] == "git@github.com:cloudfroghq/vault.git"
    assert env["VAULT_PATH"] == "/data/vault-repo"
    assert "KNOWLEDGE_CONFIG_JSON" not in env
    for name in ("MCP_API_KEY", "MCP_ADMIN_KEY", "MCP_WEBHOOK_SECRET", "GIT_DEPLOY_KEY_B64"):
        assert PLACEHOLDER.fullmatch(env[name]), f"{name} must be a ${{NAME}} placeholder"
    assert "vault-cloudfrog:/data/vault-repo" in service["volumes"]
    assert "vault-cloudfrog" in compose["volumes"]
    assert "vault-repo" not in compose["volumes"]


def test_vault_thresholds_come_from_the_vault_manifest(tmp_path: Path) -> None:
    """Without KNOWLEDGE_CONFIG_JSON, a vault.yaml at the vault root sets the thresholds.

    The vault is a stand-in carrying only a manifest, never a real one.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "vault.yaml").write_text(
        "slug: acme\nreview:\n  thresholds_days:\n    runbook: 90\n    default: 270\n"
    )
    loaded = load_settings(
        {"VAULT_PATH": str(vault), "KNOWLEDGE_EMBEDDINGS_PROVIDER": "none"},
        project_root=tmp_path,
    )
    spec = loaded.default_vault()
    assert spec.thresholds_from_config
    assert spec.review_thresholds["runbook"] == 90


def test_bootstrap_is_executable() -> None:
    assert BOOTSTRAP.stat().st_mode & stat.S_IXUSR


@pytest.mark.parametrize("path", [GENERIC, LIVE_COMPOSE], ids=lambda p: p.name)
def test_compose_parses(path: Path) -> None:
    assert "services" in _load_compose(path)


def _run_bootstrap(
    tmp_path: Path, env: dict[str, str], args: tuple[str, ...] = ("true",), python_exit: str = "0"
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run the bootstrap with ``git`` and ``python`` replaced by loggers.

    Each fake appends ``<name> <args>`` to one log, so the order of the calls
    is what the log reads. ``python_exit`` is the status the fake ``python``
    returns for ``migrate-index``.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    fake_git = bin_dir / "git"
    fake_git.write_text(f'#!/bin/sh\necho "git $*" >> "{log}"\n')
    fake_git.chmod(0o755)
    fake_python = bin_dir / "python"
    fake_python.write_text(
        f'#!/bin/sh\necho "python $*" >> "{log}"\n'
        f'case "$*" in *migrate-index*) exit {python_exit};; esac\nexit 0\n'
    )
    fake_python.chmod(0o755)
    base = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path / "home"),
    }
    done = subprocess.run(
        ["sh", str(BOOTSTRAP), *args],
        env={**base, **env},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    lines = log.read_text().splitlines() if log.exists() else []
    calls = [line.removeprefix("git ") for line in lines if line.startswith("git ")]
    calls += [line for line in lines if line.startswith("python ")]
    return done, calls


def test_bootstrap_without_remote_never_calls_git(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    done, calls = _run_bootstrap(tmp_path, {"VAULT_PATH": str(vault)})
    assert done.returncode == 0, done.stderr
    assert calls == []


def test_bootstrap_without_arguments_migrates_the_index_before_serving(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    done, calls = _run_bootstrap(tmp_path, {"VAULT_PATH": str(vault)}, args=())
    assert done.returncode == 0, done.stderr
    assert calls == ["python -m knowledge migrate-index", "python -m knowledge serve"]


def test_bootstrap_does_not_serve_when_migrate_index_fails(tmp_path: Path) -> None:
    """An index of a newer layout (rc 2) stops the start instead of being served."""
    vault = tmp_path / "vault"
    vault.mkdir()
    done, calls = _run_bootstrap(tmp_path, {"VAULT_PATH": str(vault)}, args=(), python_exit="2")
    assert done.returncode == 2
    assert calls == ["python -m knowledge migrate-index"]


def test_bootstrap_with_arguments_runs_them_instead_of_migrating(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    done, calls = _run_bootstrap(tmp_path, {"VAULT_PATH": str(vault)}, args=("python", "-V"))
    assert done.returncode == 0, done.stderr
    assert calls == ["python -V"]


def test_bootstrap_keeps_stdout_for_the_protocol() -> None:
    """On the stdio transport stdout is the protocol: the migration report goes to stderr."""
    assert "python -m knowledge migrate-index >&2" in BOOTSTRAP.read_text()


def test_bootstrap_missing_vault_path_dir_fails_loudly(tmp_path: Path) -> None:
    done, _ = _run_bootstrap(tmp_path, {"VAULT_PATH": str(tmp_path / "absent")})
    assert done.returncode == 1
    assert errors.E_VAULT_PATH_NOT_DIR in done.stderr


def test_bootstrap_unset_vault_path_fails_loudly(tmp_path: Path) -> None:
    done, _ = _run_bootstrap(tmp_path, {})
    assert done.returncode == 1
    assert errors.E_VAULT_PATH_MISSING in done.stderr


def test_bootstrap_remote_without_key_exits_64_and_leaks_nothing(tmp_path: Path) -> None:
    done, calls = _run_bootstrap(
        tmp_path,
        {
            "VAULT_PATH": str(tmp_path / "vault"),
            "VAULT_REMOTE": "git@example.com:org/vault.git",
            "GIT_DEPLOY_KEY_B64": "",
            "UNRELATED_SECRET": SEEDED_KEY,
        },
    )
    assert done.returncode == 64
    assert "E_DEPLOY_KEY_MISSING" in done.stderr
    assert calls == []
    assert SEEDED_KEY not in done.stdout + done.stderr


def test_example_manifest_parses() -> None:
    specs, warnings = parse_vaults_file(ROOT / "vaults.yaml.example", {}, check_paths=False)
    assert [s.slug for s in specs] == ["acme", "globex", "initech"]
    assert sum(s.is_default for s in specs) == 1
    assert warnings == []


def test_runbook_names_every_error_code() -> None:
    runbook = (ROOT / "docs" / "runbooks" / "servir-varios-vaults.md").read_text()
    codes = {v for k, v in vars(errors).items() if k.startswith("E_") and isinstance(v, str)}
    codes.add("E_DEPLOY_KEY_MISSING")
    missing = sorted(c for c in codes if c not in runbook)
    assert missing == []


def test_runbook_documents_the_generic_compose_and_its_entrypoint_codes() -> None:
    runbook = (ROOT / "docs" / "runbooks" / "servir-varios-vaults.md").read_text()
    for name in (
        "infra/compose.yml",
        "KNOWLEDGE_VAULTS_YAML",
        "GIT_DEPLOY_KEY_B64",
        "KNOWLEDGE_ENV_FILE",
        "COMPOSE_PROFILES=memory",
        "E_VAULT_CLONE_FAILED",
        "W_VAULT_REFRESH_FAILED",
        "lab/README.md",
    ):
        assert name in runbook, f"the runbook must mention {name}"


# ---------------------------------------------------------------------------
# The generic compose, the multi-vault resolver and the bootstrap's vaults mode
# ---------------------------------------------------------------------------

RESOLVER = INFRA / "resolve-vaults.py"
LAB = ROOT / "lab"
TENANT_NAMES = re.compile(r"cloudfrog|acme-drive|initech|cloudfroghq", re.IGNORECASE)
KEY_B64 = base64.b64encode(SEEDED_KEY.encode()).decode()


def _load_compose(path: Path) -> dict[str, Any]:
    """A compose file read with plain ``safe_load``: Compose's merge tags (``!override``, ``!reset``) are dropped."""
    loaded = yaml.safe_load(re.sub(r"(?m)(:[ \t]*)!(?:override|reset)\b", r"\1", path.read_text()))
    assert isinstance(loaded, dict)
    return loaded


def _service_env(service: dict[str, Any]) -> dict[str, str]:
    return {k: str(v) for k, v in service["environment"].items()}


@functools.cache
def _resolver() -> ModuleType:
    spec = importlib.util.spec_from_file_location("resolve_vaults", RESOLVER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# -- infra/compose.yml ---------------------------------------------------------


def test_generic_compose_names_no_tenant() -> None:
    """The one compose, the entrypoint and the resolver carry no organization's name."""
    for path in (GENERIC, BOOTSTRAP, RESOLVER):
        assert TENANT_NAMES.search(path.read_text()) is None, f"{path.name} names a tenant"


def test_generic_compose_puts_the_memory_stack_behind_a_profile() -> None:
    services = _load(GENERIC)["services"]
    assert set(services) == {"knowledge-mcp", "memory-cloud", "postgres"}
    for name in ("memory-cloud", "postgres"):
        assert services[name]["profiles"] == ["memory"], f"{name} must only start with the memory profile"
    assert "profiles" not in services["knowledge-mcp"]
    assert "depends_on" not in services["knowledge-mcp"], "the knowledge server runs alone"
    assert services["memory-cloud"]["image"].startswith("ghcr.io/horacioespinosa/memory:${MEMORY_IMAGE_TAG")
    assert services["memory-cloud"]["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert services["postgres"]["healthcheck"]["test"][0] == "CMD-SHELL"


def test_generic_compose_passes_the_memory_identity_mode_with_grace_by_default() -> None:
    """A deployment switches memory-cloud to ``required`` from its own environment."""
    env = _service_env(_load(GENERIC)["services"]["memory-cloud"])
    assert env["MEMORY_CLOUD_IDENTITY_MODE"] == "${MEMORY_CLOUD_IDENTITY_MODE:-grace}"
    assert env["MEMORY_CLOUD_TOKEN_PEPPER"] == "${MEMORY_CLOUD_TOKEN_PEPPER:-}"


def test_generic_compose_injects_vaults_from_the_environment() -> None:
    service = _load(GENERIC)["services"]["knowledge-mcp"]
    env = _service_env(service)
    assert env["KNOWLEDGE_VAULTS_YAML"] == "${KNOWLEDGE_VAULTS_YAML:-}"
    assert env["GIT_DEPLOY_KEY_B64"] == "${GIT_DEPLOY_KEY_B64:-}"
    # The single-vault knobs stay available and empty until the operator sets them.
    assert env["VAULT_REMOTE"] == "${VAULT_REMOTE:-}"
    assert env["VAULT_BRANCH"] == "${VAULT_BRANCH:-main}"
    # Unset, the served slug is the one the vault's own vault.yaml declares (then `vault`); the compose forces none.
    assert env["CHROMA_COLLECTION"] == "${CHROMA_COLLECTION:-}"
    assert service["command"] == ["knowledge-bootstrap"]
    assert all("://" not in value and "@" not in value for value in env.values()), "no vault URL is baked in"
    # Per-vault deploy keys travel in an optional env file: the slugs are not known here.
    assert service["env_file"] == [{"path": "${KNOWLEDGE_ENV_FILE:-.env.vault-keys}", "required": False}]


def test_generic_compose_publishes_nothing_and_labels_nothing() -> None:
    for name, service in _load(GENERIC)["services"].items():
        assert "ports" not in service, f"{name}: a platform routes to the container, an override publishes"
        assert "labels" not in service and "container_name" not in service, f"{name}: no router or fixed name"


def test_generic_compose_keeps_indexes_and_clones_in_named_volumes() -> None:
    doc = _load(GENERIC)
    service = doc["services"]["knowledge-mcp"]
    mounts = {target: source for source, _, target in (str(m).rpartition(":") for m in service["volumes"])}
    assert mounts["/data/index"] == "knowledge-index"
    assert mounts["/data/vaults"] == "knowledge-vaults"
    assert mounts["/data/local"] == "${KNOWLEDGE_LOCAL_DIR:-knowledge-local}"
    assert {"knowledge-index", "knowledge-vaults", "knowledge-local", "memory-pg"} <= set(doc["volumes"])
    env = _service_env(service)
    assert env["VAULT_PATH"].startswith("${VAULT_PATH:-/data/vaults/")
    assert env["KNOWLEDGE_INDEX_DIR"].startswith("${KNOWLEDGE_INDEX_DIR:-/data/index/")


def test_generic_compose_secrets_are_placeholders() -> None:
    for name, service in _load(GENERIC)["services"].items():
        for var, value in _service_env(service).items():
            if SECRET_NAME.search(var):
                assert PLACEHOLDER.fullmatch(value), f"{name}: {var} must be a ${{NAME}} placeholder"


def test_generic_compose_runs_the_http_server_in_the_container() -> None:
    env = _service_env(_load(GENERIC)["services"]["knowledge-mcp"])
    assert env["MCP_TRANSPORT"] == "http" and env["MCP_HTTP_HOST"] == "0.0.0.0" and env["MCP_HTTP_PORT"] == "8000"


def test_dockerfile_ships_the_vaults_resolver() -> None:
    text = (ROOT / "Dockerfile").read_text()
    assert "COPY infra/resolve-vaults.py /usr/local/bin/knowledge-resolve-vaults" in text
    assert "/usr/local/bin/knowledge-resolve-vaults" in text.split("chmod 0755", 1)[1].splitlines()[0]
    assert "lab/" in (ROOT / ".dockerignore").read_text().splitlines(), "the lab fixtures never enter the build context"


@pytest.mark.parametrize("script", [RESOLVER, LAB / "run.sh", LAB / "seed.sh"], ids=lambda p: p.name)
def test_scripts_are_executable(script: Path) -> None:
    assert script.stat().st_mode & stat.S_IXUSR


# -- infra/resolve-vaults.py ---------------------------------------------------


def _resolve(raw: str, tmp_path: Path, env: dict[str, str] | None = None) -> tuple[dict[str, Any], list[Any]]:
    return _resolver().resolve(raw, env or {}, tmp_path / "out" / "vaults.yaml", root="/data/vaults")


def test_resolver_gives_a_git_vault_its_clone_directory(tmp_path: Path) -> None:
    document, plan = _resolve(
        "version: 1\nvaults:\n  - slug: team-docs\n    repo: https://example.com/org/docs.git\n", tmp_path
    )
    assert document["vaults"][0]["path"] == "/data/vaults/team-docs"
    assert [(v.slug, v.repo, v.branch, v.path, v.key_env) for v in plan] == [
        ("team-docs", "https://example.com/org/docs.git", "main", "/data/vaults/team-docs", None)
    ]


def test_resolver_keeps_a_declared_path_and_branch(tmp_path: Path) -> None:
    raw = (
        "vaults:\n  - slug: team-docs\n    repo: https://example.com/org/docs.git\n"
        "    path: /srv/docs\n    git: {branch: release}\n"
    )
    document, plan = _resolve(raw, tmp_path)
    assert document["vaults"][0]["path"] == "/srv/docs"
    assert (plan[0].path, plan[0].branch) == ("/srv/docs", "release")


def test_resolver_uses_a_mounted_folder_as_it_is_and_clones_nothing(tmp_path: Path) -> None:
    folder = tmp_path / "notes"
    folder.mkdir()
    document, plan = _resolve(f"vaults:\n  - slug: team-notes\n    path: {folder}\n", tmp_path)
    assert document["vaults"][0]["path"] == str(folder)
    assert plan == []


def test_resolver_anchors_relative_paths_like_the_server(tmp_path: Path) -> None:
    (tmp_path / "out" / "notes").mkdir(parents=True)
    document, _ = _resolve("vaults:\n  - slug: team-notes\n    path: notes\n", tmp_path)
    assert document["vaults"][0]["path"] == str((tmp_path / "out" / "notes").resolve())


def test_resolver_keeps_everything_else_for_the_server_to_validate(tmp_path: Path) -> None:
    raw = (
        "version: 1\ndata_dir: /srv/idx\nvaults:\n  - slug: team-docs\n    default: true\n"
        "    repo: https://example.com/org/docs.git\n    features: [flows]\n    memory: {project: docs}\n"
        "bearers:\n  - id: ana\n    token_env: ANA_TOKEN\n"
    )
    document, _ = _resolve(raw, tmp_path)
    assert document["data_dir"] == "/srv/idx" and document["bearers"][0]["id"] == "ana"
    entry = document["vaults"][0]
    assert entry["default"] is True and entry["features"] == ["flows"] and entry["memory"] == {"project": "docs"}


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        ("https://user:s3cret@h.example/o/v.git", "https://***@h.example/o/v.git"),
        ("https://tok3n@h.example/o/v.git", "https://***@h.example/o/v.git"),
        ("http://tok3n@127.0.0.1:8080/v.git", "http://***@127.0.0.1:8080/v.git"),
        ("ssh://deploy:pw@h.example/o/v.git", "ssh://***@h.example/o/v.git"),
        ("fatal: unable to access 'https://u:p@h.example/x/': nope", "fatal: unable to access 'https://***@h.example/x/': nope"),
        ("ssh://git@h.example/o/v.git", "ssh://git@h.example/o/v.git"),
        ("git@h.example:o/v.git", "git@h.example:o/v.git"),
        ("https://h.example/o/v.git", "https://h.example/o/v.git"),
        ("file:///repos/v.git", "file:///repos/v.git"),
    ],
)
def test_resolver_redacts_the_credentials_embedded_in_a_url(text: str, shown: str) -> None:
    assert _resolver().redact(text) == shown


@pytest.mark.parametrize(
    "second",
    ["/data/vaults/docs", "/data/vaults/docs/sub", "/data/vaults", "/data/vaults/docs/../docs"],
    ids=["same", "inside", "around", "same-after-normalizing"],
)
def test_resolver_rejects_two_vaults_that_overlap(tmp_path: Path, second: str) -> None:
    raw = (
        "vaults:\n  - {slug: team-docs, repo: 'https://example.com/o/docs.git', path: /data/vaults/docs}\n"
        f"  - {{slug: team-ops, repo: 'https://example.com/o/ops.git', path: {second}}}\n"
    )
    with pytest.raises(_resolver().ResolveError) as excinfo:
        _resolve(raw, tmp_path)
    assert excinfo.value.code == "E_VAULTS_PATH_OVERLAP"
    assert "team-docs" in str(excinfo.value) and "team-ops" in str(excinfo.value)


def test_resolver_rejects_an_overlap_between_a_clone_directory_and_a_mounted_folder(tmp_path: Path) -> None:
    folder = tmp_path / "vaults" / "team-docs" / "inner"
    folder.mkdir(parents=True)
    raw = (
        "vaults:\n  - {slug: team-docs, repo: 'https://example.com/o/docs.git'}\n"
        f"  - {{slug: team-notes, path: {folder}}}\n"
    )
    with pytest.raises(_resolver().ResolveError) as excinfo:
        _resolver().resolve(raw, {}, tmp_path / "out" / "vaults.yaml", root=str(tmp_path / "vaults"))
    assert excinfo.value.code == "E_VAULTS_PATH_OVERLAP"


def test_resolver_accepts_siblings_whose_names_share_a_prefix(tmp_path: Path) -> None:
    raw = (
        "vaults:\n  - {slug: team-docs, repo: 'https://example.com/o/docs.git', path: /data/vaults/docs}\n"
        "  - {slug: team-docs2, repo: 'https://example.com/o/docs2.git', path: /data/vaults/docs2}\n"
    )
    document, plan = _resolve(raw, tmp_path)
    assert [v["path"] for v in document["vaults"]] == ["/data/vaults/docs", "/data/vaults/docs2"] and len(plan) == 2


@pytest.mark.parametrize(
    ("repo", "needs_key"),
    [
        ("git@github.com:org/vault.git", True),
        ("ssh://git@host.example/org/vault.git", True),
        ("host.example:org/vault.git", True),
        ("file:///repos/vault.git", False),
        ("https://example.com/org/vault.git", False),
        ("http://example.com/org/vault.git", False),
        ("/srv/git/vault.git", False),
    ],
)
def test_resolver_knows_which_remotes_need_a_deploy_key(repo: str, needs_key: bool) -> None:
    assert _resolver().needs_ssh_key(repo) is needs_key


def test_resolver_picks_the_most_specific_deploy_key(tmp_path: Path) -> None:
    raw = (
        "vaults:\n"
        "  - {slug: team-docs, default: true, repo: 'git@h.example:o/a.git'}\n"
        "  - {slug: team-ops, repo: 'git@h.example:o/b.git'}\n"
        "  - {slug: team-lab, repo: 'git@h.example:o/c.git', deploy_key_env: LAB_KEY}\n"
    )
    env = {"GIT_DEPLOY_KEY_B64": "shared", "TEAM_DOCS_GIT_DEPLOY_KEY_B64": "own", "LAB_KEY": "declared"}
    _, plan = _resolve(raw, tmp_path, env)
    assert {v.slug: v.key_env for v in plan} == {
        "team-docs": "TEAM_DOCS_GIT_DEPLOY_KEY_B64",
        "team-ops": "GIT_DEPLOY_KEY_B64",
        "team-lab": "LAB_KEY",
    }


def test_resolver_needs_no_key_for_a_remote_that_is_not_ssh(tmp_path: Path) -> None:
    raw = "vaults:\n  - slug: team-docs\n    repo: file:///repos/docs.git\n"
    _, plan = _resolve(raw, tmp_path, {"GIT_DEPLOY_KEY_B64": "shared"})
    assert plan[0].key_env is None and plan[0].plan_line().endswith("\t-")


def test_resolver_refuses_an_ssh_vault_without_any_key(tmp_path: Path) -> None:
    raw = "vaults:\n  - slug: team-docs\n    repo: 'git@h.example:o/a.git'\n"
    with pytest.raises(_resolver().ResolveError) as raised:
        _resolve(raw, tmp_path, {"GIT_DEPLOY_KEY_B64": "  "})
    assert raised.value.code == "E_DEPLOY_KEY_MISSING" and raised.value.status == 64
    assert "TEAM_DOCS_GIT_DEPLOY_KEY_B64" in str(raised.value)


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("vaults: [", "E_MANIFEST_INVALID"),
        ("- just\n- a list\n", "E_MANIFEST_INVALID"),
        ("version: 1\n", "E_MANIFEST_INVALID"),
        ("vaults: []\n", "E_MANIFEST_INVALID"),
        ("vaults:\n  - just a string\n", "E_MANIFEST_INVALID"),
        ("vaults:\n  - {slug: Bad_Slug, path: /tmp}\n", "E_VAULT_SLUG_INVALID"),
        ("vaults:\n  - {path: /tmp}\n", "E_VAULT_SLUG_INVALID"),
        ("vaults:\n  - {slug: no-source}\n", "E_MANIFEST_INVALID"),
        ("vaults:\n  - {slug: team-docs, path: /absent/folder}\n", "E_VAULT_PATH_NOT_DIR"),
        ("vaults:\n  - {slug: team-docs, repo: '--upload-pack=x'}\n", "E_MANIFEST_INVALID"),
        ("vaults:\n  - {slug: team-docs, repo: 'file:///a\tb'}\n", "E_MANIFEST_INVALID"),
        ("vaults:\n  - {slug: team-docs, repo: 'file:///a', git: main}\n", "E_MANIFEST_INVALID"),
        ("vaults:\n  - {slug: team-docs, repo: 'file:///a', git: {branch: '-x'}}\n", "E_MANIFEST_INVALID"),
        ("vaults:\n  - {slug: team-docs, repo: 'file:///a', deploy_key_env: 'NOT VALID'}\n", "E_MANIFEST_INVALID"),
    ],
)
def test_resolver_rejects_a_bad_declaration_with_a_stable_code(raw: str, code: str, tmp_path: Path) -> None:
    with pytest.raises(_resolver().ResolveError) as raised:
        _resolve(raw, tmp_path)
    assert raised.value.code == code and raised.value.status == 2


def test_resolver_rejects_a_repeated_slug(tmp_path: Path) -> None:
    raw = "vaults:\n  - {slug: team-docs, repo: 'file:///a'}\n  - {slug: team-docs, repo: 'file:///b'}\n"
    with pytest.raises(_resolver().ResolveError) as raised:
        _resolve(raw, tmp_path)
    assert raised.value.code == "E_VAULT_SLUG_DUPLICATE"


def _run_resolver(
    env: dict[str, str], *args: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(RESOLVER), *args],
        env={"PATH": os.environ["PATH"], **env},
        capture_output=True,
        text=True,
        check=False,
    )


def test_resolver_output_is_a_vaults_file_the_server_accepts(tmp_path: Path) -> None:
    folder = tmp_path / "notes"
    folder.mkdir()
    raw = (
        "version: 1\nvaults:\n"
        "  - slug: team-docs\n    default: true\n    repo: https://example.com/org/docs.git\n"
        "    git: {branch: main}\n    features: [flows, git-webhook]\n"
        f"  - slug: team-notes\n    path: {folder}\n"
    )
    out = tmp_path / "vaults.resolved.yaml"
    done = _run_resolver({"KNOWLEDGE_VAULTS_YAML": raw}, str(out))
    assert done.returncode == 0, done.stderr
    specs, warnings = parse_vaults_file(out, {}, check_paths=False)
    assert [(s.slug, s.is_default, s.repo) for s in specs] == [
        ("team-docs", True, "https://example.com/org/docs.git"),
        ("team-notes", False, None),
    ]
    assert specs[0].path == Path("/data/vaults/team-docs") and specs[1].path == folder
    assert warnings == []


def test_resolver_prints_names_never_key_material(tmp_path: Path) -> None:
    raw = "vaults:\n  - slug: team-docs\n    repo: 'git@h.example:o/a.git'\n"
    out = tmp_path / "vaults.resolved.yaml"
    done = _run_resolver({"KNOWLEDGE_VAULTS_YAML": raw, "TEAM_DOCS_GIT_DEPLOY_KEY_B64": KEY_B64}, str(out))
    assert done.returncode == 0, done.stderr
    expected = "team-docs\tgit@h.example:o/a.git\tmain\t/data/vaults/team-docs\tTEAM_DOCS_GIT_DEPLOY_KEY_B64\n"
    assert done.stdout == expected
    for text in (done.stdout, done.stderr, out.read_text()):
        assert KEY_B64 not in text and SEEDED_KEY not in text


def test_resolver_command_line_errors_name_the_problem(tmp_path: Path) -> None:
    empty = _run_resolver({"KNOWLEDGE_VAULTS_YAML": "  "}, str(tmp_path / "out.yaml"))
    assert empty.returncode == 2 and "E_MANIFEST_INVALID" in empty.stderr
    usage = _run_resolver({"KNOWLEDGE_VAULTS_YAML": "vaults: []"})
    assert usage.returncode == 2 and "usage" in usage.stderr
    assert not (tmp_path / "out.yaml").exists()


# -- infra/bootstrap-vault.sh with KNOWLEDGE_VAULTS_YAML ------------------------

GIT_ENV = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def _run_vaults_bootstrap(
    tmp_path: Path,
    vaults_yaml: str,
    env: dict[str, str] | None = None,
    args: tuple[str, ...] = (),
    *,
    fake_git: bool,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run the bootstrap in vaults mode; the real resolver runs, ``python -m knowledge`` is logged.

    With ``fake_git`` every git (and ssh-keyscan) call is logged with the ssh
    command it was given; otherwise git is the real one, pointed at a throwaway
    ``$HOME``. Returns the process and the log, in call order.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    log = tmp_path / "calls.log"
    log.unlink(missing_ok=True)
    fake_python = bin_dir / "python"
    fake_python.write_text(
        '#!/bin/sh\ncase "$*" in\n'
        f'  *"-m knowledge"*) echo "python $* [vaults=$KNOWLEDGE_VAULTS vault_path=${{VAULT_PATH-unset}}]" >> "{log}"; exit 0;;\n'
        f'esac\nexec "{sys.executable}" "$@"\n'
    )
    fake_python.chmod(0o755)
    if fake_git:
        fake = bin_dir / "git"
        fake.write_text(f'#!/bin/sh\necho "git $* ${{GIT_SSH_COMMAND:+[ssh=$GIT_SSH_COMMAND]}}" >> "{log}"\n')
        fake.chmod(0o755)
        keyscan = bin_dir / "ssh-keyscan"
        keyscan.write_text(f'#!/bin/sh\necho "ssh-keyscan $*" >> "{log}"\n')
        keyscan.chmod(0o755)
    base = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(home),
        "KNOWLEDGE_VAULTS_YAML": vaults_yaml,
        "KNOWLEDGE_RESOLVER": str(RESOLVER),
        "KNOWLEDGE_VAULTS_RESOLVED": str(tmp_path / "resolved.yaml"),
        "KNOWLEDGE_VAULTS_ROOT": str(tmp_path / "vaults"),
        **GIT_ENV,
    }
    done = subprocess.run(
        ["sh", str(BOOTSTRAP), *args],
        env={**base, **(env or {})},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return done, log.read_text().splitlines() if log.exists() else []


def _git(*args: str, cwd: Path, home: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, env={**os.environ, **GIT_ENV, "HOME": str(home)})


def _bare_repo(tmp_path: Path, name: str, files: dict[str, str]) -> Path:
    """A bare repository on ``main`` holding ``files``, reachable as a ``file://`` remote."""
    bare = tmp_path / "remotes" / f"{name}.git"
    work = tmp_path / "remotes" / f"{name}-work"
    work.mkdir(parents=True)
    _git("init", "-q", "--bare", "-b", "main", str(bare), cwd=work, home=tmp_path)
    _git("init", "-q", "-b", "main", cwd=work, home=tmp_path)
    for rel, text in files.items():
        (work / rel).parent.mkdir(parents=True, exist_ok=True)
        (work / rel).write_text(text)
    _git("add", "-A", cwd=work, home=tmp_path)
    _git("commit", "-q", "-m", "docs: seed", cwd=work, home=tmp_path)
    _git("remote", "add", "origin", str(bare), cwd=work, home=tmp_path)
    _git("push", "-q", "origin", "main", cwd=work, home=tmp_path)
    return bare


def test_vaults_mode_uses_a_folder_vault_without_calling_git(tmp_path: Path) -> None:
    folder = tmp_path / "notes"
    folder.mkdir()
    yaml_text = f"vaults:\n  - {{slug: team-notes, path: {folder}}}\n"
    done, calls = _run_vaults_bootstrap(tmp_path, yaml_text, fake_git=True)
    assert done.returncode == 0, done.stderr
    resolved = str(tmp_path / "resolved.yaml")
    assert calls == [
        f"python -m knowledge migrate-index [vaults={resolved} vault_path=unset]",
        f"python -m knowledge serve [vaults={resolved} vault_path=unset]",
    ]


def test_vaults_mode_hands_the_resolved_file_to_commands_run_instead_of_serving(tmp_path: Path) -> None:
    folder = tmp_path / "notes"
    folder.mkdir()
    yaml_text = f"vaults:\n  - {{slug: team-notes, path: {folder}}}\n"
    done, calls = _run_vaults_bootstrap(
        tmp_path, yaml_text, args=("sh", "-c", 'echo "$KNOWLEDGE_VAULTS"'), fake_git=True
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == str(tmp_path / "resolved.yaml")
    assert calls == []


def test_vaults_mode_clones_every_git_vault_into_its_directory(tmp_path: Path) -> None:
    docs = _bare_repo(tmp_path, "docs", {"vault.yaml": "slug: team-docs\n", "A.md": "# A\n"})
    ops = _bare_repo(tmp_path, "ops", {"vault.yaml": "slug: team-ops\n", "B.md": "# B\n"})
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "vault.yaml").write_text("slug: team-notes\n")
    yaml_text = (
        f"vaults:\n  - {{slug: team-docs, default: true, repo: 'file://{docs}'}}\n"
        f"  - {{slug: team-ops, repo: 'file://{ops}', git: {{branch: main}}}}\n"
        f"  - {{slug: team-notes, path: {folder}}}\n"
    )
    done, calls = _run_vaults_bootstrap(tmp_path, yaml_text, fake_git=False)
    assert done.returncode == 0, done.stderr
    assert (tmp_path / "vaults" / "team-docs" / "A.md").read_text() == "# A\n"
    assert (tmp_path / "vaults" / "team-ops" / "B.md").read_text() == "# B\n"
    assert not (tmp_path / "vaults" / "team-notes").exists()
    specs, _ = parse_vaults_file(tmp_path / "resolved.yaml", {}, check_paths=True)
    assert [(s.slug, s.is_default, s.repo is not None) for s in specs] == [
        ("team-docs", True, True),
        ("team-ops", False, True),
        ("team-notes", False, False),
    ]
    assert calls[-1].startswith("python -m knowledge serve")


def test_vaults_mode_refreshes_an_existing_clone_and_survives_a_dead_remote(tmp_path: Path) -> None:
    docs = _bare_repo(tmp_path, "docs", {"vault.yaml": "slug: team-docs\n", "A.md": "# A\n"})
    yaml_text = f"vaults:\n  - {{slug: team-docs, repo: 'file://{docs}'}}\n"
    first, _ = _run_vaults_bootstrap(tmp_path, yaml_text, fake_git=False)
    assert first.returncode == 0, first.stderr

    # A new commit on the remote reaches the clone on the next start.
    update = tmp_path / "update"
    update.mkdir()
    _git("clone", "-q", str(docs), str(update), cwd=update, home=tmp_path)
    (update / "C.md").write_text("# C\n")
    _git("add", "-A", cwd=update, home=tmp_path)
    _git("commit", "-q", "-m", "docs: add c", cwd=update, home=tmp_path)
    _git("push", "-q", "origin", "main", cwd=update, home=tmp_path)
    shutil.rmtree(tmp_path / "bin")
    second, _ = _run_vaults_bootstrap(tmp_path, yaml_text, fake_git=False)
    assert second.returncode == 0, second.stderr
    assert (tmp_path / "vaults" / "team-docs" / "C.md").exists()

    # With the remote gone the clone on disk is served and the start is only warned.
    shutil.rmtree(docs)
    shutil.rmtree(tmp_path / "bin")
    third, calls = _run_vaults_bootstrap(tmp_path, yaml_text, fake_git=False)
    assert third.returncode == 0, third.stderr
    assert "W_VAULT_REFRESH_FAILED: team-docs" in third.stderr
    assert (tmp_path / "vaults" / "team-docs" / "A.md").exists()
    assert calls[-1].startswith("python -m knowledge serve")


def test_vaults_mode_stops_when_a_git_vault_cannot_be_cloned(tmp_path: Path) -> None:
    yaml_text = f"vaults:\n  - {{slug: team-docs, repo: 'file://{tmp_path}/absent.git'}}\n"
    done, calls = _run_vaults_bootstrap(tmp_path, yaml_text, fake_git=False)
    assert done.returncode == 1
    assert "E_VAULT_CLONE_FAILED: team-docs" in done.stderr
    assert not any(c.startswith("python -m knowledge") for c in calls), "nothing is served after a failed clone"


def _failing_git(tmp_path: Path, *, failing: str) -> None:
    """A ``git`` that succeeds except for ``failing``, which prints the remote URL it was given, as git does."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "git"
    remembered = tmp_path / "last-remote-url"
    fake.write_text(
        "#!/bin/sh\n"
        f'for a in "$@"; do case "$a" in *://*) printf %s "$a" > "{remembered}";; esac; done\n'
        f'if echo " $* " | grep -q " {failing} "; then\n'
        f'  echo "fatal: unable to access \'$(cat "{remembered}")/\': Could not resolve host" >&2\n'
        "  exit 128\n"
        "fi\n"
        "exit 0\n"
    )
    fake.chmod(0o755)


def test_vaults_mode_never_prints_the_credentials_of_a_clone_url(tmp_path: Path) -> None:
    _failing_git(tmp_path, failing="clone")
    yaml_text = "vaults:\n  - {slug: team-docs, repo: 'https://deploy:s3cret@h.example/o/docs.git'}\n"

    done, _calls = _run_vaults_bootstrap(tmp_path, yaml_text, fake_git=False)

    assert done.returncode == 1
    assert "E_VAULT_CLONE_FAILED: team-docs could not be cloned from https://***@h.example/o/docs.git" in done.stderr
    assert "unable to access 'https://***@h.example/o/docs.git/'" in done.stderr, "git's own message is redacted too"
    assert "s3cret" not in done.stdout + done.stderr


def test_vaults_mode_never_prints_the_credentials_when_a_refresh_fails(tmp_path: Path) -> None:
    (tmp_path / "vaults" / "team-docs" / ".git").mkdir(parents=True)
    _failing_git(tmp_path, failing="fetch")
    yaml_text = "vaults:\n  - {slug: team-docs, repo: 'https://t0ken@h.example/o/docs.git'}\n"

    done, _calls = _run_vaults_bootstrap(tmp_path, yaml_text, fake_git=False)

    assert done.returncode == 0, done.stderr
    assert "W_VAULT_REFRESH_FAILED: team-docs" in done.stderr
    assert "unable to access 'https://***@h.example/o/docs.git/'" in done.stderr
    assert "t0ken" not in done.stdout + done.stderr


def test_vaults_mode_does_not_print_ssh_credentials_when_the_key_is_missing(tmp_path: Path) -> None:
    yaml_text = "vaults:\n  - {slug: team-docs, repo: 'ssh://deploy:t0ken@h.example/o/docs.git'}\n"

    done, calls = _run_vaults_bootstrap(tmp_path, yaml_text, fake_git=True)

    assert done.returncode == 64 and calls == []
    assert "E_DEPLOY_KEY_MISSING" in done.stderr and "ssh://***@h.example/o/docs.git" in done.stderr
    assert "t0ken" not in done.stdout + done.stderr


def test_vaults_mode_gives_each_git_vault_its_own_deploy_key(tmp_path: Path) -> None:
    yaml_text = (
        "vaults:\n"
        "  - {slug: team-docs, default: true, repo: 'git@h.example:o/docs.git'}\n"
        "  - {slug: team-ops, repo: 'git@h.example:o/ops.git'}\n"
    )
    own = base64.b64encode(b"KEY-OF-DOCS").decode()
    done, calls = _run_vaults_bootstrap(
        tmp_path,
        yaml_text,
        env={"TEAM_DOCS_GIT_DEPLOY_KEY_B64": own, "GIT_DEPLOY_KEY_B64": KEY_B64},
        fake_git=True,
    )
    assert done.returncode == 0, done.stderr
    ssh = tmp_path / "home" / ".ssh"
    assert (ssh / "knowledge_team-docs").read_bytes() == b"KEY-OF-DOCS"
    assert (ssh / "knowledge_team-ops").read_bytes() == SEEDED_KEY.encode(), "the shared key is the fallback"
    for key_file in (ssh / "knowledge_team-docs", ssh / "knowledge_team-ops"):
        assert stat.S_IMODE(key_file.stat().st_mode) == 0o600

    docs_ssh = f"ssh -i {ssh}/knowledge_team-docs -o IdentitiesOnly=yes"
    logged = [c.strip() for c in calls]
    assert f"git clone --branch main -- git@h.example:o/docs.git {tmp_path}/vaults/team-docs [ssh={docs_ssh}]" in logged
    # The clone remembers its key so the server's later `git pull` reads the same remote.
    assert f"git -C {tmp_path}/vaults/team-docs config core.sshCommand {docs_ssh}" in logged
    assert any(c.startswith("ssh-keyscan h.example") for c in calls)
    for text in (done.stdout, done.stderr, (tmp_path / "resolved.yaml").read_text()):
        assert KEY_B64 not in text and SEEDED_KEY not in text and own not in text


def test_vaults_mode_without_a_key_for_an_ssh_vault_exits_64_before_git(tmp_path: Path) -> None:
    yaml_text = "vaults:\n  - {slug: team-docs, repo: 'git@h.example:o/docs.git'}\n"
    done, calls = _run_vaults_bootstrap(tmp_path, yaml_text, env={"UNRELATED_SECRET": SEEDED_KEY}, fake_git=True)
    assert done.returncode == 64
    assert "E_DEPLOY_KEY_MISSING" in done.stderr
    assert calls == []
    assert SEEDED_KEY not in done.stdout + done.stderr


def test_vaults_mode_reports_an_invalid_declaration_and_serves_nothing(tmp_path: Path) -> None:
    done, calls = _run_vaults_bootstrap(tmp_path, "vaults: []\n", fake_git=True)
    assert done.returncode == 2
    assert "E_MANIFEST_INVALID" in done.stderr
    assert calls == []


def test_vaults_mode_is_off_when_the_variable_is_empty(tmp_path: Path) -> None:
    """An empty KNOWLEDGE_VAULTS_YAML is the single-vault bootstrap, error and all."""
    done, calls = _run_vaults_bootstrap(tmp_path, "", env={"VAULT_PATH": ""}, fake_git=True)
    assert done.returncode == 1
    assert errors.E_VAULT_PATH_MISSING in done.stderr
    assert calls == []


def test_vaults_mode_leaves_the_single_vault_variables_out_of_the_server(tmp_path: Path) -> None:
    folder = tmp_path / "notes"
    folder.mkdir()
    yaml_text = f"vaults:\n  - {{slug: team-notes, path: {folder}}}\n"
    done, calls = _run_vaults_bootstrap(
        tmp_path,
        yaml_text,
        env={"VAULT_PATH": "/data/vault", "VAULT_REMOTE": "git@h.example:o/legacy.git"},
        fake_git=True,
    )
    assert done.returncode == 0, done.stderr
    assert all("vault_path=unset" in c for c in calls), "the image's VAULT_PATH would be reported as ignored"
    assert not any(c.startswith("git ") for c in calls), "VAULT_REMOTE is not used in vaults mode"


# -- the lab -------------------------------------------------------------------


def test_lab_runs_two_tenants_from_the_one_generic_service() -> None:
    services = _load_compose(LAB / "compose.yml")["services"]
    assert set(services) == {"knowledge-acme", "knowledge-globex"}
    ports, index_volumes, bearers = [], [], []
    for name, service in services.items():
        assert service["extends"] == {"file": "../infra/compose.yml", "service": "knowledge-mcp"}
        assert "build" not in service and "image" not in service, f"{name} must not redefine the service"
        env = _service_env(service)
        assert env["KNOWLEDGE_EMBEDDINGS_PROVIDER"] == "bag-of-words"
        assert env["OPENAI_API_KEY"] == "", "a key exported in the host shell never reaches the lab"
        assert "KNOWLEDGE_REQUIRE_SQLITE_VEC" not in env
        assert env["MCP_API_KEY"].startswith("${LAB_") and env["MCP_ADMIN_KEY"].startswith("${LAB_")
        bearers += [env["MCP_API_KEY"], env["MCP_ADMIN_KEY"]]
        assert re.fullmatch(r"127\.0\.0\.1:\$\{LAB_[A-Z]+_PORT:-\d+\}:8000", service["ports"][0])
        ports.append(service["ports"][0])
        index_volumes += [m for m in service["volumes"] if m.endswith(":/data/index")]
        assert len(service["networks"]) == 1, "each tenant has a network of its own"
    assert len(set(ports)) == 2 and len(set(index_volumes)) == 2 and len(set(bearers)) == 4


def test_lab_vaults_resolve_to_what_each_tenant_is_meant_to_serve(tmp_path: Path) -> None:
    services = _load_compose(LAB / "compose.yml")["services"]
    ops = tmp_path / "acme-ops"
    ops.mkdir()
    notes = tmp_path / "globex-notes"
    notes.mkdir()
    served: dict[str, dict[str, Any]] = {}
    for name, service in services.items():
        raw = (
            _service_env(service)["KNOWLEDGE_VAULTS_YAML"]
            .replace("/vaults/acme-ops", str(ops))
            .replace("/vaults/globex-notes", str(notes))
        )
        out = tmp_path / name / "vaults.yaml"
        out.parent.mkdir(parents=True, exist_ok=True)
        document, plan = _resolve(raw, tmp_path / name)
        out.write_text(yaml.safe_dump(document, sort_keys=False))
        parse_vaults_file(out, {}, check_paths=False)
        served[name] = {"slugs": [v["slug"] for v in document["vaults"]], "git": [v.slug for v in plan]}
    assert served["knowledge-acme"] == {"slugs": ["acme-handbook", "acme-ops"], "git": ["acme-handbook"]}
    assert served["knowledge-globex"] == {"slugs": ["globex-docs", "globex-notes"], "git": ["globex-docs"]}


def test_lab_fixtures_stay_out_of_git() -> None:
    assert "lab/.work/" in (ROOT / ".gitignore").read_text().splitlines()


@pytest.mark.skipif(shutil.which("git") is None or shutil.which("bash") is None, reason="needs git and bash")
def test_lab_seed_builds_tenants_whose_vaults_the_server_accepts(tmp_path: Path) -> None:
    lab = tmp_path / "lab"
    shutil.copytree(LAB, lab, ignore=shutil.ignore_patterns(".work"))
    env = {**os.environ, **GIT_ENV, "HOME": str(tmp_path)}
    subprocess.run(["bash", str(lab / "seed.sh")], check=True, capture_output=True, env=env)
    work = lab / ".work"
    clones: dict[str, Path] = {}
    for slug in ("acme-handbook", "globex-docs"):
        clones[slug] = tmp_path / slug
        _git("clone", "-q", str(work / "repos" / f"{slug}.git"), str(clones[slug]), cwd=tmp_path, home=tmp_path)
    clones["acme-ops"] = work / "vaults" / "acme-ops"
    clones["globex-notes"] = work / "vaults" / "globex-notes"

    registry = {
        "version": 1,
        "vaults": [{"slug": slug, "path": str(path), "default": slug == "acme-handbook"} for slug, path in clones.items()],
    }
    vaults_file = tmp_path / "vaults.yaml"
    vaults_file.write_text(yaml.safe_dump(registry))
    specs, _ = parse_vaults_file(vaults_file, {}, check_paths=True)
    assert {s.slug for s in specs} == set(clones)

    words = {
        "acme-handbook": {"zephyr", "marmot"},
        "acme-ops": {"zephyr", "nautilus"},
        "globex-docs": {"quokka", "pangolin"},
        "globex-notes": {"quokka", "lemur"},
    }
    every_word = set().union(*words.values())
    for slug, path in clones.items():
        assert read_vault_manifest(path)["slug"] == slug
        assert (path / "90 - Memory").is_dir()
        lint_report = vault_lint.lint(path, rules=vault_lint.RULES_1_3)
        assert lint_report.errors == [], f"{slug} must lint clean, Contexts/ being excluded from the frontmatter rules"
        body = "\n".join(p.read_text() for p in sorted(path.rglob("*.md")) if ".git" not in p.parts)
        assert {w for w in every_word if w in body} == words[slug], f"{slug} shares words with another vault"
        declaration = json.loads((path / "Contexts" / "config.json").read_text())
        manifest = read_vault_manifest(path)
        assert ("governance" in manifest.get("features", [])) == (slug == "acme-handbook")
        repos = declaration["repos"]
        assert set(repos) >= {"roots", "groups", "entries"}
        if slug == "globex-notes":
            assert (repos["roots"], repos["entries"], declaration["graph"]["targets"]) == ([], [], [])
        for entry in repos["entries"]:
            assert {"id", "context", "install_mode", "remote", "group", "root", "path"} <= set(entry)
            package = path / "Contexts" / entry["context"]
            assert (package / "CLAUDE.md").is_file() and (package / ".mcp.json").is_file()
            assert (package / ".claude" / "agents" / "x.md").is_file()


@pytest.mark.skipif(shutil.which("git") is None or shutil.which("bash") is None, reason="needs git and bash")
def test_lab_seed_builds_clients_whose_checkouts_init_finds_by_origin(tmp_path: Path) -> None:
    lab = tmp_path / "lab"
    shutil.copytree(LAB, lab, ignore=shutil.ignore_patterns(".work"))
    env = {**os.environ, **GIT_ENV, "HOME": str(tmp_path)}
    subprocess.run(["bash", str(lab / "seed.sh")], check=True, capture_output=True, env=env)
    work = lab / ".work"
    # Rebuilding only the clients is allowed and leaves the tenants where they are.
    bares = {p: p.stat().st_mtime_ns for p in sorted((work / "repos").iterdir())}
    subprocess.run(["bash", str(lab / "seed.sh"), "--clients"], check=True, capture_output=True, env=env)
    assert {p: p.stat().st_mtime_ns for p in sorted((work / "repos").iterdir())} == bares

    expected = {
        "alice": {
            "acme-handbook": {"acme-web": "would-install", "acme-api": "skipped"},
            "acme-ops": {"acme-deploy": "would-install"},
        },
        "bob": {"globex-docs": {"globex-core": "would-install"}, "globex-notes": {}},
    }
    for name, vaults in expected.items():
        home = work / "clients" / name
        specs, _ = parse_vaults_file(home / ".config" / "knowledge" / "vaults.yaml", {}, check_paths=True)
        assert {s.slug: s for s in specs}.keys() == vaults.keys()
        for spec in specs:
            outcome = contexts.init_vault(
                contexts.VaultTarget(spec.slug, spec.path),
                env={"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config")},
                search_dirs=[home / "Projects"],
                dry_run=True,
                selection=contexts.Selection(everything=True),
            )
            assert outcome.ok, outcome.detail
            assert outcome.status == ("initialized" if vaults[spec.slug] else "no-apps")
            covered = {"acme-handbook": "covered", "acme-ops": "not-applicable", "globex-docs": "covered"}
            assert outcome.gitignore == covered.get(spec.slug, "") and outcome.warnings == []
            assert {app.id: app.status for app in outcome.apps} == vaults[spec.slug]
        for slug in vaults:
            assert not (home / ".config" / "knowledge" / slug / "repos.local.json").exists(), "a dry run writes nothing"

    # The synthetic tree around alice's checkouts: about 2,000 directories, 60 repos that match nothing.
    projects = work / "clients" / "alice" / "Projects"
    directories = [p for p in projects.rglob("*") if p.is_dir() and not any(s.startswith(".") for s in p.relative_to(projects).parts)]
    assert len(directories) >= 2000
    found = contexts.scan_checkouts([projects])
    assert len(found) == 2 + 60
    assert sum(1 for _, url in found if url.startswith("git@git.lab.invalid:noise/")) == 60

    # The review queue has something to leave out: old notes, one of them inside a context package.
    handbook = work / "clients" / "alice" / "Vaults" / "acme-handbook"
    for rel in ("Architecture/Legacy Notes.md", "Contexts/acme-web/history.md"):
        meta = yaml.safe_load((handbook / rel).read_text().split("---")[1])
        assert str(meta["last_updated"]) < "2025-01-01", rel
    assert read_vault_manifest(handbook)["index"]["exclude"] == ["Contexts/**"]

    # What Claude Code writes next to a package never reaches the repository: the vault ignores it.
    for slug in ("acme-handbook", "globex-docs"):
        tracked = subprocess.run(
            ["git", "--git-dir", str(work / "repos" / f"{slug}.git"), "ls-tree", "-r", "--name-only", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        assert ".gitignore" in tracked
        assert not [t for t in tracked if t.endswith("settings.local.json") or "/agent-memory/" in t or "/worktrees/" in t]
    for vault in (work / "vaults" / "acme-ops", work / "clients" / "bob" / "Vaults" / "globex-docs"):
        assert read_vault_manifest(vault)["index"]["exclude"] == ["Contexts/**"]
        assert (vault / ".gitignore").read_text().splitlines() == [
            "Contexts/*/.claude/settings.local.json",
            "Contexts/*/.claude/agent-memory/",
            "Contexts/*/.claude/worktrees/",
        ]
