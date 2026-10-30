"""Launch ``knowledge.server`` over stdio with the suite's test doubles installed.

``test_contract_golden`` records the tool contract exactly as an MCP client
receives it: a real ``initialize`` handshake, a real ``tools/list`` and real
``tools/call`` round trips over the stdio transport. That client has to talk
to a server process, and this module is that process. It is a plain script,
not a test module, so pytest never collects it; the golden test starts it
with ``sys.executable`` and the environment described below.

Three things are set before the server module is imported, and they mirror
``conftest.py`` one for one. Without them the subprocess would behave like a
production server, which is exactly what a hermetic test cannot have:

* ``KNOWLEDGE_EMBEDDINGS_PROVIDER`` is assigned ``bag-of-words``, so the
  server embeds with ``knowledge.embeddings.BagOfWordsEmbeddings``: no network
  and no key are involved and every vector is reproducible. It is assigned,
  not defaulted, so a caller's environment can never select a real provider;
* ``CHROMA_COLLECTION`` is assigned the slug the recorded responses carry
  (:data:`GOLDEN_VAULT_SLUG`), because the fixture vault has no ``vault.yaml``
  to name itself and the fallback slug of a nameless vault is not a tenant's;
* every ``git`` invocation is refused. ``reindex`` pulls the repository that
  contains the vault, and the fixture vault lives inside this repository, so
  an unguarded run would fast-forward the developer's own checkout;
* the calendar is frozen. ``search`` and ``get_stale_docs`` report
  ``age_days`` against today's date, which is the one input a golden master
  cannot carry. The date below is the clock every recorded response was
  computed against; moving it is a deliberate re-record, never a side effect.

Everything else the server reads comes from the environment the test sets:
``VAULT_PATH`` (the fixture vault, never the real one), ``KNOWLEDGE_INDEX_DIR``
or ``CHROMA_PERSIST_DIR`` (a throwaway index directory), empty Confluence credentials so ``sync_confluence`` answers with its
"not configured" report instead of reaching for the network, and the output
switches and tool profile (``KNOWLEDGE_STRUCTURED_OUTPUT``,
``KNOWLEDGE_TEXT_INDENT``, ``KNOWLEDGE_PROFILE``), empty unless a session
sets one: the ``structured`` session sets the first, the ``legacy`` session
the last.

A rerank judge is the one thing here that could leave the machine, and this
process does not inherit the network guard of ``conftest.py`` (it is another
interpreter). So the keys and switches of every judge (``TYPESAFE_API_KEY``,
``KEV_API_KEY``, any ``*_TYPESAFE_API_KEY`` or ``*_KEV_API_KEY``,
``KNOWLEDGE_RERANK*``) are pinned empty whatever the caller's shell exports,
and the same loopback-only guard is installed on ``socket``: any connection
off ``127.0.0.1``, ``::1`` or ``localhost`` fails. The rerank session hands
its loopback double's test key through :data:`GOLDEN_KEV_KEY_ENV`, which
becomes ``KEV_API_KEY`` after the pinning; nothing else can set it.
"""

from __future__ import annotations

import datetime as _dt
import ipaddress
import os
import re
import socket
import sys
from pathlib import Path

# The tests directory is ``sys.path[0]`` when this file runs as a script, but
# the package under test is not: resolve it the same way pyproject does.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

#: The date ``date.today()`` returns inside the server for the whole run.
FROZEN_TODAY = _dt.date(2026, 6, 1)

#: The slug the recorded responses carry (``list_vaults``, ``reindex``, ``context_pack``). The fixture
#: vault declares no ``vault.yaml``, so the served slug is whatever the environment names: it is
#: assigned, never inherited, and moving it is a deliberate re-record.
GOLDEN_VAULT_SLUG = "cloudfrog"

