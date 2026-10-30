"""Checks of the dockerized lab: what the two running server instances must prove.

Run it through ``lab/run.sh`` (or ``lab/run.sh --check`` against instances that
are already up). It talks to both servers over streamable HTTP with the MCP
SDK client, authenticating with the bearers that ``run.sh`` wrote into
``lab/.work/lab.env``, and prints one PASS/FAIL row per check.

Tenants: ``acme`` serves acme-handbook (git) and acme-ops (a plain folder),
``globex`` serves globex-docs (git) and globex-notes (a plain folder whose
registry declares no apps). The words ``zephyr`` (acme), ``quokka`` (globex),
``marmot`` (acme-handbook), ``nautilus`` (acme-ops), ``pangolin`` (globex-docs)
and ``lemur`` (globex-notes) exist in exactly one place each (the tenant word
in each of its vaults).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult
from report import Row, print_table

LAB = Path(__file__).resolve().parent
WORK = LAB / ".work"
ENV_FILE = WORK / "lab.env"
SCOPE_ERROR = "E_SCOPE_REQUIRED"


@dataclass(frozen=True)
class Tenant:
    name: str
    base: str
    read: str
    admin: str


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def tenants() -> tuple[Tenant, Tenant]:
    env = load_env(ENV_FILE)

    def build(name: str, tag: str) -> Tenant:
        return Tenant(
            name=name,
            base=f"http://127.0.0.1:{env[f'LAB_{tag}_PORT']}",
            read=env[f"LAB_{tag}_READ_KEY"],
            admin=env[f"LAB_{tag}_ADMIN_KEY"],
        )

    return build("acme", "ACME"), build("globex", "GLOBEX")


@asynccontextmanager
async def session(tenant: Tenant, token: str):
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(120)) as http:
        async with streamable_http_client(f"{tenant.base}/mcp", http_client=http) as (read, write, _):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=120)) as client:
                await client.initialize()
                yield client


def texts(result: CallToolResult) -> list[str]:
    return [getattr(block, "text", "") for block in result.content]


def payload(result: CallToolResult) -> Any:
    """The JSON a tool answered: a list for row tools, an object otherwise."""
    items: list[Any] = []
    for text in texts(result):
        try:
            decoded = json.loads(text)
        except ValueError:
            continue
        if isinstance(decoded, list):
            items.extend(decoded)
        else:
            items.append(decoded)
    return items if len(items) != 1 or isinstance(items[0], list) else items[0]


def rows(result: CallToolResult) -> list[dict[str, Any]]:
    data = payload(result)
    return [r for r in (data if isinstance(data, list) else [data]) if isinstance(r, dict)]


async def call(tenant: Tenant, token: str, tool: str, **arguments: Any) -> CallToolResult:
    async with session(tenant, token) as client:
        return await client.call_tool(tool, arguments)


def paths(result: CallToolResult) -> list[str]:
    return [str(r.get("file_path")) for r in rows(result)]


# --- the checks ---------------------------------------------------------------

Check = Callable[[Tenant, Tenant], Awaitable[tuple[bool, str]]]


async def vaults_listed(acme: Tenant, globex: Tenant) -> tuple[bool, str]:
    a = payload(await call(acme, acme.read, "list_vaults"))
    g = payload(await call(globex, globex.read, "list_vaults"))
    acme_slugs = sorted(v["slug"] for v in a["vaults"])
    globex_slugs = sorted(v["slug"] for v in g["vaults"])
    ok = (
        acme_slugs == ["acme-handbook", "acme-ops"]
        and a["default"] == "acme-handbook"
        and globex_slugs == ["globex-docs", "globex-notes"]
        and g["default"] == "globex-docs"
    )
    return ok, f"acme={acme_slugs} default={a['default']}; globex={globex_slugs}"


async def vault_without_apps_is_served(_acme: Tenant, globex: Tenant) -> tuple[bool, str]:
    """A vault whose Contexts/config.json declares no apps loads and is searched like any other."""
    found = await call(globex, globex.read, "search", query="lemur", vault="globex-notes", limit=5, mode="keyword")
    elsewhere = await call(globex, globex.read, "search", query="lemur", vault="globex-docs", limit=5, mode="keyword")
    note = await call(
        globex, globex.read, "get_document", file_path="Runbooks/Globex Notes Runbook.md", vault="globex-notes"
    )
    readable = not note.isError
    ok = bool(rows(found)) and not rows(elsewhere) and readable
    return ok, f"globex-notes lemur={len(rows(found))}; globex-docs lemur={len(rows(elsewhere))}; readable={readable}"


async def own_word_found(acme: Tenant, globex: Tenant) -> tuple[bool, str]:
    a = await call(acme, acme.read, "search", query="zephyr", limit=5)
    g = await call(globex, globex.read, "search", query="quokka", limit=5)
    return bool(rows(a)) and bool(rows(g)), f"acme zephyr={len(rows(a))} hits; globex quokka={len(rows(g))} hits"


async def foreign_word_absent(acme: Tenant, globex: Tenant) -> tuple[bool, str]:
    # Keyword mode is lexical only: a word the tenant never wrote has no hit.
    a = await call(acme, acme.read, "search", query="quokka", limit=5, mode="keyword")
    g = await call(globex, globex.read, "search", query="zephyr", limit=5, mode="keyword")
    return not rows(a) and not rows(g), f"acme quokka={len(rows(a))}; globex zephyr={len(rows(g))}"


async def folder_vault_searchable(acme: Tenant, _globex: Tenant) -> tuple[bool, str]:
    result = await call(acme, acme.read, "search", query="nautilus", vault="acme-ops", limit=5, mode="keyword")
    found = paths(result)
    document = await call(acme, acme.read, "get_document", file_path="Runbooks/Acme Ops Runbook.md", vault="acme-ops")
    body = " ".join(texts(document))
    ok = "Runbooks/Acme Ops Runbook.md" in found and "nautilus" in body and not document.isError
    return ok, f"acme-ops nautilus hits={found[:3]}"


async def vaults_isolated(acme: Tenant, _globex: Tenant) -> tuple[bool, str]:
    wrong = await call(acme, acme.read, "search", query="nautilus", vault="acme-handbook", limit=5, mode="keyword")
    right = await call(acme, acme.read, "search", query="marmot", vault="acme-handbook", limit=5, mode="keyword")
    detail = f"handbook nautilus={len(rows(wrong))}; handbook marmot={len(rows(right))}"
    return not rows(wrong) and bool(rows(right)), detail


async def unknown_vault_refused(acme: Tenant, _globex: Tenant) -> tuple[bool, str]:
    result = await call(acme, acme.read, "search", query="quokka", vault="globex-docs")
    text = " ".join(texts(result))
    return bool(result.isError) and "E_" in text, text[:80]


def http_status(tenant: Tenant, token: str | None) -> int:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    return httpx.post(f"{tenant.base}/mcp", json=body, headers=headers, timeout=30).status_code


async def bearer_required(acme: Tenant, globex: Tenant) -> tuple[bool, str]:
    none, wrong = http_status(acme, None), http_status(acme, "not-a-bearer")
    crossed = http_status(acme, globex.read)
    return (none, wrong, crossed) == (401, 401, 401), f"no bearer={none}; wrong={wrong}; other tenant's={crossed}"


async def admin_scope_enforced(acme: Tenant, _globex: Tenant) -> tuple[bool, str]:
    denied = await call(acme, acme.read, "reindex", vault="acme-ops")
    allowed = await call(acme, acme.admin, "reindex", vault="acme-ops")
    text = " ".join(texts(denied))
    ok = bool(denied.isError) and SCOPE_ERROR in text and not allowed.isError
    return ok, f"read bearer -> {SCOPE_ERROR if SCOPE_ERROR in text else text[:50]}; admin ok={not allowed.isError}"


async def webhook_scopes(acme: Tenant, _globex: Tenant) -> tuple[bool, str]:
    url = f"{acme.base}/webhook/reindex?target=vault"

    def post(token: str | None) -> int:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return httpx.post(url, headers=headers, timeout=30).status_code

    none, read, admin = post(None), post(acme.read), post(acme.admin)
    return (none, read, admin) == (401, 403, 202), f"none={none} read={read} admin={admin}"


async def webhook_ignores_other_refs(acme: Tenant, _globex: Tenant) -> tuple[bool, str]:
    """A signed push to another branch, or deleting one, runs nothing; a push to main is queued."""
    secret = load_env(ENV_FILE)["LAB_ACME_WEBHOOK_SECRET"]
    url = f"{acme.base}/webhook/reindex?target=vault"

    def deliver(ref: str, *, deleted: bool = False) -> httpx.Response:
        body = json.dumps({"ref": ref, "after": "0" * 40 if deleted else "2" * 40, "deleted": deleted}).encode()
        digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "X-GitHub-Event": "push",
            "X-Hub-Signature-256": f"sha256={digest}",
        }
        return httpx.post(url, content=body, headers=headers, timeout=30)

    other = deliver("refs/heads/feature/lab")
    gone = deliver("refs/heads/feature/lab", deleted=True)
    main = deliver("refs/heads/main")
    reasons = [r.json().get("reason") if r.status_code == 200 else None for r in (other, gone)]
    statuses = (other.status_code, gone.status_code, main.status_code)
    ok = statuses == (200, 200, 202) and reasons == ["not_served_branch", "ref_deleted"]
    return ok, f"other={other.status_code}/{reasons[0]} deleted={gone.status_code}/{reasons[1]} main={main.status_code}"


def push_note(bare: Path, token: str) -> None:
    """Commit a note carrying ``token`` to the bare repository, as a teammate would."""
    git = ["git", "-c", "user.name=lab", "-c", "user.email=lab@example.invalid"]
    with tempfile.TemporaryDirectory() as tmp:
        clone = Path(tmp) / "clone"
        subprocess.run(["git", "clone", "-q", str(bare), str(clone)], check=True)
        (clone / "Notes").mkdir(exist_ok=True)
        (clone / "Notes" / "Late Addition.md").write_text(
            f"---\ntitle: Late Addition\ntype: note\nstatus: active\nlast_updated: 2026-10-16\n---\n\n"
            f"# Late Addition\n\nA note that arrived after the server started: {token}.\n",
            encoding="utf-8",
        )
        subprocess.run([*git, "-C", str(clone), "add", "-A"], check=True)
        subprocess.run([*git, "-C", str(clone), "commit", "-q", "-m", "docs: add a late note"], check=True)
        subprocess.run(["git", "-C", str(clone), "push", "-q", "origin", "main"], check=True)


async def git_vault_follows_remote(acme: Tenant, _globex: Tenant) -> tuple[bool, str]:
    token = f"aardwolf{uuid.uuid4().hex[:10]}"
    push_note(WORK / "repos" / "acme-handbook.git", token)
    before = await call(acme, acme.read, "search", query=token, vault="acme-handbook", mode="keyword")
    reindexed = await call(acme, acme.admin, "reindex", vault="acme-handbook", pull=True)
    after = await call(acme, acme.read, "search", query=token, vault="acme-handbook", mode="keyword")
    sync = payload(reindexed).get("vault_sync") if not reindexed.isError else "error"
    ok = not rows(before) and bool(rows(after)) and sync == "pulled"
    return ok, f"before={len(rows(before))} sync={sync} after={len(rows(after))}"


#: (tenant, vault, application, word in its CLAUDE.md, word in its .claude/agents/x.md).
#: lab/seed.sh gives every context package those two words and uses them nowhere else.
CONTEXT_PACKAGES = (
    ("acme", "acme-handbook", "acme-web", "heron", "osprey"),
    ("acme", "acme-handbook", "acme-api", "ibis", "kestrel"),
    ("acme", "acme-ops", "acme-deploy", "plover", "merlin"),
    ("globex", "globex-docs", "globex-core", "curlew", "harrier"),
)


def in_source(vault: str, relative: str) -> bool:
    """Whether the seeded vault holds ``relative``: in its bare repository, or in its folder."""
    bare = WORK / "repos" / f"{vault}.git"
    if bare.is_dir():
        probe = subprocess.run(
            ["git", "--git-dir", str(bare), "cat-file", "-e", f"HEAD:{relative}"], capture_output=True
        )
        return probe.returncode == 0
    return (WORK / "vaults" / vault / relative).is_file()


async def contexts_indexed_without_dot_dirs(acme: Tenant, globex: Tenant) -> tuple[bool, str]:
    """A package's CLAUDE.md is a note the server finds; its .claude/ tree is not indexed at all."""
    by_name = {"acme": acme, "globex": globex}
    problems: list[str] = []
    for name, vault, app, claude_word, agent_word in CONTEXT_PACKAGES:
        tenant = by_name[name]
        agent_file = f"Contexts/{app}/.claude/agents/x.md"
        found = await call(tenant, tenant.read, "search", query=claude_word, vault=vault, limit=5, mode="keyword")
        leaked = await call(tenant, tenant.read, "search", query=agent_word, vault=vault, limit=5, mode="keyword")
        if f"Contexts/{app}/CLAUDE.md" not in paths(found):
            problems.append(f"{app}: '{claude_word}' is not found in its CLAUDE.md ({paths(found)[:2]})")
        if rows(leaked):
            problems.append(f"{app}: '{agent_word}' leaked from .claude/ into {paths(leaked)[:2]}")
        if not in_source(vault, agent_file):
            problems.append(f"{app}: {agent_file} is not in the seeded vault, so a miss proves nothing")
    detail = (
        "; ".join(problems)
        or f"{len(CONTEXT_PACKAGES)} packages: CLAUDE.md found, agents/x.md unindexed (it is in the vault)"
    )
    return not problems, detail


