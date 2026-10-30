"""Read-only client of the Jira Cloud search API, standard library only.

The ``jira`` extension reads the issues of the projects a vault declares
under ``integrations.jira`` and mirrors them into memory tasks. This module
is the only place that speaks to Jira, so a change of the API stays here.

The endpoint is the enhanced JQL search of Jira Cloud,
``GET {site}/rest/api/3/search/jql``: ``jql``, ``fields`` and
``maxResults`` per page, paginated with the opaque ``nextPageToken`` each
page returns until ``isLast`` is true or no token comes back. The classic
``GET /rest/api/3/search`` answers 410 Gone on Jira Cloud. One issue is read
again by key with ``GET {site}/rest/api/3/issue/{key}`` (404: gone, or no
longer visible to the account). The account's time zone, the one JQL reads
dates in, comes from ``GET {site}/rest/api/3/myself`` (``timeZone``).

The rules that hold whatever Jira answers:

* Basic authentication with the account e-mail and an API token, read from
  the variables the vault names; the credentials travel only to the site the
  vault declares, which must be ``https`` on a host the settings allow
  (``*.atlassian.net`` or one the operator lists in
  ``KNOWLEDGE_INTEGRATION_SITES``), or plain ``http`` on a loopback host for a
  test double. The opener ignores the proxy variables and follows no redirect.
* Ten seconds per request. A 429 is retried once, after ``Retry-After``
  capped at five seconds.
* Errors carry a stable code and never the body Jira sent, the request URL
  or a credential: ``E_JIRA_AUTH_FAILED`` for 401 and 403,
  ``E_JIRA_UNAVAILABLE`` for everything else that is not a page of issues
  (a refused query, 5xx, a timeout, a body that is not JSON).
"""

from __future__ import annotations

import base64
import http.client
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from knowledge import errors
from knowledge.errors import KnowledgeToolError
from knowledge.settings import DEFAULT_INTEGRATION_SITES, SITE_RULE, integration_site_allowed

log = logging.getLogger(__name__)

#: Path of the enhanced JQL search, relative to the site.
SEARCH_PATH = "/rest/api/3/search/jql"
#: Path of the account the credentials belong to, relative to the site.
MYSELF_PATH = "/rest/api/3/myself"
#: Path of one issue, relative to the site; ``{key}`` is a validated issue key.
ISSUE_PATH = "/rest/api/3/issue/{key}"
#: A Jira issue key, the only thing that goes into :data:`ISSUE_PATH`.
ISSUE_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9]+-[0-9]+$")
#: Seconds one request may take, connection included.
TIMEOUT_SECONDS = 10.0
#: Issues asked for per page; Jira Cloud caps a page at 100 for these fields.
PAGE_SIZE = 100
#: Upper bound of issues one search returns; the run reports ``truncated`` past it.
MAX_ISSUES = 2000
#: Longest wait honoured from a ``Retry-After`` header, in seconds.
MAX_RETRY_AFTER_SECONDS = 5.0

Opener = Callable[..., Any]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: the credentials never follow Jira to another host."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        return None


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


@dataclass(frozen=True)
class SearchResult:
    """The issues of one search, in the order Jira returned them, and whether more were left."""

    issues: tuple[Mapping[str, Any], ...]
    truncated: bool


