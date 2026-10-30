"""The memory server client against a fake memory server on an ephemeral port.

The fake is a standard-library ``http.server`` that behaves like the memory
server's ``GET /search``: it answers only a request that carries the bearer it
was started with and an ``X-Memory-Client`` header, and records every request
it receives so a test can prove that no call was made at all. Nothing here
reaches the real memory port (``conftest._no_network`` fails any
connection to it), and the last test proves that guard holds for this client.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from knowledge import contract, errors, memory_http
from knowledge.errors import MemoryUnavailable
from knowledge.settings import EmbeddingSettings, MemorySettings, VaultSpec

TOKEN = "fake-memory-bearer-not-a-credential"
PROJECT = "knowledge"

HITS = [
    {
        "id": 41,
        "sync_id": "s-41",
        "type": "decision",
        "title": "Middleware outage runbook owner",
        "content": "The platform team owns the middleware outage runbook.",
        "project": PROJECT,
        "rank": -3.2,
    },
    {"id": 7, "type": "discovery", "title": "Gateway retries", "content": "Retries double the load.", "rank": -1.1},
]


@pytest.fixture(autouse=True)
def _memory_logger_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the client's logger live, so the log assertions below see what it writes.

    Another test configuring logging (``logging.config`` with its default
    ``disable_existing_loggers``) can leave an already created logger
    disabled, and a disabled logger would make "the bearer is not logged"
    pass on an empty log.
    """
    monkeypatch.setattr(logging.getLogger("knowledge.memory_http"), "disabled", False)


@dataclass
class FakeMemory:
    """What the fake answers and what it has been asked."""

    mode: str = "ok"
    requests: list[dict[str, Any]] = field(default_factory=list)


def _handler(state: FakeMemory) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            del format, args

        def _send(self, status: int, payload: Any) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            url = urllib.parse.urlsplit(self.path)
            state.requests.append(
                {"path": url.path, "query": urllib.parse.parse_qs(url.query), "headers": dict(self.headers)}
            )
            # The checks the real server makes when a token is configured,
            # plus the client header this client always sends.
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                self._send(401, {"error": "invalid token"})
                return
            if not self.headers.get("X-Memory-Client"):
                self._send(403, {"error": "client_header_required"})
                return
            if state.mode == "slow":
                time.sleep(2.5)
            if state.mode == "null":
                self._send(200, None)
            elif state.mode == "error":
                self._send(500, {"error": "boom"})
            elif state.mode == "object":
                self._send(200, {"results": HITS})
            else:
                self._send(200, HITS)

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


def _spec(project: str | None = PROJECT) -> VaultSpec:
    return VaultSpec(
        slug="alpha",
        path=Path("."),
        is_default=True,
        index_dir=Path("."),
        memory_folder="90 - Memory",
        review_thresholds={"default": 270},
        thresholds_from_config=False,
        embeddings=EmbeddingSettings(provider="none"),
        memory_project=project,
    )


def _code(excinfo: pytest.ExceptionInfo[MemoryUnavailable]) -> str:
    return excinfo.value.code


# --- 200 -------------------------------------------------------------------------


