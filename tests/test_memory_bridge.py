"""The memory-bridge against a fake memory server and a fake memory command.

The fake server is a standard-library ``http.server`` on ``127.0.0.1`` and an
ephemeral port that behaves like the three memory routes the bridge writes to
(``PUT /projects/{slug}``, ``POST /projects/{slug}/tasks``,
``POST /projects/{slug}/runbooks/sync``): it records the method, path, headers
and JSON body of every request, checks the bearer the way memory's
``requireAuth`` does when ``MEMORY_HTTP_TOKEN`` is set, and answers whatever a
test scripts for each route. The fake memory command is a Python script that
prints the arguments it received as JSON. Nothing reaches the real memory port
or binary: ``conftest`` fails any connection to port 6366 and points
``MEMORY_BIN`` at a path that does not exist.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import stat
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from knowledge import cli, errors, extensions, memory_bridge, settings
from knowledge.errors import KnowledgeConfigError, KnowledgeToolError, MemoryWriteFailed
from knowledge.settings import EmbeddingSettings, MemorySettings, VaultSpec

TOKEN = "fake-memory-bearer-not-a-credential"
PROJECT = "globex"
#: Text a refusal body carries that must never reach a message or a log line.
BODY_SECRET = "body-text-the-client-must-not-echo"


@pytest.fixture(autouse=True)
def _bridge_logger_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the bridge's logger live, so "the bearer is not logged" never passes on an empty log."""
    monkeypatch.setattr(logging.getLogger("knowledge.memory_bridge"), "disabled", False)


@dataclass
class FakeMemory:
    """What the fake answers per ``(method, path)`` and what it has been asked."""

    token: str | None = TOKEN
    replies: dict[tuple[str, str], tuple[int, Any]] = field(default_factory=dict)
    delay: float = 0.0
    requests: list[dict[str, Any]] = field(default_factory=list)

    def reply(self, method: str, path: str, status: int, payload: Any) -> None:
        self.replies[(method, path)] = (status, payload)


def _handler(state: FakeMemory) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            del format, args

        def _send(self, status: int, payload: Any, headers: dict[str, str] | None = None) -> None:
            body = json.dumps(payload).encode("utf-8") if not isinstance(payload, bytes) else payload
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            state.requests.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "headers": dict(self.headers),
                    "body": json.loads(raw) if raw else None,
                }
            )
            if state.token is not None and self.headers.get("Authorization") != f"Bearer {state.token}":
                self._send(401, {"error": "invalid token"}, {"WWW-Authenticate": 'Bearer realm="memory"'})
                return
            if state.delay:
                time.sleep(state.delay)
            status, payload = state.replies.get((self.command, self.path), (404, {"error": "no route", "code": "not_found"}))
            if status in (301, 302, 307, 308):
                self._send(status, b"", {"Location": payload})
                return
            self._send(status, payload)

        do_PUT = _handle  # noqa: N815
        do_POST = _handle  # noqa: N815
        do_GET = _handle  # noqa: N815

    return Handler


@pytest.fixture()
def fake_memory() -> Iterator[tuple[FakeMemory, str]]:
    """A fake memory server on 127.0.0.1 and an ephemeral port, and its base URL."""
    state = FakeMemory()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler(state))
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _writer(url: str, **kwargs: Any) -> memory_bridge.MemoryWriter:
    return memory_bridge.MemoryWriter(url, kwargs.pop("token", TOKEN), **kwargs)


def _spec(path: Path | None = None, **overrides: Any) -> VaultSpec:
    base: dict[str, Any] = {
        "slug": "globex",
        "path": path,
        "is_default": True,
        "index_dir": Path("/nonexistent"),
        "memory_folder": "90 - Memory",
        "review_thresholds": {"default": 270},
        "thresholds_from_config": False,
        "embeddings": EmbeddingSettings(provider="bag-of-words"),
        "memory_project": PROJECT,
        "features": frozenset({"memory-bridge"}),
    }
    base.update(overrides)
    return VaultSpec(**base)


def _runbook_vault(root: Path) -> Path:
    """A vault with one runbook under ``Runbooks/`` and the service map memory reads."""
    vault = root / "vault"
    (vault / "Runbooks" / "Sync").mkdir(parents=True)
    (vault / "Runbooks" / "services.json").write_text(
        json.dumps({"services": [{"slug": "nextcloud", "aliases": ["nc"]}]}), encoding="utf-8"
    )
    (vault / "Runbooks" / "Sync" / "RB-001 upload stalls.md").write_text(
        textwrap.dedent(
            """\
            ---
            type: runbook
            id: RB-001
            service: nextcloud
            status: verified
            last_updated: 2026-09-01
            ---
            # Upload stalls

            Restart the sync client.
            """
        ),
        encoding="utf-8",
    )
    return vault


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


