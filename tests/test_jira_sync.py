"""The jira extension against a fake Jira and a fake memory, both on the loopback.

``jira_sync.sync`` reads the issues of the projects a vault declares under
``integrations.jira`` and upserts one memory task per issue. Every test here
drives it through real HTTP: ``tests/jira_fakes.py`` serves Jira's enhanced
JQL search and memory's task route on ephemeral loopback ports. The clock is
injected, so every ``since`` and cursor below is exact.
"""

from __future__ import annotations

import asyncio
import json
import logging
import textwrap
import urllib.error
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from jira_fakes import (
    ENV,
    JIRA_TOKEN,
    MEMORY_TOKEN,
    SEARCH_PATH,
    FakeJira,
    FakeMemoryTasks,
    issue,
    jira_server,
    manifest,
    memory_server,
)
from knowledge import cli, errors, jira_client, jira_sync, memory_bridge, settings
from knowledge.errors import KnowledgeConfigError, KnowledgeToolError

NOW = datetime(2026, 10, 12, 15, 0, tzinfo=UTC)


def _now() -> datetime:
    return NOW


@pytest.fixture(autouse=True)
def _loggers_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the loggers live, so "no credential is logged" never passes on an empty log."""
    for name in ("knowledge.jira_client", "knowledge.jira_sync", "knowledge.memory_bridge"):
        monkeypatch.setattr(logging.getLogger(name), "disabled", False)


def _load(tmp_path: Path, body: str, **env: str) -> settings.Settings:
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    (vault / "vault.yaml").write_text("slug: globex\n" + textwrap.dedent(body), encoding="utf-8")
    vaults_file = tmp_path / "vaults.yaml"
    vaults_file.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "data_dir": str(tmp_path / "data"),
                "vaults": [{"slug": "globex", "path": str(vault), "default": True}],
            }
        ),
        encoding="utf-8",
    )
    base = {"KNOWLEDGE_VAULTS": str(vaults_file), "KNOWLEDGE_EMBEDDINGS_PROVIDER": "bag-of-words", **env}
    return settings.load_settings(base, project_root=tmp_path)


@pytest.fixture()
def jira() -> Iterator[tuple[FakeJira, str]]:
    state = FakeJira()
    with jira_server(state) as url:
        yield state, url


@pytest.fixture()
def memory() -> Iterator[tuple[FakeMemoryTasks, str]]:
    state = FakeMemoryTasks()
    with memory_server(state) as url:
        yield state, url


def _run(loaded: settings.Settings, **kwargs: Any) -> dict[str, Any]:
    env = {**ENV, **kwargs.pop("env", {})}
    return jira_sync.sync(loaded.default_vault(), env=env, memory=loaded.memory, now=_now, **kwargs)


def _state(loaded: settings.Settings) -> dict[str, Any]:
    return jira_sync.read_state(loaded.default_vault())


# --- the search ------------------------------------------------------------------


def test_pages_follow_next_page_token_until_is_last(tmp_path: Path, jira: tuple[FakeJira, str]) -> None:
    state, url = jira
    state.pages = [
        [issue("ACME-1", "2026-10-01T10:00:00.000-0300"), issue("ACME-2", "2026-10-02T10:00:00.000-0300")],
        [issue("ACME-3", "2026-10-03T10:00:00.000-0300")],
        [issue("ACME-4", "2026-10-04T10:00:00.000-0300")],
    ]
    loaded = _load(tmp_path, manifest(url))

    report = _run(loaded, dry_run=True)

    assert report["fetched"] == 4
    assert report["truncated"] is False
    assert [r["query"].get("nextPageToken") for r in state.searches] == [None, "page-1", "page-2"]
    first = state.searches[0]
    assert first["path"] == SEARCH_PATH
    assert first["query"]["fields"] == "summary,status,issuetype,assignee,updated"
    assert first["query"]["maxResults"] == "100"
    assert report["next_since"] == "2026-10-04T13:00:00Z"


def test_a_search_past_max_issues_is_truncated_and_resumes_from_the_last_issue(
    tmp_path: Path, jira: tuple[FakeJira, str], memory: tuple[FakeMemoryTasks, str]
) -> None:
    jstate, url = jira
    mstate, murl = memory
    pages = [issue(f"ACME-{n}", f"2026-10-0{n}T00:00:00.000+0000") for n in range(1, 6)]
    jstate.pages = [pages[:3], pages[3:]]
    loaded = _load(tmp_path, manifest(url), KNOWLEDGE_MEMORY_URL=murl, MEMORY_HTTP_TOKEN=MEMORY_TOKEN)

    report = _run(loaded, dry_run=False, max_issues=3)

    assert (report["fetched"], report["truncated"], report["upserted"]) == (3, True, 3)
    assert report["next_since"] == "2026-10-03T00:00:00Z"
    assert _state(loaded)["cursor"] == "2026-10-03T00:00:00Z"


@pytest.mark.parametrize("zone", ["UTC", "America/Mexico_City", "Asia/Kolkata"])
def test_a_truncated_backlog_is_mirrored_whole_over_consecutive_runs(
    tmp_path: Path, jira: tuple[FakeJira, str], memory: tuple[FakeMemoryTasks, str], zone: str | None
) -> None:
    """Six issues of one day, two per run: every run moves forward and all six reach memory, whatever zone JQL reads."""
    jstate, url = jira
    mstate, murl = memory
    jstate.time_zone = zone
    jstate.live = [issue(f"ACME-{n}", f"2026-10-11T0{n}:00:00Z") for n in range(1, 7)]
    loaded = _load(tmp_path, manifest(url), KNOWLEDGE_MEMORY_URL=murl, MEMORY_HTTP_TOKEN=MEMORY_TOKEN)

    reports = [_run(loaded, dry_run=False, max_issues=2)]
    while reports[-1]["truncated"] and len(reports) < 10:
        reports.append(_run(loaded, dry_run=False, max_issues=2))

    assert mstate.known == {f"ACME-{n}" for n in range(1, 7)}
    assert len(reports) == 5, "each resumed run re-reads the last issue of the one before and reads one more"
    assert reports[1]["since"] == "2026-10-11T02:00:00Z", "a truncated run resumes without the overlap"
    assert _state(loaded)["cursor"] == "2026-10-11T06:00:00Z"
    assert _state(loaded)["truncated"] is False
    after = _run(loaded, dry_run=False, max_issues=2)
    assert after["since"] == "2026-10-10T06:00:00Z", "after a complete run the overlap is back"


@pytest.mark.parametrize(
    ("zone", "written"),
    [
        ("UTC", "2026-10-05 17:46"),
        ("America/Mexico_City", "2026-10-05 11:46"),
        ("Asia/Kolkata", "2026-10-05 23:16"),
        (None, "2026-10-05 05:46"),
        ("Not/AZone", "2026-10-05 05:46"),
    ],
)
def test_jql_dates_are_written_in_the_zone_of_the_jira_account(
    tmp_path: Path, jira: tuple[FakeJira, str], zone: str | None, written: str
) -> None:
    """JQL reads a date in the account's zone; with none known, UTC-12 never starts after the cursor."""
    state, url = jira
    state.time_zone = zone
    state.pages = [[]]
    loaded = _load(tmp_path, manifest(url))

    report = _run(loaded, dry_run=True, since="2026-10-05T17:46:00Z")

    assert f'updated >= "{written}"' in state.searches[0]["query"]["jql"]
    assert report["since"] == "2026-10-05T17:46:00Z"
    assert [r["path"] for r in state.requests][:1] == ["/rest/api/3/myself"]