async def stale_queue_leaves_out_contexts(acme: Tenant, _globex: Tenant) -> tuple[bool, str]:
    """``get_stale_docs`` lists the overdue note of the vault and nothing under ``Contexts/``.

    acme-handbook declares ``governance``, holds an overdue note at its top level and an
    older one inside a context package, and excludes ``Contexts/**`` in its ``vault.yaml``.
    The undated CLAUDE.md files of the packages would also be queued by ``missing_last_updated``.
    """
    result = await call(acme, acme.read, "get_stale_docs", vault="acme-handbook", missing_last_updated=True, limit=50)
    queued = rows(result)
    reasons = {str(r.get("file_path")): r.get("reason") for r in queued}
    leaked = sorted(f for f in reasons if f.startswith("Contexts/"))
    proof = in_source("acme-handbook", "Contexts/acme-web/history.md")
    listed = reasons.get("Architecture/Legacy Notes.md") == "overdue"
    ok = not result.isError and bool(queued) and listed and not leaked and proof
    if result.isError:
        return False, " ".join(texts(result))[:120]
    return ok, f"queue={sorted(reasons)}; under Contexts/={leaked}; the old package note is in the vault={proof}"


CHECKS: list[tuple[str, Check]] = [
    ("list_vaults: acme has its two, globex its two", vaults_listed),
    ("vault with no apps (globex-notes) is served like any other", vault_without_apps_is_served),
    ("search: each tenant finds its own word", own_word_found),
    ("search: a tenant never finds the other's word", foreign_word_absent),
    ("folder vault (acme-ops) is searchable and readable", folder_vault_searchable),
    ("vaults of one tenant do not leak into each other", vaults_isolated),
    ("a vault of another tenant cannot be addressed", unknown_vault_refused),
    ("bearer required (none, wrong, other tenant's: 401)", bearer_required),
    ("reindex needs the admin scope (E_SCOPE_REQUIRED)", admin_scope_enforced),
    ("/webhook/reindex: 401 / 403 / 202 by scope", webhook_scopes),
    ("/webhook/reindex: another branch or a deletion is ignored (200)", webhook_ignores_other_refs),
    ("git vault follows its remote (reindex pull=true)", git_vault_follows_remote),
    ("Contexts/<app>/CLAUDE.md is indexed, .claude/ is not", contexts_indexed_without_dot_dirs),
    ("get_stale_docs lists the overdue note and nothing under Contexts/", stale_queue_leaves_out_contexts),
]


async def run_all() -> list[Row]:
    acme, globex = tenants()
    results: list[Row] = []
    for name, check in CHECKS:
        try:
            ok, detail = await check(acme, globex)
        except Exception as exc:  # a check that cannot run is a failed check
            ok, detail = False, f"{type(exc).__name__}: {exc}"[:120]
        results.append(Row(name, ok, detail))
    return results


def main() -> int:
    if not ENV_FILE.is_file():
        print(f"{ENV_FILE} is missing: run lab/run.sh first", file=sys.stderr)
        return 2
    results = asyncio.run(run_all())
    print_table("SERVER CHECKS (two tenants over HTTP)", results)
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