# --- headers and bodies ---------------------------------------------------------------


def test_the_card_is_a_put_with_the_bearer_the_client_header_and_only_the_declared_fields(
    fake_memory: tuple[FakeMemory, str],
) -> None:
    state, url = fake_memory
    state.reply("PUT", f"/projects/{PROJECT}", 201, {"card": {"slug": PROJECT}, "created": True})

    result = _writer(url).put_project_card(PROJECT, {"jira_project": "ACME", "knowledge_hub_path": "Hub.md"})

    assert result == {"status": "created"}
    (request,) = state.requests
    assert request["method"] == "PUT"
    assert request["path"] == f"/projects/{PROJECT}"
    assert request["body"] == {"jira_project": "ACME", "knowledge_hub_path": "Hub.md"}
    assert request["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert request["headers"]["X-Memory-Client"] == memory_bridge.CLIENT_ID
    assert request["headers"]["Content-Type"] == "application/json"
    assert request["headers"]["Accept"] == "application/json"


def test_an_existing_card_answers_200_and_reads_as_updated(fake_memory: tuple[FakeMemory, str]) -> None:
    state, url = fake_memory
    state.reply("PUT", f"/projects/{PROJECT}", 200, {"card": {"slug": PROJECT}, "created": False})

    assert _writer(url).put_project_card(PROJECT, {}) == {"status": "updated"}
    assert state.requests[0]["body"] == {}


def test_a_task_is_posted_as_given(fake_memory: tuple[FakeMemory, str]) -> None:
    state, url = fake_memory
    path = f"/projects/{PROJECT}/tasks"
    state.reply("POST", path, 201, {"task": {}, "created": True, "card_created": False})
    task = {"jira_key": "ACME-1", "title": "Upload stalls", "kind": "bugfix", "jira_status": "In Progress"}

    assert _writer(url).upsert_task(PROJECT, task) == {"outcome": "created", "card_created": False}
    assert state.requests[0]["method"] == "POST"
    assert state.requests[0]["path"] == path
    assert state.requests[0]["body"] == task

    state.reply("POST", path, 200, {"task": {}, "created": False, "card_created": False})
    assert _writer(url).upsert_task(PROJECT, task)["outcome"] == "updated"


@pytest.mark.parametrize(
    ("status", "code"),
    [(400, "invalid_enum"), (400, "missing_field"), (409, "task_key_conflict"), (422, "invalid_knowledge_ref")],
)
def test_a_task_memory_refuses_on_its_merits_is_skipped_with_memorys_code(
    fake_memory: tuple[FakeMemory, str], status: int, code: str
) -> None:
    state, url = fake_memory
    state.reply("POST", f"/projects/{PROJECT}/tasks", status, {"error": BODY_SECRET, "code": code})

    assert _writer(url).upsert_task(PROJECT, {"jira_key": "ACME-1"}) == {"outcome": "skipped", "reason": code}


def test_a_server_error_on_a_task_raises_instead_of_skipping(fake_memory: tuple[FakeMemory, str]) -> None:
    state, url = fake_memory
    state.reply("POST", f"/projects/{PROJECT}/tasks", 500, {"error": BODY_SECRET})

    with pytest.raises(MemoryWriteFailed) as excinfo:
        _writer(url).upsert_task(PROJECT, {"jira_key": "ACME-1"})
    assert excinfo.value.code == errors.E_MEMORY_WRITE_FAILED
    assert excinfo.value.status == 500
    assert BODY_SECRET not in str(excinfo.value)


def test_the_runbook_sync_asks_memory_to_scan_the_absolute_vault_dir(
    tmp_path: Path, fake_memory: tuple[FakeMemory, str]
) -> None:
    state, url = fake_memory
    vault = _runbook_vault(tmp_path)
    state.reply(
        "POST",
        f"/projects/{PROJECT}/runbooks/sync",
        200,
        {"scanned": 1, "upserted": 1, "unchanged": 0, "pruned": 0, "skipped": [], "stale_count": 0, "exec_recomputed": 0},
    )

    counts = _writer(url).sync_runbooks(PROJECT, vault)

    assert counts == {"scanned": 1, "upserted": 1, "unchanged": 0, "pruned": 0, "stale_count": 0, "skipped": 0}
    assert state.requests[0]["body"] == {"source": "vault-fs", "vault_dir": str(vault.resolve()), "prune_missing": False}


@pytest.mark.parametrize(
    ("status", "payload", "code", "reason"),
    [
        (404, {"error": BODY_SECRET, "code": "no_card"}, errors.E_MEMORY_PROJECT_MISSING, "no_card"),
        (404, {"error": BODY_SECRET, "code": "vault_not_found"}, errors.E_MEMORY_WRITE_FAILED, "vault_not_found"),
        (400, {"error": BODY_SECRET, "code": "vault_scan_failed"}, errors.E_MEMORY_WRITE_FAILED, "vault_scan_failed"),
        (413, {"error": BODY_SECRET, "code": "payload_too_large"}, errors.E_MEMORY_WRITE_FAILED, "payload_too_large"),
        (413, b"too big", errors.E_MEMORY_WRITE_FAILED, "payload_too_large"),
        (500, b"<html>" + BODY_SECRET.encode() + b"</html>", errors.E_MEMORY_WRITE_FAILED, "http_500"),
    ],
)
def test_runbook_sync_failures_map_to_stable_codes(
    tmp_path: Path, fake_memory: tuple[FakeMemory, str], status: int, payload: Any, code: str, reason: str
) -> None:
    state, url = fake_memory
    state.reply("POST", f"/projects/{PROJECT}/runbooks/sync", status, payload)

    with pytest.raises(MemoryWriteFailed) as excinfo:
        _writer(url).sync_runbooks(PROJECT, _runbook_vault(tmp_path))
    assert (excinfo.value.code, excinfo.value.status, excinfo.value.reason) == (code, status, reason)
    assert BODY_SECRET not in str(excinfo.value)


def test_a_success_status_with_a_body_that_is_not_an_object_fails(fake_memory: tuple[FakeMemory, str]) -> None:
    state, url = fake_memory
    state.reply("PUT", f"/projects/{PROJECT}", 200, [1, 2])

    with pytest.raises(MemoryWriteFailed) as excinfo:
        _writer(url).put_project_card(PROJECT, {})
    assert (excinfo.value.code, excinfo.value.reason) == (errors.E_MEMORY_WRITE_FAILED, "invalid_response")


# --- what never happens -----------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    ["http://192.0.2.10:6366", "https://127.0.0.1:6366", "http://memory.example.test", "file:///tmp/memory"],
)
def test_a_url_off_the_loopback_refuses_before_any_connection(url: str) -> None:
    with pytest.raises(MemoryWriteFailed) as excinfo:
        memory_bridge.MemoryWriter(url, TOKEN)
    assert excinfo.value.code == errors.E_MEMORY_URL_NOT_LOOPBACK


