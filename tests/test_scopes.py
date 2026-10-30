"""Scopes per bearer, end to end: a read bearer cannot reindex, an admin one can.

The HTTP cases run the real stack: ``server.http_app`` (the routes, the
streamable-http transport and ``BearerAuthMiddleware``) under uvicorn on
``127.0.0.1`` and an ephemeral port, called by the MCP SDK's own
streamable-http client. That is the only way to prove that the scopes the
middleware leaves on the Starlette request reach the tool through
``RequestContext.request``. Every app is a fresh ``FastMCP`` with the
server's tools registered on it, because a streamable-http session manager
runs once per instance.

Without ``MCP_ADMIN_KEY`` and without ``bearers`` the single ``MCP_API_KEY``
holds both scopes, which is how every client of 1.2.0 keeps working. Over
stdio no bearer exists and nothing is checked.

No token used here is a credential: each is built from the name of the
principal it stands for.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import logging
import socket
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.fastmcp import FastMCP
from mcp.server.lowlevel.server import request_ctx
from mcp.shared.context import RequestContext
from mcp.types import CallToolResult
from starlette.requests import Request

from knowledge import errors, principals, server, settings, webhook

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_VAULT = REPO_ROOT / "tests" / "fixtures" / "vault"
ALPHA_VAULT = REPO_ROOT / "tests" / "fixtures" / "vaults" / "alpha"
STDIO_SERVER = REPO_ROOT / "tests" / "golden_stdio_server.py"


def _placeholder(name: str) -> str:
    """A token-shaped value that is no credential: the principal's name around a marker."""
    return f"{name}-not-a-credential-{name}"


READ_TOKEN = _placeholder("read")
ADMIN_TOKEN = _placeholder("admin")
LEGACY_TOKEN = _placeholder("legacy")
PERSON_TOKEN = _placeholder("person")
OPS_TOKEN = _placeholder("ops")
WEBHOOK_SECRET = _placeholder("webhook")
ALL_TOKENS = (READ_TOKEN, ADMIN_TOKEN, LEGACY_TOKEN, PERSON_TOKEN, OPS_TOKEN, WEBHOOK_SECRET)

_PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}


# ---------------------------------------------------------------------------
# A real HTTP server on an ephemeral loopback port
# ---------------------------------------------------------------------------


