"""Confluence: report which vault pages are behind, and export them outside the vault.

The vault changes only through a pull request a person reviews (D-11), so
nothing here writes into a vault. :func:`sync` is read-only: it compares the
vault files that carry a ``confluence_id`` with their Confluence pages and
reports which ones are behind; asked to write (``dry_run=False``) it refuses
with ``E_WRITE_FENCED`` before it reads a credential. :func:`export` writes the
converted pages into a directory outside every served vault, from where a
person promotes them by pull request.

Credentials come from the vault's ``integrations.confluence`` block of
``vault.yaml`` (``site``, ``secrets.email_env``, ``secrets.token_env``: the
names of the variables, never their values) and, for whatever it leaves
undeclared, from ``CONFLUENCE_URL``, ``CONFLUENCE_EMAIL`` and
``CONFLUENCE_TOKEN``.
"""

from __future__ import annotations

import contextvars
import logging
import os
import re
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from knowledge import config, errors, settings

log = logging.getLogger(__name__)

# urlopen speaks more than http(s); everything else is refused in _api_get.
_ALLOWED_URL_SCHEMES = frozenset({"http", "https"})

# Sync scope: only files under this prefix (within VAULT_PATH) are tracked and synced from Confluence.
# By default, only "Conventions/" is kept in sync (ADR-011, point 4).
_DEFAULT_SYNC_SCOPE_PREFIX = "Conventions/"

#: Default variables of the three Confluence settings, used for whatever the
#: vault's ``integrations.confluence`` block leaves undeclared.
URL_ENV = "CONFLUENCE_URL"
EMAIL_ENV = "CONFLUENCE_EMAIL"
TOKEN_ENV = "CONFLUENCE_TOKEN"

#: Where a ``sync_confluence(dry_run=False)`` caller is sent instead.
FENCED_MESSAGE = (
    "sync_confluence writes nothing into a vault; run `cloudfrog-knowledge confluence export "
    "--vault <slug> --out <dir>` outside the vault and promote the files by pull request"
)

# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Credentials:
    """Where Confluence is and who calls it, plus the names those values came from.

    ``url_source``, ``email_env`` and ``token_env`` name settings, never
    values: they are what an error message may show.
    """

    url: str
    email: str
    token: str = field(repr=False)
    url_source: str = URL_ENV
    email_env: str = EMAIL_ENV
    token_env: str = TOKEN_ENV

    @property
    def configured(self) -> bool:
        return bool(self.url and self.email and self.token)

    @property
    def is_default(self) -> bool:
        """Whether every value comes from the default ``CONFLUENCE_*`` variables."""
        return (self.url_source, self.email_env, self.token_env) == (URL_ENV, EMAIL_ENV, TOKEN_ENV)

    def missing(self) -> list[str]:
        """Names (never values) of the settings left empty, in URL, email, token order."""
        pairs = ((self.url_source, self.url), (self.email_env, self.email), (self.token_env, self.token))
        return [name for name, value in pairs if not value]


def _declared(block: Any, key: str) -> str:
    value = block.get(key) if isinstance(block, Mapping) else None
    return value.strip() if isinstance(value, str) else ""


def credentials(
    integrations: Mapping[str, Any] | None = None, env: Mapping[str, str] | None = None
) -> Credentials:
    """The Confluence credentials of a vault whose ``vault.yaml`` declares ``integrations``.

    ``integrations.confluence.site`` is the base URL; ``secrets.email_env`` and
    ``secrets.token_env`` name the variables the email and the API token are
    read from (``env``, by default the process environment). Anything the
    block leaves undeclared falls back to ``CONFLUENCE_URL``,
    ``CONFLUENCE_EMAIL`` and ``CONFLUENCE_TOKEN`` as the settings read them.

    The settings already refuse such a block at load; this check repeats it
    for a direct caller: a ``site`` off the allowed hosts (or plain http off
    the loopback) or a variable that is not a Confluence one (see
    ``settings.integration_access_problems``) is ``E_MANIFEST_INVALID`` before any
    variable is read, so no server secret can be sent to a vault-declared site.
    """
    source = os.environ if env is None else env
    problems = [
        (where, why)
        for where, why in settings.integration_access_problems(integrations or {}, source)
        if where.startswith("integrations.confluence")
    ]
    if problems:
        where, why = problems[0]
        raise errors.KnowledgeConfigError(errors.E_MANIFEST_INVALID, f"{where} {why}")
    block = (integrations or {}).get("confluence")
    secrets = block.get("secrets") if isinstance(block, Mapping) else None
    site = _declared(block, "site")
    email_env = _declared(secrets, "email_env") or EMAIL_ENV
    token_env = _declared(secrets, "token_env") or TOKEN_ENV
    return Credentials(
        url=site or config.CONFLUENCE_URL,
        email=config.CONFLUENCE_EMAIL if email_env == EMAIL_ENV else (source.get(email_env) or "").strip(),
        token=config.CONFLUENCE_TOKEN if token_env == TOKEN_ENV else (source.get(token_env) or "").strip(),
        url_source="integrations.confluence.site" if site else URL_ENV,
        email_env=email_env,
        token_env=token_env,
    )


