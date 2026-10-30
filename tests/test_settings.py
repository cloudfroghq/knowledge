"""Explicit settings: nothing happens at import, and every rule is pinned.

``knowledge.settings`` replaces a configuration that was read, validated and
turned into live objects the moment ``knowledge.config`` or
``knowledge.server`` was imported. These cases pin the new contract:

* importing the server, the facade or the indexer reads nothing and creates
  nothing, so ``import knowledge.server`` works without an OpenAI key;
* ``load_settings`` keeps the precedence the server always applied (a present
  variable wins even when empty, then ``config.json``, then the default), with
  the declared exceptions where an empty value can only be a mistake;
* ``validate`` asks only for what the configuration will use, and fails with
  a stable code and a message that names variables, never their values;
* the ``knowledge.config`` facade answers every legacy name as before.

Every case builds its settings from an explicit ``env`` mapping and a
``project_root`` under ``tmp_path``: no case reads the real vault, the real
``config.json`` or the developer's environment.
"""

from __future__ import annotations

import ast
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from knowledge import config, errors, gitsync, settings
from knowledge.errors import KnowledgeConfigError

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_VAULT = REPO_ROOT / "tests" / "fixtures" / "vault"

#: A value shaped like a real credential. It must never reach a message.
_TOKEN_SHAPED = "sk-proj-THISISNOTAREALKEYbutlookslikeone0123456789"


def _root(tmp_path: Path, cfg: dict | None = None) -> Path:
    root = tmp_path / "root"
    root.mkdir(exist_ok=True)
    if cfg is not None:
        (root / "config.json").write_text(json.dumps(cfg))
    return root


def _load(tmp_path: Path, env: dict[str, str], cfg: dict | None = None) -> settings.Settings:
    return settings.load_settings(env, project_root=_root(tmp_path, cfg))


def _code(exc_info: pytest.ExceptionInfo[KnowledgeConfigError]) -> str:
    return exc_info.value.code


# --- import has no effects ----------------------------------------------------


def _clean_env(tmp_path: Path) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"OPENAI_API_KEY", "VAULT_PATH", "CHROMA_PERSIST_DIR"}
    }
    env["CHROMA_PERSIST_DIR"] = str(tmp_path / "x")
    env["KNOWLEDGE_EMBEDDINGS_PROVIDER"] = "none"
    return env


