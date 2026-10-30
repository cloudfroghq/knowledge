"""Test Confluence sync scope scoping and direction guard.

Test coverage for T-11.03:
1. Sync scoped to "Conventions/" by default.
2. Documents with confluence_id outside scope are NOT touched.
3. Empty sync in-scope is an error, not silent success.
4. Guard: only GET allowed to Confluence; no POST/PUT/PATCH/DELETE.
5. dry_run=True never writes; dry_run=False is fenced (tests/test_write_fence.py).
"""

from __future__ import annotations

import http.server
import json
import logging
import socketserver
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Stub Confluence Server (in-process, simulates /wiki/api/v2/pages/{id})
# ---------------------------------------------------------------------------


class ConfluenceStubHandler(http.server.BaseHTTPRequestHandler):
    """HTTP handler that simulates the Confluence API we call."""

    # Shared state across all request instances
    _request_methods: list[str] = []
    _page_database: dict[str, dict[str, Any]] = {}

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress HTTP server's default logging."""
        pass

    def do_GET(self) -> None:
        """Handle GET requests to /wiki/api/v2/pages/{id}."""
        ConfluenceStubHandler._request_methods.append("GET")

        # Parse URL
        path = urlparse(self.path).path

        # Route: /wiki/api/v2/pages/{id}
        if path.startswith("/wiki/api/v2/pages/"):
            page_id = path.split("/")[-1].split("?")[0]
            if page_id in self._page_database:
                page_data = self._page_database[page_id].copy()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(page_data).encode())
                return

        # Not found
        self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"error": "Not found"}).encode())

    def do_POST(self) -> None:
        """POST is not allowed; fail the test."""
        ConfluenceStubHandler._request_methods.append("POST")
        pytest.fail("Confluence sync attempted POST (mutation not allowed)")

    def do_PUT(self) -> None:
        """PUT is not allowed; fail the test."""
        ConfluenceStubHandler._request_methods.append("PUT")
        pytest.fail("Confluence sync attempted PUT (mutation not allowed)")

    def do_PATCH(self) -> None:
        """PATCH is not allowed; fail the test."""
        ConfluenceStubHandler._request_methods.append("PATCH")
        pytest.fail("Confluence sync attempted PATCH (mutation not allowed)")

    def do_DELETE(self) -> None:
        """DELETE is not allowed; fail the test."""
        ConfluenceStubHandler._request_methods.append("DELETE")
        pytest.fail("Confluence sync attempted DELETE (mutation not allowed)")


@contextmanager
def confluence_stub_server(port: int = 0) -> Any:
    """Start a local HTTP server simulating Confluence API.

    Yields (url, handler_class, request_methods_list).
    """
    # Create server with dynamic port assignment
    with socketserver.TCPServer(("127.0.0.1", port), ConfluenceStubHandler) as server:
        actual_port = server.server_address[1]
        url = f"http://127.0.0.1:{actual_port}"

        # Start server in a background thread
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()

        try:
            yield url
        finally:
            server.shutdown()


@contextmanager
def temp_vault_copy(fixture_vault: Path) -> Any:
    """Copy the fixture vault to a temporary directory and yield its path."""
    with tempfile.TemporaryDirectory(prefix="knowledge-mcp-test-vault-") as tmpdir:
        tmp_path = Path(tmpdir)

        # Deep copy the fixture vault
        import shutil
        shutil.copytree(fixture_vault, tmp_path / "vault", dirs_exist_ok=True)

        vault_copy = tmp_path / "vault"
        yield vault_copy


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fixture_vault(request: Any) -> Path:
    """Get the path to the fixture vault."""
    return Path(request.config.rootdir) / "tests" / "fixtures" / "vault"


@pytest.fixture
def configured_stub(monkeypatch: Any) -> Any:
    """Set up Confluence credentials and stub server, return (url, db)."""
    with confluence_stub_server() as url:
        monkeypatch.setenv("CONFLUENCE_URL", url)
        monkeypatch.setenv("CONFLUENCE_EMAIL", "test@example.com")
        monkeypatch.setenv("CONFLUENCE_TOKEN", "test-token-123")

        # Reload config to pick up the env vars
        import importlib
        import knowledge.config
        importlib.reload(knowledge.config)

        # Yield the URL and the page database reference
        yield url, ConfluenceStubHandler._page_database


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_sync_scope_only_conventions(
    fixture_vault: Path,
    configured_stub: Any,
    monkeypatch: Any,
) -> None:
    """Test that sync only scans files under Conventions/ by default."""
    url, page_db = configured_stub

    # Create a temporary vault copy
    with temp_vault_copy(fixture_vault) as vault_copy:
        # Point to the temporary vault
        monkeypatch.setenv("VAULT_PATH", str(vault_copy))

        # Reload config to pick up VAULT_PATH
        import importlib
        import knowledge.config
        importlib.reload(knowledge.config)

        # Ensure the Conventions directory exists
        conventions_dir = vault_copy / "Conventions"
        conventions_dir.mkdir(parents=True, exist_ok=True)

        # Create a test document in Conventions/ with confluence_id
        test_doc_path = conventions_dir / "Test-Convention.md"
        test_page_id = "12345"
        test_doc_path.write_text(
            "---\nconfluence_id: " + test_page_id + "\ntype: convention\nlast_updated: 2026-01-01\n---\n# Old content\n"
        )

        # Set up Confluence stub with the new version
        page_db[test_page_id] = {
            "id": test_page_id,
            "title": "Test Convention",
            "body": {"storage": {"value": "<p>New content from Confluence</p>"}},
            "version": {"number": 2, "createdAt": "2026-09-05T10:00:00Z"},
        }

        # Run sync (dry_run=True)
        from knowledge.confluence_sync import sync
        result = sync(dry_run=True, scope_prefix="Conventions/")

        # Verify the result structure
        assert result["scope_prefix"] == "Conventions/"
        assert result["total_in_scope"] == 1, f"Expected 1 file in scope, got {result['total_in_scope']}"
        assert len(result["needs_update"]) == 1
        assert result["needs_update"][0]["confluence_id"] == test_page_id

        # Verify that the file was not modified (dry_run=True)
        content = test_doc_path.read_text()
        assert "Old content" in content
        assert "New content from Confluence" not in content