@contextmanager
def _serving(*, serve_reindex_webhook: bool = True) -> Iterator[str]:
    """Run ``server.http_app`` over a fresh FastMCP under uvicorn; yield its base URL.

    The settings snapshot is dropped first, so the server reads the
    environment the test has just set. The app serves the ``legacy`` profile,
    which registers ``sync_confluence`` whatever the fixture vault declares:
    the scope check of an admin tool is the same under every profile.
    """
    settings.reset_current()
    app = FastMCP("scopes-probe")
    server.register_tools(app, structured=True, profile="legacy")
    asgi = server.http_app(app, serve_reindex_webhook=serve_reindex_webhook)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    uv = uvicorn.Server(uvicorn.Config(asgi, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=uv.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not uv.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        uv.should_exit = True
        thread.join(timeout=15)
        sock.close()


async def _call_tools(base: str, token: str, calls: list[tuple[str, dict[str, Any]]]) -> list[CallToolResult]:
    """Run ``calls`` in one MCP session authenticated with ``token``."""
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(120)) as http:
        async with streamable_http_client(f"{base}/mcp", http_client=http) as (read, write, _session_id):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return [await session.call_tool(name, arguments) for name, arguments in calls]


def _call(base: str, token: str, *calls: tuple[str, dict[str, Any]]) -> list[CallToolResult]:
    return asyncio.run(_call_tools(base, token, list(calls)))


def _text(result: CallToolResult) -> str:
    return "".join(getattr(block, "text", "") for block in result.content)


def _denied(result: CallToolResult) -> bool:
    return result.isError is True and errors.E_SCOPE_REQUIRED in _text(result)


def _assert_no_token(*texts: str) -> None:
    for text in texts:
        for token in ALL_TOKENS:
            assert token not in text


def _ping_status(base: str, token: str | None) -> int:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.post(f"{base}/mcp", json=_PING, headers=headers).status_code


@pytest.fixture
def http_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """The HTTP transport, with the default index warmed so no call waits on a first indexing."""
    monkeypatch.setenv("MCP_TRANSPORT", "http")
    rt = server.default_runtime()
    if not rt.index_state["ready"]:
        rt.indexer.index(force=False, target="vault")
        rt.index_state["ready"] = True
    return monkeypatch


# ---------------------------------------------------------------------------
# (a) No scope configured: the single MCP_API_KEY does everything, as in 1.2.0
# ---------------------------------------------------------------------------


def test_without_scopes_the_api_key_still_reindexes(http_env: pytest.MonkeyPatch) -> None:
    http_env.setenv("MCP_API_KEY", LEGACY_TOKEN)

    with _serving() as base:
        reindexed, searched = _call(base, LEGACY_TOKEN, ("reindex", {"pull": False}), ("search", {"query": "auth"}))

    assert reindexed.isError is False, _text(reindexed)
    assert json.loads(_text(reindexed))["vault"] == server.default_runtime().spec.slug
    assert searched.isError is False


def test_an_unknown_or_missing_bearer_is_401(http_env: pytest.MonkeyPatch) -> None:
    http_env.setenv("MCP_API_KEY", LEGACY_TOKEN)

    with _serving() as base:
        wrong = _ping_status(base, READ_TOKEN)
        missing = _ping_status(base, None)
        health = httpx.get(f"{base}/health").status_code

    assert (wrong, missing) == (401, 401)
    assert health == 200, "/health stays public"


# ---------------------------------------------------------------------------
# (b) MCP_ADMIN_KEY: MCP_API_KEY becomes read-only
# ---------------------------------------------------------------------------


def test_with_an_admin_key_the_read_key_cannot_reindex(
    http_env: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    http_env.setenv("MCP_API_KEY", READ_TOKEN)
    http_env.setenv("MCP_ADMIN_KEY", ADMIN_TOKEN)
    caplog.set_level(logging.DEBUG)

    with _serving() as base:
        reindex, sync, search, listed = _call(
            base,
            READ_TOKEN,
            ("reindex", {"pull": False}),
            ("sync_confluence", {"dry_run": True}),
            ("search", {"query": "auth"}),
            ("list_vaults", {}),
        )
        (admin_reindex,) = _call(base, ADMIN_TOKEN, ("reindex", {"pull": False}))

    assert _denied(reindex), _text(reindex)
    assert f"bearer {principals.SERVICE_READ_ID} lacks it" in _text(reindex)
    assert _denied(sync), "sync_confluence needs admin even on a dry run"
    assert search.isError is False
    assert listed.isError is False
    assert admin_reindex.isError is False, _text(admin_reindex)
    # (f) No token in any answer or log line, the denials included.
    _assert_no_token(caplog.text, *(_text(r) for r in (reindex, sync, search, listed, admin_reindex)))
    assert f"principal={principals.SERVICE_READ_ID}" in caplog.text


# ---------------------------------------------------------------------------
# (c) bearers in vaults.yaml: one principal per entry, and no other
# ---------------------------------------------------------------------------


@pytest.fixture
def bearers_registry(http_env: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """A ``vaults.yaml`` over the alpha fixture with a read bearer and an admin one.

    The server's registry is swapped for one over that file for the duration
    of the test (its index lives under ``tmp_path``) and restored afterwards.
    """
    manifest = tmp_path / "vaults.yaml"
    manifest.write_text(
        "version: 1\n"
        f"data_dir: {tmp_path / 'data'}\n"
        "vaults:\n"
        f"  - slug: alpha\n    path: {ALPHA_VAULT}\n"
        "bearers:\n"
        "  - id: person\n    token_env: SCOPES_PERSON_TOKEN\n    scopes: [read]\n"
        "  - id: ops\n    token_env: SCOPES_OPS_TOKEN\n    scopes: [read, admin]\n",
        encoding="utf-8",
    )
    http_env.setenv("KNOWLEDGE_VAULTS", str(manifest))
    http_env.setenv("SCOPES_PERSON_TOKEN", PERSON_TOKEN)
    http_env.setenv("SCOPES_OPS_TOKEN", OPS_TOKEN)
    # Declared bearers replace the two keys: neither may authenticate.
    http_env.setenv("MCP_API_KEY", LEGACY_TOKEN)
    http_env.setenv("MCP_ADMIN_KEY", ADMIN_TOKEN)

    original = server._registry
    server.set_registry(None)
    try:
        yield manifest
    finally:
        server.registry().close()
        server.set_registry(original)


@pytest.mark.usefixtures("bearers_registry")
def test_declared_bearers_carry_their_own_scopes() -> None:
    with _serving() as base:
        person_reindex, person_search = _call(
            base, PERSON_TOKEN, ("reindex", {"pull": False}), ("search", {"query": "gateway"})
        )
        (ops_reindex,) = _call(base, OPS_TOKEN, ("reindex", {"pull": False}))
        implicit = [_ping_status(base, token) for token in (LEGACY_TOKEN, ADMIN_TOKEN)]

    assert _denied(person_reindex)
    assert "bearer person lacks it" in _text(person_reindex)
    assert person_search.isError is False
    assert ops_reindex.isError is False, _text(ops_reindex)
    assert json.loads(_text(ops_reindex))["vault"] == "alpha"
    assert implicit == [401, 401], "MCP_API_KEY and MCP_ADMIN_KEY are no principals once bearers is declared"


# ---------------------------------------------------------------------------
# (d) POST /webhook/reindex: a manual bearer needs admin; HMAC unchanged
# ---------------------------------------------------------------------------


@pytest.fixture
def queued(http_env: pytest.MonkeyPatch) -> list[bool]:
    """Record each reindex the webhook queues instead of running it."""
    runs: list[bool] = []

    async def _record(indexer: Any, force: bool = False, *, vault_sync: Any = None, refresh: Any = None) -> None:
        del indexer, vault_sync, refresh
        runs.append(force)

    http_env.setattr(webhook, "_run_reindex_async", _record)
    http_env.setenv("MCP_WEBHOOK_SECRET", WEBHOOK_SECRET)
    return runs


def _sha256(body: bytes) -> str:
    return "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()


def _sha1(body: bytes) -> str:
    return "sha1=" + hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha1).hexdigest()


def test_the_webhook_needs_an_admin_bearer(http_env: pytest.MonkeyPatch, queued: list[bool]) -> None:
    http_env.setenv("MCP_API_KEY", READ_TOKEN)
    http_env.setenv("MCP_ADMIN_KEY", ADMIN_TOKEN)

    with _serving() as base:
        url = f"{base}/webhook/reindex?target=vault&force=true"
        read = httpx.post(url, headers={"Authorization": f"Bearer {READ_TOKEN}"})
        admin = httpx.post(url, headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})
        unknown = httpx.post(url, headers={"Authorization": f"Bearer {LEGACY_TOKEN}"})

    assert read.status_code == 403
    assert read.json() == {"error": f"{errors.E_SCOPE_REQUIRED}: /webhook/reindex requires the admin scope"}
    assert admin.status_code == 202
    assert admin.json()["mode"] == "manual" and admin.json()["force"] is True
    assert unknown.status_code == 401
    assert queued == [True], "only the admin request queued a reindex"


def test_without_scopes_the_api_key_still_triggers_the_webhook(
    http_env: pytest.MonkeyPatch, queued: list[bool]
) -> None:
    http_env.setenv("MCP_API_KEY", LEGACY_TOKEN)

    with _serving() as base:
        response = httpx.post(
            f"{base}/webhook/reindex?target=vault", headers={"Authorization": f"Bearer {LEGACY_TOKEN}"}
        )

    assert response.status_code == 202
    assert queued == [False]


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        # GitHub, webhook with a secret: both headers, the short one carrying sha1.
        pytest.param(lambda b: {"X-Hub-Signature": _sha1(b), "X-Hub-Signature-256": _sha256(b)}, 202, id="github"),
        pytest.param(lambda b: {"X-Hub-Signature-256": _sha256(b)}, 202, id="sha256-header-only"),
        # Bitbucket: the short name with a sha256 digest.
        pytest.param(lambda b: {"X-Hub-Signature": _sha256(b)}, 202, id="bitbucket"),
        pytest.param(lambda b: {"X-Hub-Signature": _sha1(b)}, 401, id="sha1-only"),
        pytest.param(lambda b: {"X-Hub-Signature-256": _sha256(b + b"tampered")}, 401, id="wrong-digest"),
    ],
)
def test_a_signed_push_needs_no_bearer(
    http_env: pytest.MonkeyPatch,
    queued: list[bool],
    headers: Callable[[bytes], dict[str, str]],
    status: int,
) -> None:
    http_env.setenv("MCP_API_KEY", READ_TOKEN)
    http_env.setenv("MCP_ADMIN_KEY", ADMIN_TOKEN)
    body = b'{"ref":"refs/heads/main"}'

    with _serving() as base:
        response = httpx.post(f"{base}/webhook/reindex?target=vault&force=true", content=body, headers=headers(body))

    assert response.status_code == status
    if status == 202:
        assert response.json()["mode"] == "signature"
        assert queued == [False], "a signed push never forces a full run"
    else:
        assert queued == []


