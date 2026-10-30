"""Loopback doubles of Jira Cloud and of the memory task route, for the jira extension.

Both are standard-library ``http.server`` instances on ``127.0.0.1`` and an
ephemeral port. ``FakeJira`` answers ``GET /rest/api/3/search/jql`` the way
Jira Cloud does (``issues``, ``isLast``, ``nextPageToken``), one scripted page
per token, or, with ``live``, by filtering its issues on the ``updated >=``
minute of the JQL and ordering them by ``updated`` and key, as Jira does; it
answers ``GET /rest/api/3/issue/{key}`` from ``by_key`` (404 when absent) and
records every request. ``FakeMemoryTasks`` answers
``POST /projects/{slug}/tasks`` like memory: 201 with ``created: true`` for a
``jira_key`` it has not seen, 200 for one it has, or whatever a test scripts
for a key. Nothing here reaches a real Jira or memory: ``conftest`` fails any
connection off the loopback and to port 6366.
"""

from __future__ import annotations

import base64
import json
import re
import threading
import time
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

JIRA_EMAIL = "bot@example.test"
JIRA_TOKEN = "fake-jira-token-not-a-credential"
MEMORY_TOKEN = "fake-memory-bearer-not-a-credential"
SEARCH_PATH = "/rest/api/3/search/jql"
ISSUE_PREFIX = "/rest/api/3/issue/"
MYSELF_PATH = "/rest/api/3/myself"
_SINCE = re.compile(r'updated >= "(\d{4}-\d{2}-\d{2} \d{2}:\d{2})"')


def issue(
    key: str,
    updated: str,
    *,
    summary: str = "An issue",
    issue_type: str = "Story",
    status: str = "In Progress",
    category: str | None = "indeterminate",
    assignee: str | None = "Ada",
) -> dict[str, Any]:
    """One issue as the search returns it with ``fields=summary,status,issuetype,assignee,updated``."""
    status_node: dict[str, Any] = {"name": status, "id": "3"}
    if category is not None:
        status_node["statusCategory"] = {"key": category, "id": 4, "name": category}
    return {
        "id": key.split("-")[-1],
        "key": key,
        "self": f"https://jira.example.test/rest/api/3/issue/{key}",
        "fields": {
            "summary": summary,
            "issuetype": {"name": issue_type},
            "status": status_node,
            "assignee": {"displayName": assignee} if assignee else None,
            "updated": updated,
        },
    }


@dataclass
class FakeJira:
    """Pages of issues, by the ``nextPageToken`` that asks for them (``None`` for the first)."""

    pages: list[list[dict[str, Any]]] = field(default_factory=list)
    #: Statuses answered instead of a page, first to last, one per request.
    statuses: list[tuple[int, dict[str, str]]] = field(default_factory=list)
    delay: float = 0.0
    requests: list[dict[str, Any]] = field(default_factory=list)
    #: Issues searched the way Jira does, instead of ``pages``, when set.
    live: list[dict[str, Any]] | None = None
    #: Issues ``GET /rest/api/3/issue/{key}`` answers; any other key is 404.
    by_key: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: ``timeZone`` of ``GET /rest/api/3/myself``, the zone the JQL dates are read in; ``None`` leaves it out.
    time_zone: str | None = "UTC"

    @property
    def searches(self) -> list[dict[str, Any]]:
        """The search requests, in order, without the account and issue reads."""
        return [r for r in self.requests if r["path"] == SEARCH_PATH]

    def serve_myself(self) -> tuple[int, Any, dict[str, str]]:
        body: dict[str, Any] = {"accountId": "fake-account", "displayName": "Bot"}
        if self.time_zone is not None:
            body["timeZone"] = self.time_zone
        return 200, body, {}

    def serve_issue(self, key: str) -> tuple[int, Any, dict[str, str]]:
        found = self.by_key.get(key)
        if found is None:
            return 404, {"errorMessages": ["Issue does not exist or you do not have permission to see it."]}, {}
        return 200, found, {}

    def _live_page(self, query: dict[str, str]) -> tuple[int, Any, dict[str, str]]:
        assert self.live is not None
        match = _SINCE.search(query.get("jql", ""))
        zone = ZoneInfo(self.time_zone or "UTC")
        floor = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=zone) if match else None

        def updated(row: dict[str, Any]) -> datetime:
            return datetime.fromisoformat(row["fields"]["updated"].replace("Z", "+00:00")).astimezone(UTC)

        def order(row: dict[str, Any]) -> tuple[datetime, str, int]:
            project, number = row["key"].rsplit("-", 1)
            return updated(row), project, int(number)

        rows = sorted((row for row in self.live if floor is None or updated(row) >= floor), key=order)
        start = int((query.get("nextPageToken") or "live-0").removeprefix("live-"))
        size = int(query.get("maxResults") or 100)
        body: dict[str, Any] = {"issues": rows[start : start + size]}
        last = start + size >= len(rows)
        if not last:
            body["nextPageToken"] = f"live-{start + size}"
        body["isLast"] = last
        return 200, body, {}

    def serve(self, query: dict[str, str]) -> tuple[int, Any, dict[str, str]]:
        if self.statuses:
            status, headers = self.statuses.pop(0)
            return status, {"errorMessages": ["body text the client must not echo"]}, headers
        if self.live is not None:
            return self._live_page(query)
        token = query.get("nextPageToken")
        index = int(token.removeprefix("page-")) if token else 0
        page = self.pages[index] if index < len(self.pages) else []
        size = int(query.get("maxResults") or 100)
        body: dict[str, Any] = {"issues": page[:size]}
        last = index + 1 >= len(self.pages)
        if not last:
            body["nextPageToken"] = f"page-{index + 1}"
        body["isLast"] = last
        return 200, body, {}