def test_without_a_bearer_nothing_is_written(fake_memory: tuple[FakeMemory, str]) -> None:
    state, url = fake_memory

    with pytest.raises(MemoryWriteFailed) as excinfo:
        memory_bridge.writer_for(MemorySettings(url=url, token=""))
    assert excinfo.value.code == errors.E_MEMORY_TOKEN_MISSING
    assert "KNOWLEDGE_MEMORY_REQUIRE_TOKEN=0" in excinfo.value.message
    assert state.requests == []


def test_require_token_off_writes_without_an_authorization_header(fake_memory: tuple[FakeMemory, str]) -> None:
    state, url = fake_memory
    state.token = None  # memory without MEMORY_HTTP_TOKEN: requireAuth is open
    state.reply("PUT", f"/projects/{PROJECT}", 201, {"created": True})

    writer = memory_bridge.writer_for(MemorySettings(url=url, token="", require_token=False))
    assert writer.put_project_card(PROJECT, {}) == {"status": "created"}
    assert "Authorization" not in state.requests[0]["headers"]


def test_a_wrong_bearer_is_unauthorized_and_never_echoed(
    fake_memory: tuple[FakeMemory, str], caplog: pytest.LogCaptureFixture
) -> None:
    _, url = fake_memory
    caplog.set_level(logging.DEBUG)

    with pytest.raises(MemoryWriteFailed) as excinfo:
        _writer(url, token="another-fake-bearer").put_project_card(PROJECT, {})
    assert (excinfo.value.status, excinfo.value.reason) == (401, "unauthorized")
    assert "another-fake-bearer" not in str(excinfo.value) + caplog.text


def test_a_redirect_is_never_followed(fake_memory: tuple[FakeMemory, str]) -> None:
    state, url = fake_memory
    state.reply("PUT", f"/projects/{PROJECT}", 307, "http://127.0.0.1:9/elsewhere")

    with pytest.raises(MemoryWriteFailed) as excinfo:
        _writer(url).put_project_card(PROJECT, {})
    assert excinfo.value.status == 307
    assert len(state.requests) == 1


