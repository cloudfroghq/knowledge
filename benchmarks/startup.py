"""Process-level scenarios: import time, ``doctor --brief`` and the stdio server.

Every sample is a fresh interpreter, so nothing here shares state with the
in-process tool benchmarks. Import time is measured inside the child around the
``import`` statement (interpreter start-up excluded); ``doctor --brief`` and the
``initialize`` handshake are wall clock from spawn, the way a client sees them.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from typing import Any, BinaryIO

from benchmarks.common import summarize

PROTOCOL_VERSION = "2024-11-05"
_IMPORT_SNIPPET = "import time;t=time.perf_counter();import {module};print((time.perf_counter()-t)*1000.0)"
_DOCTOR_SNIPPET = "import sys;from knowledge import cli;sys.exit(cli.main(['doctor','--brief']))"
_SPAWN_TIMEOUT_S = 90.0


def _run(argv: list[str], env: Mapping[str, str]) -> tuple[float, str]:
    started = time.perf_counter()
    done = subprocess.run(argv, env=dict(env), capture_output=True, text=True, timeout=_SPAWN_TIMEOUT_S, check=False)
    return (time.perf_counter() - started) * 1000.0, done.stdout


def interpreter_floor(runs: int, env: Mapping[str, str]) -> dict[str, Any]:
    """``python -c pass``: the cost of starting the interpreter, the floor under every wall-clock probe."""
    return summarize([_run([sys.executable, "-c", "pass"], env)[0] for _ in range(runs)])


def import_time(module: str, runs: int, env: Mapping[str, str]) -> dict[str, Any]:
    """Milliseconds ``import <module>`` takes in a fresh interpreter, measured in the child."""
    values = []
    for _ in range(runs):
        _, out = _run([sys.executable, "-c", _IMPORT_SNIPPET.format(module=module)], env)
        values.append(float(out.strip().splitlines()[-1]))
    return summarize(values)


def doctor_brief(runs: int, env: Mapping[str, str]) -> dict[str, Any]:
    """Wall clock of ``cloudfrog-knowledge doctor --brief`` (what the SessionStart hook runs)."""
    return summarize([_run([sys.executable, "-c", _DOCTOR_SNIPPET], env)[0] for _ in range(runs)])


class StdioServer:
    """``python -m knowledge.server`` over stdio, driven with JSON-RPC lines."""

    def __init__(self, env: Mapping[str, str]) -> None:
        self._env = dict(env)
        self._next_id = 0
        self.spawned_at = 0.0
        self.proc: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        self.spawned_at = time.perf_counter()
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "knowledge.server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=self._env,
        )
        watchdog = threading.Timer(_SPAWN_TIMEOUT_S, self.proc.kill)
        watchdog.daemon = True
        watchdog.start()
        self._watchdog = watchdog

    def _stdin(self) -> BinaryIO:
        assert self.proc is not None and self.proc.stdin is not None
        return self.proc.stdin

    def _stdout(self) -> BinaryIO:
        assert self.proc is not None and self.proc.stdout is not None
        return self.proc.stdout

    def notify(self, method: str) -> None:
        self._stdin().write(json.dumps({"jsonrpc": "2.0", "method": method}).encode() + b"\n")
        self._stdin().flush()

    def rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        request = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}
        self._stdin().write(json.dumps(request).encode() + b"\n")
        self._stdin().flush()
        while True:
            line = self._stdout().readline()
            if not line:
                raise RuntimeError(f"the server closed stdout while answering {method}")
            message = json.loads(line)
            if message.get("id") == self._next_id:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return message

    def initialize(self) -> float:
        """Milliseconds from spawn to the ``initialize`` response."""
        self.rpc(
            "initialize",
            {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "bench", "version": "0"}},
        )
        elapsed = (time.perf_counter() - self.spawned_at) * 1000.0
        self.notify("notifications/initialized")
        return elapsed

    def stop(self) -> None:
        if self.proc is None:
            return
        self._watchdog.cancel()
        try:
            self._stdin().close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self._stdout().close()


def initialize_time(runs: int, env: Mapping[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Spawn-to-``initialize`` and the first ``tools/list`` after it, over ``runs`` fresh servers."""
    first: list[float] = []
    listing: list[float] = []
    for _ in range(runs):
        server = StdioServer(env)
        server.start()
        try:
            first.append(server.initialize())
            started = time.perf_counter()
            server.rpc("tools/list", {})
            listing.append((time.perf_counter() - started) * 1000.0)
        finally:
            server.stop()
    return summarize(first), summarize(listing)


def stdio_round_trips(calls: int, queries: list[str], env: Mapping[str, str]) -> dict[str, Any]:
    """``tools/call`` round trips over a real stdio server: ``list_vaults`` and ``search``.

    The index must exist already (the harness builds it first); the first search
    is measured on its own because it also loads the index into the process.
    """
    server = StdioServer(env)
    server.start()
    results: dict[str, Any] = {}
    try:
        server.initialize()

        def call(name: str, arguments: dict[str, Any]) -> float:
            started = time.perf_counter()
            server.rpc("tools/call", {"name": name, "arguments": arguments})
            return (time.perf_counter() - started) * 1000.0

        results["stdio.search.first_call"] = summarize([call("search", {"query": queries[0], "limit": 5})])
        for _ in range(10):
            call("search", {"query": queries[0], "limit": 5})
        results["stdio.search"] = summarize(
            [call("search", {"query": queries[i % len(queries)], "limit": 5}) for i in range(calls)]
        )
        results["stdio.list_vaults"] = summarize([call("list_vaults", {}) for _ in range(calls)])
    finally:
        server.stop()
    return results