# ---------------------------------------------------------------------------
# (e) stdio: no bearer, nothing checked
# ---------------------------------------------------------------------------


async def _stdio_reindex(index_dir: Path) -> CallToolResult:
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(STDIO_SERVER)],
        env={
            "VAULT_PATH": str(FIXTURE_VAULT),
            "KNOWLEDGE_INDEX_DIR": str(index_dir),
            "MCP_TRANSPORT": "stdio",
            # Scopes are configured; over stdio they must not matter.
            "MCP_API_KEY": READ_TOKEN,
            "MCP_ADMIN_KEY": ADMIN_TOKEN,
            "CONFLUENCE_URL": "",
            "CONFLUENCE_EMAIL": "",
            "CONFLUENCE_TOKEN": "",
            "KNOWLEDGE_STRUCTURED_OUTPUT": "",
            "KNOWLEDGE_TEXT_INDENT": "",
        },
        cwd=str(REPO_ROOT),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=180)) as session:
            await session.initialize()
            return await session.call_tool("reindex", {"pull": False})


def test_stdio_reindexes_without_any_bearer(tmp_path: Path) -> None:
    result = asyncio.run(_stdio_reindex(tmp_path / "index"))

    assert result.isError is False, _text(result)
    _assert_no_token(_text(result))


# ---------------------------------------------------------------------------
# The admin check on its own: fail closed
# ---------------------------------------------------------------------------