def test_proxy_variables_never_route_the_bearer(
    fake_memory: tuple[FakeMemory, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    state, url = fake_memory
    for name in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://192.0.2.1:3128")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    state.reply("PUT", f"/projects/{PROJECT}", 200, {"created": False})

    assert _writer(url).put_project_card(PROJECT, {}) == {"status": "updated"}


def test_a_slow_server_times_out_as_unavailable(fake_memory: tuple[FakeMemory, str]) -> None:
    state, url = fake_memory
    state.delay = 1.0
    state.reply("PUT", f"/projects/{PROJECT}", 200, {"created": False})

    started = time.monotonic()
    with pytest.raises(MemoryWriteFailed) as excinfo:
        _writer(url, timeout=0.2).put_project_card(PROJECT, {})
    assert excinfo.value.code == errors.E_MEMORY_UNAVAILABLE
    assert time.monotonic() - started < 1.0


def test_a_server_that_is_down_is_unavailable() -> None:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with pytest.raises(MemoryWriteFailed) as excinfo:
        _writer(f"http://127.0.0.1:{port}").put_project_card(PROJECT, {})
    assert excinfo.value.code == errors.E_MEMORY_UNAVAILABLE


def test_a_project_that_is_not_a_memory_slug_is_refused_before_any_request(
    fake_memory: tuple[FakeMemory, str],
) -> None:
    state, url = fake_memory

    with pytest.raises(MemoryWriteFailed) as excinfo:
        _writer(url).put_project_card("../admin", {})
    assert excinfo.value.code == errors.E_MEMORY_PROJECT_MISSING
    assert state.requests == []


def test_the_bearer_is_never_logged_nor_in_a_repr(
    tmp_path: Path, fake_memory: tuple[FakeMemory, str], caplog: pytest.LogCaptureFixture
) -> None:
    state, url = fake_memory
    caplog.set_level(logging.DEBUG)
    state.reply("POST", f"/projects/{PROJECT}/runbooks/sync", 500, {"error": BODY_SECRET})
    writer = _writer(url)

    with pytest.raises(MemoryWriteFailed):
        writer.sync_runbooks(PROJECT, _runbook_vault(tmp_path))
    assert caplog.text, "the failure is logged"
    assert TOKEN not in caplog.text and BODY_SECRET not in caplog.text
    assert TOKEN not in repr(writer)
    assert TOKEN not in repr(MemorySettings(url=url, token=TOKEN))


# --- what a vault publishes ----------------------------------------------------------------


def test_the_card_carries_the_name_the_hub_and_the_first_jira_project() -> None:
    spec = _spec(
        name="Globex",
        memory_hub="Conventions/Git Workflow.md",
        integrations={"jira": {"projects": ["acme", "ACME", "OPS"]}},
    )

    assert memory_bridge.card_fields(spec) == {
        "display_name": "Globex",
        "jira_project": "ACME",
        "knowledge_hub_path": "Conventions/Git Workflow.md",
    }
    assert memory_bridge.card_fields(_spec()) == {}


def test_sync_puts_the_card_before_the_runbooks(tmp_path: Path, fake_memory: tuple[FakeMemory, str]) -> None:
    state, url = fake_memory
    vault = _runbook_vault(tmp_path)
    state.reply("PUT", f"/projects/{PROJECT}", 201, {"created": True})
    state.reply("POST", f"/projects/{PROJECT}/runbooks/sync", 200, {"scanned": 1, "upserted": 1, "skipped": []})
    before = _tree_digest(vault)

    report = memory_bridge.sync_vault(_spec(vault, memory_hub="Hub.md"), _writer(url), card=True)

    assert [(r["method"], r["path"]) for r in state.requests] == [
        ("PUT", f"/projects/{PROJECT}"),
        ("POST", f"/projects/{PROJECT}/runbooks/sync"),
    ]
    assert report["ok"] is True
    assert report["card"] == {"status": "created", "fields": ["knowledge_hub_path"]}
    assert report["runbooks"] == {"status": "synced", "scanned": 1, "upserted": 1, "skipped": 0}
    assert _tree_digest(vault) == before


def test_a_failed_card_stops_the_sync(tmp_path: Path, fake_memory: tuple[FakeMemory, str]) -> None:
    state, url = fake_memory
    state.reply("PUT", f"/projects/{PROJECT}", 400, {"error": BODY_SECRET, "code": "invalid_field"})

    report = memory_bridge.sync_vault(_spec(_runbook_vault(tmp_path)), _writer(url), card=True)

    assert report["ok"] is False
    assert report["card"] == {
        "status": "failed",
        "code": errors.E_MEMORY_WRITE_FAILED,
        "http_status": 400,
        "reason": "invalid_field",
    }
    assert "runbooks" not in report
    assert len(state.requests) == 1


def test_a_vault_without_runbooks_skips_the_scan(tmp_path: Path, fake_memory: tuple[FakeMemory, str]) -> None:
    state, url = fake_memory
    (tmp_path / "vault").mkdir()

    report = memory_bridge.sync_vault(_spec(tmp_path / "vault"), _writer(url), card=False)

    assert report["ok"] is True
    assert report["runbooks"]["status"] == "skipped"
    assert state.requests == []


# --- settings and the extension registry -------------------------------------------------


@pytest.mark.parametrize(("value", "required"), [("", True), ("1", True), ("0", False), (" 0 ", False), ("false", True)])
def test_the_token_is_required_unless_the_variable_is_exactly_zero(value: str, required: bool) -> None:
    assert settings.memory_token_required({"KNOWLEDGE_MEMORY_REQUIRE_TOKEN": value}) is required
    assert settings.memory_token_required({}) is True


def test_the_memory_bridge_extension_loads_its_module_over_stdio_only() -> None:
    ext = extensions.get("memory-bridge")

    assert ext.module == "knowledge.memory_bridge"
    assert ext.requires_env == ("MEMORY_HTTP_TOKEN",)
    assert extensions.loads_on(ext, "stdio") and not extensions.loads_on(ext, "http")


def test_require_token_zero_waives_the_memory_bearer_for_both_memory_extensions() -> None:
    spec = _spec(features=frozenset({"memory-bridge", "jira"}))

    assert extensions.missing_env(spec, extensions.get("memory-bridge"), {}) == ["MEMORY_HTTP_TOKEN"]
    assert extensions.missing_env(spec, extensions.get("memory-bridge"), {"KNOWLEDGE_MEMORY_REQUIRE_TOKEN": "0"}) == []
    assert "MEMORY_HTTP_TOKEN" not in extensions.missing_env(
        spec, extensions.get("jira"), {"KNOWLEDGE_MEMORY_REQUIRE_TOKEN": "0"}
    )


def _vaults_file(tmp_path: Path, manifest: str, *, vault: Path | None = None) -> Path:
    import yaml

    directory = vault or (tmp_path / "vault")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "vault.yaml").write_text(f"slug: globex\n{textwrap.dedent(manifest)}", encoding="utf-8")
    vaults_file = tmp_path / "vaults.yaml"
    vaults_file.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "data_dir": str(tmp_path / "data"),
                "vaults": [{"slug": "globex", "path": str(directory), "default": True}],
            }
        ),
        encoding="utf-8",
    )
    return vaults_file


