"""Shared fixtures for the knowledge-mcp test suite.

The suite runs against the versioned fixture vault in ``tests/fixtures/vault/``
and a disposable index directory, never against the real Obsidian vault
under ``vault/``. ``VAULT_PATH`` / ``CHROMA_PERSIST_DIR`` point at them through
the environment (``CHROMA_PERSIST_DIR`` is still the index directory of a
``VAULT_PATH`` deployment). Importing ``knowledge.config`` or ``knowledge.server`` has no
effect: the settings snapshot is built on first read and the server's vault
runtime (SQLite index, indexer, usage tracker) on first use, both from the
environment set here. The variables that could redirect the suite to a
deployment's own files (``KNOWLEDGE_CONFIG_JSON``, ``KNOWLEDGE_HOME``,
``KNOWLEDGE_INDEX_DIR``, ``KNOWLEDGE_VAULTS``, ``VAULT_REMOTE``) or to a developer's memory server
(``MEMORY_HTTP_TOKEN``, ``KNOWLEDGE_MEMORY_URL``, ``KNOWLEDGE_MEMORY_REQUIRE_TOKEN``)
are pinned empty, and so are the keys and switches of a rerank judge
(``TYPESAFE_API_KEY``, ``KEV_API_KEY`` and every ``*_TYPESAFE_API_KEY`` or
``*_KEV_API_KEY`` of the developer's shell, ``KNOWLEDGE_RERANK*``), so no key
or operator setting reaches a test and nothing in the suite can address a
judge outside the loopback interface, which reads as absent and keeps a ``.env`` from filling them
in. Without a memory token every vault's memory client refuses to call, and the
memory-bridge refuses to write. ``MEMORY_BIN`` points at a path that does not
exist, so a promotion command never runs the developer's own ``memory`` binary
against their database: a test that needs one writes a fake and points at it. The bearers of the HTTP surface
(``MCP_API_KEY``, ``MCP_ADMIN_KEY``) are pinned empty too: a test that needs
one sets it, so a developer's own keys never decide which scope a test holds.

``_fresh_config_facade`` drops the settings snapshot and every value a test
assigned on ``knowledge.config`` before and after each test, so a patched or
reloaded configuration never leaks into the next one.

Two global isolations keep the suite hermetic, so the same run is
reproducible on a developer laptop and on the Linux ring-1 pipeline:

* Embeddings come from the ``bag-of-words`` provider
  (``knowledge.embeddings.BagOfWordsEmbeddings``), selected through
  ``KNOWLEDGE_EMBEDDINGS_PROVIDER`` like any deployment selects its own, so the
  suite never needs an API key and never makes a network call. The stdio
  server behind the golden master selects the same provider, so both
  processes embed with the same vectors.
* ``gitsync._run_git`` is neutralised. ``reindex(target="vault")`` pulls the
  repository that contains a vault that declares one (``VAULT_REMOTE``, pinned
  empty here, or ``repo`` in ``vaults.yaml``), and the fixture vault lives
  inside this repository — a test that declared one without this guard would
  run ``git pull --ff-only`` against the developer's own checkout.

* No test opens a connection to port 6366, where a developer's local memory
  server listens, nor to any host outside the loopback interface
  (``_no_network``). The suite never reads or writes real memory, Jira or
  Confluence; a test that needs an endpoint stands up its own on
  ``127.0.0.1`` and an ephemeral port. Every Jira and Confluence variable of
  the developer's shell (``CONFLUENCE_*``, ``*_JIRA_*``, ``*_CONFLUENCE_*``)
  is pinned empty as well, so no credential reaches a test by accident.

Several cases call ``importlib.reload`` on ``knowledge.config`` to pick up
patched Confluence variables. A reload only drops the snapshot, so the next
read re-reads the environment; nothing the suite touches is built from the
repository registry.
"""

from __future__ import annotations

import ipaddress
import os
import re
import shutil
import socket
import tempfile
from pathlib import Path

import pytest

FIXTURE_VAULT = Path(__file__).parent / "fixtures" / "vault"
FIXTURE_CONFIG = Path(__file__).parent / "fixtures" / "config.json"
#: Variables of a Jira or Confluence integration, whatever their prefix
#: (``JIRA_API_TOKEN``, ``GLOBEX_JIRA_TOKEN``, ``CONFLUENCE_URL``).
_INTEGRATION_ENV = re.compile(r"(?:^|_)(?:JIRA|CONFLUENCE)(?:_|$)")
#: Keys and operator switches of a rerank judge, whatever their vault prefix
#: (``TYPESAFE_API_KEY``, ``INITECH_KEV_API_KEY``, ``KNOWLEDGE_RERANK_PROVIDERS``).
_RERANK_ENV = re.compile(r"^(?:KNOWLEDGE_RERANK(?:_[A-Z_]+)?|(?:[A-Z0-9]+_)*(?:TYPESAFE|KEV)_API_KEY)$")
_CHROMA_DIR = Path(tempfile.mkdtemp(prefix="knowledge-mcp-tests-")) / "chroma"
#: ``MEMORY_BIN`` for the whole suite: a path that never exists, so nothing
#: runs the developer's ``memory`` command.
MEMORY_BIN_DISABLED = str(_CHROMA_DIR.parent / "memory-disabled-in-tests")


