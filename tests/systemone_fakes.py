"""Loopback double of a System One server (TypeSafe's Jev, a local Kev), for the judge of ``search``.

A standard-library ``ThreadingHTTPServer`` on ``127.0.0.1`` and an ephemeral
port that speaks HTTP/1.1 with keep-alive, answers ``POST /v1/systemone``
the way System One does and counts the connections it accepts, so a test can
tell one kept-alive connection from one per call.

What it answers is decided per request, never by state shared between
requests: a marker in ``state.query`` picks the mode, so one session can mix
modes (the rerank goldens do) without any test reaching into the server.

* no marker: 200 with one ``noul`` per question, the Jaccard similarity of
  the terms of the query and of the passage, rounded to 2 decimals;
  ``usage.input_tokens`` is the estimate production budgets with
  (:func:`billed_tokens`).
* ``[401]``, ``[403]``, ``[422]``, ``[429]``, ``[500]``, ``[529]``: that
  status, with a JSON body the client must never echo.
* ``[302]``: a redirect to another path of this server, which records any
  request that follows it.
* ``[garbage]``: 200 with a body that is not JSON.
* ``[range]``: 200 with a ``noul`` of 1.5; ``[missing]``: 200 without the
  answer to the last question; ``[nousage]``: 200 without ``usage``;
  ``[badtokens]``: 200 with negative ``input_tokens``; ``[list]``: 200 with a
  JSON list instead of an object.
* ``[huge]``: 200 with a ``Content-Length`` over 256 KB; ``[huge-stream]``:
  the same body chunked, with no length announced.
* ``[slow]``: 200 whose body trickles in one byte every :attr:`drip_s`
  seconds.
* ``[delay]``: the plain answer after :attr:`delay_s` seconds, for the
  concurrency tests (never a golden: time would enter the result).
* ``[drop]``: the connection closes without an answer.

With :attr:`drop_idle`, the server closes every connection right after its
answer without saying so (no ``Connection: close``): what a provider does
with a kept-alive connection that sat idle too long.

Nothing here reaches TypeSafe or a Kev server: ``conftest`` fails any
connection off the loopback.
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

API_KEY = "fake-systemone-key-not-a-credential"
PATH = "/v1/systemone"
#: Body of every refusal; a client that echoes it leaks what the provider said.
REFUSAL_TEXT = "provider text the client must not echo"
_MARKER = re.compile(r"\[([a-z0-9-]+)\]")
_STATUS_MODES = {"401": 401, "403": 403, "422": 422, "429": 429, "500": 500, "529": 529}
_TERM = re.compile(r"\w+", re.UNICODE)
_HUGE_BYTES = 300 * 1024


def billed_tokens(state: Mapping[str, Any], questions: int) -> int:
    """Input tokens of one request, as production estimates them before sending.

    ``ceil(chars / 2.95) + 50 * questions + 300`` over the compact JSON of
    ``state`` written without ASCII escapes: the estimator calibrated against
    what Jev billed. The double bills exactly that, so a budget test and
    production agree on the number.
    """
    chars = len(json.dumps(state, ensure_ascii=False, separators=(",", ":")))
    return math.ceil(chars / 2.95) + 50 * questions + 300


def _terms(text: str) -> set[str]:
    return set(_TERM.findall(_MARKER.sub(" ", text).lower()))


def jaccard(query: str, text: str) -> float:
    """The deterministic ``noul`` of the double: shared terms over all terms, 2 decimals."""
    q, t = _terms(query), _terms(text)
    union = q | t
    return round(len(q & t) / len(union), 2) if union else 0.0


def mode_of(query: str) -> str:
    """The mode a query asks for: its first marker, or ``ok``."""
    found = _MARKER.search(query)
    return found.group(1) if found else "ok"


def shape_problem(body: Any) -> str | None:
    """Why ``body`` is not a System One request with one ``noul`` per candidate; ``None`` when it is."""
    if not isinstance(body, dict) or set(body) != {"model", "state", "questions"}:
        return "the body holds model, state and questions only"
    if not isinstance(body["model"], str) or not body["model"]:
        return "model is a non-empty string"
    state = body["state"]
    if not isinstance(state, dict) or set(state) != {"query", "candidates"} or not isinstance(state["query"], str):
        return "state holds query and candidates"
    candidates = state["candidates"]
    if not isinstance(candidates, list) or not candidates:
        return "state.candidates is a non-empty list"
    for item in candidates:
        if not isinstance(item, dict) or set(item) != {"document", "section", "text"}:
            return "each candidate holds document, section and text"
        if not all(isinstance(item[k], str) for k in item):
            return "each candidate field is a string"
    questions = body["questions"]
    if not isinstance(questions, dict) or sorted(questions) != sorted(f"c{i}" for i in range(len(candidates))):
        return "questions has one key c<i> per candidate"
    for key, question in questions.items():
        if not isinstance(question, dict) or question.get("type") != "noul":
            return "every question is a noul"
        if f"candidates[{key[1:]}]" not in str(question.get("instructions", "")):
            return "every question names its candidate"
        criteria = question.get("criteria")
        if not isinstance(criteria, dict) or set(criteria) != {"true", "false"}:
            return "every question carries true and false criteria"
    return None


def answer(body: Mapping[str, Any]) -> dict[str, Any]:
    """The 200 answer of the double for a well-formed request ``body``."""
    state = body["state"]
    query = state["query"]
    candidates = state["candidates"]
    answers = {f"c{i}": {"type": "noul", "noul": jaccard(query, item["text"])} for i, item in enumerate(candidates)}
    return {
        "model": body["model"],
        "answers": answers,
        "usage": {"input_tokens": billed_tokens(state, len(candidates)), "output_tokens": 20 * len(candidates)},
    }


@dataclass
class FakeSystemOne:
    """What the double expects and what it saw."""

    #: The bearer it accepts; ``None`` accepts any request, with or without one.
    api_key: str | None = API_KEY
    delay_s: float = 0.3
    drip_s: float = 0.2
    drop_idle: bool = False
    requests: list[dict[str, Any]] = field(default_factory=list)
    connections: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def bodies(self) -> list[Any]:
        return [r["body"] for r in self.requests if r["path"] == PATH]


def _handler(state: FakeSystemOne) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            del format, args

        def setup(self) -> None:
            super().setup()
            with state.lock:
                state.connections += 1

        def _send(self, status: int, raw: bytes, headers: Mapping[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(raw)
            self.wfile.flush()

        def do_GET(self) -> None:  # noqa: N802
            with state.lock:
                state.requests.append({"path": self.path, "headers": dict(self.headers), "body": None})
            self._send(404, b'{"detail": "no route"}')

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            try:
                body: Any = json.loads(raw.decode("utf-8"))
            except ValueError:
                body = None
            with state.lock:
                state.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
            if self.path != PATH:
                self._send(404, b'{"detail": "no route"}')
                return
            if state.api_key is not None and self.headers.get("Authorization") != f"Bearer {state.api_key}":
                self._send(401, json.dumps({"detail": REFUSAL_TEXT}).encode())
                return
            problem = shape_problem(body)
            if problem is not None:
                self._send(422, json.dumps({"detail": problem}).encode())
                return
            self._serve(mode_of(body["state"]["query"]), body)
            if state.drop_idle:
                self.close_connection = True

        def _serve(self, mode: str, body: dict[str, Any]) -> None:
            if mode in _STATUS_MODES:
                self._send(_STATUS_MODES[mode], json.dumps({"detail": REFUSAL_TEXT}).encode())
                return
            if mode == "302":
                port = self.server.server_address[1]
                self._send(302, b"{}", {"Location": f"http://127.0.0.1:{port}/elsewhere"})
                return
            if mode == "drop":
                self.close_connection = True
                return
            if mode == "delay":
                time.sleep(state.delay_s)
            payload = answer(body)
            if mode == "garbage":
                self._send(200, b"<html>not json")
                return
            if mode == "range":
                payload["answers"]["c0"]["noul"] = 1.5
            elif mode == "missing":
                payload["answers"].pop(f"c{len(payload['answers']) - 1}")
            elif mode == "nousage":
                payload.pop("usage")
            elif mode == "badtokens":
                payload["usage"]["input_tokens"] = -1
            elif mode == "list":
                self._send(200, json.dumps([payload]).encode("utf-8"))
                return
            elif mode in ("huge", "huge-stream"):
                payload["padding"] = "x" * _HUGE_BYTES
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if mode == "huge-stream":
                self._stream(raw)
            elif mode == "slow":
                self._drip(raw)
            else:
                self._send(200, raw)

        def _stream(self, raw: bytes) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                for start in range(0, len(raw), 8192):
                    piece = raw[start : start + 8192]
                    self.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
            except OSError:
                self.close_connection = True

        def _drip(self, raw: bytes) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.flush()
            try:
                for byte in raw:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    time.sleep(state.drip_s)
            except OSError:
                pass
            self.close_connection = True

    return Handler


@contextmanager
def systemone_server(state: FakeSystemOne | None = None) -> Iterator[tuple[str, FakeSystemOne]]:
    """``state`` served on the loopback; yields the origin (``http://127.0.0.1:<port>``) and the state."""
    state = state if state is not None else FakeSystemOne()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler(state))
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", state
    finally:
        httpd.shutdown()
        httpd.server_close()
