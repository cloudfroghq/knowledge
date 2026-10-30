"""The write fence: no tool and no command writes inside a vault.

The vault changes only through a pull request a person reviews (D-11). The
one exception is ``90 - Memory/``, where the memory bridge writes. Every test
here copies ``tests/fixtures/vault`` into a temporary directory, hashes the
whole tree, runs the tools or the command line against the copy and hashes
it again: a byte that changed outside ``90 - Memory/`` fails the test.

Confluence is a fake server on ``127.0.0.1`` and an ephemeral port; nothing
leaves the machine. ``sync_confluence(dry_run=False)`` answers
``E_WRITE_FENCED`` before a credential is read, and ``confluence export``
writes only into a directory outside every served vault.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import shutil
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from knowledge import cli, config, errors, settings
from knowledge.errors import KnowledgeConfigError, KnowledgeToolError

FIXTURE_VAULT = Path(__file__).parent / "fixtures" / "vault"

#: The one folder of a vault the server may write into.
MEMORY_FOLDER = "90 - Memory"

RUNBOOK = "Runbooks/Runbook - Middleware Outage.md"
TRACKED = "Conventions/Imported Page.md"
PAGE_ID = "424242"
REMOTE_MODIFIED = "2026-09-05T10:00:00Z"
EMAIL = "fence-bot@example.test"
TOKEN = "fence-placeholder-not-a-credential"

#: One valid call per registered tool. ``sync_confluence`` is called twice
#: below, once asked to write; a tool registered without an entry here fails
#: ``test_every_registered_tool_is_called_by_the_fence``.
CALLS: dict[str, dict[str, Any]] = {
    "get_document": {"file_path": RUNBOOK},
    "search": {"query": "middleware outage"},
    "search_by_metadata": {"type": "service"},
    "search_deep": {"query": "middleware outage"},
    "find_path": {"from_path": RUNBOOK, "to_path": RUNBOOK},
    "get_related": {"file_path": RUNBOOK},
    "suggest_links": {},
    "get_stale_docs": {},
    "get_flow": {"provider": "Telvia"},
    "compare_flows": {"provider1": "Telvia", "provider2": "Comet Argentina"},
    "get_impact": {"file_path": RUNBOOK},
    "sync_confluence": {"dry_run": True},
    "search_code": {"query": "anything"},
    "context_pack": {"query": "middleware outage", "include_memories": True},
    "list_vaults": {},
    # Mutating: they close the run, as in the golden session.
    "reindex": {"force": True},
}


# --- fixtures -----------------------------------------------------------------


class _Confluence(BaseHTTPRequestHandler):
    """Answers ``GET /wiki/api/v2/pages/<id>`` from :attr:`pages`; refuses any other method."""

    pages: dict[str, dict[str, Any]] = {}
    seen: list[tuple[str, str, str]] = []

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - signature of the base class
        pass

    def _record(self) -> None:
        auth = self.headers.get("Authorization", "")
        user = ""
        if auth.startswith("Basic "):
            user = base64.b64decode(auth.removeprefix("Basic ")).decode().split(":", 1)[0]
        type(self).seen.append((self.command, self.path, user))

    def do_GET(self) -> None:
        self._record()
        page_id = self.path.split("?", 1)[0].rsplit("/", 1)[-1]
        page = type(self).pages.get(page_id)
        body = json.dumps(page if page is not None else {"error": "not found"}).encode()
        self.send_response(200 if page is not None else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _refuse(self) -> None:
        self._record()
        self.send_response(405)
        self.end_headers()

    do_POST = do_PUT = do_PATCH = do_DELETE = _refuse


@pytest.fixture()
def confluence(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A fake Confluence on loopback holding the tracked page, newer than the vault's copy."""
    _Confluence.pages = {
        PAGE_ID: {
            "id": PAGE_ID,
            "title": "Imported Page",
            "body": {"storage": {"value": "<h2>Rules</h2><p>Content from <strong>Confluence</strong>.</p>"}},
            "version": {"number": 7, "createdAt": REMOTE_MODIFIED},
        }
    }
    _Confluence.seen = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Confluence)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    # _api_get pauses between requests to respect Confluence's rate limit.
    monkeypatch.setattr("time.sleep", lambda _s: None)
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _track_page(vault: Path) -> Path:
    doc = vault / TRACKED
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(
        "---\ntitle: Imported Page\ntype: convention\nconfluence_id: '424242'\nlast_updated: 2026-01-01\n---\n\n"
        "# Imported Page\n\nBody as it stands in the vault.\n",
        encoding="utf-8",
    )
    return doc