def test_a_search_sends_the_bearer_and_the_client_header(fake_memory: tuple[FakeMemory, str]) -> None:
    state, url = fake_memory
    client = memory_http.memory_client_for(_spec(), settings=MemorySettings(url=url, token=TOKEN))

    hits = client.search("middleware outage", project=PROJECT, limit=5)

    assert [hit["id"] for hit in hits] == [41, 7]
    assert set(hits[0]) == {"id", "type", "title", "content", "rank"}
    (request,) = state.requests
    assert request["path"] == "/search"
    assert request["query"] == {"q": ["middleware outage"], "project": [PROJECT], "limit": ["5"]}
    assert request["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert request["headers"]["X-Memory-Client"] == f"cloudfrog-knowledge/{contract.CONTRACT_VERSION}"
    assert request["headers"]["Accept"] == "application/json"
    assert "Origin" not in request["headers"]


def test_null_from_the_memory_server_is_no_hits(fake_memory: tuple[FakeMemory, str]) -> None:
    """The memory server serialises an empty result as ``null``, not ``[]``."""
    state, url = fake_memory
    state.mode = "null"

    assert memory_http.HttpMemoryClient(url, TOKEN).search("nothing", project=PROJECT) == []


# --- failures ----------------------------------------------------------------------


def test_a_wrong_bearer_is_unavailable_and_never_logged(
    fake_memory: tuple[FakeMemory, str], caplog: pytest.LogCaptureFixture
) -> None:
    state, url = fake_memory
    other = "another-bearer-not-a-credential"
    caplog.set_level(logging.DEBUG, logger="knowledge.memory_http")

    with pytest.raises(MemoryUnavailable) as excinfo:
        memory_http.HttpMemoryClient(url, other).search("middleware outage", project=PROJECT)

    assert _code(excinfo) == errors.E_MEMORY_UNAVAILABLE
    assert "401" in str(excinfo.value)
    assert len(state.requests) == 1
    assert errors.E_MEMORY_UNAVAILABLE in caplog.text
    for text in (str(excinfo.value), caplog.text):
        assert other not in text and TOKEN not in text
        assert url not in text and "middleware" not in text


def test_a_server_error_is_unavailable(fake_memory: tuple[FakeMemory, str]) -> None:
    state, url = fake_memory
    state.mode = "error"

    with pytest.raises(MemoryUnavailable) as excinfo:
        memory_http.HttpMemoryClient(url, TOKEN).search("x", project=PROJECT)

    assert _code(excinfo) == errors.E_MEMORY_UNAVAILABLE


def test_a_body_that_is_not_a_list_is_unavailable(fake_memory: tuple[FakeMemory, str]) -> None:
    state, url = fake_memory
    state.mode = "object"

    with pytest.raises(MemoryUnavailable) as excinfo:
        memory_http.HttpMemoryClient(url, TOKEN).search("x", project=PROJECT)

    assert _code(excinfo) == errors.E_MEMORY_UNAVAILABLE


def test_a_hanging_server_times_out_in_about_a_second(fake_memory: tuple[FakeMemory, str]) -> None:
    state, url = fake_memory
    state.mode = "slow"
    started = time.monotonic()

    with pytest.raises(MemoryUnavailable) as excinfo:
        memory_http.HttpMemoryClient(url, TOKEN).search("x", project=PROJECT)

    assert _code(excinfo) == errors.E_MEMORY_UNAVAILABLE
    assert memory_http.TIMEOUT_SECONDS == 1.0
    assert time.monotonic() - started < 2.0


def test_a_refused_connection_is_unavailable(fake_memory: tuple[FakeMemory, str]) -> None:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]

    with pytest.raises(MemoryUnavailable) as excinfo:
        memory_http.HttpMemoryClient(f"http://127.0.0.1:{closed_port}", TOKEN).search("x", project=PROJECT)

    assert _code(excinfo) == errors.E_MEMORY_UNAVAILABLE


# --- no call at all ------------------------------------------------------------------


def test_without_a_bearer_the_factory_never_calls(fake_memory: tuple[FakeMemory, str]) -> None:
    state, url = fake_memory
    client = memory_http.memory_client_for(_spec(), settings=MemorySettings(url=url, token=""))

    with pytest.raises(MemoryUnavailable) as excinfo:
        client.search("x", project=PROJECT)

    assert _code(excinfo) == errors.E_MEMORY_TOKEN_MISSING
    assert isinstance(client, memory_http.UnconfiguredMemoryClient)
    assert state.requests == []


def test_the_client_itself_refuses_to_call_without_a_bearer(fake_memory: tuple[FakeMemory, str]) -> None:
    state, url = fake_memory

    with pytest.raises(MemoryUnavailable) as excinfo:
        memory_http.HttpMemoryClient(url, "").search("x", project=PROJECT)

    assert _code(excinfo) == errors.E_MEMORY_TOKEN_MISSING
    assert state.requests == []


@pytest.mark.parametrize("build", ["factory", "client"])
def test_without_a_project_nothing_is_called(fake_memory: tuple[FakeMemory, str], build: str) -> None:
    state, url = fake_memory
    if build == "factory":
        client = memory_http.memory_client_for(_spec(project=None), settings=MemorySettings(url=url, token=TOKEN))
    else:
        client = memory_http.HttpMemoryClient(url, TOKEN)

    with pytest.raises(MemoryUnavailable) as excinfo:
        client.search("x", project="")

    assert _code(excinfo) == errors.E_MEMORY_PROJECT_MISSING
    assert state.requests == []