@dataclass
class FakeMemoryTasks:
    """memory's task upsert: created once per key, updated after, or a scripted refusal per key."""

    token: str | None = MEMORY_TOKEN
    refusals: dict[str, tuple[int, str]] = field(default_factory=dict)
    known: set[str] = field(default_factory=set)
    requests: list[dict[str, Any]] = field(default_factory=list)

    def serve(self, body: dict[str, Any]) -> tuple[int, Any]:
        key = body.get("jira_key", "")
        if key in self.refusals:
            status, code = self.refusals[key]
            return status, {"error": "body text the client must not echo", "code": code}
        created = key not in self.known
        self.known.add(key)
        return (201 if created else 200), {"task": {"jira_key": key}, "created": created, "card_created": False}


def _jira_handler(state: FakeJira) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            del format, args

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlsplit(self.path)
            query = dict(urllib.parse.parse_qsl(parsed.query))
            auth = self.headers.get("Authorization", "")
            state.requests.append({"path": parsed.path, "query": query, "authorization": auth})
            if state.delay:
                time.sleep(state.delay)
            expected = "Basic " + base64.b64encode(f"{JIRA_EMAIL}:{JIRA_TOKEN}".encode()).decode()
            if parsed.path not in (SEARCH_PATH, MYSELF_PATH) and not parsed.path.startswith(ISSUE_PREFIX):
                status, body, headers = 404, {"errorMessages": ["no route"]}, {}
            elif auth != expected:
                status, body, headers = 401, {"errorMessages": ["unauthorized"]}, {}
            elif parsed.path == MYSELF_PATH:
                status, body, headers = state.serve_myself()
            elif parsed.path.startswith(ISSUE_PREFIX):
                status, body, headers = state.serve_issue(parsed.path.removeprefix(ISSUE_PREFIX))
            else:
                status, body, headers = state.serve(query)
            raw = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(raw)

    return Handler


def _memory_handler(state: FakeMemoryTasks) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            del format, args

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            state.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
            if state.token is not None and self.headers.get("Authorization") != f"Bearer {state.token}":
                status, payload = 401, {"error": "invalid token", "code": "unauthorized"}
            elif not self.path.endswith("/tasks"):
                status, payload = 404, {"error": "no route", "code": "not_found"}
            else:
                status, payload = state.serve(body)
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    return Handler


@contextmanager
def _serving(handler: type[BaseHTTPRequestHandler]) -> Iterator[str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


@contextmanager
def jira_server(state: FakeJira) -> Iterator[str]:
    """``state`` served on the loopback; yields the site URL."""
    with _serving(_jira_handler(state)) as url:
        yield url


@contextmanager
def memory_server(state: FakeMemoryTasks) -> Iterator[str]:
    """``state`` served on the loopback; yields the memory URL."""
    with _serving(_memory_handler(state)) as url:
        yield url


def manifest(site: str, *, extra: str = "") -> str:
    """A ``vault.yaml`` body that declares ``jira`` against ``site`` with the fake credentials' names."""
    return (
        "features: [jira]\n"
        "memory:\n  project: globex\n"
        "integrations:\n"
        "  jira:\n"
        f"    site: {site}\n"
        "    projects: [ACME]\n"
        "    secrets: {email_env: GLOBEX_JIRA_EMAIL, token_env: GLOBEX_JIRA_TOKEN}\n"
        f"{extra}"
    )


#: The environment a run needs with :func:`manifest`.
ENV: dict[str, str] = {
    "GLOBEX_JIRA_EMAIL": JIRA_EMAIL,
    "GLOBEX_JIRA_TOKEN": JIRA_TOKEN,
    "MEMORY_HTTP_TOKEN": MEMORY_TOKEN,
}
