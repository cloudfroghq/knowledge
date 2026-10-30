"""System One judge (``jev`` at TypeSafe, ``kev`` on this machine), standard library only.

``search`` reranks its candidates with the judge a vault declares under
``rerank`` in its ``vault.yaml``. Both providers speak the same System One
API, ``POST /v1/systemone``, so one adapter serves them; this module is the
only place that speaks to it, and ``knowledge.server`` loads it through the
``rerank`` entry of :mod:`knowledge.extensions` only when a vault declares a
judge.

One request per query: ``state`` holds the query and every passage
(``{"query", "candidates": [{"document", "section", "text"}]}``) and
``questions`` asks one ``noul`` question per passage, keyed ``c0``, ``c1``…
by position. The answer is the probability that the passage answers the
query; ``usage.input_tokens`` is what the provider billed.

The rules that hold whatever the provider answers:

* ``jev`` calls ``https://api.typesafe.ai/v1/systemone`` and nothing else;
  ``kev`` only a plain ``http`` server on a loopback host. Any other URL is
  refused when the judge is built, so a vault can never aim a key at a host
  of its choosing. ``http.client`` reads no proxy variable and follows no
  redirect: a 3xx is a failure like any other status.
* One persistent connection per calling thread (``threading.local``), never
  a lock shared between threads: concurrent searches each keep their own
  TLS session instead of queueing behind one. A connection the server
  dropped while idle is reopened once, inside the same deadline.
* ``timeout_s`` is a hard deadline on the whole call: name resolution,
  connection, TLS, request and response all run in a helper thread the
  caller waits on with ``Future.result(timeout)``. A response that trickles
  in byte by byte, or a resolver that never answers, still returns
  ``timeout`` on time; the abandoned helper drops its connection.
* A body over :data:`MAX_BODY_BYTES` is refused unread.
* Failures raise :class:`~knowledge.ports.JudgeUnavailable` with a reason:
  ``auth`` (401, 403), ``unavailable`` (429, 529, any other status, a refused
  or dropped connection), ``timeout`` and ``invalid_response`` (a body that
  is not one ``noul`` in [0, 1] per question with its ``usage``). Messages
  name the failure class only: never the URL, the request, the body the
  provider sent back or the key. ``repr`` leaves the key out.
"""

from __future__ import annotations

import http.client
import json
import logging
import math
import select
import socket
import ssl
import threading
import time
import urllib.parse
from collections.abc import Mapping, Sequence
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any

from knowledge import contract
from knowledge.ports import Judge, JudgeUnavailable, JudgeUnit, JudgeVerdict, NullJudge
from knowledge.settings import (
    TYPESAFE_ORIGIN,
    RerankOperator,
    RerankSettings,
    header_token_valid,
    is_loopback_http_url,
)

log = logging.getLogger(__name__)

#: Path of the System One endpoint, on TypeSafe and on a local kev server alike.
SYSTEMONE_PATH = "/v1/systemone"
#: The one URL a ``jev`` judge calls outside a loopback test double.
TYPESAFE_URL = f"{TYPESAFE_ORIGIN}{SYSTEMONE_PATH}"
#: Where a kev server listens when the vault names no ``base_url``.
KEV_ORIGIN = "http://127.0.0.1:8787"
#: Largest response body read; a longer one is ``invalid_response``.
MAX_BODY_BYTES = 256 * 1024
#: Bytes asked for per read, so the deadline is checked while a body trickles in.
_READ_CHUNK = 16 * 1024
#: Who is calling, and under which tool contract.
CLIENT_ID = f"cloudfrog-knowledge/{contract.CONTRACT_VERSION}"

#: Version of the question below. A change of wording is a new version: a
#: cache keyed on it never mixes answers to two different questions.
PROMPT_VERSION = "rr-noul-v1"
#: The question asked of every passage; ``{i}`` is its position in ``candidates``.
INSTRUCTIONS = "Does `candidates[{i}]` contain the information that answers `query`?"
#: What a yes and a no mean.
CRITERIA: Mapping[str, str] = {
    "true": "The passage states the specific fact, cause, command or procedure the query asks for.",
    "false": "The passage is only on a related topic, mentions the terms in passing, or does not answer the query.",
}