_REQUIRED_ENV = ("VAULT_PATH",)
#: One of these names the index directory; ``KNOWLEDGE_INDEX_DIR`` wins.
_INDEX_DIR_ENV = ("KNOWLEDGE_INDEX_DIR", "CHROMA_PERSIST_DIR")
#: The static configuration of the recording: the suite's own, never a deployment's. It carries the
#: review thresholds the recorded ``get_stale_docs`` rows were computed with.
FIXTURE_CONFIG = Path(__file__).resolve().parent / "fixtures" / "config.json"
_PINNED_EMPTY_ENV = (
    "KNOWLEDGE_HOME",
    "KNOWLEDGE_VAULTS",
    "VAULT_REMOTE",
    # No bearer, so no memory call: ``context_pack(include_memories=True)``
    # records its "memory not configured" answer, never a developer's memory.
    "MEMORY_HTTP_TOKEN",
    "KNOWLEDGE_MEMORY_URL",
    # No judge key and no operator switch: a rerank session sets its own.
    "TYPESAFE_API_KEY",
    "KEV_API_KEY",
    "KNOWLEDGE_RERANK",
    "KNOWLEDGE_RERANK_PROVIDERS",
    "KNOWLEDGE_RERANK_BASE_URL",
    "KNOWLEDGE_RERANK_TIMEOUT_MS",
)
#: Every judge key or operator switch, whatever its vault prefix.
_RERANK_ENV = re.compile(r"^(?:KNOWLEDGE_RERANK(?:_[A-Z_]+)?|(?:[A-Z0-9]+_)*(?:TYPESAFE|KEV)_API_KEY)$")
#: The test key of the rerank session's loopback double, copied to ``KEV_API_KEY``.
GOLDEN_KEV_KEY_ENV = "KNOWLEDGE_GOLDEN_KEV_API_KEY"


class _FrozenDate(_dt.date):
    """``datetime.date`` whose ``today()`` is pinned to :data:`FROZEN_TODAY`."""

    @classmethod
    def today(cls) -> _dt.date:  # type: ignore[override]
        return FROZEN_TODAY


def _refuse_git(args: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
    del args, timeout
    return 128, "", "git disabled in the golden stdio server"


def _is_local(host: object) -> bool:
    """Whether ``host`` (as ``connect`` receives it) stays on this machine."""
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
    return address.is_loopback


def _guard_sockets() -> None:
    """Fail every connection off the loopback interface, as ``conftest.py`` does in the test process."""
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def _refused(address: object) -> bool:
        return isinstance(address, tuple) and len(address) >= 2 and not _is_local(address[0])

    def _connect(self: socket.socket, address: object) -> None:
        if _refused(address):
            raise ConnectionRefusedError("the golden stdio server never connects off the loopback interface")
        return real_connect(self, address)  # type: ignore[arg-type]

    def _connect_ex(self: socket.socket, address: object) -> int:
        if _refused(address):
            raise ConnectionRefusedError("the golden stdio server never connects off the loopback interface")
        return real_connect_ex(self, address)  # type: ignore[arg-type]

    socket.socket.connect = _connect  # type: ignore[method-assign]
    socket.socket.connect_ex = _connect_ex  # type: ignore[method-assign]


def main() -> None:
    missing = [name for name in _REQUIRED_ENV if not os.environ.get(name)]
    if not any(os.environ.get(name) for name in _INDEX_DIR_ENV):
        missing.append(" or ".join(_INDEX_DIR_ENV))
    if missing:
        raise SystemExit(f"golden stdio server: missing environment {missing}")
    if not Path(os.environ["VAULT_PATH"]).is_dir():
        raise SystemExit(f"golden stdio server: VAULT_PATH is not a directory: {os.environ['VAULT_PATH']}")
    # Empty reads as absent, and a set variable is never overridden by a
    # ``.env``: nothing on the developer's machine can redirect the recording
    # to another config.json, data directory, vault or memory server.
    os.environ["KNOWLEDGE_CONFIG_JSON"] = str(FIXTURE_CONFIG)
    golden_kev_key = os.environ.pop(GOLDEN_KEV_KEY_ENV, "")
    for name in (*_PINNED_EMPTY_ENV, *(n for n in os.environ if _RERANK_ENV.fullmatch(n))):
        os.environ[name] = ""
    if golden_kev_key:
        os.environ["KEV_API_KEY"] = golden_kev_key
    _guard_sockets()
    for name in _INDEX_DIR_ENV:
        os.environ.setdefault(name, "")
    os.environ["KNOWLEDGE_EMBEDDINGS_PROVIDER"] = "bag-of-words"
    os.environ["CHROMA_COLLECTION"] = GOLDEN_VAULT_SLUG

    from knowledge import gitsync, review

    gitsync._run_git = _refuse_git  # type: ignore[assignment]
    review.date = _FrozenDate  # type: ignore[attr-defined,misc]

    from knowledge import server

    # The real entry point: ``MCP_TRANSPORT`` is ``stdio`` in the environment
    # the test passes, so this is the same branch ``python -m knowledge.server``
    # takes for a stdio client.
    server.main()


if __name__ == "__main__":
    main()