@contextmanager
def _in_request(request: Request | None) -> Iterator[None]:
    """Run the block inside an MCP request context whose HTTP request is ``request``."""
    context: RequestContext[Any, Any, Any] = RequestContext(
        request_id=1, meta=None, session=None, lifespan_context=None, request=request
    )
    token = request_ctx.set(context)
    try:
        yield
    finally:
        request_ctx.reset(token)


def _starlette_request(state: dict[str, Any] | None) -> Request:
    scope: dict[str, Any] = {"type": "http", "method": "POST", "path": "/mcp", "headers": []}
    if state is not None:
        scope["state"] = state
    return Request(scope)


@pytest.mark.parametrize(
    ("state", "allowed"),
    [
        pytest.param(None, False, id="no-state"),
        pytest.param({"knowledge_principal": "p"}, False, id="principal-without-scopes"),
        pytest.param({"knowledge_scopes": frozenset({"read"}), "knowledge_principal": "p"}, False, id="read"),
        pytest.param({"knowledge_scopes": frozenset({"read", "admin"}), "knowledge_principal": "p"}, True, id="admin"),
    ],
)
def test_an_http_request_without_the_admin_scope_is_refused(state: dict[str, Any] | None, allowed: bool) -> None:
    with _in_request(_starlette_request(state)):
        if allowed:
            server._require_admin("reindex")
        else:
            with pytest.raises(errors.KnowledgeToolError) as exc_info:
                server._require_admin("reindex")
            assert exc_info.value.code == errors.E_SCOPE_REQUIRED