#: The credentials of the call in progress; unset, every helper reads the
#: default ``CONFLUENCE_*`` settings. A context variable, so concurrent calls
#: on worker threads never see each other's.
_ACTIVE: contextvars.ContextVar[Credentials | None] = contextvars.ContextVar(
    "knowledge_confluence_credentials", default=None
)


def _current() -> Credentials:
    active = _ACTIVE.get()
    return active if active is not None else credentials()


@contextmanager
def _using(creds: Credentials) -> Iterator[None]:
    token = _ACTIVE.set(creds)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def unconfigured_message(creds: Credentials) -> str:
    """What :func:`sync` says without credentials: the settings to set, never their values."""
    names = ", ".join(creds.missing())
    if creds.is_default:
        return (
            f"sync_confluence needs {names}; export them or declare them under "
            "integrations.confluence in the vault's vault.yaml"
        )
    return f"sync_confluence needs {names}, which the vault's integrations.confluence names; export them"


# ---------------------------------------------------------------------------
# The write fence
# ---------------------------------------------------------------------------


def check_fence(dry_run: bool) -> None:
    """Refuse a sync that would write into a vault: ``E_WRITE_FENCED``, before anything is read."""
    if dry_run is not True:
        raise errors.KnowledgeToolError(errors.E_WRITE_FENCED, FENCED_MESSAGE)


def _inside(path: Path, roots: Iterable[Path]) -> Path | None:
    """The first of ``roots`` that contains ``path`` (both resolved), or ``None``."""
    resolved = path.expanduser().resolve()
    for root in roots:
        root_resolved = root.expanduser().resolve()
        if resolved == root_resolved or resolved.is_relative_to(root_resolved):
            return root
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_configured() -> bool:
    return _current().configured


def _headers() -> dict[str, str]:
    import base64

    active = _current()
    creds = base64.b64encode(f"{active.email}:{active.token}".encode()).decode()
    return {
        "Authorization": f"Basic {creds}",
        "Accept": "application/json",
    }


def _api_get(path: str, params: dict[str, str] | None = None, *, _retries: int = 3) -> dict[str, Any]:
    """Make a GET request to the Confluence REST API with retry and rate-limit delay."""
    import time
    import urllib.error
    import urllib.request
    import json as _json

    active = _current()
    url = urljoin(active.url.rstrip("/") + "/", path.lstrip("/"))
    if params:
        from urllib.parse import urlencode
        url = f"{url}?{urlencode(params)}"

    # urlopen honours file:// and ftp:// as happily as https://. The base URL
    # comes from configuration, so a bad value here would turn this call into an
    # arbitrary-file read that also ships the Authorization header built by
    # _headers(). Pin the scheme before the request object is ever opened.
    scheme = urlsplit(url).scheme.lower()
    if scheme not in _ALLOWED_URL_SCHEMES:
        raise RuntimeError(
            f"Refusing to call Confluence over {scheme or 'an empty scheme'!r}: "
            f"{active.url_source} must be an http(s) URL."
        )

    req = urllib.request.Request(url, headers=_headers())
    last_exc: Exception | None = None

    for attempt in range(_retries):
        try:
            if attempt > 0:
                time.sleep(1.0 * attempt)  # backoff: 1s, 2s
            # SUPPRESSION, not a fix: the rule below still matches this line.
            # `dynamic-urllib-use-detected` fires on any urlopen() whose target
            # is not a string literal, and this one never can be — the
            # Confluence base URL is configuration. Removing the comment turns
            # the ring-1 SAST step red again with "Findings: 1 (1 blocking)";
            # nothing about the call stopped triggering it.
            #
            # What removes the underlying risk is the `_ALLOWED_URL_SCHEMES`
            # check above, which refuses file://, ftp:// and every other scheme
            # before a Request object exists — behaviour pinned by
            # tests/test_maintenance_tools.py::
            # test_a_non_http_confluence_url_is_refused_before_any_request.
            # The suppression is registered in tests/test_suppressions.py; an
            # undeclared one fails the suite.
            # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = _json.loads(resp.read().decode())
                time.sleep(0.5)  # rate-limit: 0.5s between requests
                return result  # type: ignore[no-any-return]
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            raise RuntimeError(f"Confluence API {exc.code}: {body}") from exc
        except (ConnectionError, OSError) as exc:
            last_exc = exc
            log.warning("Confluence request failed (attempt %d/%d): %s", attempt + 1, _retries, exc)

    raise RuntimeError(f"Confluence API failed after {_retries} retries: {last_exc}")


