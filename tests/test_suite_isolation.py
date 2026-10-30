"""The suite's hermetic guards hold: no real memory, Jira, Confluence or embeddings API."""

from __future__ import annotations

import os
import socket
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from conftest import MEMORY_PORT, MemoryConnectionRefused, NetworkConnectionRefused


def test_a_connection_to_the_memory_port_fails() -> None:
    with socket.socket() as sock, pytest.raises(MemoryConnectionRefused):
        sock.connect(("127.0.0.1", MEMORY_PORT))


def test_connect_ex_to_the_memory_port_fails() -> None:
    with socket.socket() as sock, pytest.raises(MemoryConnectionRefused):
        sock.connect_ex(("localhost", MEMORY_PORT))


def test_an_http_client_cannot_reach_the_memory_port() -> None:
    with pytest.raises((MemoryConnectionRefused, urllib.error.URLError)):
        urllib.request.urlopen(f"http://127.0.0.1:{MEMORY_PORT}/search?q=x", timeout=0.2)  # noqa: S310


@pytest.mark.parametrize("host", ["93.184.215.14", "10.0.0.5", "2001:db8::1", "example.invalid"])
def test_a_connection_outside_the_loopback_fails(host: str) -> None:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family) as sock, pytest.raises(NetworkConnectionRefused):
        sock.connect((host, 443))
    with socket.socket(family) as sock, pytest.raises(NetworkConnectionRefused):
        sock.connect_ex((host, 443))


def test_an_http_client_cannot_leave_the_machine() -> None:
    """The refusal is not an ``OSError``, so ``urllib`` cannot turn it into a ``URLError``."""
    with pytest.raises(NetworkConnectionRefused):
        urllib.request.urlopen("http://192.0.2.1/", timeout=0.2)  # noqa: S310


def test_integration_credentials_are_pinned_empty() -> None:
    for name in (
        "CONFLUENCE_URL",
        "CONFLUENCE_EMAIL",
        "CONFLUENCE_TOKEN",
        "MEMORY_HTTP_TOKEN",
        "KNOWLEDGE_MEMORY_URL",
        "KNOWLEDGE_MEMORY_REQUIRE_TOKEN",
    ):
        assert os.environ.get(name) == "", name
    leaked = [n for n in os.environ if ("JIRA" in n.split("_") or "CONFLUENCE" in n.split("_")) and os.environ[n]]
    assert leaked == []


def test_other_ports_are_not_blocked() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        with socket.socket() as client:
            client.connect(listener.getsockname())


@pytest.mark.usefixtures("_fake_openai_embeddings")
def test_the_suite_embeds_with_the_bag_of_words_provider() -> None:
    """The fixture asserts the provider; reaching this line is the check."""


def test_the_memory_command_never_runs_the_developers_binary() -> None:
    """``MEMORY_BIN`` names a path that does not exist: ``memory-bridge promote`` cannot reach real memory."""
    path = Path(os.environ["MEMORY_BIN"])
    assert path.is_absolute() and not path.exists()