@pytest.mark.parametrize(
    "url",
    ["http://10.0.0.5:6366", "https://127.0.0.1:6366", "http://memory.example:6366", "http://0.0.0.0:6366"],
)
def test_a_url_off_this_machine_disables_the_client_with_a_warning(
    url: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="knowledge.memory_http")

    client = memory_http.memory_client_for(_spec(), settings=MemorySettings(url=url, token=TOKEN))

    with pytest.raises(MemoryUnavailable) as excinfo:
        client.search("x", project=PROJECT)
    assert _code(excinfo) == errors.E_MEMORY_URL_NOT_LOOPBACK
    assert errors.E_MEMORY_URL_NOT_LOOPBACK in caplog.text
    assert TOKEN not in caplog.text


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "[::1]"])
def test_every_loopback_host_is_accepted(host: str) -> None:
    client = memory_http.memory_client_for(_spec(), settings=MemorySettings(url=f"http://{host}:1", token=TOKEN))

    assert isinstance(client, memory_http.HttpMemoryClient)


def test_the_bearer_is_not_in_the_client_repr() -> None:
    client = memory_http.HttpMemoryClient("http://127.0.0.1:1", TOKEN)

    assert TOKEN not in repr(client)
    assert TOKEN not in repr(MemorySettings(url="http://127.0.0.1:1", token=TOKEN))