def test_stdio_and_direct_calls_are_not_checked() -> None:
    server._require_admin("reindex")  # no MCP request at all: a direct Python call
    with _in_request(None):  # an MCP request without an HTTP one: stdio
        server._require_admin("reindex")


def test_an_http_server_refuses_a_call_without_a_request() -> None:
    """Fail closed: on an HTTP server, an MCP call that carries no request has no scopes to show."""
    previous = settings.current()
    settings.install(replace(previous, http=replace(previous.http, transport="http")))
    try:
        with _in_request(None), pytest.raises(errors.KnowledgeToolError) as exc_info:
            server._require_admin("reindex")
        server._require_admin("reindex")  # still no MCP request at all: a direct Python call
    finally:
        settings.install(previous)
    assert exc_info.value.code == errors.E_SCOPE_REQUIRED


def test_the_admin_tools_keep_their_input_schema() -> None:
    """``_admin`` wraps the tool; the catalog must not see the wrapper."""
    assert list(inspect.signature(server.reindex).parameters) == ["target", "force", "pull", "vault"]
    assert list(inspect.signature(server.sync_confluence).parameters) == ["dry_run", "scope_prefix", "vault"]


def test_admin_refuses_a_blocking_tool() -> None:
    with pytest.raises(TypeError):
        server._admin(lambda: None)


# ---------------------------------------------------------------------------
# (g) The principal table
# ---------------------------------------------------------------------------


def _bearer(bearer_id: str, token: str, *scopes: str) -> settings.BearerSpec:
    return settings.BearerSpec(
        id=bearer_id, token_env=f"T_{bearer_id.upper()}", scopes=frozenset(scopes), token=token
    )


def test_every_entry_is_compared_whatever_matches_first(monkeypatch: pytest.MonkeyPatch) -> None:
    table = principals.PrincipalTable.from_values(
        api_key="",
        admin_key="",
        bearers=[
            _bearer("one", PERSON_TOKEN, "read"),
            _bearer("two", OPS_TOKEN, "read", "admin"),
            _bearer("three", READ_TOKEN, "read"),
        ],
    )
    calls: list[int] = []
    real = principals.secrets.compare_digest

    def _spy(a: bytes, b: bytes) -> bool:
        calls.append(1)
        return real(a, b)

    monkeypatch.setattr(principals.secrets, "compare_digest", _spy)

    first = table.resolve(PERSON_TOKEN)
    after_first = len(calls)
    nobody = table.resolve("not-a-bearer")

    assert first is not None and first.id == "one"
    assert after_first == 3
    assert nobody is None
    assert len(calls) == 6


@pytest.mark.parametrize(
    ("api_key", "admin_key", "expected"),
    [
        pytest.param(LEGACY_TOKEN, "", {LEGACY_TOKEN: ("service", {"read", "admin"})}, id="legacy"),
        pytest.param(
            READ_TOKEN,
            ADMIN_TOKEN,
            {READ_TOKEN: ("service-read", {"read"}), ADMIN_TOKEN: ("admin", {"read", "admin"})},
            id="admin-key",
        ),
        pytest.param("", ADMIN_TOKEN, {ADMIN_TOKEN: ("admin", {"read", "admin"})}, id="admin-key-only"),
    ],
)
def test_the_two_keys_map_to_their_principals(api_key: str, admin_key: str, expected: dict[str, Any]) -> None:
    table = principals.PrincipalTable.from_values(api_key=api_key, admin_key=admin_key, bearers=())

    assert len(table) == len(expected)
    for token, (principal_id, scopes) in expected.items():
        principal = table.resolve(token)
        assert principal is not None
        assert (principal.id, set(principal.scopes)) == (principal_id, scopes)
    assert table.resolve("") is None