def test_sync_out_of_scope_not_touched(
    fixture_vault: Path,
    configured_stub: Any,
    monkeypatch: Any,
) -> None:
    """Test that files with confluence_id outside Conventions/ are NOT synced.

    This is the critical canary: without this test, the scoping would be decorative.
    """
    url, page_db = configured_stub

    with temp_vault_copy(fixture_vault) as vault_copy:
        monkeypatch.setenv("VAULT_PATH", str(vault_copy))

        import importlib
        import knowledge.config
        importlib.reload(knowledge.config)

        # Create a test document OUTSIDE Conventions/ with confluence_id
        out_of_scope_dir = vault_copy / "Architecture"
        out_of_scope_dir.mkdir(parents=True, exist_ok=True)

        out_of_scope_doc = out_of_scope_dir / "OldService.md"
        out_of_scope_page_id = "99999"
        out_of_scope_doc.write_text(
            f"---\nconfluence_id: {out_of_scope_page_id}\ntype: service\nlast_updated: 2026-01-01\n---\n# Local version\n"
        )

        # Set up Confluence with a newer version
        page_db[out_of_scope_page_id] = {
            "id": out_of_scope_page_id,
            "title": "OldService",
            "body": {"storage": {"value": "<p>Newer version in Confluence</p>"}},
            "version": {"number": 5, "createdAt": "2026-09-05T10:00:00Z"},
        }

        # Ensure Conventions/ exists but is empty
        conventions_dir = vault_copy / "Conventions"
        conventions_dir.mkdir(parents=True, exist_ok=True)

        # Run sync with scope_prefix="Conventions/"
        from knowledge.confluence_sync import sync
        from knowledge.errors import E_SCOPE_EMPTY, KnowledgeToolError

        with pytest.raises(KnowledgeToolError) as excinfo:
            sync(dry_run=True, scope_prefix="Conventions/")

        # An error, not a report: the scope is empty and the out-of-scope file is named
        assert excinfo.value.code == E_SCOPE_EMPTY
        assert "Files outside it that carry one (1): Architecture/OldService.md" in excinfo.value.message

        # Verify the file was NOT modified
        content = out_of_scope_doc.read_text()
        assert "Local version" in content
        assert "Newer version in Confluence" not in content


def test_empty_scope_is_error(
    fixture_vault: Path,
    configured_stub: Any,
    monkeypatch: Any,
) -> None:
    """Test that syncing with no files in scope returns an error, not silent success."""
    url, page_db = configured_stub

    with temp_vault_copy(fixture_vault) as vault_copy:
        monkeypatch.setenv("VAULT_PATH", str(vault_copy))

        import importlib
        import knowledge.config
        importlib.reload(knowledge.config)

        # Ensure Conventions/ exists but is empty
        conventions_dir = vault_copy / "Conventions"
        conventions_dir.mkdir(parents=True, exist_ok=True)

        # Run sync
        from knowledge.confluence_sync import sync
        from knowledge.errors import E_SCOPE_EMPTY, KnowledgeToolError

        # Must be a coded error, not a success
        with pytest.raises(KnowledgeToolError) as excinfo:
            sync(dry_run=True, scope_prefix="Conventions/")
        assert excinfo.value.code == E_SCOPE_EMPTY
        assert "No file of the vault carries a confluence_id." in excinfo.value.message