# ---------------------------------------------------------------------------
# Content conversion (basic Confluence → Markdown)
# ---------------------------------------------------------------------------

_TAG_MAP: dict[str, str] = {
    "h1": "# ",
    "h2": "## ",
    "h3": "### ",
    "h4": "#### ",
    "h5": "##### ",
    "h6": "###### ",
}


def _html_to_markdown(html: str) -> str:
    """Very basic HTML → Markdown conversion (good enough for Confluence pages)."""
    text = html

    # Headings
    for tag, prefix in _TAG_MAP.items():
        text = re.sub(
            rf"<{tag}[^>]*>(.*?)</{tag}>",
            lambda m, p=prefix: f"\n{p}{m.group(1).strip()}\n",
            text,
            flags=re.DOTALL,
        )

    # Bold / italic / code
    text = re.sub(r"<strong>(.*?)</strong>", r"**\1**", text, flags=re.DOTALL)
    text = re.sub(r"<em>(.*?)</em>", r"*\1*", text, flags=re.DOTALL)
    text = re.sub(r"<code>(.*?)</code>", r"`\1`", text, flags=re.DOTALL)

    # Links
    text = re.sub(r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', r"[\2](\1)", text, flags=re.DOTALL)

    # List items
    text = re.sub(r"<li[^>]*>(.*?)</li>", r"- \1\n", text, flags=re.DOTALL)

    # Paragraphs / line breaks
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"<p[^>]*>(.*?)</p>", r"\1\n\n", text, flags=re.DOTALL)

    # Code blocks
    text = re.sub(
        r"<ac:structured-macro[^>]*ac:name=\"code\"[^>]*>.*?<ac:plain-text-body><!\[CDATA\[(.*?)\]\]></ac:plain-text-body>.*?</ac:structured-macro>",
        r"\n```\n\1\n```\n",
        text,
        flags=re.DOTALL,
    )

    # Strip remaining tags
    text = re.sub(r"<[^>]+>", "", text)
    # Collapse blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_page(page_id: str) -> dict[str, Any]:
    """Fetch a single Confluence page with its body."""
    data = _api_get(
        f"/wiki/api/v2/pages/{page_id}",
        params={"body-format": "storage"},
    )
    return {
        "id": data["id"],
        "title": data.get("title", ""),
        "body_html": data.get("body", {}).get("storage", {}).get("value", ""),
        "version": data.get("version", {}).get("number", 0),
        "last_modified": data.get("version", {}).get("createdAt", ""),
    }


def fetch_descendants(page_id: str) -> list[dict[str, Any]]:
    """Fetch all descendant pages of a given page."""
    results: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        params: dict[str, str] = {"limit": "50"}
        if cursor:
            params["cursor"] = cursor
        data = _api_get(f"/wiki/api/v2/pages/{page_id}/children", params=params)
        for child in data.get("results", []):
            results.append({"id": child["id"], "title": child.get("title", "")})
            # Recurse
            results.extend(fetch_descendants(child["id"]))
        # Pagination
        next_link = data.get("_links", {}).get("next")
        if next_link:
            # Extract cursor from next link
            import urllib.parse
            parsed = urllib.parse.urlparse(next_link)
            qs = urllib.parse.parse_qs(parsed.query)
            cursor = qs.get("cursor", [None])[0]
            if cursor is None:
                break
        else:
            break
    return results