class JiraClient:
    """Searches one Jira Cloud site with one account."""

    def __init__(
        self,
        site: str,
        email: str,
        token: str,
        *,
        opener: Opener = _OPENER.open,
        timeout: float = TIMEOUT_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        sites: Sequence[str] = DEFAULT_INTEGRATION_SITES,
    ) -> None:
        site = site.strip().rstrip("/")
        if not integration_site_allowed(site, sites):
            raise KnowledgeToolError(errors.E_MANIFEST_INVALID, f"integrations.jira.site {SITE_RULE}")
        self._site = site
        self._auth = "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode("ascii")
        self._open = opener
        self._timeout = timeout
        self._sleep = sleep

    def __repr__(self) -> str:
        return f"JiraClient(site={self._site!r})"

    def search(
        self, jql: str, fields: Sequence[str], *, page_size: int = PAGE_SIZE, max_issues: int = MAX_ISSUES
    ) -> SearchResult:
        """Every issue ``jql`` matches, page by page, up to ``max_issues``."""
        issues: list[Mapping[str, Any]] = []
        token: str | None = None
        while True:
            params: dict[str, str] = {
                "jql": jql,
                "fields": ",".join(fields),
                "maxResults": str(max(1, min(page_size, max_issues - len(issues)))),
            }
            if token:
                params["nextPageToken"] = token
            page = self._get(SEARCH_PATH, params)
            if page is None:
                raise _unavailable("Jira answered HTTP 404", "http_404")
            batch = page.get("issues")
            if not isinstance(batch, list):
                raise _unavailable("the search answered without an issues list", "invalid_response")
            room = max_issues - len(issues)
            issues.extend(issue for issue in batch[:room] if isinstance(issue, Mapping))
            next_token = page.get("nextPageToken")
            last = page.get("isLast") is True or not isinstance(next_token, str) or not next_token
            if len(batch) > room or (len(issues) >= max_issues and not last):
                return SearchResult(tuple(issues), truncated=True)
            if last or not batch:
                return SearchResult(tuple(issues), truncated=False)
            token = next_token

    def time_zone(self) -> str | None:
        """The IANA time zone of the account (``America/Mexico_City``), or ``None`` when Jira does not say."""
        page = self._get(MYSELF_PATH, {})
        zone = page.get("timeZone") if page is not None else None
        return zone.strip() if isinstance(zone, str) and zone.strip() else None

    def issue(self, key: str, fields: Sequence[str]) -> Mapping[str, Any] | None:
        """The issue ``key`` with ``fields``, or ``None`` when Jira does not have it (or no longer shows it)."""
        if not ISSUE_KEY_PATTERN.fullmatch(key):
            raise KnowledgeToolError(errors.E_ARGUMENT_OUT_OF_RANGE, "a Jira issue key looks like PROJ-123")
        return self._get(ISSUE_PATH.format(key=key), {"fields": ",".join(fields)}, missing_ok=True)

    def _get(self, path: str, params: Mapping[str, str], *, missing_ok: bool = False) -> dict[str, Any] | None:
        request = urllib.request.Request(
            f"{self._site}{path}?{urllib.parse.urlencode(params)}",
            headers={"Authorization": self._auth, "Accept": "application/json"},
            method="GET",
        )
        for attempt in (1, 2):
            try:
                with self._open(request, timeout=self._timeout) as response:
                    raw = response.read()
                break
            except urllib.error.HTTPError as exc:
                status, retry_after = exc.code, exc.headers.get("Retry-After") if exc.headers else None
                exc.close()
                if status in (401, 403):
                    log.warning("jira search failed: %s (HTTP %d)", errors.E_JIRA_AUTH_FAILED, status)
                    raise KnowledgeToolError(
                        errors.E_JIRA_AUTH_FAILED,
                        f"Jira refused the credentials (HTTP {status}); check the e-mail and API token "
                        "variables integrations.jira.secrets names",
                    ) from None
                if status == 404 and missing_ok:
                    return None
                if status == 429 and attempt == 1:
                    self._sleep(_retry_after(retry_after))
                    continue
                if status == 400:
                    raise _unavailable(
                        "Jira refused the query (HTTP 400); check integrations.jira.projects and jql_extra",
                        "http_400",
                    ) from None
                raise _unavailable(f"Jira answered HTTP {status}", f"http_{status}") from None
            except (OSError, http.client.HTTPException) as exc:
                cause = exc.reason if isinstance(exc, urllib.error.URLError) else exc
                name = type(cause).__name__ if isinstance(cause, BaseException) else "URLError"
                raise _unavailable(f"Jira could not be reached ({name})", name) from None
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            raise _unavailable("Jira answered with a body that is not a JSON object", "invalid_response")
        return payload


def _retry_after(raw: str | None) -> float:
    try:
        seconds = float(raw) if raw is not None else 1.0
    except ValueError:
        seconds = 1.0
    return min(max(seconds, 0.0), MAX_RETRY_AFTER_SECONDS)


def _unavailable(message: str, reason: str) -> KnowledgeToolError:
    log.warning("jira search failed: %s (%s)", errors.E_JIRA_UNAVAILABLE, reason)
    return KnowledgeToolError(errors.E_JIRA_UNAVAILABLE, message)