def test_memory_hub_is_read_from_the_vault_manifest(tmp_path: Path) -> None:
    vaults_file = _vaults_file(tmp_path, "memory:\n  project: globex\n  hub: Conventions/Git Workflow.md\n")
    loaded = settings.load_settings(
        {"KNOWLEDGE_VAULTS": str(vaults_file), "KNOWLEDGE_EMBEDDINGS_PROVIDER": "bag-of-words"}, project_root=tmp_path
    )

    assert loaded.default_vault().memory_hub == "Conventions/Git Workflow.md"
    assert loaded.memory.require_token is True


@pytest.mark.parametrize("hub", ["/etc/hub.md", "../Hub.md", "a//b.md", "C:/Hub.md", "a\\\\b.md", "''"])
def test_a_memory_hub_outside_the_vault_is_refused(tmp_path: Path, hub: str) -> None:
    vaults_file = _vaults_file(tmp_path, f"memory:\n  hub: {hub}\n")

    with pytest.raises(KnowledgeConfigError) as excinfo:
        settings.load_settings({"KNOWLEDGE_VAULTS": str(vaults_file)}, project_root=tmp_path)
    assert excinfo.value.code == errors.E_MANIFEST_INVALID
    assert "memory.hub" in excinfo.value.message


# --- reindex ------------------------------------------------------------------------------


def _serve_vault(tmp_path: Path, manifest: str, url: str, **env: str) -> settings.Settings:
    from knowledge import server

    vault = _runbook_vault(tmp_path)
    vaults_file = _vaults_file(tmp_path, manifest, vault=vault)
    base = {
        "KNOWLEDGE_VAULTS": str(vaults_file),
        "KNOWLEDGE_EMBEDDINGS_PROVIDER": "bag-of-words",
        "KNOWLEDGE_MEMORY_URL": url,
        "MEMORY_HTTP_TOKEN": TOKEN,
    }
    base.update(env)
    loaded = settings.load_settings(base, project_root=tmp_path)
    settings.install(loaded)
    server.set_registry(server._registry_for(loaded))
    return loaded


@pytest.fixture()
def _restore_registry() -> Iterator[None]:
    from knowledge import server

    yield
    server.set_registry(None)


@pytest.mark.usefixtures("_restore_registry")
def test_reindex_with_the_feature_has_memory_rescan_the_runbooks(
    tmp_path: Path, fake_memory: tuple[FakeMemory, str]
) -> None:
    from knowledge import server

    state, url = fake_memory
    state.reply("POST", f"/projects/{PROJECT}/runbooks/sync", 200, {"scanned": 1, "upserted": 1, "skipped": []})
    _serve_vault(tmp_path, "features: [memory-bridge]\nmemory:\n  project: globex\n", url)

    result = asyncio.run(server.reindex(vault="globex", pull=False))

    assert result["memory_bridge"] == {"status": "synced"}
    (request,) = state.requests
    assert request["path"] == f"/projects/{PROJECT}/runbooks/sync"
    assert request["body"]["vault_dir"] == str((tmp_path / "vault").resolve())