def test_a_run_whose_issues_all_share_its_first_minute_is_stalled(
    tmp_path: Path, jira: tuple[FakeJira, str], memory: tuple[FakeMemoryTasks, str]
) -> None:
    jstate, url = jira
    mstate, murl = memory
    jstate.live = [issue(f"ACME-{n}", f"2026-10-11T10:00:{n:02d}Z") for n in range(1, 4)]
    loaded = _load(tmp_path, manifest(url), KNOWLEDGE_MEMORY_URL=murl, MEMORY_HTTP_TOKEN=MEMORY_TOKEN)

    with pytest.raises(KnowledgeToolError) as excinfo:
        _run(loaded, dry_run=False, max_issues=2, since="2026-10-11T10:00:00Z")

    assert excinfo.value.code == errors.E_JIRA_SYNC_STALLED
    assert "2026-10-11 10:00" in excinfo.value.message
    assert mstate.requests == []
    assert "cursor" not in _state(loaded)


def test_a_refused_issue_is_read_again_by_key_until_memory_takes_it(
    tmp_path: Path, jira: tuple[FakeJira, str], memory: tuple[FakeMemoryTasks, str]
) -> None:
    """memory refuses ACME-1 and takes ACME-2: the cursor moves, and ACME-1 is not lost."""
    jstate, url = jira
    mstate, murl = memory
    first = issue("ACME-1", "2026-10-01T00:00:00Z", issue_type="Bug")
    jstate.live = [first, issue("ACME-2", "2026-10-02T00:00:00Z")]
    jstate.by_key = {"ACME-1": first}
    mstate.refusals["ACME-1"] = (400, "invalid_enum")
    loaded = _load(tmp_path, manifest(url), KNOWLEDGE_MEMORY_URL=murl, MEMORY_HTTP_TOKEN=MEMORY_TOKEN)

    refused = _run(loaded, dry_run=False)
    assert refused["skipped"] == [{"key": "ACME-1", "reason": "invalid_enum"}]
    assert _state(loaded)["retry"] == ["ACME-1"]

    jstate.live = []
    still = _run(loaded, dry_run=False)
    assert (still["retried"], still["upserted"]) == (1, 0)
    assert _state(loaded)["retry"] == ["ACME-1"], "a key memory refuses again stays in the list"

    del mstate.refusals["ACME-1"]
    taken = _run(loaded, dry_run=False)
    assert (taken["retried"], taken["upserted"], taken["created"]) == (1, 1, 1)
    assert _state(loaded)["retry"] == []
    assert "ACME-1" in mstate.known
    assert any(r["path"] == "/rest/api/3/issue/ACME-1" for r in jstate.requests)


def test_a_refused_key_jira_no_longer_has_leaves_the_list(
    tmp_path: Path, jira: tuple[FakeJira, str], memory: tuple[FakeMemoryTasks, str]
) -> None:
    jstate, url = jira
    mstate, murl = memory
    jstate.live = [issue("ACME-1", "2026-10-01T00:00:00Z")]
    mstate.refusals["ACME-1"] = (409, "task_key_conflict")
    loaded = _load(tmp_path, manifest(url), KNOWLEDGE_MEMORY_URL=murl, MEMORY_HTTP_TOKEN=MEMORY_TOKEN)
    _run(loaded, dry_run=False)
    jstate.live = []

    report = _run(loaded, dry_run=False)

    assert report["retried"] == 0
    assert _state(loaded)["retry"] == []