def test_a_token_that_stands_for_two_principals_is_refused() -> None:
    with pytest.raises(errors.KnowledgeConfigError) as same_keys:
        principals.PrincipalTable.from_values(api_key=READ_TOKEN, admin_key=READ_TOKEN, bearers=())
    with pytest.raises(errors.KnowledgeConfigError) as same_bearers:
        principals.PrincipalTable.from_values(
            api_key="", admin_key="", bearers=[_bearer("a", OPS_TOKEN, "read"), _bearer("b", OPS_TOKEN, "read", "admin")]
        )

    assert same_keys.value.code == same_bearers.value.code == errors.E_MANIFEST_INVALID
    _assert_no_token(str(same_keys.value), str(same_bearers.value))


def test_the_table_never_shows_a_token() -> None:
    table = principals.PrincipalTable.from_values(api_key=READ_TOKEN, admin_key=ADMIN_TOKEN, bearers=())

    _assert_no_token(repr(table), repr(table.principals()), repr(_bearer("x", OPS_TOKEN, "read")))


@pytest.mark.parametrize(
    ("header", "token"),
    [
        (None, None),
        ("", None),
        ("Basic abc", None),
        ("Bearer ", None),
        ("Bearer   ", None),
        (f"Bearer {READ_TOKEN}", READ_TOKEN),
        (f"Bearer  {READ_TOKEN} ", READ_TOKEN),
    ],
)
def test_bearer_token_parsing(header: str | None, token: str | None) -> None:
    assert principals.bearer_token(header) == token


# ---------------------------------------------------------------------------
# bearers in vaults.yaml, MCP_ADMIN_KEY and the startup checks
# ---------------------------------------------------------------------------


def _manifest(tmp_path: Path, bearers: str) -> Path:
    path = tmp_path / "vaults.yaml"
    path.write_text(
        f"version: 1\ndata_dir: {tmp_path / 'data'}\nvaults:\n  - slug: alpha\n    path: {ALPHA_VAULT}\n{bearers}",
        encoding="utf-8",
    )
    return path


def _load(tmp_path: Path, bearers: str = "", **env: str) -> settings.Settings:
    base = {
        "KNOWLEDGE_EMBEDDINGS_PROVIDER": "bag-of-words",
        "MCP_TRANSPORT": "http",
        "KNOWLEDGE_VAULTS": str(_manifest(tmp_path, bearers)),
    }
    return settings.load_settings({**base, **env}, project_root=tmp_path)


_TWO_BEARERS = (
    "bearers:\n"
    "  - id: person\n    token_env: SCOPES_PERSON_TOKEN\n"
    "  - id: ops\n    token_env: SCOPES_OPS_TOKEN\n    scopes: [read, admin]\n"
)


def test_bearers_parse_with_read_as_the_default_scope(tmp_path: Path) -> None:
    s = _load(tmp_path, _TWO_BEARERS, SCOPES_PERSON_TOKEN=PERSON_TOKEN, SCOPES_OPS_TOKEN=OPS_TOKEN)

    assert [(b.id, b.token_env, set(b.scopes)) for b in s.bearers] == [
        ("person", "SCOPES_PERSON_TOKEN", {"read"}),
        ("ops", "SCOPES_OPS_TOKEN", {"read", "admin"}),
    ]
    assert s.validate() is not None
    _assert_no_token(repr(s))


