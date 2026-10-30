"""Command line for the knowledge server.

Subcommands: ``serve``, ``eval``, ``vault-lint``, ``doctor``, ``init``,
``migrate-index``, ``confluence export``, ``memory-bridge sync|promote`` and
``jira sync``.

Running with no subcommand is ``serve``, so registrations that invoke the
entry point with no arguments keep working. Configuration problems print one
``<code>: <message>`` line on stderr and exit with status 2, never a traceback.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import runpy
import shutil
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from importlib import metadata
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from dotenv import load_dotenv

from knowledge import contexts, contract, extensions, principals, repos, server, settings, stores, usage
from knowledge.errors import (
    E_ARGUMENT_CONFLICT,
    E_CONTEXTS_CHECKOUT_INVALID,
    E_CONTEXTS_CONFLICT,
    E_CONTEXTS_INVALID,
    E_CONTEXTS_REPO_UNKNOWN,
    E_EMBEDDINGS_KEY_MISSING,
    E_EMBEDDINGS_PROVIDER_UNKNOWN,
    E_EVAL_UNAVAILABLE,
    E_EXPORT_FAILED,
    E_FEATURE_DISABLED,
    E_INDEX_UNREADABLE,
    E_JIRA_AUTH_FAILED,
    E_JIRA_SYNC_STALLED,
    E_JIRA_UNAVAILABLE,
    E_KNOWLEDGE_HOME_NOT_WRITABLE,
    E_LINT_PATH_NOT_DIR,
    E_MEMORY_PROJECT_MISSING,
    E_ROOT_UNKNOWN,
    E_WRITE_FENCED,
    W_BEARERS_IGNORED_STDIO,
    W_CONTEXTS_CHECKOUT_UNVERIFIED,
    W_CONTEXTS_INIT_PENDING,
    W_CONTEXTS_LINKED_NOT_SELECTED,
    W_CONTEXTS_SELECTION_MISSING,
    KnowledgeConfigError,
    KnowledgeToolError,
    MemoryWriteFailed,
)
from knowledge.vault_lint import LATEST_RULES, RULES_1_2, RULES_1_3, RULESETS, fix, lint

PROJECT_ROOT: Path = settings.PROJECT_ROOT

#: Distribution name whose installed version ``doctor`` reports.
DISTRIBUTION = "cloudfrog-knowledge"

#: Upper bound, in UTF-8 bytes, of the line ``doctor --brief`` prints. A
#: session hook forwards that line into the model context, so it stays small.
BRIEF_MAX_BYTES = 200


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cloudfrog-knowledge",
        description="Knowledge MCP server and vault tooling.",
    )
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run the MCP server (default)")
    serve.add_argument("--transport", choices=("stdio", "http"), default=None)
    serve.add_argument("--vaults", metavar="FILE", default=None, help="vaults.yaml to serve")
    serve.add_argument(
        "--profile",
        metavar="NAME",
        default=None,
        help=f"tool profile to serve: {', '.join(contract.PUBLIC_PROFILES)} (default: {contract.DEFAULT_PROFILE}); "
        f"wins over {settings.PROFILE_ENV}",
    )

    # Listed for --help only: ``main`` hands everything after ``eval`` to the
    # evaluation script untouched, flags included.
    sub.add_parser("eval", help="run the retrieval evaluation (source checkout only)", add_help=False)

    lint_p = sub.add_parser(
        "vault-lint", help="check frontmatter, wikilinks and the vault rules; read-only unless --fix"
    )
    lint_p.add_argument("path", metavar="PATH")
    lint_p.add_argument("--format", choices=("json",), default="json")
    lint_p.add_argument(
        "--rules",
        choices=RULESETS,
        default=LATEST_RULES,
        help=f"rule set to check (default {LATEST_RULES}; {RULES_1_2} is the one reindex counts)",
    )
    lint_p.add_argument(
        "--fix",
        action="store_true",
        help=(
            "move updated, last_review or last_verified.date into last_updated (needs rules 1.3); "
            "never inside a served vault"
        ),
    )
    lint_p.add_argument(
        "--out",
        metavar="DIR",
        default=None,
        help="with --fix: copy PATH to DIR (new or empty, outside every served vault) and fix the copy",
    )

    doctor = sub.add_parser("doctor", help="check that this installation can serve, read-only")
    doctor.add_argument("--vaults", metavar="FILE", default=None, help="vaults.yaml to check")
    doctor.add_argument(
        "--brief", action="store_true", help=f"print one line of at most {BRIEF_MAX_BYTES} bytes"
    )
    doctor.add_argument(
        "--client",
        action="store_true",
        help="check only what a machine that consumes a remote server owns: the vault, Contexts/config.json, "
        "repos.local.json and each checkout's package; skip the server checks",
    )

    init = sub.add_parser(
        "init",
        help="link the context packages of every registered vault into the checkouts on this machine",
        description=(
            "For each registered vault with Contexts/config.json: locate its roots, write "
            "<config home>/knowledge/<slug>/repos.local.json and install the package of every checkout "
            "of its 'apply' list that is on this machine (--repo, --all and --drop change the list). "
            "Writes nothing inside a vault."
        ),
    )
    init.add_argument("--vaults", metavar="FILE", default=None, help="vaults.yaml whose vaults to initialize")
    init.add_argument("--vault", metavar="SLUG", default=None, help="initialize only this vault")
    init.add_argument(
        "--root",
        metavar="[SLUG:]NAME=DIR",
        action="append",
        default=[],
        type=_root_flag,
        help="directory of root NAME (of every vault that declares it, or of vault SLUG); repeatable",
    )
    init.add_argument(
        "--search-dir",
        metavar="DIR",
        action="append",
        default=[],
        help="where to look for checkouts by their origin remote (default: ~/Projects and the parents "
        "of every known path); repeatable",
    )
    init.add_argument(
        "--max-depth",
        metavar="N",
        type=int,
        default=contexts.DEFAULT_MAX_DEPTH,
        help=f"how many levels below a search directory to look (default {contexts.DEFAULT_MAX_DEPTH})",
    )
    init.add_argument(
        "--repo",
        metavar="[SLUG:]ID",
        action="append",
        default=[],
        type=_repo_flag,
        help="install the package of checkout ID on this machine: add it to 'apply' (of every vault that "
        "declares it, or of vault SLUG); repeatable",
    )
    init.add_argument(
        "--all",
        action="store_true",
        help="add every checkout on this machine that has a context package to 'apply'",
    )
    init.add_argument(
        "--drop",
        metavar="[SLUG:]ID",
        action="append",
        default=[],
        type=_repo_flag,
        help="remove checkout ID from 'apply' and delete its links into Contexts/<package>; repeatable",
    )
    init.add_argument(
        "--yes",
        action="store_true",
        help="never prompt: a root nothing locates is skipped, and a machine with no selection installs nothing",
    )
    init.add_argument("--dry-run", action="store_true", help="report what would change; write nothing")
    init.add_argument("--json", action="store_true", help="print the full JSON report")
    replace = init.add_mutually_exclusive_group()
    replace.add_argument(
        "--force",
        action="store_true",
        help="move a pre-existing .claude, CLAUDE.md or .mcp.json aside (<name>.pre-ai-context.bak-<epoch>)",
    )
    replace.add_argument(
        "--refresh",
        action="store_true",
        help="copy mode: bring a copy that drifted from its package back in sync, without a backup",
    )
    init.add_argument(
        "--exclude",
        choices=contexts.EXCLUDE_TARGETS,
        default=contexts.EXCLUDE_LOCAL,
        help="where the ignore entries go: .git/info/exclude (local, default) or .gitignore",
    )

    migrate = sub.add_parser(
        "migrate-index",
        help="prepare each vault's index.sqlite and keep the sidecars of its Chroma index; embeds nothing",
    )
    migrate.add_argument("--vaults", metavar="FILE", default=None, help="vaults.yaml whose vaults to migrate")
    migrate.add_argument("--vault", metavar="SLUG", default=None, help="migrate only this vault")
    migrate.add_argument(
        "--from-chroma", metavar="DIR", default=None, help="Chroma directory to copy sidecars from (one vault)"
    )
    migrate.add_argument("--build", action="store_true", help="also index each vault now (embeds the vault)")
    migrate.add_argument(
        "--prune-chroma",
        action="store_true",
        help="delete chroma.sqlite3 and Chroma's segment directories from the source afterwards",
    )

    confluence = sub.add_parser("confluence", help="Confluence pages the vault is behind on")
    confluence_sub = confluence.add_subparsers(dest="confluence_command", required=True)
    export = confluence_sub.add_parser(
        "export",
        help="write the pages the vault is behind on into a directory outside every vault, for a pull request",
    )
    export.add_argument("--vaults", metavar="FILE", default=None, help="vaults.yaml that declares the vault")
    export.add_argument("--vault", metavar="SLUG", default=None, help="vault to compare (default: the default vault)")
    export.add_argument(
        "--out",
        metavar="DIR",
        default=None,
        help="directory to write into, outside every served vault (default: <data_dir>/<slug>/confluence-export)",
    )
    export.add_argument("--space", metavar="KEY", default="CB", help="Confluence space key (reserved)")
    export.add_argument("--scope-prefix", metavar="PREFIX", default="Conventions/", help="vault prefix to compare")
    export.add_argument("--dry-run", action="store_true", help="report the files, write nothing")

    bridge = sub.add_parser("memory-bridge", help="publish a vault to its project on the local memory server")
    bridge_sub = bridge.add_subparsers(dest="bridge_command", required=True)
    bridge_sync = bridge_sub.add_parser(
        "sync", help="have memory rescan the vault's Runbooks/ (and, with --card, update the project card first)"
    )
    bridge_sync.add_argument("--vaults", metavar="FILE", default=None, help="vaults.yaml that declares the vault")
    bridge_sync.add_argument("--vault", metavar="SLUG", default=None, help="vault to publish (default: the default)")
    bridge_sync.add_argument(
        "--card",
        action="store_true",
        help="also create or update the project card (name, memory.hub, first integrations.jira project)",
    )
    promote = bridge_sub.add_parser("promote", help="memory observations that became vault documents")
    promote_sub = promote.add_subparsers(dest="promote_command", required=True)
    promote_list = promote_sub.add_parser("list", help="pinned observations a vault document can come from")
    promote_stamp = promote_sub.add_parser("stamp", help="record on an observation the vault document it became")
    for cmd in (promote_list, promote_stamp):
        cmd.add_argument("--vaults", metavar="FILE", default=None, help="vaults.yaml that declares the vault")
        cmd.add_argument("--vault", metavar="SLUG", default=None, help="vault whose memory project to use")
    promote_list.add_argument("--types", metavar="LIST", default=None, help="comma-separated observation types")
    promote_list.add_argument("--limit", metavar="N", type=int, default=None, help="cap the candidate list")
    promote_stamp.add_argument("observation", metavar="OBS", help="observation id or obs-<hex>")
    promote_stamp.add_argument(
        "--knowledge-ref", metavar="PATH", required=True, help="vault-relative path of the merged document"
    )
    jira = sub.add_parser("jira", help="mirror the issues of a vault's Jira projects into memory tasks")
    jira_sub = jira.add_subparsers(dest="jira_command", required=True)
    jira_sync = jira_sub.add_parser(
        "sync", help="upsert one memory task per Jira issue updated since the last run (stdio deployments)"
    )
    jira_sync.add_argument("--vaults", metavar="FILE", default=None, help="vaults.yaml that declares the vault")
    jira_sync.add_argument("--vault", metavar="SLUG", default=None, help="vault to sync (default: the default)")
    jira_sync.add_argument(
        "--since", metavar="WHEN", default=None, help="ISO date or date-time, or -Nd (default: last run minus a day)"
    )
    jira_sync.add_argument("--dry-run", action="store_true", help="read Jira and report; write nothing")
    jira_sync.add_argument(
        "--max-issues",
        metavar="N",
        type=int,
        default=None,
        help="stop after N issues (1-2000, default 2000); the report says truncated and next_since",
    )
    jira_sync.add_argument("--json", action="store_true", help="print the full JSON report")
    jira_sync.add_argument(
        "--auto",
        action="store_true",
        help="every vault that declares jira and is due by its sync.throttle_minutes (the session hook)",
    )
    return parser


def _root_flag(raw: str) -> tuple[str | None, str, Path]:
    """``[SLUG:]NAME=DIR`` as ``(slug, name, dir)``; argparse reports a malformed one."""
    name, sep, directory = raw.partition("=")
    slug: str | None = None
    if ":" in name:
        slug, _, name = name.partition(":")
    if not sep or not name.strip() or not directory.strip() or (slug is not None and not slug.strip()):
        raise argparse.ArgumentTypeError(f"{raw!r} is not [SLUG:]NAME=DIR")
    return (slug.strip() if slug else None), name.strip(), Path(directory.strip()).expanduser()


def _repo_flag(raw: str) -> tuple[str | None, str]:
    """``[SLUG:]ID`` as ``(slug, id)``; argparse reports a malformed one."""
    slug: str | None = None
    repo_id = raw
    if ":" in raw:
        slug, _, repo_id = raw.partition(":")
    if not repo_id.strip() or (slug is not None and not slug.strip()):
        raise argparse.ArgumentTypeError(f"{raw!r} is not [SLUG:]ID")
    return (slug.strip() if slug else None), repo_id.strip()


def _fail(exc: KnowledgeConfigError) -> int:
    print(str(exc), file=sys.stderr)
    return 2


def _environment(transport: str | None, vaults: str | None, profile: str | None = None) -> dict[str, str]:
    # Same precedence as ``load_settings()`` with no env: the project ``.env``
    # fills only what the process environment leaves unset, and the flags
    # override both.
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    env = dict(os.environ)
    if transport:
        env["MCP_TRANSPORT"] = transport
    if vaults:
        env["KNOWLEDGE_VAULTS"] = vaults
    if profile is not None:
        env[settings.PROFILE_ENV] = profile
    return env


def _serve(transport: str | None, vaults: str | None, profile: str | None = None) -> int:
    env = _environment(transport, vaults, profile)
    try:
        server.serve(settings.load_settings(env, project_root=PROJECT_ROOT))
    except KnowledgeConfigError as exc:
        return _fail(exc)
    return 0


def _eval(rest: Sequence[str]) -> int:
    script = PROJECT_ROOT / "scripts" / "eval_retrieval.py"
    if not script.is_file():
        return _fail(
            KnowledgeConfigError(
                E_EVAL_UNAVAILABLE,
                "eval needs a source checkout; run it from the knowledge repository",
            )
        )
    namespace = runpy.run_path(str(script), run_name="knowledge_eval")
    return int(namespace["main"](list(rest)))


def _overlap(path: Path, served: Sequence[Path]) -> Path | None:
    """The first served vault that contains ``path`` or that ``path`` contains, both resolved."""
    resolved = path.expanduser().resolve()
    for vault in served:
        root = vault.expanduser().resolve()
        if resolved == root or resolved.is_relative_to(root) or root.is_relative_to(resolved):
            return vault
    return None


def _fix_target(source: Path, out: str | None) -> Path:
    """The directory ``vault-lint --fix`` may write: ``source``, or a fresh copy of it at ``out``.

    Either one overlapping a vault this installation serves (the same
    discovery as ``serve``: ``KNOWLEDGE_VAULTS``, the user's ``vaults.yaml``
    or ``VAULT_PATH``) is ``E_WRITE_FENCED``, checked before anything is
    written: a vault changes only through a pull request a person reviews.
    """
    env = _environment(None, None)
    loaded = settings.load_settings(env, project_root=PROJECT_ROOT)
    served = [spec.path for spec in loaded.vaults if spec.path is not None]
    target = Path(out).expanduser() if out else source
    hit = _overlap(target, served)
    if hit is not None:
        raise KnowledgeToolError(
            E_WRITE_FENCED,
            f"{target} overlaps the served vault at {hit}; run --fix on a checkout that is not served, "
            "or pass --out DIR outside every served vault, and propose the change by pull request",
        )
    if out is None:
        return source
    if not source.is_dir():
        raise KnowledgeConfigError(E_LINT_PATH_NOT_DIR, f"{source} is not a directory")
    if target.resolve().is_relative_to(source.resolve()):
        raise KnowledgeToolError(E_ARGUMENT_CONFLICT, f"--out {target} is inside PATH; pass a directory outside it")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise KnowledgeToolError(E_ARGUMENT_CONFLICT, f"--out {target} exists and is not an empty directory")
    # Symbolic links are copied as the files they point to, so the copy
    # never writes through a link back into the source.
    shutil.copytree(source, target, ignore=shutil.ignore_patterns(".git"), dirs_exist_ok=True)
    return target


def _vault_lint(path: str, rules: str, apply_fix: bool, out: str | None = None) -> int:
    root = Path(path)
    try:
        if apply_fix:
            root = _fix_target(root, out)
        outcome = fix(root) if apply_fix else None
        report = lint(root, rules=rules)
    except (KnowledgeConfigError, KnowledgeToolError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    payload = report.to_json()
    if outcome is not None:
        payload.update(outcome.to_json())
    print(json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2))
    return 0 if report.ok else 1


def _package_version() -> str:
    try:
        return metadata.version(DISTRIBUTION)
    except metadata.PackageNotFoundError:
        return "unknown"


def _registry_source(env: Mapping[str, str], loaded: settings.Settings) -> str:
    """Which discovery rule picked the vaults: the first one that applies wins."""
    if loaded.vaults_file is not None:
        return "KNOWLEDGE_VAULTS" if env.get("KNOWLEDGE_VAULTS", "").strip() else "user_vaults_yaml"
    if env.get("VAULT_PATH", "").strip():
        return "VAULT_PATH"
    if loaded.default_vault().path is not None:
        return "config_json"
    return "none"


def _writable(path: Path) -> tuple[bool, bool]:
    """Whether ``path`` exists, and whether this process could write there.

    A missing directory counts as writable when its closest existing parent
    is: the server creates it on first use. Nothing is created here.
    """
    try:
        if path.exists():
            return True, path.is_dir() and os.access(path, os.W_OK | os.X_OK)
        parent = path.parent
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        return False, parent.is_dir() and os.access(parent, os.W_OK | os.X_OK)
    except OSError:
        return False, False


def _is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def _vault_report(spec: settings.VaultSpec) -> tuple[dict[str, Any], list[dict[str, str]]]:
    problems: list[dict[str, str]] = []
    try:
        settings.check_vault_dir(spec)
    except KnowledgeConfigError as exc:
        problems.append({"code": exc.code, "vault": spec.slug, "message": exc.message})
    emb = spec.embeddings
    if emb.provider not in settings.EMBEDDING_PROVIDERS:
        problems.append(
            {
                "code": E_EMBEDDINGS_PROVIDER_UNKNOWN,
                "vault": spec.slug,
                "message": f"provider {emb.provider!r} is not one of "
                f"{', '.join(sorted(settings.EMBEDDING_PROVIDERS))}",
            }
        )
    key_required = emb.provider == "openai"
    key_set = bool(emb.api_key.strip())
    if key_required and not key_set:
        problems.append(
            {
                "code": E_EMBEDDINGS_KEY_MISSING,
                "vault": spec.slug,
                "message": f"the openai provider needs {emb.key_env} in the environment",
            }
        )
    row: dict[str, Any] = {
        "slug": spec.slug,
        "default": spec.is_default,
        "path": None if spec.path is None else str(spec.path),
        "provider": emb.provider,
        "key_env": emb.key_env,
        "key_required": key_required,
        # Presence only: the value never leaves this function.
        "key_set": key_set,
        "index": (spec.index_dir / stores.INDEX_FILE).is_file(),
        "ok": not problems,
    }
    return row, problems


def _auth_report(loaded: settings.Settings) -> tuple[dict[str, Any], list[dict[str, str]], list[str]]:
    """Who may call the HTTP surface, and with which scopes: ids, scopes and variable names, never a token.

    Problems are reported only for ``MCP_TRANSPORT=http``, the only transport
    that checks a bearer; over stdio declared ``bearers`` only warn.
    """
    transport = "http" if loaded.http.transport.lower().strip() == "http" else "stdio"
    rows = principals.declared(loaded)
    problems: list[dict[str, str]] = []
    warnings: list[str] = []
    if transport == "http":
        try:
            loaded.check_bearers()
        except KnowledgeConfigError as exc:
            problems.append({"code": exc.code, "vault": "", "message": exc.message})
    elif loaded.bearers:
        warnings.append(
            f"{W_BEARERS_IGNORED_STDIO}: bearers apply only to MCP_TRANSPORT=http; over stdio no bearer is checked"
        )
    active = [row for row in rows if row.token_set]
    block = {
        "transport": transport,
        "source": "bearers" if loaded.bearers else "keys",
        "bearers": len(active),
        "admin": sum(1 for row in active if row.principal.allows(principals.ADMIN)),
        "read_only": sum(1 for row in active if not row.principal.allows(principals.ADMIN)),
        "principals": [
            {
                "id": row.principal.id,
                "scopes": sorted(row.principal.scopes),
                "token_env": row.token_env,
                # Presence only: the value never leaves this function.
                "token_set": row.token_set,
            }
            for row in rows
        ],
    }
    return block, problems, warnings


def _jira_report(spec: settings.VaultSpec, env: Mapping[str, str], memory: settings.MemorySettings) -> dict[str, Any]:
    """Whether vault ``spec`` can run ``jira sync``: variable names and codes, never a value.

    Informational: a person without Jira credentials still has a healthy
    installation, so nothing here is a doctor problem.
    """
    ext = extensions.get("jira")
    names = [name for name in extensions.env_names(spec, ext) if name not in ext.fixed_env]
    missing = [name for name in names if not (env.get(name) or "").strip()]
    if len(names) < len(ext.secret_keys):
        secrets = "undeclared"
    else:
        secrets = "ok" if not missing else "missing:" + ",".join(missing)
    if memory.require_token and not memory.token:
        memory_state = "E_MEMORY_TOKEN_MISSING"
    elif not settings.is_loopback_http_url(memory.url):
        memory_state = "E_MEMORY_URL_NOT_LOOPBACK"
    else:
        memory_state = "ok"
    return {"declared": True, "secrets": secrets, "memory": memory_state}


def _rerank_report(spec: settings.VaultSpec, operator: settings.RerankOperator) -> dict[str, Any]:
    """The judge vault ``spec`` declares and what it did: names, settings and aggregates, never a key.

    Informational: a vault whose key is missing still searches, without a
    judge, so nothing here is a doctor problem. The aggregates come from the
    vault's ``usage_stats.json``, shared by every process over the vault.
    """
    from knowledge.registry import USAGE_FILE

    rr = spec.rerank
    return {
        "provider": rr.provider,
        "default": "on" if rr.default_on else "off",
        "model": rr.model,
        # Presence only, by variable NAME: the value never leaves this function.
        "key": "ok" if rr.api_key else f"missing:{rr.key_env}",
        "allowed": operator.allows(rr.provider),
        "candidates": rr.candidates,
        "units_per_doc": rr.units_per_doc,
        "timeout_ms": operator.timeout_ms or rr.timeout_ms,
        "max_input_tokens": rr.max_input_tokens,
        "max_usd_per_day": rr.max_usd_per_day,
        **usage.rerank_summary(spec.index_dir / USAGE_FILE),
    }


def _tool_calls_report(spec: settings.VaultSpec) -> dict[str, Any]:
    """The tool calls a server over vault ``spec`` counted: names and counts, never an argument.

    Read from the vault's ``usage_stats.json`` (section ``tool_calls``), where
    every process that serves ``spec`` as its default vault adds its calls.
    ``deprecated`` names the calls to a tool the contract marks deprecated
    or retired (``E_TOOL_RETIRED`` under ``all``, served under ``legacy``);
    ``unregistered`` the calls to a name the contract does not define.
    """
    from knowledge.registry import USAGE_FILE

    return usage.tool_calls_summary(
        spec.index_dir / USAGE_FILE,
        registered=frozenset(contract.TOOL_SINCE) | frozenset(contract.TOOL_REMOVED),
        deprecated=frozenset(contract.TOOL_REMOVED)
        | frozenset(name for name, meta in contract.TOOL_META.items() if meta.get("knowledge/deprecated") is True),
    )


def _contexts_report(spec: settings.VaultSpec, env: Mapping[str, str]) -> dict[str, Any] | None:
    """Whether this machine initialized vault ``spec``'s context packages; ``None`` when it declares none.

    A fact about this machine, never a problem: the server serves the vault
    whether or not its packages are linked into the checkouts.
    """
    if spec.path is None or not _is_dir(spec.path):
        return None
    declaration = repos.contexts_declaration(spec.path)
    if not declaration.is_file():
        return None
    try:
        overlay = repos.overlay_path_for(spec.slug, env)
    except repos.RegistryError:
        overlay = None
    return {
        "config": str(declaration),
        "overlay": None if overlay is None else str(overlay),
        "initialized": overlay is not None and overlay.is_file(),
        # A vault that declares no apps has nothing to link, so it is never pending.
        "apps": contexts.declared_apps(spec.path),
    }


def _doctor_report(env: Mapping[str, str], loaded: settings.Settings) -> dict[str, Any]:
    problems: list[dict[str, str]] = []
    vaults: list[dict[str, Any]] = []
    pending: list[str] = []
    for spec in loaded.vaults:
        row, found = _vault_report(spec)
        if spec.is_default:
            row["tool_calls"] = _tool_calls_report(spec)
        if "jira" in spec.features:
            row["jira"] = _jira_report(spec, env, loaded.memory)
        if spec.rerank.configured:
            row["rerank"] = _rerank_report(spec, loaded.rerank_operator)
        contexts_row = _contexts_report(spec, env)
        if contexts_row is not None:
            row["contexts"] = contexts_row
            if not contexts_row["initialized"] and contexts_row["apps"] != 0:
                pending.append(spec.slug)
        vaults.append(row)
        problems.extend(found)
    home_exists, home_writable = _writable(loaded.knowledge_home)
    if not home_writable:
        problems.append(
            {
                "code": E_KNOWLEDGE_HOME_NOT_WRITABLE,
                "vault": "",
                "message": f"{loaded.knowledge_home} cannot be written; set KNOWLEDGE_HOME "
                "or data_dir in vaults.yaml to a writable directory",
            }
        )
    auth, auth_problems, auth_warnings = _auth_report(loaded)
    problems.extend(auth_problems)
    init_command = contexts.command_label(env, "init")
    # Linking packages into checkouts happens on the person's machine; an
    # HTTP deployment has no checkouts, so only stdio is told to run init.
    init_warnings = (
        [_pending_warning(slug, init_command) for slug in pending] if auth["transport"] == "stdio" else []
    )
    host = urlsplit(loaded.memory.url).hostname or ""
    return {
        "ok": not problems,
        "init": {"pending": pending, "command": init_command},
        "doctor_command": contexts.command_label(env, "doctor"),
        "package": {"name": DISTRIBUTION, "version": _package_version()},
        "contract": contract.CONTRACT_VERSION,
        "auth": auth,
        "python": platform.python_version(),
        # How this Python ranks vectors: ``sqlite-vec`` when the extension
        # loads, ``python`` (standard library, slower) when it does not.
        "vector_backend": stores.probe_vector_backend(),
        "registry": {
            "source": _registry_source(env, loaded),
            "file": None if loaded.vaults_file is None else str(loaded.vaults_file),
            "vaults": len(loaded.vaults),
            "default": loaded.default_vault().slug,
        },
        "knowledge_home": {
            "path": str(loaded.knowledge_home),
            "exists": home_exists,
            "writable": home_writable,
        },
        "vaults": vaults,
        "memory": {
            "url_loopback": host in settings.LOOPBACK_HOSTS,
            "token_set": bool(loaded.memory.token),
        },
        "problems": problems,
        "warnings": [*loaded.load_warnings, *auth_warnings, *init_warnings],
    }


def _pending_warning(slug: str, init_command: str, still: Sequence[str] = ()) -> str:
    if still:
        return (
            f"{W_CONTEXTS_INIT_PENDING}: {slug}: checkout(s) {', '.join(still)} still to install; "
            f"run: {init_command}"
        )
    return (
        f"{W_CONTEXTS_INIT_PENDING}: {slug}: {repos.CONTEXTS_DIR_NAME}/{repos.DECLARATION_NAME} is declared "
        f"and this machine has no repos.local.json for it; run: {init_command}"
    )


#: What ``doctor --client`` leaves to the server the machine consumes.
CLIENT_SKIPPED = (
    "auth",
    "embeddings",
    "index",
    "jira",
    "knowledge_home",
    "memory",
    "rerank",
    "tool_calls",
    "vector_backend",
)
#: The code ``doctor --client`` reports for a checkout ``init`` cannot install into, by its status.
_CHECKOUT_CODES = {
    contexts.CONFLICT: E_CONTEXTS_CONFLICT,
    contexts.ERROR: E_CONTEXTS_CHECKOUT_INVALID,
    contexts.UNVERIFIED: E_CONTEXTS_CHECKOUT_INVALID,
}


def _client_vault_report(
    spec: settings.VaultSpec, env: Mapping[str, str], fence: Sequence[Path]
) -> tuple[dict[str, Any], list[dict[str, str]], list[str], list[str]]:
    """What a client machine owns of vault ``spec``: ``(row, problems, warnings, still to install)``.

    The checkouts come from a dry run of ``init`` that never scans and never
    prompts: it locates the roots this machine already declares and writes nothing.
    """
    row: dict[str, Any] = {
        "slug": spec.slug,
        "default": spec.is_default,
        "path": None if spec.path is None else str(spec.path),
        "readable": False,
    }
    try:
        settings.check_vault_dir(spec)
    except KnowledgeConfigError as exc:
        row["ok"] = False
        return row, [{"code": exc.code, "vault": spec.slug, "message": exc.message}], [], []
    row["readable"] = True
    contexts_row = _contexts_report(spec, env)
    if contexts_row is None:
        row["ok"] = True
        return row, [], [], []
    problems: list[dict[str, str]] = []
    warnings: list[str] = []
    outcome = contexts.init_vault(contexts.VaultTarget(spec.slug, spec.path), env=env, fence=fence, dry_run=True)
    if outcome.status == contexts.VAULT_ERROR:
        code, sep, message = outcome.detail.partition(": ")
        if not (sep and code.startswith("E_")):
            code, message = E_CONTEXTS_INVALID, outcome.detail
        problems.append({"code": code, "vault": spec.slug, "message": message})
    for app in outcome.apps:
        code = _CHECKOUT_CODES.get(app.status)
        if code is None:
            continue
        what = "; ".join([*app.actions, app.detail] if app.detail else app.actions)
        if app.status == contexts.UNVERIFIED and app.origin_missing:
            # Nothing proves the checkout is another repository either: a clone not published yet.
            warnings.append(f"{W_CONTEXTS_CHECKOUT_UNVERIFIED}: {spec.slug}: {app.id} ({app.path}): {what}")
            continue
        problems.append({"code": code, "vault": spec.slug, "message": f"{app.id} ({app.path}): {what}"})
    if contexts_row["initialized"] and outcome.status == contexts.VAULT_INITIALIZED:
        selection_warning = _selection_missing_warning(spec.slug, contexts_row["overlay"], env)
        if selection_warning is not None:
            warnings.append(selection_warning)
    contexts_row["gitignore"] = outcome.gitignore
    contexts_row["checkouts"] = [app.to_json() for app in outcome.apps]
    contexts_row["selection"] = None if outcome.selection is None else outcome.selection.to_json()
    row["contexts"] = contexts_row
    row["ok"] = not problems
    still = [app.id for app in outcome.apps if app.status == contexts.WOULD_INSTALL]
    return row, problems, [*outcome.warnings, *warnings], still


def _selection_missing_warning(slug: str, overlay: str | None, env: Mapping[str, str]) -> str | None:
    """``W_CONTEXTS_SELECTION_MISSING`` when this machine's ``repos.local.json`` has no ``apply`` list."""
    if overlay is None:
        return None
    try:
        payload = repos.read_overlay(Path(overlay))
    except repos.RegistryError:
        return None
    if contexts.APPLY_KEY in payload:
        return None
    init_command = contexts.command_label(env, "init")
    return (
        f"{W_CONTEXTS_SELECTION_MISSING}: {slug}: {overlay} has no '{contexts.APPLY_KEY}' list of the checkouts "
        f"that receive a package on this machine; run: {init_command} (it keeps the checkouts already "
        f"installed), or choose with {init_command} --repo ID or {init_command} --all"
    )