def fetch_space_pages(space_key: str) -> list[dict[str, Any]]:
    """Fetch all pages in a Confluence space."""
    results: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        params: dict[str, str] = {"space-key": space_key, "limit": "50", "status": "current"}
        if cursor:
            params["cursor"] = cursor
        data = _api_get("/wiki/api/v2/pages", params=params)
        for page in data.get("results", []):
            results.append({"id": page["id"], "title": page.get("title", "")})
        next_link = data.get("_links", {}).get("next")
        if next_link:
            import urllib.parse
            parsed = urllib.parse.urlparse(next_link)
            qs = urllib.parse.parse_qs(parsed.query)
            cursor = qs.get("cursor", [None])[0]
            if cursor is None:
                break
        else:
            break
    return results


def _vault_root(vault_path: Path | None) -> Path:
    """The vault to scan: the caller's, or the configured one."""
    return Path(config.VAULT_PATH) if vault_path is None else vault_path


def _scan_vault_confluence_ids(
    scope_prefix: str = _DEFAULT_SYNC_SCOPE_PREFIX, vault_path: Path | None = None
) -> dict[str, Path]:
    """Scan the vault for files with confluence_id in frontmatter, scoped to a prefix.

    Returns a mapping of confluence_id → vault file path.
    Only checks files that:
    1. We previously imported from Confluence (have confluence_id in frontmatter)
    2. Are under the specified scope_prefix (relative to VAULT_PATH)

    By default, only files under "Conventions/" are scanned (ADR-011, point 4).
    """
    import frontmatter as fm

    mapping: dict[str, Path] = {}
    vault = _vault_root(vault_path)
    scope_dir = vault / scope_prefix.rstrip("/")

    # Only scan files under the scope prefix
    if not scope_dir.is_dir():
        # Scope directory does not exist; no files to scan
        return mapping

    for md_path in scope_dir.rglob("*.md"):
        try:
            post = fm.load(str(md_path))
            cid = post.metadata.get("confluence_id")
            if cid:
                mapping[str(cid)] = md_path
        except Exception:
            continue

    return mapping


def _scan_vault_out_of_scope_confluence_ids(
    scope_prefix: str = _DEFAULT_SYNC_SCOPE_PREFIX, vault_path: Path | None = None
) -> dict[str, Path]:
    """Scan the vault for files with confluence_id that are OUT OF SCOPE.

    Returns a mapping of confluence_id → vault file path for all files that have
    confluence_id but are NOT under the scope_prefix. These files are tracked
    but explicitly excluded from sync.

    By default, any file with confluence_id outside "Conventions/" is reported here (ADR-011, point 4).
    """
    import frontmatter as fm

    mapping: dict[str, Path] = {}
    vault = _vault_root(vault_path)
    scope_dir = vault / scope_prefix.rstrip("/")

    for md_path in vault.rglob("*.md"):
        # Skip files inside the scope directory
        if scope_dir.is_dir() and md_path.is_relative_to(scope_dir):
            continue

        try:
            post = fm.load(str(md_path))
            cid = post.metadata.get("confluence_id")
            if cid:
                mapping[str(cid)] = md_path
        except Exception:
            continue

    return mapping


_CONFLUENCE_ID_RE = re.compile(r"[0-9]+")


def _is_valid_confluence_id(cid: str) -> bool:
    """Confluence Cloud page ids are non-negative integers, serialized as strings.

    Any other shape could not name a real page: handing it to fetch_page()
    would only spend a network round trip to have the API reject it, which
    conflates an input-validation failure with a transport failure (both land
    in the same generic error path otherwise). Checking the shape locally
    means an invalid id never reaches the network at all.

    ``str.isdigit()`` is not this check: it returns True for any Unicode
    codepoint with a "digit" numeric-type property, which includes non-ASCII
    digits from other numbering systems (e.g. Bengali "০"/"১২৩") and
    superscript/circled digit forms (e.g. "²³", "①②③") that are not even
    parseable by int(). A confluence_id is only ever an ASCII digit string,
    so the match is anchored to that character class explicitly rather than
    relying on what Unicode considers a "digit".
    """
    s = str(cid)
    return bool(s) and _CONFLUENCE_ID_RE.fullmatch(s) is not None