@pytest.mark.parametrize(
    ("bearers", "where"),
    [
        pytest.param("bearers: []\n", "bearers", id="empty-list"),
        pytest.param("bearers: {id: x}\n", "bearers", id="not-a-list"),
        pytest.param("bearers:\n  - id: Bad_Id\n    token_env: T\n", "bearers[0].id", id="bad-id"),
        pytest.param("bearers:\n  - id: a\n    token_env: T\n  - id: a\n    token_env: U\n", "bearers[1].id", id="duplicate-id"),
        pytest.param("bearers:\n  - id: a\n", "bearers[0].token_env", id="no-token-env"),
        pytest.param("bearers:\n  - id: a\n    token_env: not-a-name\n", "bearers[0].token_env", id="bad-token-env"),
        pytest.param("bearers:\n  - id: a\n    token_env: T\n    scopes: [write]\n", "bearers[0].scopes", id="unknown-scope"),
        pytest.param("bearers:\n  - id: a\n    token_env: T\n    scopes: [admin]\n", "bearers[0].scopes", id="admin-without-read"),
        pytest.param("bearers:\n  - id: a\n    token_env: T\n    scopes: []\n", "bearers[0].scopes", id="no-scopes"),
        pytest.param("bearers:\n  - id: a\n    token_env: T\n    role: admin\n", "bearers[0].role", id="unknown-key"),
    ],
)
def test_an_invalid_bearer_entry_names_its_key(tmp_path: Path, bearers: str, where: str) -> None:
    with pytest.raises(errors.KnowledgeConfigError) as exc_info:
        _load(tmp_path, bearers)

    assert exc_info.value.code == errors.E_MANIFEST_INVALID
    assert where in exc_info.value.message


def test_a_bearer_whose_token_is_unset_stops_http(tmp_path: Path) -> None:
    s = _load(tmp_path, _TWO_BEARERS, SCOPES_PERSON_TOKEN=PERSON_TOKEN, SCOPES_OPS_TOKEN="")

    with pytest.raises(errors.KnowledgeConfigError) as exc_info:
        s.validate()

    assert exc_info.value.code == errors.E_SECRET_MISSING
    assert "SCOPES_OPS_TOKEN" in exc_info.value.message
    assert "SCOPES_PERSON_TOKEN" not in exc_info.value.message
    _assert_no_token(str(exc_info.value))


def test_two_bearers_with_one_token_stop_http(tmp_path: Path) -> None:
    s = _load(tmp_path, _TWO_BEARERS, SCOPES_PERSON_TOKEN=OPS_TOKEN, SCOPES_OPS_TOKEN=OPS_TOKEN)

    with pytest.raises(errors.KnowledgeConfigError) as exc_info:
        s.validate()

    assert exc_info.value.code == errors.E_MANIFEST_INVALID
    assert "duplicate bearer value" in exc_info.value.message
    _assert_no_token(str(exc_info.value))


def test_an_admin_key_equal_to_the_api_key_stops_http(tmp_path: Path) -> None:
    s = _load(tmp_path, MCP_API_KEY=READ_TOKEN, MCP_ADMIN_KEY=READ_TOKEN)

    with pytest.raises(errors.KnowledgeConfigError) as exc_info:
        s.validate()

    assert exc_info.value.code == errors.E_MANIFEST_INVALID
    assert "MCP_ADMIN_KEY equals MCP_API_KEY" in exc_info.value.message
    _assert_no_token(str(exc_info.value))


@pytest.mark.parametrize(
    ("keys", "ok"),
    [
        pytest.param({}, False, id="none"),
        pytest.param({"MCP_API_KEY": LEGACY_TOKEN}, True, id="api-key"),
        pytest.param({"MCP_ADMIN_KEY": ADMIN_TOKEN}, True, id="admin-key-only"),
        pytest.param({"MCP_API_KEY": READ_TOKEN, "MCP_ADMIN_KEY": ADMIN_TOKEN}, True, id="both"),
    ],
)
def test_http_needs_at_least_one_bearer(tmp_path: Path, keys: dict[str, str], ok: bool) -> None:
    s = _load(tmp_path, **keys)

    if ok:
        s.validate()
    else:
        with pytest.raises(errors.KnowledgeConfigError) as exc_info:
            s.validate()
        assert exc_info.value.code == errors.E_HTTP_API_KEY_MISSING