def _client_doctor_report(env: Mapping[str, str], loaded: settings.Settings) -> dict[str, Any]:
    """``doctor --client``: only what a machine that consumes a remote server owns."""
    init_command = contexts.command_label(env, "init")
    fence = [spec.path for spec in loaded.vaults if spec.path is not None]
    problems: list[dict[str, str]] = []
    warnings: list[str] = list(loaded.load_warnings)
    vaults: list[dict[str, Any]] = []
    pending: list[str] = []
    for spec in loaded.vaults:
        row, found, vault_warnings, still = _client_vault_report(spec, env, fence)
        vaults.append(row)
        problems.extend(found)
        warnings.extend(vault_warnings)
        contexts_row = row.get("contexts")
        if contexts_row is None:
            continue
        if not contexts_row["initialized"] and contexts_row["apps"] != 0:
            pending.append(spec.slug)
            warnings.append(_pending_warning(spec.slug, init_command))
        elif still:
            pending.append(spec.slug)
            warnings.append(_pending_warning(spec.slug, init_command, still))
    # Without a label the hint names this CLI, which needs the flag to stay a client check;
    # a wrapper carries its own flags.
    doctor_command = contexts.command_label(env, "doctor")
    if not env.get(contexts.COMMAND_LABEL_ENV, "").strip():
        doctor_command += " --client"
    return {
        "ok": not problems,
        "mode": "client",
        "init": {"pending": pending, "command": init_command},
        "doctor_command": doctor_command,
        "package": {"name": DISTRIBUTION, "version": _package_version()},
        "contract": contract.CONTRACT_VERSION,
        "python": platform.python_version(),
        "registry": {
            "source": _registry_source(env, loaded),
            "file": None if loaded.vaults_file is None else str(loaded.vaults_file),
            "vaults": len(loaded.vaults),
            "default": loaded.default_vault().slug,
        },
        "skipped": list(CLIENT_SKIPPED),
        "vaults": vaults,
        "problems": problems,
        "warnings": warnings,
    }