@pytest.mark.usefixtures("_restore_registry")
@pytest.mark.parametrize(
    ("manifest", "env", "expected"),
    [
        ("features: [memory-bridge]\nmemory:\n  project: globex\n", {"MEMORY_HTTP_TOKEN": ""},
         {"status": "skipped", "code": errors.E_MEMORY_TOKEN_MISSING}),
        ("features: [memory-bridge]\n", {}, {"status": "skipped", "code": errors.E_MEMORY_PROJECT_MISSING}),
        ("features: [memory-bridge]\nmemory:\n  project: globex\n", {"MCP_TRANSPORT": "http", "MCP_API_KEY": "k"},
         {"status": "skipped", "code": errors.W_EXTENSION_UNAVAILABLE_HTTP}),
    ],
)
def test_reindex_reports_why_the_bridge_did_not_write(
    tmp_path: Path, fake_memory: tuple[FakeMemory, str], manifest: str, env: dict[str, str], expected: dict[str, str]
) -> None:
    from knowledge import server

    state, url = fake_memory
    _serve_vault(tmp_path, manifest, url, **env)

    result = asyncio.run(server.reindex(vault="globex", pull=False))

    assert result["memory_bridge"] == expected
    assert state.requests == []


@pytest.mark.usefixtures("_restore_registry")
def test_reindex_reports_a_failed_write_and_still_returns_the_index(
    tmp_path: Path, fake_memory: tuple[FakeMemory, str]
) -> None:
    from knowledge import server

    state, url = fake_memory
    state.reply("POST", f"/projects/{PROJECT}/runbooks/sync", 404, {"error": "no card", "code": "no_card"})
    _serve_vault(tmp_path, "features: [memory-bridge]\nmemory:\n  project: globex\n", url)

    result = asyncio.run(server.reindex(vault="globex", pull=False))

    assert result["memory_bridge"] == {"status": "failed", "code": errors.E_MEMORY_PROJECT_MISSING}
    assert result["files"] >= 1


@pytest.mark.usefixtures("_restore_registry")
def test_reindex_without_the_feature_has_no_memory_bridge_key(
    tmp_path: Path, fake_memory: tuple[FakeMemory, str]
) -> None:
    from knowledge import server

    state, url = fake_memory
    _serve_vault(tmp_path, "memory:\n  project: globex\n", url)

    result = asyncio.run(server.reindex(vault="globex", pull=False))

    assert "memory_bridge" not in result
    assert state.requests == []


# --- command line -------------------------------------------------------------------------


def _cli_env(monkeypatch: pytest.MonkeyPatch, vaults_file: Path, url: str, **env: str) -> None:
    monkeypatch.setenv("KNOWLEDGE_VAULTS", str(vaults_file))
    monkeypatch.setenv("KNOWLEDGE_MEMORY_URL", url)
    monkeypatch.setenv("MEMORY_HTTP_TOKEN", TOKEN)
    for name, value in env.items():
        monkeypatch.setenv(name, value)