def _needs_update(page_data: dict[str, Any], vault_file: Path) -> bool:
    """Check whether the vault file is stale compared to Confluence."""
    if not vault_file.exists():
        return True

    import frontmatter as fm
    try:
        post = fm.load(str(vault_file))
        local_date = str(post.metadata.get("last_updated", ""))
    except Exception:
        return True

    remote_modified = page_data.get("last_modified", "")
    if not remote_modified:
        return True

    try:
        remote_dt = datetime.fromisoformat(remote_modified.replace("Z", "+00:00"))
        # Compare against frontmatter last_updated (date string like "2026-04-11")
        if len(local_date) == 10:  # "YYYY-MM-DD"
            local_dt = datetime.fromisoformat(local_date + "T00:00:00+00:00")
        else:
            local_dt = datetime.fromisoformat(local_date.replace("Z", "+00:00"))
        return remote_dt > local_dt
    except (ValueError, OSError):
        return True


def _out_of_scope_rows(out_of_scope_map: Mapping[str, Path], vault: Path) -> list[dict[str, str]]:
    return [
        {
            "confluence_id": cid,
            "vault_path": str(path.relative_to(vault)),
        }
        for cid, path in sorted(out_of_scope_map.items())
    ]


#: Out-of-scope files :func:`_empty_scope_message` names before it only counts the rest.
_EMPTY_SCOPE_LISTED = 20


def _empty_scope_text(scope_prefix: str) -> str:
    return (
        f"No files with confluence_id found in scope '{scope_prefix}'. "
        "This is an error condition: either the scope is misconfigured, "
        "or the mapping from Confluence pages to vault files has not been established."
    )


def _empty_scope_message(scope_prefix: str, out_of_scope_map: Mapping[str, Path], vault: Path) -> str:
    """``E_SCOPE_EMPTY`` of :func:`sync`: the scope, and the tracked files that live outside it."""
    outside = [row["vault_path"] for row in _out_of_scope_rows(out_of_scope_map, vault)]
    if not outside:
        return f"{_empty_scope_text(scope_prefix)} No file of the vault carries a confluence_id."
    listed = ", ".join(outside[:_EMPTY_SCOPE_LISTED])
    more = len(outside) - _EMPTY_SCOPE_LISTED
    tail = f" and {more} more" if more > 0 else ""
    return (
        f"{_empty_scope_text(scope_prefix)} Files outside it that carry one ({len(outside)}): {listed}{tail}; "
        "pass the scope_prefix that holds them"
    )


def _empty_scope(scope_prefix: str, out_of_scope_map: Mapping[str, Path], vault: Path) -> dict[str, Any]:
    """The ``confluence export`` report of a scope with no tracked file: an error, not a silent success."""
    return {
        "error": _empty_scope_text(scope_prefix),
        "scope_prefix": scope_prefix,
        "total_in_scope": 0,
        "total_out_of_scope": len(out_of_scope_map),
        "out_of_scope_files": _out_of_scope_rows(out_of_scope_map, vault),
    }


def _behind(
    vault_map: Mapping[str, Path], vault: Path, report: dict[str, Any]
) -> Iterator[tuple[str, str, Path, dict[str, Any]]]:
    """Compare every tracked file with its page; yield ``(cid, rel_path, file, page)`` for each one behind.

    Files whose id is malformed or whose page cannot be fetched go to
    ``report["errors"]``, files Confluence has nothing newer for to
    ``report["up_to_date"]`` and the ones behind to ``report["needs_update"]``.
    """
    for cid, vault_file in vault_map.items():
        if not _is_valid_confluence_id(cid):
            report["errors"].append({
                "confluence_id": cid,
                "file": str(vault_file.relative_to(vault)),
                "error": (
                    f"Invalid confluence_id format: {cid!r} is not a numeric "
                    "Confluence page id. Skipped before contacting Confluence."
                ),
            })
            continue

        try:
            page_data = fetch_page(cid)
        except Exception as exc:
            report["errors"].append({
                "confluence_id": cid,
                "file": str(vault_file.relative_to(vault)),
                "error": str(exc),
            })
            continue

        if not _needs_update(page_data, vault_file):
            report["up_to_date"].append(str(vault_file.relative_to(vault)))
            continue

        rel_path = str(vault_file.relative_to(vault))
        report["needs_update"].append({
            "file": rel_path,
            "title": page_data["title"],
            "confluence_id": cid,
            "remote_modified": page_data.get("last_modified", ""),
        })
        yield cid, rel_path, vault_file, page_data