def _brief(text: str) -> str:
    """``text`` cut to :data:`BRIEF_MAX_BYTES` UTF-8 bytes, never mid-character."""
    raw = text.encode("utf-8")
    if len(raw) <= BRIEF_MAX_BYTES:
        return text
    return raw[: BRIEF_MAX_BYTES - 3].decode("utf-8", errors="ignore") + "..."


def _brief_line(report: Mapping[str, Any]) -> str:
    """The one line ``doctor --brief`` prints.

    ``knowledge doctor: ok, ...`` when there is nothing to do, which the
    session hook keeps to itself. A healthy installation whose served vaults
    declare context packages this machine has not linked yet (over stdio)
    gets a line that names them and the command instead, still with exit
    status 0: the hook prints any line that is not the ``ok`` one.
    """
    client = report.get("mode") == "client"
    pending = report["init"]["pending"] if client or report["auth"]["transport"] == "stdio" else []
    if report["ok"] and pending:
        return _brief(
            f"knowledge doctor: init pending for vault(s) {', '.join(pending)}; "
            f"run: {report['init']['command']}"
        )
    if report["ok"] and client:
        registry = report["registry"]
        line = f"knowledge doctor: ok, client, {registry['vaults']} vault(s), default {registry['default']}"
        unverified = sum(1 for w in report["warnings"] if w.startswith(f"{W_CONTEXTS_CHECKOUT_UNVERIFIED}: "))
        if unverified:
            line += f", {unverified} checkout(s) unverified"
        linked = sum(1 for w in report["warnings"] if w.startswith(f"{W_CONTEXTS_LINKED_NOT_SELECTED}: "))
        if linked:
            line += f", {linked} checkout(s) linked and not selected"
        unset = sum(1 for w in report["warnings"] if w.startswith(f"{W_CONTEXTS_SELECTION_MISSING}: "))
        if unset:
            line += f", selection missing in {unset} vault(s)"
        return _brief(line)
    if report["ok"]:
        registry = report["registry"]
        judges = [
            f"{row['slug']}:{row['rerank']['provider']}/{row['rerank']['default']}"
            for row in report["vaults"]
            if "rerank" in row
        ]
        rerank = f", rerank {' '.join(judges)}" if judges else ""
        return _brief(
            f"knowledge doctor: ok, contract {report['contract']}, "
            f"{registry['vaults']} vault(s), default {registry['default']}{rerank}"
        )
    # One entry per code, in first-seen order, naming every vault it hit.
    by_code: dict[str, list[str]] = {}
    for problem in report["problems"]:
        vaults = by_code.setdefault(problem["code"], [])
        if problem["vault"]:
            vaults.append(problem["vault"])
    found = "; ".join(f"{code} ({', '.join(v)})" if v else code for code, v in by_code.items())
    return _brief(f"knowledge doctor: {found}; run: {report['doctor_command']}")