def _configure_env() -> None:
    if not FIXTURE_VAULT.is_dir():
        raise RuntimeError(f"fixture vault is missing: {FIXTURE_VAULT}")
    os.environ["VAULT_PATH"] = str(FIXTURE_VAULT)
    os.environ["CHROMA_PERSIST_DIR"] = str(_CHROMA_DIR)
    # The static configuration is the suite's own: no config.json belongs to the
    # repository, and a deployment's file (or a developer's) must never reach a test.
    os.environ["KNOWLEDGE_CONFIG_JSON"] = str(FIXTURE_CONFIG)
    for name in (
        "KNOWLEDGE_HOME",
        "KNOWLEDGE_INDEX_DIR",
        "KNOWLEDGE_VAULTS",
        "VAULT_REMOTE",
        "MCP_API_KEY",
        "MCP_ADMIN_KEY",
        "MEMORY_HTTP_TOKEN",
        "KNOWLEDGE_MEMORY_URL",
        "KNOWLEDGE_MEMORY_REQUIRE_TOKEN",
        "KNOWLEDGE_INTEGRATION_SITES",
        "CONFLUENCE_URL",
        "CONFLUENCE_EMAIL",
        "CONFLUENCE_TOKEN",
    ):
        os.environ[name] = ""
    for name in [n for n in os.environ if _INTEGRATION_ENV.search(n)]:
        os.environ[name] = ""
    for name in (
        "TYPESAFE_API_KEY",
        "KEV_API_KEY",
        "KNOWLEDGE_RERANK",
        "KNOWLEDGE_RERANK_PROVIDERS",
        "KNOWLEDGE_RERANK_BASE_URL",
        "KNOWLEDGE_RERANK_TIMEOUT_MS",
        *(n for n in os.environ if _RERANK_ENV.fullmatch(n)),
    ):
        os.environ[name] = ""
    # Assigned, not defaulted: a developer shell exporting another provider
    # must never send the fixture vault to a real embeddings API.
    os.environ["KNOWLEDGE_EMBEDDINGS_PROVIDER"] = "bag-of-words"
    os.environ["MEMORY_BIN"] = MEMORY_BIN_DISABLED
    os.environ["MCP_TRANSPORT"] = "stdio"


_configure_env()

# Read the configuration once while the suite is collected: an unreadable
# config.json stops collection here, with its coded reason, instead of
# surfacing later as unrelated assertion failures.
from knowledge import config as _config  # noqa: E402

_config.CONFIG_JSON_LOADED  # noqa: B018


@pytest.fixture(autouse=True)
def _fresh_config_facade():
    """Re-read the configuration for every test, before and after it.

    Cleaning on both sides matters: ``monkeypatch`` restores a patched
    ``knowledge.config`` name by assigning its old value back, which can land
    after this fixture's teardown and would otherwise leak into the next test.
    """
    from knowledge import settings

    def _clean() -> None:
        settings.reset_current()
        for name in _config._LEGACY:
            _config.__dict__.pop(name, None)

    _clean()
    yield
    _clean()


@pytest.fixture(scope="session", autouse=True)
def _cleanup_chroma_dir():
    yield
    shutil.rmtree(_CHROMA_DIR.parent, ignore_errors=True)