def test_dry_run_never_writes(
    fixture_vault: Path,
    configured_stub: Any,
    monkeypatch: Any,
) -> None:
    """Test that dry_run=True never modifies files."""
    url, page_db = configured_stub

    with temp_vault_copy(fixture_vault) as vault_copy:
        monkeypatch.setenv("VAULT_PATH", str(vault_copy))

        import importlib
        import knowledge.config
        importlib.reload(knowledge.config)

        conventions_dir = vault_copy / "Conventions"
        conventions_dir.mkdir(parents=True, exist_ok=True)

        # Create a document with confluence_id that needs update
        test_doc = conventions_dir / "Convention.md"
        page_id = "45678"
        original_content = f"---\nconfluence_id: {page_id}\nlast_updated: 2026-01-01\n---\n# Original\n"
        test_doc.write_text(original_content)

        # Set up Confluence with a newer version
        page_db[page_id] = {
            "id": page_id,
            "title": "Convention",
            "body": {"storage": {"value": "<h1>Updated</h1>"}},
            "version": {"number": 2, "createdAt": "2026-09-05T10:00:00Z"},
        }

        # Run sync with dry_run=True
        from knowledge.confluence_sync import sync
        result = sync(dry_run=True, scope_prefix="Conventions/")

        # File should not have been modified
        current_content = test_doc.read_text()
        assert current_content == original_content, "File was modified despite dry_run=True"
        assert len(result["updated"]) == 0, "Updated list should be empty in dry_run"


def test_only_get_requests_to_confluence(
    fixture_vault: Path,
    configured_stub: Any,
    monkeypatch: Any,
) -> None:
    """Test that the sync guard enforces only GET requests (no POST/PUT/PATCH/DELETE)."""
    url, page_db = configured_stub

    with temp_vault_copy(fixture_vault) as vault_copy:
        monkeypatch.setenv("VAULT_PATH", str(vault_copy))

        import importlib
        import knowledge.config
        importlib.reload(knowledge.config)

        conventions_dir = vault_copy / "Conventions"
        conventions_dir.mkdir(parents=True, exist_ok=True)

        # Create a document
        test_doc = conventions_dir / "Convention.md"
        page_id = "11111"
        test_doc.write_text(
            f"---\nconfluence_id: {page_id}\nlast_updated: 2026-01-01\n---\n# Test\n"
        )

        # Set up Confluence
        page_db[page_id] = {
            "id": page_id,
            "title": "Convention",
            "body": {"storage": {"value": "<p>Content</p>"}},
            "version": {"number": 1, "createdAt": "2026-09-05T10:00:00Z"},
        }

        # Run sync
        from knowledge.confluence_sync import sync

        # _request_methods is class-level state that no fixture clears, so a
        # slice taken from here on is the only way to attribute a request to
        # this sync rather than to one an earlier case in the session made.
        methods_before = len(ConfluenceStubHandler._request_methods)

        result = sync(dry_run=True, scope_prefix="Conventions/")

        # Assert the sync did the work before reading how it did it. Without
        # this, a sync that reached Confluence zero times — a scope that matched
        # nothing, a fetch that raised and was swallowed into the report —
        # satisfies every method assertion below and proves nothing.
        assert result["errors"] == [], f"sync reported errors: {result['errors']}"
        examined = [entry["file"] for entry in result["needs_update"]]
        assert examined == ["Conventions/Convention.md"], (
            f"sync did not examine the in-scope document; report was {result}"
        )

        # Check that only GET was used
        methods_used = ConfluenceStubHandler._request_methods[methods_before:]
        assert "GET" in methods_used, "No GET requests made"
        assert "POST" not in methods_used, "POST was used (forbidden)"
        assert "PUT" not in methods_used, "PUT was used (forbidden)"
        assert "PATCH" not in methods_used, "PATCH was used (forbidden)"
        assert "DELETE" not in methods_used, "DELETE was used (forbidden)"


def test_url_scheme_guard(
    fixture_vault: Path,
    monkeypatch: Any,
) -> None:
    """Test that the URL scheme guard rejects file:// and ftp://."""
    # This test doesn't need the stub server since we're testing the guard itself
    from knowledge.confluence_sync import _api_get

    # Test with file:// scheme (should fail)
    monkeypatch.setenv("CONFLUENCE_URL", "file:///etc/passwd")
    monkeypatch.setenv("CONFLUENCE_EMAIL", "test@example.com")
    monkeypatch.setenv("CONFLUENCE_TOKEN", "token")

    import importlib
    import knowledge.config
    importlib.reload(knowledge.config)

    with pytest.raises(RuntimeError, match="Refusing to call Confluence over"):
        _api_get("/wiki/api/v2/pages/123")

    # Test with ftp:// scheme (should fail)
    monkeypatch.setenv("CONFLUENCE_URL", "ftp://example.com/path")
    importlib.reload(knowledge.config)

    with pytest.raises(RuntimeError, match="Refusing to call Confluence over"):
        _api_get("/wiki/api/v2/pages/123")

    # Test with https:// on a closed loopback port: the scheme guard passes and
    # the call fails on the connection, never leaving the machine.
    monkeypatch.setenv("CONFLUENCE_URL", "https://127.0.0.1:9")
    importlib.reload(knowledge.config)

    with pytest.raises(RuntimeError) as excinfo:
        _api_get("/wiki/api/v2/pages/123", _retries=1)
    assert "Refusing to call Confluence over" not in str(excinfo.value)