#: US dollars per million input tokens, by pinned model; output tokens are free.
#: A kev model runs on this machine and costs nothing.
PRICES_USD_PER_MTOK: Mapping[str, float] = {"jev-1.13.0": 0.042}

#: Errors that mean a reused connection was closed by the server while idle.
_DROPPED = (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError, ConnectionAbortedError)


def cost_usd(model: str, input_tokens: int) -> float:
    """What ``input_tokens`` of ``model`` cost, in US dollars.

    A kev model is free. A jev model missing from :data:`PRICES_USD_PER_MTOK`
    is priced at the highest known rate, so a spending cap errs on the safe side.
    """
    if model.startswith("kev-"):
        return 0.0
    rate = PRICES_USD_PER_MTOK.get(model, max(PRICES_USD_PER_MTOK.values()))
    return input_tokens * rate / 1_000_000


def _provider_of(model: str) -> str:
    return "kev" if model.startswith("kev-") else "jev"


def _endpoint(url: str, provider: str) -> urllib.parse.SplitResult:
    """``url`` split, with the System One path when it names only an origin.

    ``jev`` accepts TypeSafe over ``https`` or a loopback double over plain
    ``http``; ``kev`` accepts only the loopback. Raises ``ValueError``, with a
    message that never repeats the URL, for anything else.
    """
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.path in ("", "/"):
        parsed = parsed._replace(path=SYSTEMONE_PATH)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise ValueError("a System One URL carries no credentials, query or fragment")
    if parsed.path != SYSTEMONE_PATH:
        raise ValueError(f"a System One URL ends in {SYSTEMONE_PATH}")
    if is_loopback_http_url(origin):
        return parsed
    if provider == "jev" and origin == TYPESAFE_ORIGIN:
        return parsed
    allowed = "an http URL on a loopback host" if provider == "kev" else f"{TYPESAFE_URL} or a loopback http URL"
    raise ValueError(f"the {provider} judge only calls {allowed}")


@dataclass
class _Slot:
    """The connection of one calling thread, and the warm-up running on it, if any."""

    conn: http.client.HTTPConnection | None = None
    warming: Future[None] | None = None


@dataclass
class _Call:
    """One judge call handed to a helper thread, and whether its caller gave up on it."""

    conn: http.client.HTTPConnection
    warming: Future[None] | None
    deadline: float
    timeout_s: float
    lock: threading.Lock = field(default_factory=threading.Lock)
    abandoned: bool = False


