"""The System One judge (``knowledge.judges``) against a loopback double.

Every request goes to ``tests/systemone_fakes.py`` on ``127.0.0.1``: no test
reaches TypeSafe or a Kev server, and ``conftest`` fails any connection that
tries. The exact body of a request is frozen in
``tests/fixtures/systemone/request.json`` and the double's answer to it in
``response.json``.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import resource
import socket
import ssl
import threading
import time
from pathlib import Path

import pytest

from knowledge import contract, judges
from knowledge.ports import Judge, JudgeUnavailable, JudgeUnit, NullJudge
from knowledge.settings import TYPESAFE_ORIGIN, RerankOperator, RerankSettings
from systemone_fakes import API_KEY, REFUSAL_TEXT, FakeSystemOne, billed_tokens, systemone_server

FIXTURES = Path(__file__).parent / "fixtures" / "systemone"
QUERY = "¿Cómo creo un tenant de prueba desde la CLI?"
UNITS = (
    JudgeUnit("a", "Crear tenant de prueba", "Pasos", "Ejecuta `docker compose exec api ./cli tenant create <sub>` para crear el tenant de prueba."),
    JudgeUnit("b", "Crear tenant de prueba", "Contexto", "El tenant de prueba vive en el stack local."),
    JudgeUnit("c", "Rotar certificados", "", "Los certificados se rotan cada noventa días con certbot."),
)


def _judge(origin: str, *, key: str = API_KEY, model: str = "jev-1.13.0") -> judges.SystemOneJudge:
    return judges.SystemOneJudge(origin, model, key, client_id="tests")


def _wait_for(predicate, timeout: float = 2.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# --- the request ---------------------------------------------------------------


def test_the_request_is_the_frozen_system_one_body() -> None:
    with systemone_server() as (origin, fake):
        verdict = _judge(origin).judge(QUERY, UNITS, timeout_s=2.0)

    expected_request = json.loads((FIXTURES / "request.json").read_text(encoding="utf-8"))
    expected_response = json.loads((FIXTURES / "response.json").read_text(encoding="utf-8"))
    assert fake.bodies == [expected_request]
    answers = expected_response["answers"]
    assert dict(verdict.scores) == {unit.key: answers[f"c{i}"]["noul"] for i, unit in enumerate(UNITS)}
    assert verdict.input_tokens == expected_response["usage"]["input_tokens"]
    assert verdict.model == expected_response["model"] == "jev-1.13.0"


def test_the_request_carries_the_bearer_and_names_the_client() -> None:
    with systemone_server() as (origin, fake):
        _judge(origin).judge(QUERY, UNITS[:1], timeout_s=2.0)

    headers = fake.requests[0]["headers"]
    assert fake.requests[0]["path"] == judges.SYSTEMONE_PATH
    assert headers["Authorization"] == f"Bearer {API_KEY}"
    assert headers["Content-Type"] == "application/json"
    assert headers["User-Agent"] == f"cloudfrog-knowledge/{contract.CONTRACT_VERSION} (tests)"


def test_the_body_keeps_non_ascii_text_as_utf8() -> None:
    with systemone_server() as (origin, fake):
        _judge(origin).judge(QUERY, UNITS[:1], timeout_s=2.0)

    assert fake.bodies[0]["state"]["query"] == QUERY


def test_the_billed_tokens_are_the_estimate_production_budgets_with() -> None:
    with systemone_server() as (origin, fake):
        verdict = _judge(origin).judge(QUERY, UNITS, timeout_s=2.0)

    assert verdict.input_tokens == billed_tokens(fake.bodies[0]["state"], len(UNITS))


def test_without_a_key_no_bearer_travels() -> None:
    with systemone_server(FakeSystemOne(api_key=None)) as (origin, fake):
        _judge(origin, key="").judge(QUERY, UNITS[:1], timeout_s=2.0)

    assert "Authorization" not in fake.requests[0]["headers"]


def test_no_units_means_no_request() -> None:
    with systemone_server() as (origin, fake):
        verdict = _judge(origin).judge(QUERY, (), timeout_s=2.0)

    assert dict(verdict.scores) == {} and verdict.input_tokens == 0
    assert fake.requests == [] and fake.connections == 0


def test_two_units_with_one_key_are_refused_before_any_request() -> None:
    with systemone_server() as (origin, fake):
        with pytest.raises(ValueError, match="own key"):
            _judge(origin).judge(QUERY, (UNITS[0], UNITS[0]), timeout_s=2.0)

    assert fake.requests == []


def test_the_judge_satisfies_the_port() -> None:
    judge = _judge("http://127.0.0.1:9")

    assert isinstance(judge, Judge)
    assert (judge.name, judge.model, judge.prompt_version) == ("jev", "jev-1.13.0", judges.PROMPT_VERSION)


# --- failures ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("marker", "reason"),
    [
        ("[401]", "auth"),
        ("[403]", "auth"),
        ("[422]", "unavailable"),
        ("[429]", "unavailable"),
        ("[500]", "unavailable"),
        ("[529]", "unavailable"),
        ("[drop]", "unavailable"),
        ("[garbage]", "invalid_response"),
        ("[range]", "invalid_response"),
        ("[missing]", "invalid_response"),
        ("[nousage]", "invalid_response"),
        ("[badtokens]", "invalid_response"),
        ("[list]", "invalid_response"),
        ("[huge]", "invalid_response"),
        ("[huge-stream]", "invalid_response"),
    ],
)
def test_each_failure_maps_to_its_reason_and_echoes_nothing(
    marker: str, reason: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="knowledge.judges")
    with systemone_server() as (origin, fake):
        with pytest.raises(JudgeUnavailable) as raised:
            _judge(origin).judge(f"{QUERY} {marker}", UNITS, timeout_s=2.0)

    assert raised.value.reason == reason
    port = origin.rsplit(":", 1)[1]
    for text in (str(raised.value), repr(raised.value), caplog.text):
        for secret in (API_KEY, REFUSAL_TEXT, port, "systemone", QUERY):
            assert secret not in text


def test_a_wrong_key_is_auth() -> None:
    with systemone_server() as (origin, _fake):
        with pytest.raises(JudgeUnavailable) as raised:
            _judge(origin, key="another-key").judge(QUERY, UNITS, timeout_s=2.0)

    assert raised.value.reason == "auth"
    assert "another-key" not in str(raised.value)


def test_a_redirect_is_never_followed() -> None:
    with systemone_server() as (origin, fake):
        with pytest.raises(JudgeUnavailable) as raised:
            _judge(origin).judge(f"{QUERY} [302]", UNITS, timeout_s=2.0)

    assert raised.value.reason == "unavailable"
    assert [r["path"] for r in fake.requests] == [judges.SYSTEMONE_PATH]


def test_a_refused_connection_is_unavailable() -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    with pytest.raises(JudgeUnavailable) as raised:
        _judge(f"http://127.0.0.1:{port}").judge(QUERY, UNITS, timeout_s=1.0)

    assert raised.value.reason == "unavailable"


def test_proxy_variables_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, "http://proxy.invalid:3128")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)

    with systemone_server() as (origin, fake):
        verdict = _judge(origin).judge(QUERY, UNITS, timeout_s=2.0)

    assert set(verdict.scores) == {"a", "b", "c"}
    assert len(fake.requests) == 1


def test_the_typesafe_connection_verifies_the_certificate_and_the_host() -> None:
    conn = _judge(judges.TYPESAFE_URL)._new_connection(1.0)

    assert isinstance(conn, http.client.HTTPSConnection)
    assert conn.host == "api.typesafe.ai"
    assert conn._tunnel_host is None
    assert conn._context.verify_mode == ssl.CERT_REQUIRED
    assert conn._context.check_hostname is True
    assert conn.sock is None


# --- the deadline --------------------------------------------------------------


def test_a_body_that_trickles_in_times_out_on_the_deadline() -> None:
    with systemone_server(FakeSystemOne(drip_s=0.2)) as (origin, fake):
        judge = _judge(origin)
        started = time.monotonic()
        with pytest.raises(JudgeUnavailable) as raised:
            judge.judge(f"{QUERY} [slow]", UNITS, timeout_s=0.5)
        elapsed = time.monotonic() - started

        assert raised.value.reason == "timeout"
        # The body drips one byte every 0.2 s, so a per-read timeout would wait
        # for tens of seconds; 1.5 s of slack above the deadline keeps the check
        # meaningful on a loaded machine.
        assert elapsed < 0.5 + 1.5
        # The abandoned connection is never reused: the next call opens its own.
        verdict = judge.judge(QUERY, UNITS, timeout_s=2.0)

    assert set(verdict.scores) == {"a", "b", "c"}
    assert fake.connections == 2


def test_a_resolver_that_never_answers_times_out_on_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    real = socket.getaddrinfo
    release = threading.Event()

    def stuck(*args, **kwargs):
        release.wait(5.0)
        return real(*args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", stuck)
    with systemone_server() as (origin, fake):
        judge = _judge(origin.replace("127.0.0.1", "localhost"))
        started = time.monotonic()
        with pytest.raises(JudgeUnavailable) as raised:
            judge.judge(QUERY, UNITS, timeout_s=0.3)
        elapsed = time.monotonic() - started
        release.set()

    assert raised.value.reason == "timeout"
    assert elapsed <= 0.3 + 0.1
    assert fake.requests == []


# --- connections ---------------------------------------------------------------


def test_two_calls_on_one_thread_share_one_connection() -> None:
    with systemone_server() as (origin, fake):
        judge = _judge(origin)
        judge.judge(QUERY, UNITS, timeout_s=2.0)
        judge.judge("otra consulta", UNITS, timeout_s=2.0)

    assert len(fake.requests) == 2
    assert fake.connections == 1


def test_prewarm_opens_the_connection_the_next_call_uses() -> None:
    with systemone_server() as (origin, fake):
        judge = _judge(origin)
        judge.prewarm()
        assert _wait_for(lambda: fake.connections == 1)
        assert fake.requests == []

        judge.prewarm()  # already open: nothing new
        judge.judge(QUERY, UNITS, timeout_s=2.0)

    assert fake.connections == 1
    assert len(fake.requests) == 1


def test_a_call_right_after_prewarm_waits_for_the_handshake() -> None:
    with systemone_server() as (origin, fake):
        judge = _judge(origin)
        judge.prewarm()
        verdict = judge.judge(QUERY, UNITS, timeout_s=2.0)

    assert set(verdict.scores) == {"a", "b", "c"}
    assert fake.connections == 1


class _HandshakingConnection(http.client.HTTPConnection):
    """A connection whose ``connect`` behaves like TLS: the plain socket is detached during the handshake.

    ``ssl.SSLContext.wrap_socket`` takes the descriptor out of the plain
    socket first and only then shakes hands, while ``conn.sock`` still names
    the detached socket; any call on it in that window fails with EBADF.
    """

    def connect(self) -> None:
        raw = socket.create_connection((self.host, self.port), self.timeout)
        self.sock = raw
        wrapped = socket.socket(raw.family, raw.type, raw.proto, fileno=raw.detach())
        time.sleep(0.3)
        self.sock = wrapped


def test_a_call_during_the_warm_up_handshake_waits_instead_of_touching_the_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with systemone_server() as (origin, fake):
        judge = _judge(origin)
        monkeypatch.setattr(
            judge, "_new_connection", lambda timeout_s: _HandshakingConnection(judge._host, judge._port, timeout=timeout_s)
        )
        judge.prewarm()
        time.sleep(0.05)
        verdict = judge.judge(QUERY, UNITS, timeout_s=2.0)

    assert set(verdict.scores) == {"a", "b", "c"}
    assert fake.connections == 1


def test_a_failed_prewarm_leaves_the_call_to_connect_itself() -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    judge = _judge(f"http://127.0.0.1:{port}")

    judge.prewarm(timeout_s=0.5)
    with pytest.raises(JudgeUnavailable) as raised:
        judge.judge(QUERY, UNITS, timeout_s=1.0)

    assert raised.value.reason == "unavailable"


def test_each_thread_keeps_its_own_connection_and_none_waits_for_another() -> None:
    fake_state = FakeSystemOne(delay_s=0.4)
    results: dict[int, float] = {}
    barrier = threading.Barrier(2)

    with systemone_server(fake_state) as (origin, fake):
        judge = _judge(origin)

        def search(n: int) -> None:
            barrier.wait()
            judge.judge(f"consulta {n} [delay]", UNITS, timeout_s=2.0)
            judge.judge(f"consulta {n} bis", UNITS, timeout_s=2.0)
            results[n] = time.monotonic()

        started = time.monotonic()
        threads = [threading.Thread(target=search, args=(n,)) for n in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5.0)

    assert sorted(results) == [1, 2]
    # Serialized behind one lock, the two delayed calls would take 0.8 s.
    assert max(results.values()) - started < 0.4 * 2
    assert fake.connections == 2
    assert len(fake.requests) == 4


def test_a_connection_the_server_dropped_while_idle_is_reopened() -> None:
    with systemone_server(FakeSystemOne(drop_idle=True)) as (origin, fake):
        judge = _judge(origin)
        judge.judge(QUERY, UNITS, timeout_s=2.0)
        assert _wait_for(lambda: True, 0.05)
        verdict = judge.judge("otra consulta", UNITS, timeout_s=2.0)

    assert set(verdict.scores) == {"a", "b", "c"}
    assert fake.connections == 2


def test_prewarm_replaces_a_connection_the_server_dropped() -> None:
    with systemone_server(FakeSystemOne(drop_idle=True)) as (origin, fake):
        judge = _judge(origin)
        judge.judge(QUERY, UNITS, timeout_s=2.0)
        time.sleep(0.05)
        judge.prewarm()
        assert _wait_for(lambda: fake.connections == 2)
        judge.judge("otra consulta", UNITS, timeout_s=2.0)

    assert fake.connections == 2
    assert len(fake.requests) == 2


def test_an_idle_connection_on_a_descriptor_past_fd_setsize_is_not_stale() -> None:
    """A busy server holds descriptors past 1024, where ``select()`` refuses to look."""
    high = 1500
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft <= high:
        if hard != resource.RLIM_INFINITY and hard <= high:
            pytest.skip("the descriptor limit does not reach past FD_SETSIZE")
        resource.setrlimit(resource.RLIMIT_NOFILE, (high + 1, hard))
    ours, theirs = socket.socketpair()
    try:
        sock = socket.socket(fileno=os.dup2(ours.fileno(), high))
        conn = http.client.HTTPConnection("127.0.0.1")
        conn.sock = sock
        assert sock.fileno() >= 1024

        assert not judges.SystemOneJudge._stale(conn)
        theirs.close()
        assert judges.SystemOneJudge._stale(conn)
        sock.close()
    finally:
        ours.close()
        theirs.close()
        if soft <= high:
            resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


def test_a_drop_noticed_only_on_send_is_retried_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(judges.SystemOneJudge, "_stale", staticmethod(lambda conn: False))
    with systemone_server(FakeSystemOne(drop_idle=True)) as (origin, fake):
        judge = _judge(origin)
        judge.judge(QUERY, UNITS, timeout_s=2.0)
        time.sleep(0.05)
        verdict = judge.judge("otra consulta", UNITS, timeout_s=2.0)

    assert set(verdict.scores) == {"a", "b", "c"}
    assert fake.connections == 2
    assert len(fake.requests) == 2


def test_a_server_that_drops_every_answer_is_retried_only_once() -> None:
    with systemone_server() as (origin, fake):
        judge = _judge(origin)
        judge.judge(QUERY, UNITS, timeout_s=2.0)
        with pytest.raises(JudgeUnavailable) as raised:
            judge.judge(f"{QUERY} [drop]", UNITS, timeout_s=2.0)

    assert raised.value.reason == "unavailable"
    assert len(fake.requests) == 3  # the answered call, the dropped one and its single retry
    assert fake.connections == 2


# --- what the judge never shows -------------------------------------------------


def test_the_key_never_shows_in_repr() -> None:
    judge = _judge("http://127.0.0.1:9")

    assert API_KEY not in repr(judge) and API_KEY not in str(judge)
    assert repr(judge) == "SystemOneJudge(provider='jev', model='jev-1.13.0', url='http://127.0.0.1:9/v1/systemone')"


@pytest.mark.parametrize(
    ("model", "provider"), [("jev-1.13.0", "local"), ("  ", None)]
)
def test_a_judge_needs_a_known_provider_and_a_model(model: str, provider: str | None) -> None:
    with pytest.raises(ValueError):
        judges.SystemOneJudge("http://127.0.0.1:9", model, API_KEY, provider=provider)


def test_a_key_a_bearer_cannot_carry_is_refused_without_repeating_it() -> None:
    with pytest.raises(ValueError) as raised:
        judges.SystemOneJudge("http://127.0.0.1:9", "jev-1.13.0", "secret with spaces")

    assert "secret" not in str(raised.value)


@pytest.mark.parametrize(
    ("url", "model"),
    [
        ("https://evil.example.test/v1/systemone", "jev-1.13.0"),
        ("http://api.typesafe.ai/v1/systemone", "jev-1.13.0"),
        ("https://api.typesafe.ai:8443/v1/systemone", "jev-1.13.0"),
        ("https://user:pass@api.typesafe.ai/v1/systemone", "jev-1.13.0"),
        ("https://api.typesafe.ai/v1/other", "jev-1.13.0"),
        ("https://api.typesafe.ai/v1/systemone?x=1", "jev-1.13.0"),
        (judges.TYPESAFE_URL, "kev-latest"),
        ("http://10.0.0.5:8787/v1/systemone", "kev-latest"),
    ],
)
def test_a_url_off_the_provider_hosts_is_refused(url: str, model: str) -> None:
    with pytest.raises(ValueError) as raised:
        judges.SystemOneJudge(url, model, API_KEY)

    assert url not in str(raised.value) and API_KEY not in str(raised.value)


@pytest.mark.parametrize(
    ("url", "model", "expected"),
    [
        (judges.TYPESAFE_URL, "jev-1.13.0", judges.TYPESAFE_URL),
        (TYPESAFE_ORIGIN, "jev-1.13.0", judges.TYPESAFE_URL),
        ("http://127.0.0.1:8787", "kev-latest", "http://127.0.0.1:8787/v1/systemone"),
        ("http://[::1]:8787/", "kev-latest", "http://[::1]:8787/v1/systemone"),
        ("http://localhost:8787/v1/systemone", "jev-1.13.0", "http://localhost:8787/v1/systemone"),
    ],
)
def test_the_provider_hosts_are_accepted(url: str, model: str, expected: str) -> None:
    judge = judges.SystemOneJudge(url, model, API_KEY)

    assert f"url={expected!r}" in repr(judge)


# --- build_judge ----------------------------------------------------------------


def _rerank(**overrides) -> RerankSettings:
    values = {"provider": "jev", "model": "jev-1.13.0", "key_env": "TYPESAFE_API_KEY", "api_key": API_KEY}
    values.update(overrides)
    return RerankSettings(**values)


def test_a_vault_without_a_judge_builds_the_null_judge() -> None:
    assert isinstance(judges.build_judge(RerankSettings()), NullJudge)


def test_the_operator_switch_and_list_rule_a_judge_out() -> None:
    assert isinstance(judges.build_judge(_rerank(), RerankOperator(off=True)), NullJudge)
    assert isinstance(judges.build_judge(_rerank(), RerankOperator(providers=frozenset({"kev"}))), NullJudge)


def test_jev_calls_typesafe_with_its_key() -> None:
    judge = judges.build_judge(_rerank(), client_id="acme")

    assert isinstance(judge, judges.SystemOneJudge)
    assert judge.name == "jev" and judge.model == "jev-1.13.0"
    assert f"url={judges.TYPESAFE_URL!r}" in repr(judge)


def test_an_empty_key_never_builds_a_judge_off_the_loopback() -> None:
    assert isinstance(judges.build_judge(_rerank(api_key="")), NullJudge)
    assert isinstance(judges.build_judge(_rerank(api_key=""), RerankOperator(base_url=TYPESAFE_ORIGIN)), NullJudge)

    loopback = judges.build_judge(_rerank(api_key=""), RerankOperator(base_url="http://127.0.0.1:9"))
    assert isinstance(loopback, judges.SystemOneJudge)


def test_the_operator_url_replaces_every_provider_url() -> None:
    judge = judges.build_judge(_rerank(), RerankOperator(base_url="http://127.0.0.1:9"))

    assert "url='http://127.0.0.1:9/v1/systemone'" in repr(judge)


def test_kev_calls_its_loopback_server_and_never_typesafe() -> None:
    kev = _rerank(provider="kev", model="kev-latest", key_env="KEV_API_KEY")

    assert f"url='{judges.KEV_ORIGIN}/v1/systemone'" in repr(judges.build_judge(kev))
    own = judges.build_judge(RerankSettings(**{**kev.__dict__, "base_url": "http://127.0.0.1:8899"}))
    assert "url='http://127.0.0.1:8899/v1/systemone'" in repr(own)
    assert isinstance(judges.build_judge(kev, RerankOperator(base_url=TYPESAFE_ORIGIN)), NullJudge)


def test_a_built_judge_answers_through_the_double() -> None:
    with systemone_server() as (origin, fake):
        judge = judges.build_judge(_rerank(), RerankOperator(base_url=origin))
        verdict = judge.judge(QUERY, UNITS, timeout_s=2.0)

    assert set(verdict.scores) == {"a", "b", "c"}
    assert fake.requests[0]["headers"]["User-Agent"] == judges.CLIENT_ID


# --- price and loading ------------------------------------------------------------


def test_the_price_of_a_call() -> None:
    assert judges.cost_usd("jev-1.13.0", 1_000_000) == pytest.approx(0.042)
    assert judges.cost_usd("jev-1.13.0", 11_668) == pytest.approx(0.00049, abs=1e-5)
    assert judges.cost_usd("kev-latest", 1_000_000) == 0.0
    assert judges.cost_usd("jev-9.9.9", 1_000_000) == pytest.approx(0.042)


def test_the_server_loads_the_judges_as_the_rerank_extension(monkeypatch: pytest.MonkeyPatch) -> None:
    from knowledge import server

    monkeypatch.setattr(server, "_LOADED_EXTENSIONS", {})

    assert server._load_extension("rerank") is judges