def _tree(root: Path) -> dict[str, str]:
    """Every path under ``root`` (directories too, so an empty one counts) mapped to its sha256."""
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        out[rel] = "dir" if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def _changed_outside_memory(before: dict[str, str], after: dict[str, str]) -> list[str]:
    changed = {rel for rel in before.keys() | after.keys() if before.get(rel) != after.get(rel)}
    return sorted(rel for rel in changed if rel != MEMORY_FOLDER and not rel.startswith(f"{MEMORY_FOLDER}/"))


@pytest.fixture()
def configured(confluence: str, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(config, "CONFLUENCE_URL", confluence)
    monkeypatch.setattr(config, "CONFLUENCE_EMAIL", EMAIL)
    monkeypatch.setattr(config, "CONFLUENCE_TOKEN", TOKEN)
    return confluence


# --- the tools ----------------------------------------------------------------


def test_every_registered_tool_is_called_by_the_fence() -> None:
    """Every tool a server can register without ``jira``: the ``legacy`` catalog, extension tools and all."""
    from mcp.server.fastmcp import FastMCP

    from knowledge import server

    probe = FastMCP("fence-probe")
    server.register_tools(probe, structured=True, profile="legacy")
    registered = {tool.name for tool in asyncio.run(probe.list_tools())}

    assert set(CALLS) == registered


def test_no_tool_writes_inside_the_vault(server_on_temp_vault: Any, configured: str) -> None:
    """Every tool, the fenced write and a forced reindex leave the vault byte for byte as they found it."""
    server = server_on_temp_vault
    vault = Path(config.VAULT_PATH)
    _track_page(vault)
    before = _tree(vault)

    results: dict[str, Any] = {}
    for name, arguments in CALLS.items():
        if name == "reindex":
            with pytest.raises(KnowledgeToolError) as fenced:
                asyncio.run(server.sync_confluence(dry_run=False))
        results[name] = asyncio.run(getattr(server, name)(**arguments))

    assert fenced.value.code == errors.E_WRITE_FENCED
    report = results["sync_confluence"]
    assert [row["file"] for row in report["needs_update"]] == [TRACKED], report
    assert report["updated"] == []
    assert ("GET", f"/wiki/api/v2/pages/{PAGE_ID}?body-format=storage", EMAIL) in _Confluence.seen
    assert _changed_outside_memory(before, _tree(vault)) == []


def test_a_reranked_search_writes_nothing_inside_the_vault(tmp_path: Path) -> None:
    """``search`` with a judge (default on, explicit, with pointers, judge down) leaves the vault untouched.

    The judge is the loopback System One double; its index and usage file
    live outside the vault.
    """
    from knowledge import server

    from systemone_fakes import API_KEY, systemone_server

    vault = tmp_path / "vault"
    shutil.copytree(FIXTURE_VAULT, vault)
    with systemone_server() as (origin, fake):
        (vault / "vault.yaml").write_text(
            "rerank:\n  provider: kev\n  model: kev-latest\n  key_env: KEV_API_KEY\n  default: on\n"
            f"  base_url: {origin}\n",
            encoding="utf-8",
        )
        before = _tree(vault)
        loaded = settings.load_settings(
            {
                "VAULT_PATH": str(vault),
                "KNOWLEDGE_INDEX_DIR": str(tmp_path / "index"),
                "KNOWLEDGE_EMBEDDINGS_PROVIDER": "bag-of-words",
                "KEV_API_KEY": API_KEY,
            },
            project_root=tmp_path,
        )
        settings.install(loaded)
        server.set_registry(server._registry_for(loaded))
        try:
            for arguments in (
                {"query": "middleware outage"},
                {"query": "middleware outage", "rerank": "on", "fields": ["file_path", "chunk_id"], "snippet": True},
                {"query": "otp session [529]", "rerank": "on"},
            ):
                asyncio.run(server.search(**arguments))
            server.registry().default.usage.flush()
        finally:
            server.set_registry(None)
        assert fake.bodies, "the judge was asked"

    assert _changed_outside_memory(before, _tree(vault)) == []


@pytest.mark.parametrize("dry_run", [False, 0, None])
def test_the_fence_answers_before_any_credential_is_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, dry_run: Any
) -> None:
    from knowledge import confluence_sync

    def _explode(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("credentials were read before the fence answered")

    monkeypatch.setattr(confluence_sync, "credentials", _explode)

    with pytest.raises(KnowledgeToolError) as excinfo:
        confluence_sync.sync(dry_run=dry_run, vault_path=tmp_path)

    assert excinfo.value.code == errors.E_WRITE_FENCED
    assert "confluence export" in excinfo.value.message


def test_a_dry_run_reads_the_credentials_the_vault_declares(
    confluence: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``integrations.confluence`` names the site and the variables; the defaults stay unused."""
    from knowledge import confluence_sync

    vault = tmp_path / "vault"
    shutil.copytree(FIXTURE_VAULT, vault)
    _track_page(vault)
    monkeypatch.setenv("FENCE_CONFLUENCE_EMAIL", "declared-bot@example.test")
    monkeypatch.setenv("FENCE_CONFLUENCE_TOKEN", TOKEN)
    integrations = {
        "confluence": {"site": confluence, "secrets": {"email_env": "FENCE_CONFLUENCE_EMAIL", "token_env": "FENCE_CONFLUENCE_TOKEN"}}
    }
    before = _tree(vault)

    report = confluence_sync.sync(dry_run=True, vault_path=vault, integrations=integrations)

    assert [row["file"] for row in report["needs_update"]] == [TRACKED]
    assert {user for _m, _p, user in _Confluence.seen} == {"declared-bot@example.test"}
    assert _tree(vault) == before


def test_missing_declared_credentials_are_named_never_valued(tmp_path: Path) -> None:
    from knowledge import confluence_sync

    integrations = {"confluence": {"site": "https://wiki.atlassian.net", "secrets": {"token_env": "FENCE_CONFLUENCE_TOKEN"}}}

    with pytest.raises(errors.KnowledgeToolError) as excinfo:
        confluence_sync.sync(dry_run=True, vault_path=tmp_path, integrations=integrations)

    assert excinfo.value.code == errors.E_SECRET_MISSING
    assert excinfo.value.message == (
        "sync_confluence needs CONFLUENCE_EMAIL, FENCE_CONFLUENCE_TOKEN, which the vault's "
        "integrations.confluence names; export them"
    )


def test_credentials_fall_back_to_the_default_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    from knowledge import confluence_sync

    monkeypatch.setattr(config, "CONFLUENCE_URL", "https://wiki.example.test")
    monkeypatch.setattr(config, "CONFLUENCE_EMAIL", EMAIL)
    monkeypatch.setattr(config, "CONFLUENCE_TOKEN", TOKEN)

    creds = confluence_sync.credentials({})

    assert (creds.url, creds.email, creds.token) == ("https://wiki.example.test", EMAIL, TOKEN)
    assert creds.is_default and creds.configured
    assert TOKEN not in repr(creds)


# --- confluence export ---------------------------------------------------------


@pytest.fixture()
def cli_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured: str) -> Path:
    """A copy of the fixture vault with one tracked page, served by ``VAULT_PATH`` to the command line."""
    vault = tmp_path / "vault"
    shutil.copytree(FIXTURE_VAULT, vault)
    _track_page(vault)
    monkeypatch.setenv("VAULT_PATH", str(vault))
    monkeypatch.setenv("KNOWLEDGE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CONFLUENCE_URL", configured)
    monkeypatch.setenv("CONFLUENCE_EMAIL", EMAIL)
    monkeypatch.setenv("CONFLUENCE_TOKEN", TOKEN)
    # No project .env: the command reads only what this test sets.
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    settings.reset_current()
    return vault


def test_export_writes_outside_the_vault_only(
    cli_vault: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "exports"
    before = _tree(cli_vault)

    rc = cli.main(["confluence", "export", "--out", str(out)])

    report = json.loads(capsys.readouterr().out)
    assert rc == 0, report
    assert report["written"] == [TRACKED]
    assert report["unchanged"] == []
    assert report["errors"] == []
    assert Path(report["out_dir"]) == out
    exported = (out / TRACKED).read_text(encoding="utf-8")
    assert "confluence_id: '424242'" in exported
    assert "last_updated: '2026-09-05'" in exported
    assert "## Rules" in exported and "**Confluence**" in exported
    assert _changed_outside_memory(before, _tree(cli_vault)) == []
    assert {method for method, _p, _u in _Confluence.seen} == {"GET"}


def test_export_dry_run_writes_nothing(cli_vault: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "exports"

    rc = cli.main(["confluence", "export", "--out", str(out), "--dry-run"])

    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert report["dry_run"] is True
    assert report["written"] == [TRACKED]
    assert not out.exists()


def test_export_defaults_to_the_data_directory(
    cli_vault: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli.main(["confluence", "export"])

    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    expected = tmp_path / "home" / "vault" / "confluence-export"
    assert Path(report["out_dir"]) == expected
    assert (expected / TRACKED).is_file()


@pytest.mark.parametrize("inside", [".", "Conventions", "Exports/new"])
def test_export_into_the_vault_is_fenced_before_any_request(
    cli_vault: Path, inside: str, capsys: pytest.CaptureFixture[str]
) -> None:
    before = _tree(cli_vault)

    rc = cli.main(["confluence", "export", "--out", str(cli_vault / inside)])

    err = capsys.readouterr().err
    assert rc == 2
    assert err.startswith(f"{errors.E_WRITE_FENCED}: ")
    assert _Confluence.seen == []
    assert _tree(cli_vault) == before


def test_export_through_a_symlink_into_the_vault_is_fenced(
    cli_vault: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "exports"
    out.mkdir()
    (out / "Conventions").symlink_to(cli_vault / "Conventions", target_is_directory=True)
    before = _tree(cli_vault)

    rc = cli.main(["confluence", "export", "--out", str(out)])

    assert rc == 2
    assert capsys.readouterr().err.startswith(f"{errors.E_WRITE_FENCED}: ")
    assert _tree(cli_vault) == before


def test_export_into_another_served_vault_is_fenced(tmp_path: Path, configured: str) -> None:
    from knowledge import confluence_sync

    vault = tmp_path / "vault"
    other = tmp_path / "other"
    shutil.copytree(FIXTURE_VAULT, vault)
    other.mkdir()

    with pytest.raises(KnowledgeToolError) as excinfo:
        confluence_sync.export(other / "drop", vault_path=vault, served_vaults=[vault, other])

    assert excinfo.value.code == errors.E_WRITE_FENCED
    assert not (other / "drop").exists()
    assert _Confluence.seen == []


def test_export_without_credentials_names_the_variables(
    cli_vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(config, "CONFLUENCE_EMAIL", "")
    monkeypatch.setattr(config, "CONFLUENCE_TOKEN", "")

    rc = cli.main(["confluence", "export", "--out", str(tmp_path / "exports")])

    err = capsys.readouterr().err
    assert rc == 2
    assert err.startswith(f"{errors.E_SECRET_MISSING}: confluence export needs CONFLUENCE_EMAIL, CONFLUENCE_TOKEN")
    assert TOKEN not in err
    assert not (tmp_path / "exports").exists()


def test_export_of_an_empty_scope_exits_1(
    cli_vault: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (cli_vault / TRACKED).unlink()

    rc = cli.main(["confluence", "export", "--out", str(tmp_path / "exports")])

    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["total_in_scope"] == 0
    assert "error" in report


def test_export_missing_credentials_is_a_config_error(tmp_path: Path) -> None:
    from knowledge import confluence_sync

    with pytest.raises(KnowledgeConfigError) as excinfo:
        confluence_sync.export(tmp_path / "out", vault_path=FIXTURE_VAULT)

    assert excinfo.value.code == errors.E_SECRET_MISSING


def test_one_page_that_cannot_be_written_is_reported_and_exits_1(
    cli_vault: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A file where the page's folder should go fails that page only: JSON report, exit 1, no traceback."""
    out = tmp_path / "exports"
    out.mkdir()
    (out / "Conventions").write_text("a file where a folder should be\n", encoding="utf-8")

    rc = cli.main(["confluence", "export", "--out", str(out)])

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert rc == 1
    assert report["written"] == []
    assert [(row["confluence_id"], row["file"]) for row in report["errors"]] == [(PAGE_ID, TRACKED)]
    assert report["errors"][0]["error"].startswith(("FileExistsError: ", "NotADirectoryError: "))
    assert "Traceback" not in captured.err


def test_a_filesystem_failure_outside_a_page_is_a_stable_code(
    cli_vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from knowledge import confluence_sync

    def _unreadable(*_a: Any, **_k: Any) -> Any:
        raise PermissionError(13, "Permission denied", str(cli_vault))

    monkeypatch.setattr(confluence_sync, "_scan_vault_confluence_ids", _unreadable)

    rc = cli.main(["confluence", "export", "--out", str(tmp_path / "exports")])

    err = capsys.readouterr().err
    assert rc == 2
    assert err.startswith(f"{errors.E_EXPORT_FAILED}: PermissionError: ")
    assert "Traceback" not in err


# --- what a vault may declare under integrations.confluence ---------------------

#: Server secrets a curated vault must never be able to route to a site it declares.
SERVER_SECRETS = ("MCP_ADMIN_KEY", "MCP_API_KEY", "MCP_WEBHOOK_SECRET", "OPENAI_API_KEY", "MEMORY_HTTP_TOKEN")


def _manifest(vault: Path, body: str) -> None:
    (vault / "vault.yaml").write_text(body, encoding="utf-8")


@pytest.mark.parametrize(
    ("body", "where"),
    [
        *(
            pytest.param(
                f"integrations:\n  confluence:\n    secrets: {{token_env: {name}}}\n",
                "integrations.confluence.secrets.token_env",
                id=f"token-{name}",
            )
            for name in SERVER_SECRETS
        ),
        pytest.param(
            "integrations:\n  confluence:\n    secrets: {email_env: MEMORY_HTTP_TOKEN}\n",
            "integrations.confluence.secrets.email_env",
            id="email-server-secret",
        ),
        pytest.param(
            "integrations:\n  confluence:\n    secrets: {token_env: GLOBEX_JIRA_TOKEN}\n",
            "integrations.confluence.secrets.token_env",
            id="another-integration",
        ),
        pytest.param(
            "integrations:\n  confluence:\n    secrets: {token_env: confluence_token}\n",
            "integrations.confluence.secrets.token_env",
            id="lower-case",
        ),
        pytest.param(
            "integrations:\n  confluence:\n    secrets: {email_env: CONFLUENCE_TOKEN}\n",
            "integrations.confluence.secrets.email_env",
            id="wrong-suffix",
        ),
        pytest.param(
            "integrations:\n  confluence:\n    site: http://evil.example\n",
            "integrations.confluence.site",
            id="plain-http",
        ),
        pytest.param(
            "integrations:\n  confluence:\n    site: ftp://wiki.example.test\n",
            "integrations.confluence.site",
            id="other-scheme",
        ),
        pytest.param(
            "integrations:\n  confluence:\n    site: https://collector.example.test\n",
            "integrations.confluence.site",
            id="https-unlisted-host",
        ),
        pytest.param(
            "integrations:\n  jira:\n    site: https://wiki.atlassian.net@collector.example.test\n",
            "integrations.jira.site",
            id="https-user-info",
        ),
        pytest.param(
            "integrations:\n  confluence:\n    secrets: {token_env: ORG_CONFLUENCE_TOKEN}\n",
            "integrations.confluence.secrets.token_env",
            id="another-vaults-prefix",
        ),
        pytest.param(
            "integrations:\n  jira:\n    secrets: {token_env: OPENAI_API_KEY}\n",
            "integrations.jira.secrets.token_env",
            id="jira-server-secret",
        ),
    ],
)
def test_a_vault_cannot_route_a_server_secret_to_its_site(tmp_path: Path, body: str, where: str) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    _manifest(vault, body)

    with pytest.raises(KnowledgeConfigError) as excinfo:
        settings.load_settings({"VAULT_PATH": str(vault)}, project_root=tmp_path)

    assert excinfo.value.code == errors.E_MANIFEST_INVALID
    assert where in excinfo.value.message


@pytest.mark.parametrize(
    "block",
    [
        "secrets: {email_env: CONFLUENCE_EMAIL, token_env: CONFLUENCE_TOKEN}",
        "secrets: {email_env: ACME_CONFLUENCE_EMAIL, token_env: ACME_CONFLUENCE_TOKEN}",
        "secrets: {token_env: ACME_CONFLUENCE_BOT_TOKEN}",
        "secrets: {token_env: CONFLUENCE_BOT_TOKEN}",
        "site: https://acme.atlassian.net",
        "site: http://127.0.0.1:8090",
        "site: http://localhost:8090",
    ],
)
def test_a_vault_may_name_its_own_confluence_site_and_variables(tmp_path: Path, block: str) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    _manifest(vault, f"slug: acme\nintegrations:\n  confluence:\n    {block}\n")

    loaded = settings.load_settings({"VAULT_PATH": str(vault)}, project_root=tmp_path)

    assert "confluence" in loaded.default_vault().integrations


@pytest.mark.parametrize("name", SERVER_SECRETS)
def test_a_direct_caller_cannot_send_a_server_secret_either(
    confluence: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str
) -> None:
    """Defense in depth: ``sync`` and ``export`` refuse the block before reading any variable."""
    from knowledge import confluence_sync

    monkeypatch.setenv(name, TOKEN)
    monkeypatch.setenv("FENCE_CONFLUENCE_EMAIL", EMAIL)
    vault = tmp_path / "vault"
    shutil.copytree(FIXTURE_VAULT, vault)
    _track_page(vault)
    integrations = {"confluence": {"site": confluence, "secrets": {"email_env": "FENCE_CONFLUENCE_EMAIL", "token_env": name}}}

    for call in (
        lambda: confluence_sync.sync(dry_run=True, vault_path=vault, integrations=integrations),
        lambda: confluence_sync.export(tmp_path / "out", vault_path=vault, integrations=integrations, dry_run=True),
        lambda: confluence_sync.credentials(integrations),
    ):
        with pytest.raises(KnowledgeConfigError) as excinfo:
            call()
        assert excinfo.value.code == errors.E_MANIFEST_INVALID
        assert TOKEN not in str(excinfo.value)

    assert _Confluence.seen == []


def test_export_refuses_a_vault_that_names_a_server_secret(
    cli_vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MCP_ADMIN_KEY", TOKEN)
    _manifest(cli_vault, "integrations:\n  confluence:\n    secrets: {token_env: MCP_ADMIN_KEY}\n")

    rc = cli.main(["confluence", "export", "--out", str(tmp_path / "exports"), "--dry-run"])

    err = capsys.readouterr().err
    assert rc == 2
    assert err.startswith(f"{errors.E_MANIFEST_INVALID}: ")
    assert "integrations.confluence.secrets.token_env" in err
    assert TOKEN not in err
    assert _Confluence.seen == []


# --- vault-lint --fix -------------------------------------------------------------------

LEGACY_LINT = Path(__file__).parent / "fixtures" / "lint" / "legacy"


@pytest.fixture()
def served_legacy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A copy of the legacy lint fixture, served as the vault of this installation."""
    vault = tmp_path / "served"
    shutil.copytree(LEGACY_LINT, vault)
    monkeypatch.setenv("VAULT_PATH", str(vault))
    return vault


@pytest.mark.parametrize("target", [".", "Sub", ".."], ids=["the-vault", "inside", "a-parent"])
def test_fix_never_writes_inside_a_served_vault(
    served_legacy: Path, target: str, capsys: pytest.CaptureFixture[str]
) -> None:
    sub = served_legacy / "Sub"
    sub.mkdir()
    (sub / "Old.md").write_text("---\ntype: note\nupdated: 2026-01-01\n---\n\n# Old\n", encoding="utf-8")
    before = _tree(served_legacy)
    path = served_legacy / target

    rc = cli.main(["vault-lint", str(path), "--fix"])

    captured = capsys.readouterr()
    assert rc == 2 and captured.out == ""
    assert captured.err.startswith(f"{errors.E_WRITE_FENCED}: ")
    assert _tree(served_legacy) == before


def test_fix_with_out_corrects_a_copy_and_leaves_the_vault_alone(
    served_legacy: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = _tree(served_legacy)
    out = tmp_path / "proposal"

    rc = cli.main(["vault-lint", str(served_legacy), "--fix", "--out", str(out)])
    report = json.loads(capsys.readouterr().out)

    assert rc in (0, 1)
    assert len(report["fixed"]) == 7
    assert report["vault"] == out.name
    assert _tree(served_legacy) == before
    assert set(_tree(out)) == set(before), "the copy holds the same files"

    again = cli.main(["vault-lint", str(out), "--fix"])
    assert again in (0, 1)
    assert json.loads(capsys.readouterr().out)["fixed"] == []


@pytest.mark.parametrize("where", ["inside-vault", "inside-path", "not-empty"])
def test_fix_refuses_an_out_it_may_not_fill(
    served_legacy: Path, tmp_path: Path, where: str, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "checkout"
    shutil.copytree(LEGACY_LINT, source)
    out = {
        "inside-vault": served_legacy / "proposal",
        "inside-path": source / "proposal",
        "not-empty": tmp_path / "busy",
    }[where]
    if where == "not-empty":
        out.mkdir()
        (out / "keep.txt").write_text("mine", encoding="utf-8")
    before, source_before = _tree(served_legacy), _tree(source)

    rc = cli.main(["vault-lint", str(source), "--fix", "--out", str(out)])

    err = capsys.readouterr().err
    assert rc == 2
    code = errors.E_WRITE_FENCED if where == "inside-vault" else errors.E_ARGUMENT_CONFLICT
    assert err.startswith(f"{code}: ")
    assert _tree(served_legacy) == before and _tree(source) == source_before
    assert not (served_legacy / "proposal").exists() and not (source / "proposal").exists()


def test_out_needs_fix(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["vault-lint", str(tmp_path), "--out", str(tmp_path / "x")])
    assert raised.value.code == 2
    assert "--out needs --fix" in capsys.readouterr().err


# --- init ---------------------------------------------------------------------------


def test_init_links_the_packages_and_writes_nothing_inside_the_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``init`` writes its overlay in the config home and its links in the checkouts, never in the vault."""
    import subprocess

    vault = tmp_path / "vault"
    shutil.copytree(Path(__file__).parent / "fixtures" / "vaults" / "contexts", vault)
    manifest = tmp_path / "vaults.yaml"
    manifest.write_text(f"version: 1\nvaults:\n  - slug: contexts\n    path: {vault}\n    default: true\n")
    checkout = tmp_path / "work" / "ledger" / "api"
    checkout.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(checkout)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(checkout), "remote", "add", "origin", "git@example.test:team/ledger-api.git"], check=True
    )
    monkeypatch.setattr(cli, "load_dotenv", lambda *_a, **_k: False)
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("KNOWLEDGE_VAULTS", str(manifest))
    monkeypatch.setenv("VAULT_PATH", "")
    before = _tree(vault)

    rc = cli.main(["init", "--yes", "--all", "--json", "--search-dir", str(tmp_path / "work")])

    capsys.readouterr()
    assert rc == 0
    assert (checkout / "CLAUDE.md").is_symlink()
    assert (tmp_path / "xdg" / "knowledge" / "contexts" / "repos.local.json").is_file()
    assert _changed_outside_memory(before, _tree(vault)) == []
    assert _tree(vault) == before