class SystemOneJudge:
    """One System One model behind one URL, judging every passage of a query in one request."""

    prompt_version = PROMPT_VERSION

    def __init__(
        self,
        url: str,
        model: str,
        api_key: str = "",
        *,
        provider: str | None = None,
        client_id: str = "",
    ) -> None:
        self.name = provider or _provider_of(model)
        if self.name not in ("jev", "kev"):
            raise ValueError("a System One judge is jev or kev")
        if not model.strip():
            raise ValueError("a System One judge needs a pinned model")
        self.model = model.strip()
        parsed = _endpoint(url, self.name)
        self._scheme = parsed.scheme
        self._host = parsed.hostname or ""
        self._port = parsed.port
        self._path = parsed.path
        self._url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if api_key and not header_token_valid(api_key):
            raise ValueError("the judge key holds whitespace or characters a bearer cannot carry")
        self._key = api_key
        agent = CLIENT_ID
        if client_id and header_token_valid(client_id):
            agent = f"{CLIENT_ID} ({client_id})"
        self._agent = agent
        self._local = threading.local()
        self._tls = ssl.create_default_context() if self._scheme == "https" else None

    def __repr__(self) -> str:
        return f"SystemOneJudge(provider={self.name!r}, model={self.model!r}, url={self._url!r})"

    # -- request ----------------------------------------------------------------

    def request_body(self, query: str, units: Sequence[JudgeUnit]) -> dict[str, Any]:
        """The JSON body of one call: every unit in ``state``, one ``noul`` question per unit."""
        return {
            "model": self.model,
            "state": {
                "query": query,
                "candidates": [{"document": u.document, "section": u.section, "text": u.text} for u in units],
            },
            "questions": {
                f"c{i}": {"type": "noul", "instructions": INSTRUCTIONS.format(i=i), "criteria": dict(CRITERIA)}
                for i in range(len(units))
            },
        }

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": self._agent}
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        return headers

    # -- connections ------------------------------------------------------------

    def _new_connection(self, timeout_s: float) -> http.client.HTTPConnection:
        if self._tls is not None:
            # A direct connection on purpose: no proxy, no redirect, and a TLS
            # context from ssl.create_default_context() that verifies the
            # certificate and the host name.
            # nosemgrep: python.lang.security.audit.httpsconnection-detected.httpsconnection-detected
            return http.client.HTTPSConnection(self._host, self._port, timeout=timeout_s, context=self._tls)
        return http.client.HTTPConnection(self._host, self._port, timeout=timeout_s)

    def _slot(self) -> _Slot:
        slot = getattr(self._local, "slot", None)
        if slot is None:
            slot = _Slot()
            self._local.slot = slot
        return slot

    @staticmethod
    def _stale(conn: http.client.HTTPConnection) -> bool:
        """Whether an open, idle connection was closed by the server (it reads as ready: EOF or an alert)."""
        sock = conn.sock
        if sock is None:
            return False
        poll = getattr(select, "poll", None)
        try:
            if poll is not None:
                # poll(), not select(): select() refuses descriptors at or above
                # FD_SETSIZE (1024), which a busy HTTP server reaches.
                poller = poll()
                poller.register(sock, select.POLLIN | select.POLLERR | select.POLLHUP)
                return bool(poller.poll(0))
            readable, _, _ = select.select([sock], [], [], 0)
        except OSError:
            return True
        return bool(readable)

    def prewarm(self, timeout_s: float = 1.5) -> None:
        """Open this thread's connection (DNS, TCP, TLS) in the background and return at once.

        ``search`` calls it as soon as it knows it will rerank, so the
        handshake overlaps the embedding of the query. The next
        :meth:`judge` on the same thread waits for it within its own
        deadline; a failed warm-up only means that call connects itself.
        """
        slot = self._slot()
        if slot.warming is not None and not slot.warming.done():
            return
        conn = slot.conn
        if conn is not None and conn.sock is not None and not self._stale(conn):
            return
        if conn is not None:
            conn.close()
        conn = self._new_connection(timeout_s)
        slot.conn = conn
        warming: Future[None] = Future()
        slot.warming = warming

        def _connect() -> None:
            try:
                conn.connect()
            except BaseException as exc:  # noqa: BLE001 - handed to the waiting judge call
                conn.close()
                warming.set_exception(exc)
            else:
                warming.set_result(None)

        threading.Thread(target=_connect, name="systemone-prewarm", daemon=True).start()

    # -- the call ---------------------------------------------------------------

    def judge(self, query: str, units: Sequence[JudgeUnit], *, timeout_s: float) -> JudgeVerdict:
        """Score every unit against ``query`` in one request, within ``timeout_s`` seconds in all.

        Raises :class:`~knowledge.ports.JudgeUnavailable` when the provider
        cannot answer; see the module docstring for the reasons.
        """
        if not units:
            return JudgeVerdict(scores={}, input_tokens=0, model=self.model)
        keys = [unit.key for unit in units]
        if len(set(keys)) != len(keys):
            raise ValueError("every unit a judge scores needs its own key")
        body = json.dumps(self.request_body(query, units), ensure_ascii=False).encode("utf-8")

        slot = self._slot()
        conn = slot.conn if slot.conn is not None else self._new_connection(timeout_s)
        # The socket is not touched here: a warm-up may still be in its TLS
        # handshake, during which the plain socket is already detached. The
        # helper sets the timeout once the warm-up is over.
        call = _Call(conn=conn, warming=slot.warming, deadline=time.monotonic() + timeout_s, timeout_s=timeout_s)
        # The helper owns the connection until it hands it back; a caller
        # that times out never sees it again.
        slot.conn, slot.warming = None, None
        result: Future[Mapping[str, Any]] = Future()

        def _run() -> None:
            try:
                outcome = self._exchange(call, body)
            except BaseException as exc:  # noqa: BLE001 - re-raised by the caller
                with call.lock:
                    call.conn.close()
                    if not call.abandoned:
                        result.set_exception(exc)
                return
            with call.lock:
                if call.abandoned:
                    call.conn.close()
                else:
                    result.set_result(outcome)

        threading.Thread(target=_run, name="systemone-judge", daemon=True).start()
        try:
            payload = result.result(timeout=max(0.0, call.deadline - time.monotonic()))
        except TimeoutError:
            # Either the deadline passed, or the helper itself raised a
            # timeout: concurrent.futures.TimeoutError is the builtin.
            with call.lock:
                if not result.done():
                    call.abandoned = True
                    _abort(call.conn)
                    log.debug("system one judge: deadline of %.3f s reached", timeout_s)
                    raise JudgeUnavailable("timeout", f"the {self.name} judge did not answer in time") from None
            failure = result.exception()
            if isinstance(failure, TimeoutError):
                raise JudgeUnavailable("timeout", f"the {self.name} judge did not answer in time") from None
            if failure is not None:
                raise failure from None
            payload = result.result()
        slot.conn = call.conn
        return self._verdict(payload, keys)

    def _exchange(self, call: _Call, body: bytes) -> Mapping[str, Any]:
        """Send ``body`` on the call's connection and return the parsed answer; runs on the helper thread."""
        if call.warming is not None:
            try:
                call.warming.result(timeout=max(0.0, call.deadline - time.monotonic()))
            except TimeoutError:
                raise JudgeUnavailable("timeout", f"the {self.name} judge did not answer in time") from None
            except (OSError, http.client.HTTPException):
                call.conn.close()
        call.conn.timeout = call.timeout_s
        if call.conn.sock is not None:
            try:
                call.conn.sock.settimeout(call.timeout_s)
            except OSError:
                call.conn.close()
        reused = call.conn.sock is not None
        if reused and self._stale(call.conn):
            call.conn.close()
            reused = False
        try:
            response = self._send(call.conn, body)
        except _DROPPED:
            if not reused:
                raise JudgeUnavailable("unavailable", f"the {self.name} judge closed the connection") from None
            # The server closed the kept-alive connection; one fresh try.
            call.conn.close()
            try:
                response = self._send(call.conn, body)
            except _DROPPED:
                raise JudgeUnavailable("unavailable", f"the {self.name} judge closed the connection") from None
        return self._read(call, response)

    def _send(self, conn: http.client.HTTPConnection, body: bytes) -> http.client.HTTPResponse:
        try:
            conn.request("POST", self._path, body=body, headers=self._headers())
            return conn.getresponse()
        except _DROPPED:
            raise
        except TimeoutError:
            raise JudgeUnavailable("timeout", f"the {self.name} judge did not answer in time") from None
        except (OSError, http.client.HTTPException) as exc:
            log.debug("system one judge: %s while sending", type(exc).__name__)
            raise JudgeUnavailable("unavailable", f"the {self.name} judge could not be reached") from None

    def _read(self, call: _Call, response: http.client.HTTPResponse) -> Mapping[str, Any]:
        status = response.status
        if status in (401, 403):
            call.conn.close()
            raise JudgeUnavailable("auth", f"the {self.name} judge refused the key (HTTP {status})")
        if status != 200:
            call.conn.close()
            raise JudgeUnavailable("unavailable", f"the {self.name} judge answered HTTP {status}")
        declared = response.getheader("Content-Length")
        if declared is not None and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            call.conn.close()
            raise JudgeUnavailable("invalid_response", f"the {self.name} judge sent a body over {MAX_BODY_BYTES} bytes")
        chunks: list[bytes] = []
        size = 0
        try:
            while True:
                if time.monotonic() > call.deadline:
                    call.conn.close()
                    raise JudgeUnavailable("timeout", f"the {self.name} judge did not answer in time")
                chunk = response.read1(_READ_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_BODY_BYTES:
                    call.conn.close()
                    raise JudgeUnavailable(
                        "invalid_response", f"the {self.name} judge sent a body over {MAX_BODY_BYTES} bytes"
                    )
                chunks.append(chunk)
        except TimeoutError:
            call.conn.close()
            raise JudgeUnavailable("timeout", f"the {self.name} judge did not answer in time") from None
        except (OSError, http.client.HTTPException) as exc:
            log.debug("system one judge: %s while reading", type(exc).__name__)
            call.conn.close()
            raise JudgeUnavailable("unavailable", f"the {self.name} judge dropped the answer") from None
        try:
            payload = json.loads(b"".join(chunks))
        except (UnicodeDecodeError, ValueError):
            raise JudgeUnavailable("invalid_response", f"the {self.name} judge sent a body that is not JSON") from None
        if not isinstance(payload, dict):
            raise JudgeUnavailable("invalid_response", f"the {self.name} judge sent a body that is not an object")
        return payload

    def _verdict(self, payload: Mapping[str, Any], keys: Sequence[str]) -> JudgeVerdict:
        answers = payload.get("answers")
        usage = payload.get("usage")
        if not isinstance(answers, Mapping) or not isinstance(usage, Mapping):
            raise JudgeUnavailable("invalid_response", f"the {self.name} judge sent no answers or usage")
        tokens = usage.get("input_tokens")
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
            raise JudgeUnavailable("invalid_response", f"the {self.name} judge sent no input_tokens")
        scores: dict[str, float] = {}
        for i, key in enumerate(keys):
            answer = answers.get(f"c{i}")
            value = answer.get("noul") if isinstance(answer, Mapping) else None
            kind = answer.get("type", "noul") if isinstance(answer, Mapping) else None
            if (
                kind != "noul"
                or isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
                or not 0.0 <= value <= 1.0
            ):
                raise JudgeUnavailable("invalid_response", f"the {self.name} judge sent no noul in [0, 1] for c{i}")
            scores[key] = float(value)
        model = payload.get("model")
        return JudgeVerdict(
            scores=scores, input_tokens=tokens, model=model if isinstance(model, str) and model else self.model
        )


def _abort(conn: http.client.HTTPConnection) -> None:
    """Unblock a helper thread stuck reading ``conn``: shut its socket down from the caller's side."""
    sock = conn.sock
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def build_judge(
    rerank: RerankSettings, operator: RerankOperator | None = None, *, client_id: str = ""
) -> Judge:
    """The judge vault settings ``rerank`` declare, as the operator allows it.

    A :class:`~knowledge.ports.NullJudge` when the vault declares none, when
    ``KNOWLEDGE_RERANK=off`` or ``KNOWLEDGE_RERANK_PROVIDERS`` rule its
    provider out, when the URL would be off the provider's hosts (``kev``
    only on the loopback) and when the key is missing for a URL off the
    loopback: an empty key never travels, and a test without one can only
    reach a loopback double. ``KNOWLEDGE_RERANK_BASE_URL`` replaces the URL
    of every provider; without it ``jev`` calls :data:`TYPESAFE_URL` and
    ``kev`` its ``base_url`` or :data:`KEV_ORIGIN`.
    """
    operator = operator or RerankOperator()
    if not rerank.configured or not operator.allows(rerank.provider):
        return NullJudge()
    if operator.base_url:
        origin = operator.base_url
    elif rerank.provider == "kev":
        origin = rerank.base_url or KEV_ORIGIN
    else:
        origin = TYPESAFE_ORIGIN
    if not rerank.api_key and not is_loopback_http_url(origin):
        return NullJudge()
    try:
        return SystemOneJudge(
            origin, rerank.model, rerank.api_key, provider=rerank.provider, client_id=client_id
        )
    except ValueError as exc:
        log.warning("rerank: %s; this vault searches without a judge", exc)
        return NullJudge()