def test_stdio_ignores_bearers_with_a_warning(tmp_path: Path) -> None:
    s = _load(tmp_path, _TWO_BEARERS, MCP_TRANSPORT="stdio", SCOPES_PERSON_TOKEN="", SCOPES_OPS_TOKEN="")

    warnings = s.validate()

    assert any(w.startswith(f"{errors.W_BEARERS_IGNORED_STDIO}:") for w in warnings)


def test_stdio_ignores_the_two_keys_silently(tmp_path: Path) -> None:
    s = _load(tmp_path, MCP_TRANSPORT="stdio", MCP_API_KEY=READ_TOKEN, MCP_ADMIN_KEY=READ_TOKEN)

    warnings = s.validate()

    assert not any(errors.W_BEARERS_IGNORED_STDIO in w for w in warnings)


# ---------------------------------------------------------------------------
# doctor: the auth block names ids, scopes and variables, never a token
# ---------------------------------------------------------------------------


@pytest.fixture
def doctor_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    from knowledge import cli

    monkeypatch.setattr(cli, "load_dotenv", lambda *_a, **_k: False)
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("KNOWLEDGE_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("VAULT_PATH", "")
    return tmp_path


def _doctor(capsys: pytest.CaptureFixture[str], manifest: Path) -> tuple[int, dict[str, Any], str]:
    from knowledge import cli

    rc = cli.main(["doctor", "--vaults", str(manifest)])
    out = capsys.readouterr().out
    return rc, json.loads(out), out


def test_doctor_reports_the_bearers_of_an_http_server(
    doctor_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MCP_TRANSPORT", "http")
    monkeypatch.setenv("SCOPES_PERSON_TOKEN", PERSON_TOKEN)
    monkeypatch.setenv("SCOPES_OPS_TOKEN", OPS_TOKEN)

    rc, report, out = _doctor(capsys, _manifest(doctor_home, _TWO_BEARERS))

    assert rc == 0, report["problems"]
    assert report["auth"] == {
        "transport": "http",
        "source": "bearers",
        "bearers": 2,
        "admin": 1,
        "read_only": 1,
        "principals": [
            {"id": "person", "scopes": ["read"], "token_env": "SCOPES_PERSON_TOKEN", "token_set": True},
            {"id": "ops", "scopes": ["admin", "read"], "token_env": "SCOPES_OPS_TOKEN", "token_set": True},
        ],
    }
    _assert_no_token(out)


def test_doctor_flags_a_missing_bearer_token_over_http(
    doctor_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MCP_TRANSPORT", "http")
    monkeypatch.setenv("SCOPES_PERSON_TOKEN", PERSON_TOKEN)
    monkeypatch.setenv("SCOPES_OPS_TOKEN", "")

    rc, report, out = _doctor(capsys, _manifest(doctor_home, _TWO_BEARERS))

    assert rc == 1
    assert [p["code"] for p in report["problems"]] == [errors.E_SECRET_MISSING]
    assert report["auth"]["bearers"] == 1 and report["auth"]["admin"] == 0
    _assert_no_token(out)


def test_doctor_over_stdio_only_warns_about_bearers(
    doctor_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SCOPES_PERSON_TOKEN", "")
    monkeypatch.setenv("SCOPES_OPS_TOKEN", "")

    rc, report, _out = _doctor(capsys, _manifest(doctor_home, _TWO_BEARERS))

    assert rc == 0
    assert report["auth"]["transport"] == "stdio"
    assert any(w.startswith(errors.W_BEARERS_IGNORED_STDIO) for w in report["warnings"])


def test_doctor_reports_the_two_keys(
    doctor_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MCP_TRANSPORT", "http")
    monkeypatch.setenv("MCP_API_KEY", READ_TOKEN)
    monkeypatch.setenv("MCP_ADMIN_KEY", ADMIN_TOKEN)

    rc, report, out = _doctor(capsys, _manifest(doctor_home, ""))

    assert rc == 0
    assert report["auth"]["source"] == "keys"
    assert [(p["id"], p["token_env"]) for p in report["auth"]["principals"]] == [
        ("admin", "MCP_ADMIN_KEY"),
        ("service-read", "MCP_API_KEY"),
    ]
    _assert_no_token(out)