@pytest.mark.parametrize("module", ["knowledge.server", "knowledge.config", "knowledge.indexer"])
def test_importing_reads_and_creates_nothing(tmp_path: Path, module: str) -> None:
    probe = (
        "import sys\n"
        f"import {module}\n"
        "from knowledge import settings\n"
        "sys.exit(0 if settings._current is None else 3)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        env=_clean_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "x").exists(), "importing must not create the sidecar directory"


def test_main_reports_a_config_error_as_a_code_without_a_traceback(tmp_path: Path) -> None:
    env = _clean_env(tmp_path)
    env.update(
        {
            "VAULT_PATH": str(FIXTURE_VAULT),
            "KNOWLEDGE_EMBEDDINGS_PROVIDER": "openai",
            # Present and empty: a .env can never fill it in.
            "OPENAI_API_KEY": "",
            "MCP_API_KEY": _TOKEN_SHAPED,
        }
    )
    result = subprocess.run(
        [sys.executable, "-c", "from knowledge import server; server.main()"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 2
    assert result.stderr.startswith("E_EMBEDDINGS_KEY_MISSING: ")
    assert "OPENAI_API_KEY" in result.stderr
    assert "Traceback" not in result.stderr
    assert _TOKEN_SHAPED not in result.stderr + result.stdout


# --- precedence ---------------------------------------------------------------


def test_a_present_empty_variable_wins_over_config_json(tmp_path: Path) -> None:
    s = _load(tmp_path, {"CONFLUENCE_URL": ""}, {"confluence": {"url": "https://wiki.example.test"}})
    assert s.confluence.url == ""


def test_an_absent_variable_falls_back_to_config_json(tmp_path: Path) -> None:
    s = _load(tmp_path, {}, {"confluence": {"url": "https://wiki.example.test"}})
    assert s.confluence.url == "https://wiki.example.test"


def test_an_empty_vault_path_falls_back_to_config_json(tmp_path: Path) -> None:
    s = _load(tmp_path, {"VAULT_PATH": ""}, {"vault_path": "vault/team"})
    assert s.default_vault().path == tmp_path / "root" / "vault" / "team"


def test_an_empty_vault_path_never_resolves_to_the_checkout_root(tmp_path: Path) -> None:
    s = _load(tmp_path, {"VAULT_PATH": "", "KNOWLEDGE_EMBEDDINGS_PROVIDER": "none"})
    assert s.default_vault().path is None
    with pytest.raises(KnowledgeConfigError) as exc_info:
        s.validate()
    assert _code(exc_info) == errors.E_VAULT_PATH_MISSING


def test_a_relative_vault_path_is_anchored_at_the_project_root(tmp_path: Path) -> None:
    s = _load(tmp_path, {"VAULT_PATH": "some/vault"})
    assert s.default_vault().path == tmp_path / "root" / "some" / "vault"


def test_a_missing_config_json_is_reported_as_not_loaded(tmp_path: Path) -> None:
    s = _load(tmp_path, {})
    assert s.config_json_loaded is False
    assert dict(s.default_vault().review_thresholds) == {"default": 270}
    assert s.default_vault().thresholds_from_config is False


def test_knowledge_config_json_points_at_another_file(tmp_path: Path) -> None:
    other = tmp_path / "elsewhere.json"
    other.write_text(json.dumps({"review": {"thresholds_days": {"runbook": 7}}}))
    s = _load(tmp_path, {"KNOWLEDGE_CONFIG_JSON": str(other)}, {"review": {"thresholds_days": {"runbook": 90}}})
    assert s.config_json_path == other
    assert s.default_vault().review_thresholds["runbook"] == 7


def test_an_unreadable_config_json_raises_a_coded_error(tmp_path: Path) -> None:
    root = _root(tmp_path)
    (root / "config.json").write_text("{not json")
    with pytest.raises(KnowledgeConfigError) as exc_info:
        settings.load_settings({}, project_root=root)
    assert _code(exc_info) == errors.E_CONFIG_JSON_INVALID
    assert isinstance(exc_info.value, RuntimeError)


def test_a_non_numeric_port_raises_a_coded_error(tmp_path: Path) -> None:
    with pytest.raises(KnowledgeConfigError) as exc_info:
        _load(tmp_path, {"MCP_HTTP_PORT": "eighty"})
    assert _code(exc_info) == errors.E_HTTP_PORT_INVALID


def test_dotenv_never_overrides_a_variable_pinned_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _root(tmp_path, {"review": {"thresholds_days": {"runbook": 90}}})
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps({"review": {"thresholds_days": {"runbook": 1}}}))
    (root / ".env").write_text(f"KNOWLEDGE_CONFIG_JSON={elsewhere}\n")
    monkeypatch.setenv("KNOWLEDGE_CONFIG_JSON", "")

    s = settings.load_settings(project_root=root)

    assert s.config_json_path == root / "config.json"
    assert s.default_vault().review_thresholds["runbook"] == 90


# --- sidecar location -----------------------------------------------------------


def test_knowledge_index_dir_wins(tmp_path: Path) -> None:
    s = _load(
        tmp_path,
        {
            "KNOWLEDGE_INDEX_DIR": str(tmp_path / "i"),
            "CHROMA_PERSIST_DIR": str(tmp_path / "c"),
            "KNOWLEDGE_HOME": str(tmp_path / "h"),
        },
    )
    assert s.default_vault().index_dir == tmp_path / "i"
    assert s.default_vault().chroma_dir == tmp_path / "c"
    assert s.default_vault().skipped_log == tmp_path / "i" / "skipped_files.json"


def test_chroma_persist_dir_stays_the_index_directory(tmp_path: Path) -> None:
    """A deployment that mounts CHROMA_PERSIST_DIR keeps its volume: index.sqlite lives beside Chroma."""
    s = _load(tmp_path, {"CHROMA_PERSIST_DIR": str(tmp_path / "c"), "KNOWLEDGE_HOME": str(tmp_path / "h")})
    assert s.default_vault().index_dir == tmp_path / "c"
    assert s.default_vault().chroma_dir == tmp_path / "c"
    assert s.default_vault().skipped_log == tmp_path / "c" / "skipped_files.json"


def test_an_explicit_knowledge_home_holds_the_slug_directory(tmp_path: Path) -> None:
    s = _load(tmp_path, {"KNOWLEDGE_HOME": str(tmp_path / "h"), "CHROMA_COLLECTION": "alpha"})
    assert s.default_vault().index_dir == tmp_path / "h" / "alpha"
    assert s.default_vault().chroma_dir == tmp_path / "h" / "alpha" / "chroma"
    assert s.default_vault().slug == "alpha"


def test_without_either_the_index_stays_under_the_checkout(tmp_path: Path) -> None:
    """An XDG data home alone never relocates an index."""
    s = _load(tmp_path, {"XDG_DATA_HOME": str(tmp_path / "xdg")})
    assert s.default_vault().index_dir == tmp_path / "root" / ".data" / "vault"
    assert s.default_vault().chroma_dir == tmp_path / "root" / ".data" / "chroma"
    assert s.knowledge_home == tmp_path / "xdg" / "knowledge"


# --- validate -----------------------------------------------------------------


def _servable(tmp_path: Path, **overrides: str) -> dict[str, str]:
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    env = {"VAULT_PATH": str(vault), "OPENAI_API_KEY": "placeholder-not-a-credential"}
    env.update(overrides)
    return env


def test_a_servable_stdio_configuration_validates(tmp_path: Path) -> None:
    warnings = _load(tmp_path, _servable(tmp_path), {"review": {"thresholds_days": {"default": 1}}}).validate()
    assert warnings == []


def test_a_vault_in_a_git_checkout_without_a_repo_warns_that_pull_is_off(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    cfg = {"review": {"thresholds_days": {"default": 1}}}

    silent = _load(tmp_path, _servable(tmp_path), cfg).validate()
    declared = _load(tmp_path, _servable(tmp_path, VAULT_REMOTE="git@example.invalid:org/vault.git"), cfg).validate()

    assert [w.split(":")[0] for w in silent] == [errors.W_VAULT_GIT_PULL_OFF]
    assert declared == []


def test_openai_without_a_key_is_refused_naming_the_variable_only(tmp_path: Path) -> None:
    env = _servable(tmp_path, OPENAI_API_KEY="", MCP_API_KEY=_TOKEN_SHAPED, CONFLUENCE_TOKEN=_TOKEN_SHAPED)
    with pytest.raises(KnowledgeConfigError) as exc_info:
        _load(tmp_path, env).validate()
    assert _code(exc_info) == errors.E_EMBEDDINGS_KEY_MISSING
    assert "OPENAI_API_KEY" in str(exc_info.value)
    assert _TOKEN_SHAPED not in str(exc_info.value)


def test_the_none_provider_needs_no_key(tmp_path: Path) -> None:
    env = _servable(tmp_path, OPENAI_API_KEY="", KNOWLEDGE_EMBEDDINGS_PROVIDER="none")
    _load(tmp_path, env).validate()


def test_an_unknown_provider_is_refused(tmp_path: Path) -> None:
    with pytest.raises(KnowledgeConfigError) as exc_info:
        _load(tmp_path, _servable(tmp_path, KNOWLEDGE_EMBEDDINGS_PROVIDER="ollama")).validate()
    assert _code(exc_info) == errors.E_EMBEDDINGS_PROVIDER_UNKNOWN


def test_a_vault_path_that_is_not_a_directory_is_refused(tmp_path: Path) -> None:
    env = _servable(tmp_path, VAULT_PATH=str(tmp_path / "nowhere"))
    with pytest.raises(KnowledgeConfigError) as exc_info:
        _load(tmp_path, env).validate()
    assert _code(exc_info) == errors.E_VAULT_PATH_NOT_DIR


def test_http_without_a_bearer_is_refused(tmp_path: Path) -> None:
    with pytest.raises(KnowledgeConfigError) as exc_info:
        _load(tmp_path, _servable(tmp_path, MCP_TRANSPORT="http")).validate()
    assert _code(exc_info) == errors.E_HTTP_API_KEY_MISSING


def test_survivable_problems_come_back_as_warning_codes(tmp_path: Path) -> None:
    remote = "git@example.test:org/vault.git"
    http = _load(
        tmp_path, _servable(tmp_path, MCP_TRANSPORT="http", MCP_API_KEY=_TOKEN_SHAPED, VAULT_REMOTE=remote)
    ).validate()
    odd = _load(tmp_path, _servable(tmp_path, MCP_TRANSPORT="carrier-pigeon")).validate()

    assert [w.split(":")[0] for w in http] == [
        errors.W_REVIEW_THRESHOLDS_FALLBACK,
        errors.W_WEBHOOK_SECRET_MISSING,
    ]
    assert errors.W_TRANSPORT_UNKNOWN in [w.split(":")[0] for w in odd]
    assert not any(_TOKEN_SHAPED in w for w in http + odd)


def test_no_webhook_secret_warning_without_the_route(tmp_path: Path) -> None:
    """Without a repository the server registers no /webhook/reindex, so no secret is missing."""
    http = _load(tmp_path, _servable(tmp_path, MCP_TRANSPORT="http", MCP_API_KEY=_TOKEN_SHAPED)).validate()

    assert errors.W_WEBHOOK_SECRET_MISSING not in [w.split(":")[0] for w in http]


def test_secrets_stay_out_of_reprs(tmp_path: Path) -> None:
    env = _servable(
        tmp_path, OPENAI_API_KEY=_TOKEN_SHAPED, MCP_API_KEY=_TOKEN_SHAPED, CONFLUENCE_TOKEN=_TOKEN_SHAPED
    )
    assert _TOKEN_SHAPED not in repr(_load(tmp_path, env))


# --- the legacy facade ----------------------------------------------------------

_FACADE_CONFIG_JSON = {
    "embedding_model": "text-embedding-3-small",
    "vault_path": "vault/ignored",
    "chroma_collection": "team",
    "review": {"thresholds_days": {"runbook": 90, "default": 270}},
    "memory": {
        "vault_folder": "99 - Memories",
        "export_types": ["decision"],
        "promotion": {"output_dir": "out/promotions", "doc_status": "draft", "default_owner": "ops"},
    },
    "confluence": {"url": "https://wiki.example.test"},
}


def test_every_legacy_name_resolves_as_before(tmp_path: Path) -> None:
    """The values ``config.py`` produced at import, over the same environment."""
    root = _root(tmp_path, _FACADE_CONFIG_JSON)
    env = {
        "VAULT_PATH": str(tmp_path / "vault"),
        "CHROMA_PERSIST_DIR": str(tmp_path / "chroma"),
        "OPENAI_API_KEY": "placeholder-not-a-credential",
        "CONFLUENCE_EMAIL": "bot@example.test",
        "CONFLUENCE_TOKEN": "token-placeholder",
        "MCP_TRANSPORT": "http",
        "MCP_API_KEY": "bearer-placeholder",
        "MCP_HTTP_PORT": "8200",
    }
    settings.install(settings.load_settings(env, project_root=root))

    expected = {
        "CONFIG_JSON_PATH": root / "config.json",
        "CONFIG_JSON_LOADED": True,
        "OPENAI_API_KEY": "placeholder-not-a-credential",
        "CONFLUENCE_EMAIL": "bot@example.test",
        "CONFLUENCE_TOKEN": "token-placeholder",
        "CONFLUENCE_URL": "https://wiki.example.test",
        "MCP_TRANSPORT": "http",
        "MCP_API_KEY": "bearer-placeholder",
        "MCP_ADMIN_KEY": "",
        "MCP_WEBHOOK_SECRET": "",
        "MCP_HTTP_HOST": "0.0.0.0",
        "MCP_HTTP_PORT": 8200,
        "MCP_HTTP_PATH": "/mcp",
        "EMBEDDING_MODEL": "text-embedding-3-small",
        "REVIEW_THRESHOLDS_FROM_CONFIG": True,
        "REVIEW_THRESHOLDS_DAYS": {"runbook": 90, "default": 270},
        "MEMORY_FOLDER": "99 - Memories",
        "MEMORY_EXPORT_TYPES": ["decision"],
        "PROMOTION_FROM_CONFIG": True,
        "PROMOTION_OUTPUT_DIR": root / "out" / "promotions",
        "PROMOTION_TARGET_FOLDERS": {
            "decision": "Architecture/Decisions",
            "discovery": "Architecture/Discoveries",
        },
        "PROMOTION_DOC_STATUS": "draft",
        "PROMOTION_DEFAULT_OWNER": "ops",
        "PROMOTION_BRANCH_PREFIX": "feature/memory-promotion",
        "VAULT_PATH": tmp_path / "vault",
        "CHROMA_PERSIST_DIR": tmp_path / "chroma",
        "CHROMA_COLLECTION": "team",
    }
    assert set(expected) == set(config._LEGACY)
    assert {name: getattr(config, name) for name in expected} == expected


def test_reloading_the_facade_rereads_the_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CONFLUENCE_URL", "https://first.example.test")
    importlib.reload(config)
    assert config.CONFLUENCE_URL == "https://first.example.test"

    monkeypatch.setenv("CONFLUENCE_URL", "https://second.example.test")
    importlib.reload(config)
    assert config.CONFLUENCE_URL == "https://second.example.test"


def test_an_assignment_shadows_the_lazy_name_until_reset() -> None:
    config.MEMORY_FOLDER = "patched"
    assert config.MEMORY_FOLDER == "patched"

    importlib.reload(config)
    assert config.MEMORY_FOLDER != "patched"


def test_an_unknown_name_is_still_an_attribute_error() -> None:
    with pytest.raises(AttributeError):
        config.NOT_A_SETTING  # noqa: B018


# --- the core no longer reads module-level configuration -------------------------


@pytest.mark.parametrize("module", ["indexer", "graph", "usage", "gitsync", "server"])
def test_core_modules_read_no_config_globals(module: str) -> None:
    tree = ast.parse((REPO_ROOT / "src" / "knowledge" / f"{module}.py").read_text())
    reads = sorted(
        f"config.{node.attr} (line {node.lineno})"
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "config"
    )
    assert reads == [], f"knowledge.{module} still reads the legacy facade: {reads}"


def test_disabled_git_sync_runs_no_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(gitsync, "_run_git", lambda args, *, timeout=120: calls.append(args) or (0, "true", ""))

    assert gitsync.pull_vault(tmp_path, enabled=False) is False
    assert calls == []


# --- the transport is configured at startup, with FastMCP's own loopback rule ----


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "0.0.0.0"])
def test_transport_security_parity(host: str) -> None:
    from mcp.server.fastmcp import FastMCP

    from knowledge import server

    probe = FastMCP("probe", host="0.0.0.0")
    server._configure_transport(probe, settings.HttpSettings(host=host, port=8200, path="/x"))
    reference = FastMCP("reference", host=host)

    assert (probe.settings.host, probe.settings.port, probe.settings.streamable_http_path) == (host, 8200, "/x")
    assert probe.settings.transport_security == reference.settings.transport_security


# --- output switches ------------------------------------------------------------


def test_output_defaults_to_text_only_and_compact(tmp_path: Path) -> None:
    """The text blocks are the canonical answer; ``structuredContent`` is opt-in."""
    s = _load(tmp_path, {})

    assert s.output == settings.OutputSettings(structured=False, text_indent=None)


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"KNOWLEDGE_STRUCTURED_OUTPUT": "0"}, settings.OutputSettings(structured=False, text_indent=None)),
        ({"KNOWLEDGE_STRUCTURED_OUTPUT": "1"}, settings.OutputSettings(structured=True, text_indent=None)),
        ({"KNOWLEDGE_STRUCTURED_OUTPUT": ""}, settings.OutputSettings(structured=False, text_indent=None)),
        ({"KNOWLEDGE_TEXT_INDENT": "2"}, settings.OutputSettings(structured=False, text_indent=2)),
        ({"KNOWLEDGE_TEXT_INDENT": ""}, settings.OutputSettings(structured=False, text_indent=None)),
    ],
)
def test_output_switches_are_read_from_the_environment(
    tmp_path: Path, env: dict[str, str], expected: settings.OutputSettings
) -> None:
    assert _load(tmp_path, env).output == expected