def _doctor(vaults: str | None, brief: bool, client: bool = False) -> int:
    """Report whether this installation can serve, without writing anything.

    Exit status 0 when every check passes, 1 when a check fails and 2 when the
    configuration cannot even be loaded. ``--brief`` prints one line on stdout
    in every case, so a hook can forward it as is. A pending ``init`` is a
    warning, not a failed check: it leaves the exit status at 0. ``--client``
    checks only what a machine that consumes a remote server owns (the vault,
    its ``Contexts/config.json``, ``repos.local.json`` and each checkout's
    package) and skips every server check.
    """
    env = _environment(None, vaults)
    try:
        loaded = settings.load_settings(env, project_root=PROJECT_ROOT)
    except KnowledgeConfigError as exc:
        if brief:
            print(_brief(f"knowledge doctor: {exc}"))
            return 2
        return _fail(exc)
    report = _client_doctor_report(env, loaded) if client else _doctor_report(env, loaded)
    if brief:
        print(_brief_line(report))
    else:
        print(json.dumps(report, sort_keys=True, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


def _ask(question: str) -> str | None:
    """One answer typed at the terminal; the question goes to stderr so stdout stays a report."""
    print(question, end="", file=sys.stderr, flush=True)
    line = sys.stdin.readline()
    return line.strip() or None


def _init_root_flags(
    flags: Sequence[tuple[str | None, str, Path]], specs: Sequence[settings.VaultSpec]
) -> dict[str, dict[str, Path]]:
    """``--root`` flags per vault slug; a root no targeted vault declares is ``E_ROOT_UNKNOWN``."""
    declared = {spec.slug: contexts.declared_roots(spec.path) for spec in specs}
    out: dict[str, dict[str, Path]] = {spec.slug: {} for spec in specs}
    for slug, name, directory in flags:
        targets = [slug] if slug is not None else [s for s, roots in declared.items() if name in roots]
        known = [s for s in targets if name in declared.get(s, frozenset())]
        if not known:
            where = f"vault {slug!r}" if slug is not None else "any vault being initialized"
            raise KnowledgeConfigError(
                E_ROOT_UNKNOWN,
                f"--root {name}: {where} declares no root {name!r} in {repos.CONTEXTS_DIR_NAME}/"
                f"{repos.DECLARATION_NAME}",
            )
        for target in known:
            out[target][name] = directory
    return out


def _init_selection(
    add: Sequence[tuple[str | None, str]],
    drop: Sequence[tuple[str | None, str]],
    everything: bool,
    specs: Sequence[settings.VaultSpec],
) -> dict[str, contexts.Selection]:
    """``--repo``, ``--drop`` and ``--all`` per vault slug.

    An id that no targeted vault declares is ``E_CONTEXTS_REPO_UNKNOWN``.
    """
    declared = {spec.slug: contexts.declared_ids(spec.path) for spec in specs}
    picked: dict[str, dict[str, set[str]]] = {spec.slug: {"add": set(), "drop": set()} for spec in specs}
    for flag, flags in (("--repo", add), ("--drop", drop)):
        for slug, repo_id in flags:
            targets = [slug] if slug is not None else list(declared)
            known = [s for s in targets if repo_id in declared.get(s, frozenset())]
            if not known:
                where = f"vault {slug!r}" if slug is not None else "any vault being initialized"
                raise KnowledgeConfigError(
                    E_CONTEXTS_REPO_UNKNOWN,
                    f"{flag} {repo_id}: {where} declares no checkout {repo_id!r} in {repos.CONTEXTS_DIR_NAME}/"
                    f"{repos.DECLARATION_NAME}",
                )
            for target in known:
                picked[target]["add" if flag == "--repo" else "drop"].add(repo_id)
    return {
        slug: contexts.Selection(add=frozenset(sets["add"]), drop=frozenset(sets["drop"]), everything=everything)
        for slug, sets in picked.items()
    }


def _known_paths(specs: Sequence[settings.VaultSpec], env: Mapping[str, str]) -> list[Path]:
    """Directories this machine already names: vaults, and the roots and pins of their overlays."""
    known = [spec.path for spec in specs if spec.path is not None]
    overlays: list[Path] = []
    for spec in specs:
        try:
            overlay = repos.overlay_path_for(spec.slug, env)
        except repos.RegistryError:
            overlay = None
        if overlay is not None:
            overlays.append(overlay)
    for overlay in overlays:
        try:
            payload = repos.read_overlay(overlay)
        except repos.RegistryError:
            continue
        for key in ("roots", "repos"):
            block = payload.get(key)
            if isinstance(block, dict):
                known.extend(Path(str(value)).expanduser() for value in block.values() if str(value).strip())
    return known


def _init_report(
    loaded: settings.Settings, outcomes: Sequence[contexts.VaultOutcome], env: Mapping[str, str], dry_run: bool
) -> dict[str, Any]:
    vaults = [outcome.to_json() for outcome in outcomes]
    checkouts = [
        {"vault": outcome.slug, "id": app.id, "path": app.path, "status": app.status}
        for outcome in outcomes
        for app in outcome.apps
        if app.status in (contexts.INSTALLED, contexts.UNCHANGED, contexts.WOULD_INSTALL)
    ]
    home = repos.config_home(env)
    return {
        "ok": all(outcome.ok for outcome in outcomes),
        "dry_run": dry_run,
        "config_home": None if home is None else str(home),
        "vaults": vaults,
        "files": {
            "vaults_yaml": None if loaded.vaults_file is None else str(loaded.vaults_file),
            "repos_local": [
                {"vault": outcome.slug, "path": outcome.overlay, "action": outcome.overlay_action}
                for outcome in outcomes
                if outcome.overlay is not None and outcome.status == contexts.VAULT_INITIALIZED
            ],
            "checkouts": checkouts,
        },
    }


def _init_text(report: Mapping[str, Any], loaded: settings.Settings, init_command: str) -> str:
    lines = [init_command + (" (dry run: nothing is written)" if report["dry_run"] else ""), ""]
    for vault in report["vaults"]:
        lines.append(f"vault {vault['slug']}  {vault['path'] or '-'}")
        if vault["status"] != contexts.VAULT_INITIALIZED:
            lines.append(f"  {vault['status']}: {vault['detail']}")
            lines.append("")
            continue
        if vault["detail"]:
            lines.append(f"  note: {vault['detail']}")
        lines.extend(f"  warning: {warning}" for warning in vault.get("warnings", []))
        width = max([len(root["id"]) for root in vault["roots"]] + [len(app["id"]) for app in vault["apps"]] + [4])
        for root in vault["roots"]:
            where = root["path"] or "-"
            how = {
                "environment": root["source"],
                "overlay": repos.OVERLAY_NAME,
                "default": f"default of {repos.CONTEXTS_DIR_NAME}/{repos.DECLARATION_NAME}",
            }.get(root["kind"], root["kind"])
            note = f"  ({how})" if how else ""
            if root["detail"]:
                note += f"  {root['detail']}"
            lines.append(f"  root {root['id'].ljust(width)}  {where}{note}")
        lines.extend(_selection_text(vault.get("selection"), init_command, report["dry_run"]))
        for app in vault["apps"]:
            lines.append(f"  app  {app['id'].ljust(width)}  {app['status'].ljust(13)}  {app['path'] or '-'}")
            if app["status"] in (contexts.INSTALLED, contexts.WOULD_INSTALL, contexts.CONFLICT):
                lines.extend(f"         {action}" for action in app["actions"])
            if app["detail"]:
                lines.append(f"         {app['detail']}")
        lines.append("")
    if not any(vault["status"] == contexts.VAULT_INITIALIZED for vault in report["vaults"]):
        declaration = f"{repos.CONTEXTS_DIR_NAME}/{repos.DECLARATION_NAME}"
        if any(vault["status"] == contexts.VAULT_NO_APPS for vault in report["vaults"]):
            lines.append("No registered vault declares an app to link; nothing to initialize.")
        else:
            lines.append(f"No registered vault declares {declaration}; nothing to initialize.")
        lines.append("")
    files = report["files"]
    lines.append("Where everything lives (edit these files to change it):")
    if files["vaults_yaml"] is not None:
        lines.append(f"  vaults.yaml       {files['vaults_yaml']}")
    else:
        default = loaded.default_vault()
        lines.append(f"  vaults.yaml       none; the vault comes from VAULT_PATH ({default.path or 'unset'})")
    for overlay in files["repos_local"]:
        lines.append(f"  repos.local.json  {overlay['path']}  ({overlay['vault']}, {overlay['action']})")
    for checkout in files["checkouts"]:
        owner = f"{checkout['vault']}/{checkout['id']}"
        lines.append(f"  checkout          {checkout['path']}  ({owner}, {checkout['status']})")
    return "\n".join(lines)


def _selection_text(selection: Mapping[str, Any] | None, init_command: str, dry_run: bool) -> list[str]:
    """The lines of the text report that say which checkouts this machine selected and how that changed."""
    if selection is None:
        return []
    lines: list[str] = []
    if selection["apply"] is None:
        lines.append("  apply  not set: no checkout receives a package until one is chosen")
    else:
        how = {
            contexts.SELECTION_ADOPTED: "adopted from the packages already installed",
            contexts.SELECTION_PROMPT: "chosen at the prompt",
            contexts.SELECTION_FLAGS: "chosen with flags",
        }.get(selection["source"], repos.OVERLAY_NAME)
        lines.append(f"  apply  {', '.join(selection['apply']) or '(none)'}  ({how})")
    verb = "would " if dry_run else ""
    if selection["added"]:
        lines.append(f"         {verb}add: {', '.join(selection['added'])}")
    if selection["removed"]:
        lines.append(f"         {verb}remove: {', '.join(selection['removed'])}")
    if selection["unselected"]:
        lines.append(
            f"         on this machine and not selected: {', '.join(selection['unselected'])}; "
            f"add one with {init_command} --repo ID, or all with {init_command} --all"
        )
    return lines


def _init(args: argparse.Namespace) -> int:
    """Initialize this machine for every registered vault with ``Contexts/config.json``.

    Exit status 0 when every vault was handled (a checkout that is not on
    this machine is skipped, never a failure), 1 when a vault's declaration
    cannot be read, a ``--root`` names no directory, a checkout's origin is
    not its declared remote or a package conflicts with what a checkout
    already holds, and 2 when nothing could be attempted: a configuration
    error or a ``--root`` that no vault declares (``E_ROOT_UNKNOWN``).
    Nothing is written inside a vault.
    """
    env = _environment(None, args.vaults)
    try:
        loaded = settings.load_settings(env, project_root=PROJECT_ROOT)
        specs = [loaded.vault(args.vault)] if args.vault else list(loaded.vaults)
        root_flags = _init_root_flags(args.root, specs)
        selections = _init_selection(args.repo, args.drop, args.all, specs)
    except KnowledgeConfigError as exc:
        return _fail(exc)
    if args.search_dir:
        search_dirs = [Path(d).expanduser() for d in args.search_dir]
    else:
        search_dirs = contexts.default_search_dirs(env, _known_paths(specs, env))
    prompt = None if args.yes or not sys.stdin.isatty() else _ask
    served = [spec.path for spec in loaded.vaults if spec.path is not None]
    scan = contexts.memoized_scan()
    outcomes = [
        contexts.init_vault(
            contexts.VaultTarget(spec.slug, spec.path),
            env=env,
            root_flags=root_flags.get(spec.slug, {}),
            search_dirs=search_dirs,
            max_depth=args.max_depth,
            prompt=prompt,
            fence=served,
            dry_run=args.dry_run,
            force=args.force,
            refresh=args.refresh,
            exclude=args.exclude,
            scan=scan,
            selection=selections.get(spec.slug),
        )
        for spec in specs
    ]
    report = _init_report(loaded, outcomes, env, args.dry_run)
    if args.json:
        print(json.dumps(report, sort_keys=True, ensure_ascii=False, indent=2))
    else:
        print(_init_text(report, loaded, contexts.command_label(env, "init")))
    return 0 if report["ok"] else 1


def _migrate_index(args: argparse.Namespace) -> int:
    """One JSON line per vault: ``{vault, outcome, index_version, from}``.

    Exit status 0 when every vault migrated, 2 on the first configuration
    error (an unknown slug, an index of a newer layout, an index file SQLite
    cannot read, a refused prune); the lines of the vaults already handled
    stay printed.
    """
    from knowledge import registry

    env = _environment(None, args.vaults)
    try:
        loaded = settings.load_settings(env, project_root=PROJECT_ROOT)
        specs = [loaded.vault(args.vault)] if args.vault else list(loaded.vaults)
        if args.from_chroma and len(specs) != 1:
            raise KnowledgeConfigError(
                E_ARGUMENT_CONFLICT, "--from-chroma names one Chroma directory; pass --vault to choose its vault"
            )
        vault_paths = [spec.path for spec in loaded.vaults if spec.path is not None]
        for spec in specs:
            migration = registry.migrate_index(
                spec,
                from_chroma=Path(args.from_chroma).expanduser() if args.from_chroma else None,
                prune=args.prune_chroma,
                vault_paths=vault_paths,
            )
            line = migration.to_json()
            if args.build:
                settings.check_vault_dir(spec)
                runtime = registry.build_runtime(spec)
                line["built"] = runtime.indexer.index()
            print(json.dumps(line, ensure_ascii=False), flush=True)
    except KnowledgeConfigError as exc:
        return _fail(exc)
    except (sqlite3.Error, OSError) as exc:
        return _fail(KnowledgeConfigError(E_INDEX_UNREADABLE, f"{type(exc).__name__}: {exc}"))
    return 0


def _confluence_export(args: argparse.Namespace) -> int:
    """Write the Confluence pages the vault is behind on outside every served vault; print a JSON report.

    Exit status 0 when every tracked page was compared, 1 when the report
    carries an error (nothing tracked in scope, a page that could not be
    fetched, read, converted or written) and 2 on a configuration error: an
    unknown vault, an invalid ``integrations.confluence`` block
    (``E_MANIFEST_INVALID``), missing credentials (``E_SECRET_MISSING``), an
    ``--out`` inside a served vault (``E_WRITE_FENCED``), refused before
    anything is written, or a filesystem failure outside a single page
    (``E_EXPORT_FAILED``). Errors are one ``<code>: <message>`` line on
    stderr, never a traceback.
    """
    env = _environment(None, args.vaults)
    try:
        loaded = settings.load_settings(env, project_root=PROJECT_ROOT)
        spec = loaded.vault(args.vault) if args.vault else loaded.default_vault()
        settings.check_vault_dir(spec)
        out = (
            Path(args.out).expanduser()
            if args.out
            else loaded.knowledge_home / spec.slug / "confluence-export"
        )
        report = server.load_extension("confluence").export(
            out,
            vault_path=spec.require_path(),
            served_vaults=[s.path for s in loaded.vaults if s.path is not None],
            space_key=args.space,
            scope_prefix=args.scope_prefix,
            integrations=spec.integrations,
            dry_run=args.dry_run,
        )
    except (KnowledgeConfigError, KnowledgeToolError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except OSError as exc:
        print(
            f"{E_EXPORT_FAILED}: {type(exc).__name__}: {exc}; check that the vault is readable "
            "and that --out can be created and written",
            file=sys.stderr,
        )
        return 2
    print(json.dumps(report, sort_keys=True, ensure_ascii=False, indent=2))
    return 1 if report.get("error") or report.get("errors") else 0


def _bridge_vault(args: argparse.Namespace) -> tuple[dict[str, str], settings.Settings, settings.VaultSpec]:
    """The environment, settings and vault a ``memory-bridge`` subcommand acts on.

    The vault must declare the ``memory-bridge`` feature
    (``E_FEATURE_DISABLED``) and a ``memory.project``
    (``E_MEMORY_PROJECT_MISSING``).
    """
    env = _environment(None, args.vaults)
    loaded = settings.load_settings(env, project_root=PROJECT_ROOT)
    spec = loaded.vault(args.vault) if args.vault else loaded.default_vault()
    if "memory-bridge" not in spec.features:
        raise KnowledgeConfigError(
            E_FEATURE_DISABLED,
            f"{spec.slug}: the vault does not declare the memory-bridge feature; add it to features in vault.yaml",
        )
    if not spec.memory_project:
        raise KnowledgeConfigError(
            E_MEMORY_PROJECT_MISSING, f"{spec.slug}: the vault declares no memory.project to publish to"
        )
    settings.check_vault_dir(spec)
    return env, loaded, spec


def _memory_bridge(args: argparse.Namespace) -> int:
    """Run a ``memory-bridge`` subcommand and print its JSON report.

    Exit status 0 when every step succeeded, 1 when memory refused or failed
    a step (the report says which), 2 when nothing could be attempted: a
    configuration error, no bearer, a memory URL off the loopback, an invalid
    ``--knowledge-ref``, or a memory command that cannot run. Errors are one
    ``<code>: <message>`` line on stderr, never a traceback, never the bearer.
    """
    try:
        env, loaded, spec = _bridge_vault(args)
        bridge = server.load_extension("memory-bridge")
        project = spec.memory_project or ""
        if args.bridge_command == "sync":
            report = bridge.sync_vault(spec, bridge.writer_for(loaded.memory), card=args.card)
            status = 0 if report["ok"] else 1
        elif args.promote_command == "list":
            types = [t.strip() for t in (args.types or "").split(",") if t.strip()]
            status, report = bridge.promote_list(project, env=env, types=types, limit=args.limit)
        else:
            status, report = bridge.promote_stamp(
                project, args.observation, args.knowledge_ref, vault_path=spec.require_path(), env=env
            )
            status = 0 if status == 0 else 1
    except (KnowledgeConfigError, KnowledgeToolError, MemoryWriteFailed) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True, ensure_ascii=False, indent=2))
    return 0 if status == 0 else 1


#: Codes of a ``jira sync`` that reached Jira and failed there: exit status 1, not 2.
_JIRA_RUNTIME_CODES = frozenset({E_JIRA_UNAVAILABLE, E_JIRA_AUTH_FAILED, E_JIRA_SYNC_STALLED})


def _jira_line(report: Mapping[str, Any]) -> str:
    skipped = len(report["skipped"])
    verb = "would upsert" if report["dry_run"] else "upserted"
    count = report["fetched"] + report["retried"] - skipped if report["dry_run"] else report["upserted"]
    line = (
        f"jira sync {report['vault']} -> memory {report['memory_project']}: fetched {report['fetched']}, "
        f"{verb} {count}, created {report['created']}, skipped {skipped}, next_since {report['next_since']}"
    )
    if report["retried"]:
        line += f", {report['retried']} read again after a refusal"
    if report["truncated"]:
        line += " (truncated: run again to continue)"
    if "error" in report:
        line += f"; stopped: {report['error']['code']}"
    return line


def _jira(args: argparse.Namespace) -> int:
    """Run ``jira sync`` and print its report (one line, or JSON with ``--json`` or ``--auto``).

    Exit status 0 when every issue read was handled, 1 when Jira refused or
    failed the search or memory stopped answering mid-run, 2 when nothing
    could be attempted: a configuration error, missing credentials (names
    only), no memory bearer, a vault without the jira feature. ``--auto``
    always exits 0, whatever fails: it is the session hook's background run.
    """
    if args.auto:
        return _jira_auto(args.vaults)
    env = _environment(None, args.vaults)
    try:
        loaded = settings.load_settings(env, project_root=PROJECT_ROOT)
        jira = server.load_extension("jira")
        spec = loaded.vault(args.vault) if args.vault else loaded.default_vault()
        bound = {} if args.max_issues is None else {"max_issues": args.max_issues}
        report = jira.sync(spec, env=env, memory=loaded.memory, dry_run=args.dry_run, since=args.since, **bound)
    except (KnowledgeConfigError, KnowledgeToolError) as exc:
        print(str(exc), file=sys.stderr)
        return 1 if exc.code in _JIRA_RUNTIME_CODES else 2
    if args.json:
        print(json.dumps(report, sort_keys=True, ensure_ascii=False, indent=2))
    else:
        print(_jira_line(report))
    return 1 if "error" in report else 0


def _jira_auto(vaults: str | None) -> int:
    """``jira sync --auto``: the JSON rows of :func:`knowledge.jira_sync.auto`, or nothing; always 0."""
    try:
        env = _environment(None, vaults)
        loaded = settings.load_settings(env, project_root=PROJECT_ROOT)
        rows = server.load_extension("jira").auto(loaded, env)
    except Exception as exc:  # noqa: BLE001 - the session hook's run never fails the session
        logging.getLogger(__name__).warning("jira sync --auto did not run: %s", type(exc).__name__)
        return 0
    print(json.dumps(rows, sort_keys=True, ensure_ascii=False))
    return 0


def _glued_since(tokens: list[str]) -> list[str]:
    """``--since -7d`` as ``--since=-7d``: argparse reads a value that starts with ``-`` as an option."""
    out: list[str] = []
    rest = iter(tokens)
    for token in rest:
        if token == "--since":
            value = next(rest, None)
            out.append(token if value is None else f"--since={value}")
        else:
            out.append(token)
    return out


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch a subcommand and return the process exit status."""
    tokens = list(sys.argv[1:] if argv is None else argv)
    if tokens and tokens[0] == "eval":
        return _eval(tokens[1:])
    args = _build_parser().parse_args(_glued_since(tokens))
    if args.command in (None, "serve"):
        return _serve(getattr(args, "transport", None), getattr(args, "vaults", None), getattr(args, "profile", None))
    if args.command == "doctor":
        return _doctor(args.vaults, args.brief, args.client)
    if args.command == "init":
        return _init(args)
    if args.command == "migrate-index":
        return _migrate_index(args)
    if args.command == "confluence":
        return _confluence_export(args)
    if args.command == "memory-bridge":
        return _memory_bridge(args)
    if args.command == "jira":
        return _jira(args)
    if args.fix and args.rules != RULES_1_3:
        _build_parser().error(f"--fix needs --rules {RULES_1_3}")
    if args.out is not None and not args.fix:
        _build_parser().error("--out needs --fix")
    return _vault_lint(args.path, args.rules, args.fix, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