def test_cli_sync_with_card_publishes_and_prints_the_report(
    tmp_path: Path,
    fake_memory: tuple[FakeMemory, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state, url = fake_memory
    state.reply("PUT", f"/projects/{PROJECT}", 201, {"created": True})
    state.reply("POST", f"/projects/{PROJECT}/runbooks/sync", 200, {"scanned": 1, "upserted": 1, "skipped": []})
    vault = _runbook_vault(tmp_path)
    manifest = (
        "name: Globex\nfeatures: [memory-bridge]\nmemory:\n  project: globex\n  hub: Hub.md\n"
        "integrations:\n  jira:\n    site: https://example.atlassian.net\n    projects: [ACME]\n"
    )
    _cli_env(monkeypatch, _vaults_file(tmp_path, manifest, vault=vault), url)
    before = _tree_digest(vault)

    assert cli.main(["memory-bridge", "sync", "--vault", "globex", "--card"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["card"]["status"] == "created"
    assert state.requests[0]["body"] == {"display_name": "Globex", "jira_project": "ACME", "knowledge_hub_path": "Hub.md"}
    assert report["runbooks"]["status"] == "synced"
    assert _tree_digest(vault) == before


def test_cli_sync_exits_1_when_memory_refuses(
    tmp_path: Path,
    fake_memory: tuple[FakeMemory, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state, url = fake_memory
    state.reply("POST", f"/projects/{PROJECT}/runbooks/sync", 413, {"error": BODY_SECRET, "code": "payload_too_large"})
    vault = _runbook_vault(tmp_path)
    _cli_env(monkeypatch, _vaults_file(tmp_path, "features: [memory-bridge]\nmemory:\n  project: globex\n", vault=vault), url)

    assert cli.main(["memory-bridge", "sync"]) == 1
    out = capsys.readouterr().out
    assert json.loads(out)["runbooks"]["reason"] == "payload_too_large"
    assert BODY_SECRET not in out and TOKEN not in out


@pytest.mark.parametrize(
    ("manifest", "env", "code"),
    [
        ("memory:\n  project: globex\n", {}, errors.E_FEATURE_DISABLED),
        ("features: [memory-bridge]\n", {}, errors.E_MEMORY_PROJECT_MISSING),
        ("features: [memory-bridge]\nmemory:\n  project: globex\n", {"MEMORY_HTTP_TOKEN": ""}, errors.E_MEMORY_TOKEN_MISSING),
        (
            "features: [memory-bridge]\nmemory:\n  project: globex\n",
            {"KNOWLEDGE_MEMORY_URL": "http://192.0.2.10:6366"},
            errors.E_MEMORY_URL_NOT_LOOPBACK,
        ),
    ],
)
def test_cli_refuses_with_status_2_before_any_request(
    tmp_path: Path,
    fake_memory: tuple[FakeMemory, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    manifest: str,
    env: dict[str, str],
    code: str,
) -> None:
    state, url = fake_memory
    _cli_env(monkeypatch, _vaults_file(tmp_path, manifest, vault=_runbook_vault(tmp_path)), url, **env)

    assert cli.main(["memory-bridge", "sync", "--card"]) == 2
    captured = capsys.readouterr()
    assert captured.err.startswith(f"{code}: ")
    assert TOKEN not in captured.err + captured.out
    assert state.requests == []


# --- promotion: the memory command --------------------------------------------------------


def _fake_memory_command(tmp_path: Path, *, exit_status: int = 0, payload: Any = None) -> Path:
    """An executable that prints the arguments it received (or ``payload``) as JSON."""
    script = tmp_path / "fake-memory"
    body = json.dumps(payload) if payload is not None else None
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"payload = {body!r}\n"
        "print(payload if payload is not None else json.dumps({'argv': sys.argv[1:]}))\n"
        f"sys.exit({exit_status})\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def test_cli_promote_list_runs_the_memory_command_with_an_argument_list(
    tmp_path: Path,
    fake_memory: tuple[FakeMemory, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, url = fake_memory
    vault = _runbook_vault(tmp_path)
    _cli_env(
        monkeypatch,
        _vaults_file(tmp_path, "features: [memory-bridge]\nmemory:\n  project: globex\n", vault=vault),
        url,
        MEMORY_BIN=str(_fake_memory_command(tmp_path)),
    )

    assert cli.main(["memory-bridge", "promote", "list", "--types", "decision", "--limit", "5"]) == 0
    assert json.loads(capsys.readouterr().out)["argv"] == [
        "project", PROJECT, "promote", "list", "--json", "--types", "decision", "--limit", "5",
    ]


def test_cli_promote_stamp_checks_the_reference_and_writes_nothing_into_the_vault(
    tmp_path: Path,
    fake_memory: tuple[FakeMemory, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, url = fake_memory
    vault = _runbook_vault(tmp_path)
    ref = "Runbooks/Sync/RB-001 upload stalls.md"
    _cli_env(
        monkeypatch,
        _vaults_file(tmp_path, "features: [memory-bridge]\nmemory:\n  project: globex\n", vault=vault),
        url,
        MEMORY_BIN=str(_fake_memory_command(tmp_path)),
    )
    before = _tree_digest(vault)

    assert cli.main(["memory-bridge", "promote", "stamp", "obs-1a2b3c", "--knowledge-ref", ref]) == 0
    assert json.loads(capsys.readouterr().out)["argv"] == [
        "project", PROJECT, "promote", "stamp", "obs-1a2b3c", "--knowledge-ref", ref, "--json",
    ]
    assert _tree_digest(vault) == before


@pytest.mark.parametrize(
    "ref", ["/etc/passwd", "../outside.md", "Runbooks/missing.md", "Runbooks/services.json", "Runbooks//x.md"]
)
def test_a_reference_that_is_not_a_vault_document_is_refused(tmp_path: Path, ref: str) -> None:
    vault = _runbook_vault(tmp_path)
    (tmp_path / "outside.md").write_text("# not in the vault\n", encoding="utf-8")

    with pytest.raises(KnowledgeToolError) as excinfo:
        memory_bridge.promote_stamp(PROJECT, "obs-1", ref, vault_path=vault, env={"MEMORY_BIN": "/nonexistent"})
    assert excinfo.value.code == errors.E_KNOWLEDGE_REF_INVALID


def test_an_observation_that_looks_like_a_flag_is_refused(tmp_path: Path) -> None:
    with pytest.raises(KnowledgeToolError) as excinfo:
        memory_bridge.promote_stamp(
            PROJECT, "--allow-any-type", "Runbooks/Sync/RB-001 upload stalls.md",
            vault_path=_runbook_vault(tmp_path), env={},
        )
    assert excinfo.value.code == errors.E_ARGUMENT_OUT_OF_RANGE


def test_memorys_own_error_comes_back_as_its_envelope_with_status_1(
    tmp_path: Path,
    fake_memory: tuple[FakeMemory, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, url = fake_memory
    envelope = {"ok": False, "error": {"code": "observation_not_pinned", "message": "pin it first"}}
    _cli_env(
        monkeypatch,
        _vaults_file(tmp_path, "features: [memory-bridge]\nmemory:\n  project: globex\n", vault=_runbook_vault(tmp_path)),
        url,
        MEMORY_BIN=str(_fake_memory_command(tmp_path, exit_status=1, payload=envelope)),
    )

    assert cli.main(["memory-bridge", "promote", "list"]) == 1
    assert json.loads(capsys.readouterr().out) == envelope


def test_a_memory_command_that_cannot_run_is_unavailable_with_status_2(
    tmp_path: Path,
    fake_memory: tuple[FakeMemory, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, url = fake_memory
    _cli_env(
        monkeypatch,
        _vaults_file(tmp_path, "features: [memory-bridge]\nmemory:\n  project: globex\n", vault=_runbook_vault(tmp_path)),
        url,
    )

    assert cli.main(["memory-bridge", "promote", "list"]) == 2
    assert capsys.readouterr().err.startswith(f"{errors.E_MEMORY_UNAVAILABLE}: ")


def test_a_memory_command_past_its_timeout_is_unavailable() -> None:
    def _slow(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    with pytest.raises(MemoryWriteFailed) as excinfo:
        memory_bridge.promote_list(PROJECT, env={}, runner=_slow)
    assert (excinfo.value.code, excinfo.value.reason) == (errors.E_MEMORY_UNAVAILABLE, "timeout")


def test_the_memory_command_is_memory_bin_or_memory_on_path() -> None:
    assert memory_bridge.memory_command({"MEMORY_BIN": "/opt/memory/bin/memory"}) == "/opt/memory/bin/memory"
    assert memory_bridge.memory_command({"MEMORY_BIN": " "}) == "memory"
    assert memory_bridge.memory_command({}) == "memory"


# --- what never reaches a message ------------------------------------------------------

UNSENDABLE = ["bearer-with\nnewline", "bearer with space", "bearer-ñ", "bearer\twith-tab"]


@pytest.mark.parametrize("token", UNSENDABLE, ids=["newline", "space", "non-ascii", "tab"])
def test_a_token_that_cannot_travel_in_a_header_is_refused_without_quoting_it(
    fake_memory: tuple[FakeMemory, str], token: str
) -> None:
    state, url = fake_memory

    with pytest.raises(MemoryWriteFailed) as excinfo:
        _writer(url, token=token)

    assert excinfo.value.code == errors.E_MEMORY_TOKEN_MISSING
    assert token not in str(excinfo.value)
    assert state.requests == []


def test_a_request_http_client_refuses_never_carries_its_text(fake_memory: tuple[FakeMemory, str]) -> None:
    """Defense in depth: a ``ValueError`` from ``http.client`` quotes the header; only the class survives."""
    _, url = fake_memory
    secret = "the-header-text-the-error-would-quote"

    def refusing(request: Any, timeout: float) -> Any:
        raise ValueError(f"Invalid header value b'Bearer {secret}\\n'")

    writer = memory_bridge.MemoryWriter(url, TOKEN, opener=refusing)

    with pytest.raises(MemoryWriteFailed) as excinfo:
        writer.upsert_task(PROJECT, {"jira_key": "ACME-1", "title": "t", "kind": "feature"})

    assert excinfo.value.code == errors.E_MEMORY_WRITE_FAILED
    assert excinfo.value.reason == "invalid_request"
    assert secret not in str(excinfo.value)


@pytest.mark.parametrize("types", [["--json"], ["decision", "-x"], ["Decision"], ["a b"], [""]])
def test_promote_list_refuses_a_type_that_is_not_an_observation_type(types: list[str]) -> None:
    ran: list[Any] = []

    with pytest.raises(KnowledgeToolError) as excinfo:
        memory_bridge.promote_list(PROJECT, env={}, types=types, runner=lambda *a, **k: ran.append(a))

    assert excinfo.value.code == errors.E_ARGUMENT_OUT_OF_RANGE
    assert ran == []