def sync(
    dry_run: bool = True,
    scope_prefix: str = _DEFAULT_SYNC_SCOPE_PREFIX,
    vault_path: Path | None = None,
    integrations: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Report which vault files that came from Confluence are behind their page.

    Scans the vault for files with ``confluence_id`` in their frontmatter and
    located under ``scope_prefix`` (default: "Conventions/"), and checks
    whether Confluence has a newer version of each. Files with
    ``confluence_id`` outside the scope are reported explicitly as
    out-of-scope (ADR-011, point 4).

    Read-only: it never writes into the vault. ``dry_run=False`` raises
    ``KnowledgeToolError`` with ``E_WRITE_FENCED`` before any credential or
    file is read; :func:`export` writes the pages outside the vault instead.
    Without credentials it raises ``E_SECRET_MISSING`` naming the settings to
    set, and a scope that holds no tracked file raises ``E_SCOPE_EMPTY``: an
    error is never a report.

    Args:
        dry_run: Must stay True; anything else is ``E_WRITE_FENCED``.
        scope_prefix: Vault directory prefix to scan (must be relative to VAULT_PATH).
                     Default is "Conventions/".
        vault_path: The vault to read; defaults to the configured VAULT_PATH.
        integrations: The vault's ``integrations`` from ``vault.yaml``; its
            ``confluence`` block names where the credentials come from.

    Returns:
        A report dict with keys:
        - dry_run: Always True.
        - scope_prefix: The prefix that was scanned.
        - total_in_scope: Number of files with confluence_id found in scope.
        - total_out_of_scope: Number of files with confluence_id found outside scope.
        - out_of_scope_files: List of out-of-scope files (not synced).
        - up_to_date: List of files that don't need updating.
        - needs_update: List of files that have newer versions on Confluence.
        - updated: Always empty: nothing is written into a vault.
        - errors: List of sync errors (includes files whose confluence_id is
          not a numeric string — those are rejected before any request to
          Confluence, and never fetch_page/network related failures).
    """
    check_fence(dry_run)
    creds = credentials(integrations)
    with _using(creds):
        return _sync_report(dry_run, scope_prefix, _vault_root(vault_path), creds)


def _sync_report(dry_run: bool, scope_prefix: str, vault: Path, creds: Credentials) -> dict[str, Any]:
    """The body of :func:`sync`, run with ``creds`` as the active credentials."""
    if not _is_configured():
        raise errors.KnowledgeToolError(errors.E_SECRET_MISSING, unconfigured_message(creds))

    # Scan vault for files within scope
    vault_map = _scan_vault_confluence_ids(scope_prefix=scope_prefix, vault_path=vault)

    # Scan vault for files out of scope (for explicit reporting)
    out_of_scope_map = _scan_vault_out_of_scope_confluence_ids(
        scope_prefix=scope_prefix, vault_path=vault
    )

    # Empty scan within scope is an error, not success
    if not vault_map:
        message = _empty_scope_message(scope_prefix, out_of_scope_map, vault)
        raise errors.KnowledgeToolError(errors.E_SCOPE_EMPTY, message)

    report: dict[str, Any] = {
        "dry_run": dry_run,
        "scope_prefix": scope_prefix,
        "total_in_scope": len(vault_map),
        "total_out_of_scope": len(out_of_scope_map),
        "out_of_scope_files": _out_of_scope_rows(out_of_scope_map, vault),
        "up_to_date": [],
        "needs_update": [],
        "updated": [],
        "errors": [],
    }

    for _ in _behind(vault_map, vault, report):
        pass

    return report


def _stamp(remote_modified: str) -> str:
    """The ``last_updated`` of an exported page: the day Confluence last changed it, else today (UTC)."""
    try:
        return datetime.fromisoformat(remote_modified.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except (AttributeError, ValueError):
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def export(
    out_dir: Path,
    *,
    vault_path: Path,
    served_vaults: Iterable[Path] = (),
    space_key: str = "CB",
    scope_prefix: str = _DEFAULT_SYNC_SCOPE_PREFIX,
    integrations: Mapping[str, Any] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Write the Confluence pages the vault is behind on under ``out_dir``, never into a vault.

    Each tracked file under ``scope_prefix`` whose page is newer is written
    to ``out_dir/<its vault-relative path>``: the vault file's frontmatter,
    with ``last_updated`` set to the day the page last changed, over the page
    converted to Markdown. A person copies the files into a branch of the
    vault repository and promotes them by pull request.

    ``out_dir`` inside ``vault_path`` or inside any of ``served_vaults`` is
    ``E_WRITE_FENCED``, checked before credentials are read; every file is
    checked again, resolved, right before it is written. Missing credentials
    are ``E_SECRET_MISSING`` naming the variables. ``dry_run=True`` reports
    the same files and writes nothing. ``space_key`` is reserved
    and unused.

    Returns ``{out_dir, dry_run, scope_prefix, total_in_scope,
    total_out_of_scope, out_of_scope_files, written, unchanged, errors}``:
    ``written`` lists the vault-relative paths written (or that a dry run
    would write) and ``unchanged`` the files Confluence has nothing newer for.
    A file that cannot be read, converted or written is reported under
    ``errors`` as ``{confluence_id, file, error}`` and the rest are still
    exported.
    A scope with no tracked file returns a report with ``error`` and the
    out-of-scope files, and the command exits 1.
    """
    del space_key  # reserved and unused
    vault = Path(vault_path)
    fenced = [vault, *served_vaults]
    out = Path(out_dir).expanduser()
    hit = _inside(out, fenced)
    if hit is not None:
        raise errors.KnowledgeToolError(
            errors.E_WRITE_FENCED,
            f"{out} is inside the vault at {hit}; pass an --out directory outside every served vault",
        )

    creds = credentials(integrations)
    with _using(creds):
        configured = _is_configured()
    if not configured:
        raise errors.KnowledgeConfigError(
            errors.E_SECRET_MISSING,
            f"confluence export needs {', '.join(creds.missing())}; export them or declare them under "
            "integrations.confluence in the vault's vault.yaml",
        )

    vault_map = _scan_vault_confluence_ids(scope_prefix=scope_prefix, vault_path=vault)
    out_of_scope_map = _scan_vault_out_of_scope_confluence_ids(scope_prefix=scope_prefix, vault_path=vault)
    if not vault_map:
        return {**_empty_scope(scope_prefix, out_of_scope_map, vault), "out_dir": str(out)}

    import frontmatter as fm

    comparison: dict[str, Any] = {"up_to_date": [], "needs_update": [], "errors": []}
    written: list[str] = []
    with _using(creds):
        for _cid, rel_path, vault_file, page_data in _behind(vault_map, vault, comparison):
            target = out / rel_path
            hit = _inside(target, fenced)
            if hit is not None:
                raise errors.KnowledgeToolError(
                    errors.E_WRITE_FENCED, f"{target} resolves inside the vault at {hit}; nothing more is written"
                )
            if not dry_run:
                try:
                    post = fm.load(str(vault_file))
                    post.content = _html_to_markdown(page_data["body_html"])
                    post.metadata["last_updated"] = _stamp(page_data.get("last_modified", ""))
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(fm.dumps(post), encoding="utf-8")
                except Exception as exc:  # one file that cannot be exported never stops the rest
                    comparison["errors"].append({
                        "confluence_id": _cid,
                        "file": rel_path,
                        "error": f"{type(exc).__name__}: {exc}",
                    })
                    continue
                log.info("Exported %s from Confluence page %s to %s", rel_path, _cid, target)
            written.append(rel_path)

    return {
        "out_dir": str(out),
        "dry_run": dry_run,
        "scope_prefix": scope_prefix,
        "total_in_scope": len(vault_map),
        "total_out_of_scope": len(out_of_scope_map),
        "out_of_scope_files": _out_of_scope_rows(out_of_scope_map, vault),
        "written": sorted(written),
        "unchanged": sorted(comparison["up_to_date"]),
        "errors": comparison["errors"],
    }