def test_refusals_past_the_retry_list_hold_the_cursor_at_the_oldest(
    tmp_path: Path,
    jira: tuple[FakeJira, str],
    memory: tuple[FakeMemoryTasks, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jstate, url = jira
    mstate, murl = memory
    monkeypatch.setattr(jira_sync, "MAX_RETRY", 1)
    jstate.live = [issue(f"ACME-{n}", f"2026-10-0{n}T00:00:00Z") for n in range(1, 5)]
    mstate.refusals.update({"ACME-1": (400, "invalid_enum"), "ACME-3": (400, "invalid_enum")})
    loaded = _load(tmp_path, manifest(url), KNOWLEDGE_MEMORY_URL=murl, MEMORY_HTTP_TOKEN=MEMORY_TOKEN)

    report = _run(loaded, dry_run=False)

    assert _state(loaded)["retry"] == ["ACME-1"]
    assert report["next_since"] == "2026-10-03T00:00:00Z", "ACME-3 has no room in the list, so the cursor waits"


def test_an_explicit_since_later_than_the_cursor_never_moves_it_forward(
    tmp_path: Path, jira: tuple[FakeJira, str], memory: tuple[FakeMemoryTasks, str]
) -> None:
    jstate, url = jira
    _, murl = memory
    jstate.live = [issue("ACME-1", "2026-10-01T00:00:00Z"), issue("ACME-2", "2026-10-11T00:00:00Z")]
    loaded = _load(tmp_path, manifest(url), KNOWLEDGE_MEMORY_URL=murl, MEMORY_HTTP_TOKEN=MEMORY_TOKEN)
    jstate.live = jstate.live[:1]
    _run(loaded, dry_run=False)
    assert _state(loaded)["cursor"] == "2026-10-01T00:00:00Z"
    jstate.live = [issue("ACME-1", "2026-10-01T00:00:00Z"), issue("ACME-2", "2026-10-11T00:00:00Z")]

    report = _run(loaded, dry_run=False, since="2026-10-10")

    assert report["upserted"] == 1
    assert report["next_since"] == "2026-10-01T00:00:00Z"
    assert _state(loaded)["cursor"] == "2026-10-01T00:00:00Z"


def test_two_runs_never_share_the_temporary_state_file(tmp_path: Path) -> None:
    loaded = _load(tmp_path, manifest("https://example.atlassian.net"))
    spec = loaded.default_vault()
    spec.index_dir.mkdir(parents=True)
    # What another run would have left half-written.
    (spec.index_dir / "jira_sync.json.tmp").write_text("{", encoding="utf-8")

    jira_sync.mark_attempt(spec, NOW)

    assert _state(loaded)["last_attempt_at"] == "2026-10-12T15:00:00Z"
    assert sorted(p.name for p in spec.index_dir.iterdir()) == ["jira_sync.json", "jira_sync.json.tmp"]


def test_auto_reports_an_unexpected_failure_and_raises_nothing(
    tmp_path: Path, jira: tuple[FakeJira, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, url = jira
    loaded = _load(tmp_path, manifest(url))

    def broken(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise FileNotFoundError("jira_sync.json.tmp")

    monkeypatch.setattr(jira_sync, "sync", broken)

    assert jira_sync.auto(loaded, ENV, now=_now) == [{"vault": "globex", "status": "failed", "reason": "FileNotFoundError"}]


@pytest.mark.parametrize(
    ("since", "expected"),
    [
        ("-7d", '"2026-10-05 15:00"'),
        ("2026-09-01", '"2026-09-01 00:00"'),
        ("2026-09-01T12:30:00-03:00", '"2026-09-01 15:30"'),
        (None, '"2026-09-12 15:00"'),
    ],
)
def test_since_from_the_argument_or_thirty_days_back(
    tmp_path: Path, jira: tuple[FakeJira, str], since: str | None, expected: str
) -> None:
    state, url = jira
    state.pages = [[]]
    loaded = _load(tmp_path, manifest(url))

    report = _run(loaded, dry_run=True, since=since)

    assert f"updated >= {expected}" in state.searches[0]["query"]["jql"]
    assert report["since"] == expected.strip('"').replace(" ", "T") + ":00Z"


def test_a_later_run_starts_a_day_before_the_stored_cursor(
    tmp_path: Path, jira: tuple[FakeJira, str], memory: tuple[FakeMemoryTasks, str]
) -> None:
    jstate, url = jira
    _, murl = memory
    jstate.pages = [[issue("ACME-1", "2026-10-10T08:00:00.000+0000")]]
    loaded = _load(tmp_path, manifest(url), KNOWLEDGE_MEMORY_URL=murl, MEMORY_HTTP_TOKEN=MEMORY_TOKEN)
    _run(loaded, dry_run=False)
    jstate.pages = [[]]

    second = _run(loaded, dry_run=False)

    assert 'updated >= "2026-10-09 08:00"' in jstate.searches[-1]["query"]["jql"]
    assert second["fetched"] == 0
    assert _state(loaded)["cursor"] == "2026-10-10T08:00:00Z", "an empty run never walks the cursor back"


@pytest.mark.parametrize("since", ["yesterday", "2026-13-01", "-0x", "2027-01-01"])
def test_an_unreadable_or_future_since_is_refused_before_jira(
    tmp_path: Path, jira: tuple[FakeJira, str], since: str
) -> None:
    state, url = jira
    loaded = _load(tmp_path, manifest(url))

    with pytest.raises(KnowledgeToolError) as excinfo:
        _run(loaded, dry_run=True, since=since)
    assert excinfo.value.code == errors.E_ARGUMENT_OUT_OF_RANGE
    assert state.requests == []


def test_the_jql_names_every_project_and_the_extra_condition(tmp_path: Path, jira: tuple[FakeJira, str]) -> None:
    state, url = jira
    state.pages = [[]]
    body = manifest(url).replace("projects: [ACME]", "projects: [ACME, OPS]") + '    jql_extra: "labels = sync"\n'
    loaded = _load(tmp_path, body)

    report = _run(loaded, dry_run=True, since="2026-10-01")

    assert report["jql"] == (
        'project in (ACME, OPS) AND updated >= "2026-10-01 00:00" AND (labels = sync) ORDER BY updated ASC, key ASC'
    )
    assert state.searches[0]["query"]["jql"] == report["jql"]


# --- what Jira answers -------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
def test_refused_credentials_are_e_jira_auth_failed(tmp_path: Path, jira: tuple[FakeJira, str], status: int) -> None:
    state, url = jira
    state.statuses = [(status, {})]
    loaded = _load(tmp_path, manifest(url))

    with pytest.raises(KnowledgeToolError) as excinfo:
        _run(loaded, dry_run=True)
    assert excinfo.value.code == errors.E_JIRA_AUTH_FAILED
    assert "must not echo" not in str(excinfo.value)


def test_a_wrong_token_reaches_jira_as_basic_auth_and_is_refused(tmp_path: Path, jira: tuple[FakeJira, str]) -> None:
    _, url = jira
    loaded = _load(tmp_path, manifest(url))

    with pytest.raises(KnowledgeToolError) as excinfo:
        _run(loaded, dry_run=True, env={"GLOBEX_JIRA_TOKEN": "another-token"})
    assert excinfo.value.code == errors.E_JIRA_AUTH_FAILED


@pytest.mark.parametrize("status", [500, 502, 400])
def test_a_jira_error_writes_no_cursor(
    tmp_path: Path, jira: tuple[FakeJira, str], memory: tuple[FakeMemoryTasks, str], status: int
) -> None:
    jstate, url = jira
    mstate, murl = memory
    jstate.statuses = [(status, {})]
    loaded = _load(tmp_path, manifest(url), KNOWLEDGE_MEMORY_URL=murl, MEMORY_HTTP_TOKEN=MEMORY_TOKEN)

    with pytest.raises(KnowledgeToolError) as excinfo:
        _run(loaded, dry_run=False)
    assert excinfo.value.code == errors.E_JIRA_UNAVAILABLE
    assert "cursor" not in _state(loaded)
    assert _state(loaded)["last_attempt_at"] == "2026-10-12T15:00:00Z"
    assert mstate.requests == []


def test_a_jira_timeout_is_e_jira_unavailable(jira: tuple[FakeJira, str]) -> None:
    state, url = jira
    state.delay = 0.5
    client = jira_client.JiraClient(url, "bot@example.test", JIRA_TOKEN, timeout=0.1)

    with pytest.raises(KnowledgeToolError) as excinfo:
        client.search("project = ACME", ["summary"])
    assert excinfo.value.code == errors.E_JIRA_UNAVAILABLE


def test_a_429_is_retried_once_after_retry_after(jira: tuple[FakeJira, str]) -> None:
    state, url = jira
    state.statuses = [(429, {"Retry-After": "30"})]
    state.pages = [[issue("ACME-1", "2026-10-01T00:00:00.000+0000")]]
    waits: list[float] = []
    client = jira_client.JiraClient(url, "bot@example.test", JIRA_TOKEN, sleep=waits.append)

    assert len(client.search("project = ACME", ["summary"]).issues) == 1
    assert waits == [jira_client.MAX_RETRY_AFTER_SECONDS]

    state.statuses = [(429, {"Retry-After": "0"}), (429, {"Retry-After": "0"})]
    with pytest.raises(KnowledgeToolError) as excinfo:
        client.search("project = ACME", ["summary"])
    assert excinfo.value.code == errors.E_JIRA_UNAVAILABLE


@pytest.mark.parametrize(
    "site", ["http://jira.example.test", "ftp://jira.example.test", "https://", "https://jira.example.test"]
)
def test_the_client_only_calls_https_or_loopback_http(site: str) -> None:
    with pytest.raises(KnowledgeToolError) as excinfo:
        jira_client.JiraClient(site, "bot@example.test", JIRA_TOKEN)
    assert excinfo.value.code == errors.E_MANIFEST_INVALID


def test_the_client_refuses_an_issue_key_that_is_not_a_key_before_any_request() -> None:
    client = jira_client.JiraClient("https://acme.atlassian.net", "bot@example.test", JIRA_TOKEN)

    with pytest.raises(KnowledgeToolError) as excinfo:
        client.issue("not-a-key", ["summary"])

    assert excinfo.value.code == errors.E_ARGUMENT_OUT_OF_RANGE
    assert "PROJ-123" in str(excinfo.value)


# --- the task bodies -------------------------------------------------------------------


def test_the_task_body_carries_the_jira_status_as_given() -> None:
    body, unmapped = jira_sync.task_body(issue("ACME-7", "2026-10-01T00:00:00.000+0000", status="In Code Review"), {}, {})

    assert body == {
        "jira_key": "ACME-7",
        "title": "An issue",
        "kind": "feature",
        "jira_status": "In Code Review",
        "jira_status_category": "indeterminate",
        "assignee": "Ada",
    }
    assert unmapped is None


@pytest.mark.parametrize(
    ("issue_type", "kind_map", "kind"),
    [
        ("Bug", {}, "bugfix"),
        ("Incident", {}, "incident"),
        ("Spike", {}, "spike"),
        ("Story", {}, "feature"),
        ("Story", {"default": "refactor"}, "refactor"),
        ("Bug", {"default": "refactor"}, "bugfix"),
        ("Bug", {"Bug": "incident"}, "incident"),
        ("Migration", {"Migration": "migration"}, "migration"),
    ],
)
def test_kind_follows_the_kind_map_over_a_default_per_issue_type(
    issue_type: str, kind_map: dict[str, str], kind: str
) -> None:
    body, _ = jira_sync.task_body(issue("ACME-1", "2026-10-01T00:00:00Z", issue_type=issue_type), kind_map, {})

    assert body is not None and body["kind"] == kind


def test_state_travels_only_when_the_status_map_names_the_status() -> None:
    mapped, _ = jira_sync.task_body(issue("ACME-1", "x", status="Pending QA"), {}, {"Pending QA": "review"})
    plain, _ = jira_sync.task_body(issue("ACME-1", "x", status="Pending QA"), {}, {})

    assert mapped is not None and mapped["state"] == "review"
    assert plain is not None and "state" not in plain


def test_a_status_without_a_known_category_is_reported_unmapped() -> None:
    body, unmapped = jira_sync.task_body(issue("ACME-1", "x", status="Limbo", category="undefined"), {}, {})
    covered, none = jira_sync.task_body(issue("ACME-1", "x", status="Limbo", category=None), {}, {"Limbo": "blocked"})

    assert body is not None and "jira_status_category" not in body and unmapped == "Limbo"
    assert covered is not None and covered["state"] == "blocked" and none is None


def test_title_falls_back_to_the_key_and_stops_at_300_characters() -> None:
    empty, _ = jira_sync.task_body(issue("ACME-1", "x", summary="  "), {}, {})
    long, _ = jira_sync.task_body(issue("ACME-1", "x", summary="a" * 400), {}, {})
    nobody, _ = jira_sync.task_body(issue("ACME-1", "x", assignee=None), {}, {})

    assert empty is not None and empty["title"] == "ACME-1"
    assert long is not None and len(long["title"]) == jira_sync.TITLE_MAX
    assert nobody is not None and "assignee" not in nobody


def test_an_invalid_key_is_skipped_without_a_request(
    tmp_path: Path, jira: tuple[FakeJira, str], memory: tuple[FakeMemoryTasks, str]
) -> None:
    jstate, url = jira
    mstate, murl = memory
    jstate.pages = [[issue("acme-1", "2026-10-01T00:00:00Z"), issue("ACME-2", "2026-10-02T00:00:00Z")]]
    loaded = _load(tmp_path, manifest(url), KNOWLEDGE_MEMORY_URL=murl, MEMORY_HTTP_TOKEN=MEMORY_TOKEN)

    report = _run(loaded, dry_run=False)

    assert report["skipped"] == [{"key": "acme-1", "reason": "invalid_key"}]
    assert [r["body"]["jira_key"] for r in mstate.requests] == ["ACME-2"]


# --- the writes to memory --------------------------------------------------------------


def test_dry_run_reads_jira_and_writes_nothing(
    tmp_path: Path, jira: tuple[FakeJira, str], memory: tuple[FakeMemoryTasks, str]
) -> None:
    jstate, url = jira
    mstate, murl = memory
    jstate.pages = [[issue("ACME-1", "2026-10-01T00:00:00Z")]]
    loaded = _load(tmp_path, manifest(url), KNOWLEDGE_MEMORY_URL=murl, MEMORY_HTTP_TOKEN=MEMORY_TOKEN)

    report = _run(loaded, dry_run=True)

    assert (report["fetched"], report["upserted"], report["created"], report["dry_run"]) == (1, 0, 0, True)
    assert mstate.requests == []
    assert not jira_sync.state_path(loaded.default_vault()).exists()


def test_a_dry_run_needs_no_memory_bearer(tmp_path: Path, jira: tuple[FakeJira, str]) -> None:
    state, url = jira
    state.pages = [[]]
    loaded = _load(tmp_path, manifest(url))

    assert _run(loaded, dry_run=True, env={"MEMORY_HTTP_TOKEN": ""})["fetched"] == 0


def test_created_and_updated_are_counted_and_a_second_run_creates_nothing(
    tmp_path: Path, jira: tuple[FakeJira, str], memory: tuple[FakeMemoryTasks, str]
) -> None:
    jstate, url = jira
    mstate, murl = memory
    jstate.pages = [[issue("ACME-1", "2026-10-01T00:00:00Z"), issue("ACME-2", "2026-10-02T00:00:00Z")]]
    mstate.known.add("ACME-2")
    loaded = _load(tmp_path, manifest(url), KNOWLEDGE_MEMORY_URL=murl, MEMORY_HTTP_TOKEN=MEMORY_TOKEN)

    first = _run(loaded, dry_run=False)
    second = _run(loaded, dry_run=False)

    assert (first["upserted"], first["created"]) == (2, 1)
    assert (second["upserted"], second["created"]) == (2, 0)
    request = mstate.requests[0]
    assert request["path"] == "/projects/globex/tasks"
    assert request["headers"]["Authorization"] == f"Bearer {MEMORY_TOKEN}"
    assert request["headers"]["X-Memory-Client"].startswith("cloudfrog-knowledge/")
    assert set(request["body"]) == {"jira_key", "title", "kind", "jira_status", "jira_status_category", "assignee"}
    saved = _state(loaded)
    assert saved["cursor"] == "2026-10-02T00:00:00Z"
    assert saved["last_result"] == {"fetched": 2, "retried": 0, "upserted": 2, "created": 0, "skipped": 0}


@pytest.mark.parametrize(("status", "code"), [(409, "task_key_conflict"), (400, "invalid_enum"), (422, "invalid_field")])
def test_a_task_memory_refuses_is_skipped_and_the_run_goes_on(
    tmp_path: Path, jira: tuple[FakeJira, str], memory: tuple[FakeMemoryTasks, str], status: int, code: str
) -> None:
    jstate, url = jira
    mstate, murl = memory
    jstate.pages = [[issue("ACME-1", "2026-10-01T00:00:00Z"), issue("ACME-2", "2026-10-02T00:00:00Z")]]
    mstate.refusals["ACME-1"] = (status, code)
    loaded = _load(tmp_path, manifest(url), KNOWLEDGE_MEMORY_URL=murl, MEMORY_HTTP_TOKEN=MEMORY_TOKEN)

    report = _run(loaded, dry_run=False)

    assert report["skipped"] == [{"key": "ACME-1", "reason": code}]
    assert report["upserted"] == 1
    assert "error" not in report
    assert _state(loaded)["cursor"] == "2026-10-02T00:00:00Z"


def test_memory_going_down_mid_run_skips_the_rest_and_keeps_the_cursor(
    tmp_path: Path, jira: tuple[FakeJira, str], memory: tuple[FakeMemoryTasks, str]
) -> None:
    jstate, url = jira
    mstate, murl = memory
    jstate.pages = [[issue(f"ACME-{n}", f"2026-10-0{n}T00:00:00Z") for n in range(1, 4)]]
    loaded = _load(tmp_path, manifest(url), KNOWLEDGE_MEMORY_URL=murl, MEMORY_HTTP_TOKEN=MEMORY_TOKEN)
    calls = {"n": 0}
    real_open = memory_bridge._OPENER.open

    def flaky(request: Any, timeout: float) -> Any:
        calls["n"] += 1
        if calls["n"] > 1:
            raise urllib.error.URLError(ConnectionRefusedError("refused"))
        return real_open(request, timeout=timeout)

    writer = memory_bridge.MemoryWriter(murl, MEMORY_TOKEN, opener=flaky)

    report = _run(loaded, dry_run=False, writer=writer)

    assert report["upserted"] == 1
    assert report["skipped"] == [
        {"key": "ACME-2", "reason": "memory_unavailable"},
        {"key": "ACME-3", "reason": "memory_unavailable"},
    ]
    assert report["error"]["code"] == errors.E_MEMORY_UNAVAILABLE
    assert calls["n"] == 2, "the run stops at the first transport failure"
    assert "cursor" not in _state(loaded)


def test_a_write_without_a_memory_bearer_is_refused_before_jira(
    tmp_path: Path, jira: tuple[FakeJira, str], memory: tuple[FakeMemoryTasks, str]
) -> None:
    jstate, url = jira
    _, murl = memory
    loaded = _load(tmp_path, manifest(url), KNOWLEDGE_MEMORY_URL=murl, MEMORY_HTTP_TOKEN="")

    with pytest.raises(KnowledgeToolError) as excinfo:
        _run(loaded, dry_run=False, env={"MEMORY_HTTP_TOKEN": ""})
    assert excinfo.value.code == errors.E_MEMORY_TOKEN_MISSING
    assert jstate.requests == []


def test_a_memory_without_a_bearer_is_reached_when_the_requirement_is_off(
    tmp_path: Path, jira: tuple[FakeJira, str]
) -> None:
    jstate, url = jira
    mstate = FakeMemoryTasks(token=None)
    jstate.pages = [[issue("ACME-1", "2026-10-01T00:00:00Z")]]
    with memory_server(mstate) as murl:
        loaded = _load(
            tmp_path, manifest(url), KNOWLEDGE_MEMORY_URL=murl, MEMORY_HTTP_TOKEN="", KNOWLEDGE_MEMORY_REQUIRE_TOKEN="0"
        )
        report = _run(loaded, dry_run=False, env={"MEMORY_HTTP_TOKEN": ""})

    assert report["created"] == 1
    assert "Authorization" not in mstate.requests[0]["headers"]


# --- what the vault declares ----------------------------------------------------------


def test_a_vault_without_the_feature_is_e_feature_disabled(tmp_path: Path, jira: tuple[FakeJira, str]) -> None:
    _, url = jira
    loaded = _load(tmp_path, manifest(url).replace("features: [jira]", "features: [governance]"))

    with pytest.raises(KnowledgeToolError) as excinfo:
        _run(loaded, dry_run=True)
    assert excinfo.value.code == errors.E_FEATURE_DISABLED


def test_missing_credentials_are_named_and_never_valued(tmp_path: Path, jira: tuple[FakeJira, str]) -> None:
    state, url = jira
    loaded = _load(tmp_path, manifest(url))

    with pytest.raises(KnowledgeToolError) as excinfo:
        _run(loaded, dry_run=True, env={"GLOBEX_JIRA_TOKEN": ""})
    assert excinfo.value.code == errors.E_SECRET_MISSING
    assert "GLOBEX_JIRA_TOKEN" in excinfo.value.message
    assert "bot@example.test" not in excinfo.value.message
    assert state.requests == []


@pytest.mark.parametrize(
    ("body", "where"),
    [
        ("features: [jira]\nmemory:\n  project: globex\nintegrations:\n  jira:\n    projects: [ACME]\n"
         "    secrets: {email_env: GLOBEX_JIRA_EMAIL, token_env: GLOBEX_JIRA_TOKEN}\n", "site"),
        ("features: [jira]\nmemory:\n  project: globex\nintegrations:\n  jira:\n    site: https://example.atlassian.net\n"
         "    secrets: {email_env: GLOBEX_JIRA_EMAIL, token_env: GLOBEX_JIRA_TOKEN}\n", "projects"),
        ("features: [jira]\nmemory:\n  project: globex\nintegrations:\n  jira:\n    site: https://example.atlassian.net\n"
         "    projects: [ACME]\n", "secrets.email_env"),
    ],
)
def test_a_sync_needs_a_site_projects_and_both_secret_names(tmp_path: Path, body: str, where: str) -> None:
    loaded = _load(tmp_path, body)

    with pytest.raises(KnowledgeToolError) as excinfo:
        _run(loaded, dry_run=True)
    assert excinfo.value.code == errors.E_MANIFEST_INVALID
    assert where in excinfo.value.message


@pytest.mark.parametrize(
    ("extra", "where"),
    [
        ("    kind_map: {Bug: bug}\n", "integrations.jira.kind_map"),
        ("    status_map: {Done: closed}\n", "integrations.jira.status_map"),
        ("    status_map: {Done: todo}\n", "integrations.jira.status_map"),
        ("    jql_extra: [labels]\n", "integrations.jira.jql_extra"),
        ("    sync: {throttle_minutes: -1}\n", "integrations.jira.sync.throttle_minutes"),
        ("    target: jira\n", "integrations.jira.target"),
    ],
)
def test_the_manifest_is_checked_at_load(tmp_path: Path, extra: str, where: str) -> None:
    with pytest.raises(KnowledgeConfigError) as excinfo:
        _load(tmp_path, manifest("https://example.atlassian.net", extra=extra))
    assert excinfo.value.code == errors.E_MANIFEST_INVALID
    assert where in excinfo.value.message


def test_projects_must_be_jira_keys(tmp_path: Path) -> None:
    with pytest.raises(KnowledgeConfigError) as excinfo:
        _load(tmp_path, manifest("https://example.atlassian.net").replace("[ACME]", "[acme]"))
    assert "integrations.jira.projects" in excinfo.value.message


def test_memorys_vocabulary_is_the_one_the_manifest_accepts() -> None:
    assert settings.MEMORY_TASK_KINDS == {"feature", "bugfix", "refactor", "incident", "migration", "spike"}
    assert settings.MEMORY_TASK_STATES == {
        "open", "analysis", "in_progress", "review", "verified", "done", "blocked", "cancelled"
    }
    assert set(jira_sync.DEFAULT_KINDS.values()) <= settings.MEMORY_TASK_KINDS


# --- secrets never leak ------------------------------------------------------------


def test_no_credential_reaches_the_report_or_a_log_line(
    tmp_path: Path,
    jira: tuple[FakeJira, str],
    memory: tuple[FakeMemoryTasks, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    jstate, url = jira
    mstate, murl = memory
    jstate.pages = [[issue("ACME-1", "2026-10-01T00:00:00Z"), issue("ACME-2", "2026-10-02T00:00:00Z")]]
    mstate.refusals["ACME-2"] = (409, "task_key_conflict")
    loaded = _load(tmp_path, manifest(url), KNOWLEDGE_MEMORY_URL=murl, MEMORY_HTTP_TOKEN=MEMORY_TOKEN)

    with caplog.at_level(logging.DEBUG):
        report = _run(loaded, dry_run=False)
        jstate.statuses = [(500, {})]
        with pytest.raises(KnowledgeToolError) as excinfo:
            _run(loaded, dry_run=False)

    seen = caplog.text + json.dumps(report) + str(excinfo.value)
    seen += jira_sync.state_path(loaded.default_vault()).read_text(encoding="utf-8")
    for secret in (JIRA_TOKEN, MEMORY_TOKEN, "bot@example.test", "must not echo"):
        assert secret not in seen


# --- the session hook's run ------------------------------------------------------------


def test_auto_runs_a_due_vault_and_then_waits_for_its_throttle(
    tmp_path: Path, jira: tuple[FakeJira, str], memory: tuple[FakeMemoryTasks, str]
) -> None:
    jstate, url = jira
    _, murl = memory
    jstate.pages = [[issue("ACME-1", "2026-10-01T00:00:00Z")]]
    body = manifest(url, extra="    sync: {throttle_minutes: 30}\n")
    loaded = _load(tmp_path, body, KNOWLEDGE_MEMORY_URL=murl, MEMORY_HTTP_TOKEN=MEMORY_TOKEN)

    first = jira_sync.auto(loaded, ENV, now=_now)
    second = jira_sync.auto(loaded, ENV, now=_now)
    later = jira_sync.auto(loaded, ENV, now=lambda: datetime(2026, 10, 12, 15, 31, tzinfo=UTC))

    assert first == [{"vault": "globex", "status": "synced", "fetched": 1, "upserted": 1, "created": 1}]
    assert second == [{"vault": "globex", "status": "skipped", "reason": "throttled"}]
    assert later[0]["status"] == "synced" and later[0]["created"] == 0


def test_auto_skips_an_unconfigured_vault_without_a_request(tmp_path: Path, jira: tuple[FakeJira, str]) -> None:
    state, url = jira
    loaded = _load(tmp_path, manifest(url))

    assert jira_sync.auto(loaded, {}, now=_now) == [{"vault": "globex", "status": "skipped", "reason": "unconfigured"}]
    assert state.requests == []
    assert not jira_sync.state_path(loaded.default_vault()).exists()


# --- the command line ------------------------------------------------------------------


def _cli_env(monkeypatch: pytest.MonkeyPatch, loaded: settings.Settings, murl: str | None = None) -> None:
    monkeypatch.setenv("KNOWLEDGE_VAULTS", str(loaded.vaults_file))
    monkeypatch.setenv("KNOWLEDGE_EMBEDDINGS_PROVIDER", "bag-of-words")
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    if murl:
        monkeypatch.setenv("KNOWLEDGE_MEMORY_URL", murl)


def test_the_cli_prints_one_line_or_the_json_report(
    tmp_path: Path,
    jira: tuple[FakeJira, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    jstate, url = jira
    jstate.pages = [[issue("ACME-1", "2026-10-01T00:00:00Z")]]
    _cli_env(monkeypatch, _load(tmp_path, manifest(url)))

    assert cli.main(["jira", "sync", "--vault", "globex", "--dry-run", "--since", "-30d"]) == 0
    line = capsys.readouterr().out
    assert line.startswith("jira sync globex -> memory globex: fetched 1, would upsert 1")

    assert cli.main(["jira", "sync", "--dry-run", "--since", "2026-09-01", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["dry_run"] is True and report["fetched"] == 1


@pytest.mark.parametrize(
    ("statuses", "env", "rc", "code"),
    [
        ([(500, {})], {}, 1, errors.E_JIRA_UNAVAILABLE),
        ([(401, {})], {}, 1, errors.E_JIRA_AUTH_FAILED),
        ([], {"GLOBEX_JIRA_TOKEN": ""}, 2, errors.E_SECRET_MISSING),
    ],
)
def test_the_cli_exit_status_says_whether_anything_was_attempted(
    tmp_path: Path,
    jira: tuple[FakeJira, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    statuses: list[tuple[int, dict[str, str]]],
    env: dict[str, str],
    rc: int,
    code: str,
) -> None:
    jstate, url = jira
    jstate.statuses = list(statuses)
    _cli_env(monkeypatch, _load(tmp_path, manifest(url)))
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    assert cli.main(["jira", "sync", "--dry-run"]) == rc
    err = capsys.readouterr().err
    assert err.startswith(f"{code}: ")
    assert "Traceback" not in err


def test_the_cli_auto_mode_always_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    loaded = _load(tmp_path, manifest("https://example.atlassian.net"))
    monkeypatch.setenv("KNOWLEDGE_VAULTS", str(loaded.vaults_file))
    monkeypatch.setenv("KNOWLEDGE_EMBEDDINGS_PROVIDER", "bag-of-words")

    assert cli.main(["jira", "sync", "--auto"]) == 0
    assert json.loads(capsys.readouterr().out) == [{"reason": "unconfigured", "status": "skipped", "vault": "globex"}]


def test_doctor_reports_the_jira_block_by_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    loaded = _load(tmp_path, manifest("https://example.atlassian.net"))
    monkeypatch.setenv("KNOWLEDGE_VAULTS", str(loaded.vaults_file))
    monkeypatch.setenv("KNOWLEDGE_EMBEDDINGS_PROVIDER", "bag-of-words")
    monkeypatch.setenv("GLOBEX_JIRA_EMAIL", "bot@example.test")

    cli.main(["doctor"])
    out = capsys.readouterr().out
    row = json.loads(out)["vaults"][0]

    assert row["jira"] == {"declared": True, "secrets": "missing:GLOBEX_JIRA_TOKEN", "memory": "E_MEMORY_TOKEN_MISSING"}
    assert "bot@example.test" not in out


# --- the tool ---------------------------------------------------------------------------


@pytest.fixture()
def _jira_registry(tmp_path: Path) -> Iterator[None]:
    from knowledge import server

    previous = settings.current()
    yield
    server.set_registry(None)
    settings.install(previous)


@pytest.mark.usefixtures("_jira_registry")
def test_the_tool_reads_the_vault_it_is_given(
    tmp_path: Path, jira: tuple[FakeJira, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from knowledge import server

    jstate, url = jira
    jstate.pages = [[issue("ACME-1", "2026-10-01T00:00:00Z")]]
    loaded = _load(tmp_path, manifest(url))
    settings.install(loaded)
    server.set_registry(server._registry_for(loaded))
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)

    report = asyncio.run(server.sync_jira(vault="globex", since="2026-09-01"))

    assert report["dry_run"] is True and report["fetched"] == 1
    assert server.registry().opened() == (), "the tool reads the vault's spec and opens no index"


@pytest.mark.usefixtures("_jira_registry")
def test_the_tool_refuses_a_bearer_without_admin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from knowledge import server

    loaded = _load(tmp_path, manifest("https://example.atlassian.net"))
    settings.install(loaded)
    server.set_registry(server._registry_for(loaded))
    request = SimpleNamespace(state=SimpleNamespace(knowledge_scopes=frozenset({"read"}), knowledge_principal="alice"))
    monkeypatch.setattr(server.mcp, "get_context", lambda: SimpleNamespace(request_context=SimpleNamespace(request=request)))

    with pytest.raises(KnowledgeToolError) as excinfo:
        asyncio.run(server.sync_jira(vault="globex"))
    assert excinfo.value.code == errors.E_SCOPE_REQUIRED
    assert "alice" in excinfo.value.message


def test_the_cli_caps_the_issues_it_reads(
    tmp_path: Path,
    jira: tuple[FakeJira, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    jstate, url = jira
    jstate.pages = [[issue("ACME-1", "2026-10-01T00:00:00Z")], [issue("ACME-2", "2026-10-02T00:00:00Z")]]
    _cli_env(monkeypatch, _load(tmp_path, manifest(url)))

    assert cli.main(["jira", "sync", "--dry-run", "--since", "2026-09-01", "--max-issues", "1", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert (report["fetched"], report["truncated"], report["next_since"]) == (1, True, "2026-10-01T00:00:00Z")

    assert cli.main(["jira", "sync", "--dry-run", "--max-issues", "0"]) == 2
    assert capsys.readouterr().err.startswith(f"{errors.E_ARGUMENT_OUT_OF_RANGE}: ")