@pytest.fixture()
def fake_proxy() -> Iterator[tuple[list[dict[str, Any]], str]]:
    """An HTTP proxy on an ephemeral port that records every request and answers 502, and its URL."""
    seen: list[dict[str, Any]] = []

    class Proxy(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            del format, args

        def do_GET(self) -> None:  # noqa: N802
            seen.append({"path": self.path, "headers": dict(self.headers)})
            self.send_response(502)
            self.send_header("Content-Length", "0")
            self.end_headers()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield seen, f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_a_proxy_in_the_environment_never_sees_the_bearer(
    fake_memory: tuple[FakeMemory, str],
    fake_proxy: tuple[list[dict[str, Any]], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loopback check is on the URL; the opener must connect where the URL says.

    With ``http_proxy`` set and no ``no_proxy``, ``urllib``'s default opener
    sends a request for ``127.0.0.1`` to the proxy, bearer included. The
    client's opener has no proxies, so the proxy receives nothing.
    """
    import urllib.request

    state, url = fake_memory
    proxied, proxy_url = fake_proxy
    for name in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, proxy_url)
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    # ``urlopen`` caches the opener it builds from the environment; drop it so
    # an implementation that goes through ``urlopen`` reads the proxy above.
    monkeypatch.setattr(urllib.request, "_opener", None)
    client = memory_http.memory_client_for(_spec(), settings=MemorySettings(url=url, token=TOKEN))

    hits = client.search("middleware outage", project=PROJECT)

    assert [hit["id"] for hit in hits] == [41, 7]
    assert len(state.requests) == 1
    assert proxied == []


# --- the suite's own guard -----------------------------------------------------------


def test_the_suite_never_reaches_the_real_memory_port() -> None:
    """A client pointed at the default memory URL is stopped by the suite, not by the network.

    The guard's error is not an ``OSError``, so the client does not turn it
    into ``E_MEMORY_UNAVAILABLE``: the test fails loudly instead of passing
    on a "memory is down" answer.
    """
    from conftest import MEMORY_PORT, MemoryConnectionRefused

    from knowledge.settings import DEFAULT_MEMORY_URL

    assert DEFAULT_MEMORY_URL.endswith(f":{MEMORY_PORT}")
    client = memory_http.memory_client_for(_spec(), settings=MemorySettings(url=DEFAULT_MEMORY_URL, token=TOKEN))

    with pytest.raises(MemoryConnectionRefused):
        client.search("x", project=PROJECT)


# --- context_pack over the fake --------------------------------------------------------


@pytest.fixture()
def pack_server(indexed_server: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The live server with the default vault's memory project set to :data:`PROJECT`."""
    rt = indexed_server.default_runtime()
    monkeypatch.setattr(rt, "spec", replace(rt.spec, memory_project=PROJECT))
    return indexed_server


def test_context_pack_fuses_memory_hits_inside_their_share(
    pack_server: Any, fake_memory: tuple[FakeMemory, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    state, url = fake_memory
    rt = pack_server.default_runtime()
    monkeypatch.setattr(rt, "memory", memory_http.HttpMemoryClient(url, TOKEN))
    budget = 1000

    pack = asyncio.run(pack_server.context_pack("middleware outage", budget_tokens=budget, include_memories=True))

    memory_items = [item for item in pack["items"] if item["source"] == "memory"]
    assert pack["sources_used"] == ["vault", "memory"]
    assert "degraded" not in pack
    assert [item["id"] for item in memory_items] == [41, 7]
    assert memory_items[0] == {
        "source": "memory",
        "id": 41,
        "type": "decision",
        "title": "Middleware outage runbook owner",
        "text": HITS[0]["content"],
    }
    # Reciprocal rank fusion: the first vault hit, then the first memory hit.
    assert [item["source"] for item in pack["items"][:2]] == ["vault", "memory"]
    memory_cost = sum(pack_server.estimate_tokens(item) for item in memory_items)
    assert memory_cost <= int(budget * pack_server.PACK_MEMORY_SHARE)
    assert pack["used_tokens"] <= budget
    assert state.requests[0]["query"]["project"] == [PROJECT]


def test_context_pack_reports_an_unreachable_memory_as_degraded(
    pack_server: Any, fake_memory: tuple[FakeMemory, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    _, url = fake_memory
    rt = pack_server.default_runtime()
    monkeypatch.setattr(rt, "memory", memory_http.HttpMemoryClient(url, "wrong-bearer-not-a-credential"))

    pack = asyncio.run(pack_server.context_pack("middleware outage", budget_tokens=1000, include_memories=True))

    assert pack["degraded"] == [{"source": "memory", "code": errors.E_MEMORY_UNAVAILABLE}]
    assert pack["sources_used"] == ["vault"]
    assert pack["items"]


def test_context_pack_never_asks_memory_unless_told(
    pack_server: Any, fake_memory: tuple[FakeMemory, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    state, url = fake_memory
    monkeypatch.setattr(pack_server.default_runtime(), "memory", memory_http.HttpMemoryClient(url, TOKEN))

    pack = asyncio.run(pack_server.context_pack("middleware outage", budget_tokens=1000))

    assert state.requests == []
    assert "degraded" not in pack


# --- configuration and wiring ------------------------------------------------------------


def _settings_env(vault: Path, **extra: str) -> dict[str, str]:
    return {"VAULT_PATH": str(vault), "KNOWLEDGE_EMBEDDINGS_PROVIDER": "none", **extra}


def test_settings_read_the_memory_url_and_bearer(tmp_path: Path) -> None:
    from knowledge import settings

    vault = tmp_path / "vault"
    vault.mkdir()

    configured = settings.load_settings(
        _settings_env(vault, MEMORY_HTTP_TOKEN=TOKEN, KNOWLEDGE_MEMORY_URL="http://localhost:7000"),
        project_root=tmp_path,
    )
    pinned_empty = settings.load_settings(
        _settings_env(vault, MEMORY_HTTP_TOKEN="", KNOWLEDGE_MEMORY_URL=""), project_root=tmp_path
    )

    assert configured.memory == MemorySettings(url="http://localhost:7000", token=TOKEN)
    assert pinned_empty.memory == MemorySettings(url=settings.DEFAULT_MEMORY_URL, token="")
    assert TOKEN not in repr(configured)


def test_the_server_registry_gives_each_vault_its_memory_client(tmp_path: Path) -> None:
    """``serve()`` and the lazy registry build runtimes through the memory factory."""
    from knowledge import server, settings

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "vault.yaml").write_text(f"slug: acme\nmemory:\n  project: {PROJECT}\n", encoding="utf-8")

    with_token = server._registry_for(
        settings.load_settings(_settings_env(vault, MEMORY_HTTP_TOKEN=TOKEN), project_root=tmp_path)
    )
    without = server._registry_for(settings.load_settings(_settings_env(vault), project_root=tmp_path))

    assert isinstance(with_token.default.memory, memory_http.HttpMemoryClient)
    assert isinstance(without.default.memory, memory_http.UnconfiguredMemoryClient)
    assert without.default.memory.code == errors.E_MEMORY_TOKEN_MISSING


@pytest.mark.parametrize("token", ["bearer-with\nnewline", "bearer with space", "bearer-ñ"], ids=["newline", "space", "non-ascii"])
def test_a_token_that_cannot_travel_in_a_header_is_never_sent_nor_quoted(
    fake_memory: tuple[FakeMemory, str], token: str
) -> None:
    state, url = fake_memory

    with pytest.raises(MemoryUnavailable) as excinfo:
        memory_http.HttpMemoryClient(url, token).search("middleware outage", project=PROJECT)

    assert excinfo.value.code == errors.E_MEMORY_TOKEN_MISSING
    assert token not in str(excinfo.value)
    assert state.requests == []