@pytest.mark.parametrize(("value", "profile"), [("", "all"), ("all", "all"), ("legacy", "legacy")])
def test_the_profile_is_read_from_the_environment(tmp_path: Path, value: str, profile: str) -> None:
    assert _load(tmp_path, {"KNOWLEDGE_PROFILE": value}).output.profile == profile


@pytest.mark.parametrize("value", ["core", "ext", "LEGACY", "everything"])
def test_a_profile_that_cannot_be_served_is_a_coded_error(tmp_path: Path, value: str) -> None:
    with pytest.raises(KnowledgeConfigError) as exc_info:
        _load(tmp_path, {"KNOWLEDGE_PROFILE": value})

    assert exc_info.value.code == errors.E_PROFILE_UNKNOWN
    assert value not in exc_info.value.message


@pytest.mark.parametrize(
    "env",
    [
        {"KNOWLEDGE_STRUCTURED_OUTPUT": "yes"},
        {"KNOWLEDGE_STRUCTURED_OUTPUT": "2"},
        {"KNOWLEDGE_TEXT_INDENT": "0"},
        {"KNOWLEDGE_TEXT_INDENT": "9"},
        {"KNOWLEDGE_TEXT_INDENT": "two"},
        {"KNOWLEDGE_TEXT_INDENT": "-1"},
    ],
)
def test_an_invalid_output_switch_is_a_coded_error(tmp_path: Path, env: dict[str, str]) -> None:
    with pytest.raises(KnowledgeConfigError) as exc_info:
        _load(tmp_path, env)

    assert _code(exc_info) == errors.E_OUTPUT_SETTING_INVALID
    assert next(iter(env.values())) not in exc_info.value.message
