"""The command line: argparse wiring, exit codes, the read-only vault linter and ``doctor``."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from knowledge import cli, contract, server, settings
from knowledge.errors import KnowledgeConfigError

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_VAULT = REPO_ROOT / "tests" / "fixtures" / "vault"


def _tree_state(root: Path) -> list[tuple[str, int, int]]:
    return sorted(
        (str(p.relative_to(root)), p.stat().st_mtime_ns, p.stat().st_size)
        for p in root.rglob("*")
        if p.is_file()
    )


def test_vault_lint_on_the_fixture_vault_is_stable(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["vault-lint", str(FIXTURE_VAULT), "--rules", "1.2"])
    out = capsys.readouterr().out
    report = json.loads(out)
    assert rc == 0
    assert report["files"] == 14
    assert report["ok"] is True
    assert report["errors"] == []
    assert report["vault"] == "vault"
    assert list(report) == sorted(report)  # sort_keys

    cli.main(["vault-lint", str(FIXTURE_VAULT), "--rules", "1.2", "--format", "json"])
    assert capsys.readouterr().out == out


def test_vault_lint_reports_an_unresolved_wikilink(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    vault = tmp_path / "copy"
    shutil.copytree(FIXTURE_VAULT, vault)
    (vault / "Broken.md").write_text("---\ntype: note\n---\nSee [[No Existe]].\n", encoding="utf-8")

    rc = cli.main(["vault-lint", str(vault), "--rules", "1.2"])
    report = json.loads(capsys.readouterr().out)

    assert rc == 1
    assert report["ok"] is False
    assert report["files"] == 15
    [err] = report["errors"]
    assert err["code"] == "E_WIKILINK_UNRESOLVED"
    assert err["file"] == "Broken.md"
    assert err["target"] == "No Existe"
    assert str(tmp_path) not in json.dumps(report)  # paths are relative


def test_vault_lint_reports_unparseable_frontmatter(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    vault = tmp_path / "v"
    vault.mkdir()
    (vault / "Bad.md").write_text("---\nkey: [unclosed\n---\nbody\n", encoding="utf-8")

    rc = cli.main(["vault-lint", str(vault), "--rules", "1.2"])
    report = json.loads(capsys.readouterr().out)

    assert rc == 1
    assert [e["code"] for e in report["errors"]] == ["E_FRONTMATTER_UNPARSEABLE"]
    assert report["errors"][0]["file"] == "Bad.md"


def test_vault_lint_warns_on_an_ambiguous_stem(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    vault = tmp_path / "v"
    (vault / "a").mkdir(parents=True)
    (vault / "b").mkdir()
    (vault / "a" / "Same.md").write_text("x\n", encoding="utf-8")
    (vault / "b" / "Same.md").write_text("y\n", encoding="utf-8")

    rc = cli.main(["vault-lint", str(vault), "--rules", "1.2"])
    report = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert report["warnings"] == [
        {"code": "W_WIKILINK_AMBIGUOUS_STEM", "stem": "same", "files": ["a/Same.md", "b/Same.md"]}
    ]


def test_vault_lint_skips_hidden_directories(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    vault = tmp_path / "v"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / ".obsidian" / "Hidden.md").write_text("[[Nowhere]]\n", encoding="utf-8")
    (vault / "Note.md").write_text("plain\n", encoding="utf-8")

    rc = cli.main(["vault-lint", str(vault), "--rules", "1.2"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["files"] == 1


def test_vault_lint_missing_path_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["vault-lint", str(tmp_path / "nope")])
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    assert captured.err.startswith("E_LINT_PATH_NOT_DIR: ")
    assert "Traceback" not in captured.err


def test_vault_lint_is_read_only(tmp_path: Path) -> None:
    vault = tmp_path / "copy"
    shutil.copytree(FIXTURE_VAULT, vault)
    (vault / "Broken.md").write_text("[[Missing]]\n", encoding="utf-8")
    before = _tree_state(vault)
    cli.main(["vault-lint", str(vault)])
    cli.main(["vault-lint", str(vault), "--rules", "1.2"])
    assert _tree_state(vault) == before



@pytest.fixture
def captured_serve(monkeypatch: pytest.MonkeyPatch) -> list[settings.Settings]:
    seen: list[settings.Settings] = []
    monkeypatch.setattr(server, "serve", lambda s: seen.append(s))
    monkeypatch.setattr(cli, "load_dotenv", lambda *_a, **_k: False)
    monkeypatch.setenv("MCP_TRANSPORT", "")
    monkeypatch.setenv("KNOWLEDGE_VAULTS", "")
    return seen


def test_serve_passes_the_flags_into_settings(
    captured_serve: list[settings.Settings], tmp_path: Path
) -> None:
    manifest = tmp_path / "vaults.yaml"
    manifest.write_text("version: 1\nvaults:\n  - slug: alpha\n    path: " + str(FIXTURE_VAULT) + "\n    default: true\n")

    rc = cli.main(["serve", "--transport", "http", "--vaults", str(manifest)])

    assert rc == 0
    [s] = captured_serve
    assert s.http.transport == "http"
    assert s.vaults_file == manifest


def test_no_subcommand_means_serve(captured_serve: list[settings.Settings]) -> None:
    assert cli.main([]) == 0
    assert len(captured_serve) == 1


def test_a_configuration_error_is_one_line_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(_s: settings.Settings) -> None:
        raise KnowledgeConfigError("E_VAULT_PATH_MISSING", "set VAULT_PATH")

    monkeypatch.setattr(server, "serve", boom)
    monkeypatch.setattr(cli, "load_dotenv", lambda *_a, **_k: False)
    monkeypatch.setenv("MCP_TRANSPORT", "")
    monkeypatch.setenv("KNOWLEDGE_VAULTS", "")

    rc = cli.main(["serve"])
    err = capsys.readouterr().err

    assert rc == 2
    assert err == "E_VAULT_PATH_MISSING: set VAULT_PATH\n"


def test_serve_reads_the_project_dotenv_like_server_main(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    keys = (
        "OPENAI_API_KEY",
        "VAULT_PATH",
        "KNOWLEDGE_VAULTS",
        "KNOWLEDGE_CONFIG_JSON",
        "KNOWLEDGE_EMBEDDINGS_PROVIDER",
        "KNOWLEDGE_HOME",
        "MCP_TRANSPORT",
    )
    for key in keys:
        # setenv then delenv records the key as absent, so teardown also drops
        # whatever the .env loader puts into os.environ.
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    (tmp_path / ".env").write_text(
        f"OPENAI_API_KEY=placeholder-not-a-credential\nVAULT_PATH={FIXTURE_VAULT}\n", encoding="utf-8"
    )
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    seen: list[list[str]] = []
    monkeypatch.setattr(server, "serve", lambda s: seen.append(s.validate()))

    rc = cli.main([])

    assert rc == 0
    assert len(seen) == 1


def test_eval_without_a_checkout_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    rc = cli.main(["eval", "--stdio"])
    err = capsys.readouterr().err
    assert rc == 2
    assert err.startswith("E_EVAL_UNAVAILABLE: ")
    assert "source checkout" in err


def test_eval_help_runs_the_script_parser() -> None:
    with pytest.raises(SystemExit) as info:
        cli.main(["eval", "--help"])
    assert info.value.code == 0


def test_the_entry_point_targets_the_cli_and_the_server_alias_survives() -> None:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["scripts"]["cloudfrog-knowledge"] == "knowledge.cli:main"
    assert callable(server.main)


def test_python_dash_m_knowledge_reports_a_bad_path_without_a_traceback() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "knowledge", "vault-lint", "/nonexistent-vault-path"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 2
    assert "Traceback" not in result.stderr


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

_SENTINEL_KEY = "doctor-sentinel-value-never-printed"
_SENTINEL_MEMORY_TOKEN = "memory-token-sentinel-never-printed"
_SENTINEL_MEMORY_URL = "http://127.0.0.1:9/memory-url-sentinel-never-printed"


@pytest.fixture
def doctor_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A hermetic environment: no .env, no user registry, a fresh data home."""
    monkeypatch.setattr(cli, "load_dotenv", lambda *_a, **_k: False)
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    for key in ("KNOWLEDGE_VAULTS", "VAULT_PATH", "OPENAI_API_KEY", "CHROMA_PERSIST_DIR"):
        monkeypatch.setenv(key, "")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("KNOWLEDGE_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("KNOWLEDGE_EMBEDDINGS_PROVIDER", "bag-of-words")
    return tmp_path


def _registry(tmp_path: Path, *, missing: int = 0) -> Path:
    """alpha (default) and beta from the fixtures, plus ``missing`` vaults whose path does not exist."""
    fixtures = REPO_ROOT / "tests" / "fixtures" / "vaults"
    entries = [("alpha", fixtures / "alpha"), ("beta", fixtures / "beta")]
    entries += [(f"gone-{n:02d}", tmp_path / f"gone-{n:02d}") for n in range(missing)]
    lines = ["version: 1", "vaults:"]
    for slug, path in entries:
        lines += [f"  - slug: {slug}", f"    path: {path}"]
        if slug == "alpha":
            lines.append("    default: true")
    manifest = tmp_path / "vaults.yaml"
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return manifest


def test_doctor_reports_a_healthy_registry(doctor_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    manifest = _registry(doctor_env)

    rc = cli.main(["doctor", "--vaults", str(manifest)])
    report = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert report["ok"] is True
    assert report["problems"] == []
    assert report["contract"] == contract.CONTRACT_VERSION
    assert report["package"]["name"] == "cloudfrog-knowledge"
    assert report["registry"] == {
        "default": "alpha",
        "file": str(manifest),
        "source": "KNOWLEDGE_VAULTS",
        "vaults": 2,
    }
    assert report["knowledge_home"] == {
        "exists": False,
        "path": str(doctor_env / "data"),
        "writable": True,
    }
    assert [v["slug"] for v in report["vaults"]] == ["alpha", "beta"]
    assert all(v["provider"] == "bag-of-words" and not v["key_required"] for v in report["vaults"])
    assert all(v["index"] is False for v in report["vaults"]), "doctor writes nothing, so no index exists yet"
    assert report["vector_backend"] in ("sqlite-vec", "python")
    assert list(report) == sorted(report)


def test_doctor_reads_the_user_registry_when_none_is_named(
    doctor_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    user_dir = doctor_env / "xdg" / "knowledge"
    user_dir.mkdir(parents=True)
    _registry(doctor_env).rename(user_dir / "vaults.yaml")

    assert cli.main(["doctor"]) == 0
    registry = json.loads(capsys.readouterr().out)["registry"]
    assert registry["source"] == "user_vaults_yaml"
    assert registry["file"] == str(user_dir / "vaults.yaml")


def test_doctor_names_the_vault_path_rule(
    doctor_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("VAULT_PATH", str(FIXTURE_VAULT))

    assert cli.main(["doctor"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["registry"]["source"] == "VAULT_PATH"
    assert report["registry"]["file"] is None
    assert report["vaults"][0]["path"] == str(FIXTURE_VAULT)


def test_doctor_without_any_vault_is_a_problem(doctor_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["doctor"])
    report = json.loads(capsys.readouterr().out)

    assert rc == 1
    assert report["registry"]["source"] == "none"
    assert [p["code"] for p in report["problems"]] == ["E_VAULT_PATH_MISSING"]


def test_doctor_flags_a_missing_key_without_printing_any_value(
    doctor_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("KNOWLEDGE_EMBEDDINGS_PROVIDER", "openai")
    manifest = _registry(doctor_env)

    rc = cli.main(["doctor", "--vaults", str(manifest)])
    report = json.loads(capsys.readouterr().out)

    assert rc == 1
    assert [(p["code"], p["vault"]) for p in report["problems"]] == [
        ("E_EMBEDDINGS_KEY_MISSING", "alpha"),
        ("E_EMBEDDINGS_KEY_MISSING", "beta"),
    ]
    assert all(v["key_required"] and not v["key_set"] for v in report["vaults"])

    monkeypatch.setenv("OPENAI_API_KEY", _SENTINEL_KEY)
    rc = cli.main(["doctor", "--vaults", str(manifest)])
    out = capsys.readouterr().out
    assert rc == 0
    assert all(v["key_set"] for v in json.loads(out)["vaults"])
    assert _SENTINEL_KEY not in out
    assert cli.main(["doctor", "--brief", "--vaults", str(manifest)]) == 0
    assert _SENTINEL_KEY not in capsys.readouterr().out


def test_doctor_flags_an_unknown_provider(
    doctor_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # vaults.yaml refuses an unknown provider at load time; the environment
    # override is only checked when the server validates, so doctor reports it.
    monkeypatch.setenv("KNOWLEDGE_EMBEDDINGS_PROVIDER", "carrier-pigeon")

    assert cli.main(["doctor", "--vaults", str(_registry(doctor_env))]) == 1
    problems = json.loads(capsys.readouterr().out)["problems"]
    assert [(p["code"], p["vault"]) for p in problems] == [
        ("E_EMBEDDINGS_PROVIDER_UNKNOWN", "alpha"),
        ("E_EMBEDDINGS_PROVIDER_UNKNOWN", "beta"),
    ]


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root writes anywhere")
def test_doctor_flags_a_home_it_cannot_write(
    doctor_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    locked = doctor_env / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    monkeypatch.setenv("KNOWLEDGE_HOME", str(locked / "knowledge"))
    try:
        rc = cli.main(["doctor", "--vaults", str(_registry(doctor_env))])
    finally:
        locked.chmod(0o700)
    report = json.loads(capsys.readouterr().out)

    assert rc == 1
    assert report["knowledge_home"]["writable"] is False
    assert [p["code"] for p in report["problems"]] == ["E_KNOWLEDGE_HOME_NOT_WRITABLE"]


def test_doctor_brief_is_one_short_line(
    doctor_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = _registry(doctor_env)

    assert cli.main(["doctor", "--brief", "--vaults", str(manifest)]) == 0
    ok = capsys.readouterr().out
    assert ok == f"knowledge doctor: ok, contract {contract.CONTRACT_VERSION}, 2 vault(s), default alpha\n"

    monkeypatch.setenv("KNOWLEDGE_EMBEDDINGS_PROVIDER", "openai")
    crowded = _registry(doctor_env, missing=12)
    assert cli.main(["doctor", "--brief", "--vaults", str(crowded)]) == 1
    bad = capsys.readouterr().out
    assert bad.count("\n") == 1
    assert len(bad.rstrip("\n").encode("utf-8")) <= cli.BRIEF_MAX_BYTES
    assert bad.startswith("knowledge doctor: E_EMBEDDINGS_KEY_MISSING (alpha, beta, gone-00,")
    assert bad.rstrip("\n").endswith("...")


def test_doctor_brief_groups_problems_by_code(
    doctor_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("KNOWLEDGE_EMBEDDINGS_PROVIDER", "openai")

    assert cli.main(["doctor", "--brief", "--vaults", str(_registry(doctor_env, missing=1))]) == 1
    assert capsys.readouterr().out == (
        "knowledge doctor: E_EMBEDDINGS_KEY_MISSING (alpha, beta, gone-00); "
        "E_VAULT_PATH_NOT_DIR (gone-00); run: cloudfrog-knowledge doctor\n"
    )


def test_doctor_brief_cuts_on_a_character_boundary() -> None:
    line = cli._brief("ñ" * 300)
    assert len(line.encode("utf-8")) <= cli.BRIEF_MAX_BYTES
    assert line.endswith("...")
    assert set(line[:-3]) == {"ñ"}


def test_doctor_on_an_unloadable_registry_exits_2(
    doctor_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = doctor_env / "nope.yaml"

    rc = cli.main(["doctor", "--vaults", str(missing)])
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    assert captured.err.startswith("E_VAULTS_FILE_MISSING: ")

    rc = cli.main(["doctor", "--brief", "--vaults", str(missing)])
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out.startswith("knowledge doctor: E_VAULTS_FILE_MISSING: ")
    assert captured.out.count("\n") == 1
    assert "Traceback" not in captured.out + captured.err


@pytest.mark.parametrize("brief", [True, False], ids=["brief", "full"])
def test_doctor_never_prints_the_memory_url_or_token(
    doctor_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], brief: bool
) -> None:
    # The same two sentinels go through both outcomes: a registry that loads and
    # one that does not, so neither the report nor the load error can echo them.
    monkeypatch.setenv("KNOWLEDGE_MEMORY_URL", _SENTINEL_MEMORY_URL)
    monkeypatch.setenv("MEMORY_HTTP_TOKEN", _SENTINEL_MEMORY_TOKEN)
    flags = ["doctor", "--brief"] if brief else ["doctor"]

    assert cli.main([*flags, "--vaults", str(doctor_env / "nope.yaml")]) == 2
    failed = capsys.readouterr()
    reported = failed.out.removeprefix("knowledge doctor: ") if brief else failed.err
    assert reported.startswith("E_VAULTS_FILE_MISSING: ")

    assert cli.main([*flags, "--vaults", str(_registry(doctor_env))]) == 0
    healthy = capsys.readouterr()

    for stream in (failed.out, failed.err, healthy.out, healthy.err):
        assert _SENTINEL_MEMORY_TOKEN not in stream
        assert _SENTINEL_MEMORY_URL not in stream
        assert "memory-url-sentinel" not in stream


def test_doctor_writes_nothing(doctor_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    manifest = _registry(doctor_env)
    before = _tree_state(doctor_env)

    cli.main(["doctor", "--vaults", str(manifest)])
    cli.main(["doctor", "--brief", "--vaults", str(manifest)])
    capsys.readouterr()

    assert _tree_state(doctor_env) == before
    assert not (doctor_env / "data").exists()