@pytest.fixture(autouse=True)
def _no_git_subprocess(monkeypatch: pytest.MonkeyPatch):
    """Make every git call from the server a no-op that reports "not a worktree".

    ``pull_vault()`` short-circuits on ``is_git_worktree() is False``, so this
    single patch covers the whole module: no test can shell out to git against
    the repository that holds the fixture vault.
    """
    from knowledge import gitsync

    def _refuse(args: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
        return 128, "", "git disabled in tests"

    monkeypatch.setattr(gitsync, "_run_git", _refuse)


#: Port of a developer's local memory server. Nothing in the suite may reach it.
MEMORY_PORT = 6366


class MemoryConnectionRefused(AssertionError):
    """A test tried to open a connection to the real memory port."""


class NetworkConnectionRefused(AssertionError):
    """A test tried to open a connection to a host outside the loopback interface."""


def _is_local(host: object) -> bool:
    """Whether ``host`` (an address or a name, as ``connect`` receives it) stays on this machine."""
    if not isinstance(host, str):
        return False
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback or address.is_unspecified


def _refusal(address: object) -> AssertionError | None:
    """Why the suite refuses to connect to ``address``, or ``None`` when it may."""
    if not isinstance(address, tuple) or len(address) < 2:
        return None  # a Unix socket path or another non-IP family
    if address[1] == MEMORY_PORT:
        return MemoryConnectionRefused(f"the suite never connects to port {MEMORY_PORT}")
    if not _is_local(address[0]):
        return NetworkConnectionRefused(
            f"the suite never connects outside the loopback interface (asked for {address[0]!r}); "
            "stand up a fake server on 127.0.0.1 instead"
        )
    return None


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch):
    """Fail any connection to port 6366 or to a host that is not loopback.

    Patches the socket methods every client in the standard library goes
    through (``connect`` and ``connect_ex``), so ``urllib``, ``http.client``
    and raw sockets are all covered without naming a client that may not
    exist yet. Servers a test starts on ``127.0.0.1`` and an ephemeral port
    are untouched, and so are Unix sockets. The refusal is an
    ``AssertionError``: a client that catches ``OSError`` cannot swallow it.
    """
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def _guard_connect(self: socket.socket, address: object) -> None:
        refusal = _refusal(address)
        if refusal is not None:
            raise refusal
        return real_connect(self, address)  # type: ignore[arg-type]

    def _guard_connect_ex(self: socket.socket, address: object) -> int:
        refusal = _refusal(address)
        if refusal is not None:
            raise refusal
        return real_connect_ex(self, address)  # type: ignore[arg-type]

    monkeypatch.setattr(socket.socket, "connect", _guard_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _guard_connect_ex)


@pytest.fixture()
def _fake_openai_embeddings():
    """Assert the server embeds with the bag-of-words provider, never a real API.

    Nothing is patched: the provider is chosen by ``KNOWLEDGE_EMBEDDINGS_PROVIDER``
    in ``_configure_env``. This fails loudly if that selection is ever lost,
    instead of letting an indexing test reach for a network provider.
    """
    from knowledge import server

    embedder = server.default_runtime().embedder
    assert embedder.name == "bag-of-words", (
        f"the suite must embed with the bag-of-words provider, got {embedder.name!r}"
    )


TypeSpec = type | tuple[type, ...]


def _shape(
    payload: dict,
    *,
    required: dict[str, TypeSpec],
    optional: dict[str, TypeSpec] | None = None,
    where: str = "payload",
) -> None:
    """Freeze the shape of one response object.

    Two directions, both of which matter for an MCP contract:

    * every required key is present and carries the declared type — a client
      that relies on it keeps working;
    * no key outside ``required | optional`` appears — a field added to a tool
      without updating this freeze fails the suite instead of silently
      reaching every consumer of the server.
    """
    optional = optional or {}
    assert isinstance(payload, dict), f"{where}: expected a dict, got {type(payload).__name__}"

    missing = sorted(k for k in required if k not in payload)
    assert not missing, f"{where}: missing required key(s) {missing} in {sorted(payload)}"

    unexpected = sorted(set(payload) - set(required) - set(optional))
    assert not unexpected, (
        f"{where}: unfrozen key(s) {unexpected} — a tool's response grew; "
        "extend the freeze deliberately or drop the field"
    )

    for key, expected in {**required, **optional}.items():
        if key in payload:
            assert isinstance(payload[key], expected), (
                f"{where}[{key!r}]: expected {expected}, got {type(payload[key]).__name__}"
            )


@pytest.fixture()
def assert_shape():
    """Expose ``_shape`` as a fixture so it resolves regardless of import mode."""
    return _shape


@pytest.fixture()
def indexed_server(_fake_openai_embeddings):
    """The live ``knowledge.server`` module with its vault index warmed up.

    Tools index lazily on first call, and several of them (``find_path``,
    ``get_related``, ``get_impact``, ``suggest_links``) read the wiki-link
    graph that indexing builds. Warming it here keeps each test's assertions
    about the graph, not about lazy-init ordering.
    """
    from knowledge import server

    rt = server.default_runtime()
    rt.indexer.index(force=False, target="vault")
    rt.index_state["ready"] = True
    return server


@pytest.fixture()
def server_on_temp_vault(indexed_server, tmp_path, monkeypatch):
    """``knowledge.server`` re-pointed at a writable copy of the fixture vault.

    A test that needs an extra document has to add it somewhere: never inside
    ``tests/fixtures/vault/``, which is versioned and shared with every other
    test. This hands over a throwaway copy, and restores both the vault path
    and the shared index afterwards even when the test fails mid-way.
    """
    from knowledge import config

    temp_vault = tmp_path / "vault"
    shutil.copytree(FIXTURE_VAULT, temp_vault)
    rt = indexed_server.default_runtime()
    original = rt.vault_path
    rt.retarget(temp_vault)
    monkeypatch.setattr(config, "VAULT_PATH", temp_vault)
    try:
        rt.indexer.index(force=True, target="vault")
        yield indexed_server
    finally:
        rt.retarget(original)
        rt.indexer.index(force=True, target="vault")
