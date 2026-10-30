#!/usr/bin/env python3
"""Resolve ``KNOWLEDGE_VAULTS_YAML`` into a ``vaults.yaml`` the server reads and a clone plan.

The container entrypoint (``infra/bootstrap-vault.sh``) calls this script when
``KNOWLEDGE_VAULTS_YAML`` carries the content of a ``vaults.yaml``. Every vault
entry is one of two kinds:

* ``repo`` (optionally ``git.branch``, default ``main``): a git vault. The
  entrypoint clones or refreshes it, into ``path`` when the entry names one and
  into ``<KNOWLEDGE_VAULTS_ROOT>/<slug>`` (``/data/vaults/<slug>``) otherwise.
* ``path`` only: a folder that is already mounted. It is used as it is and git
  never runs for it.

The script writes the resolved document to ``OUT_FILE`` (every entry with an
absolute ``path``) and prints one tab-separated line per git vault on stdout::

    <slug> <repo> <branch> <path> <key-variable-or-"-">

The key variable is the *name* of the environment variable that holds the
deploy key, picked in this order: ``deploy_key_env`` of the entry,
``<SLUG_UPPER>_GIT_DEPLOY_KEY_B64`` and the shared ``GIT_DEPLOY_KEY_B64``. A
repository reached over ``file://``, ``http(s)://`` or a local path needs no
key and gets ``-``. Key material is never read into the output, never printed
and never written to ``OUT_FILE``.

Credentials embedded in a remote (``https://user:token@host/...``) are needed
by git, so they stay in ``OUT_FILE``; every message this script prints shows the
URL through :func:`redact` instead.

Only the standard library and PyYAML are used, so the script runs from the
image's interpreter without importing the ``knowledge`` package. Schema checks
beyond what the entrypoint needs (features, bearers, memory) are left to the
server, which validates the resolved file when it starts.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Same rule as ``knowledge.settings.SLUG_PATTERN``: it names the index directory.
SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}[a-z0-9]$")
#: A branch name that cannot be read as an option and carries no whitespace.
BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
#: ``git@host:path`` and ``host:path`` remotes; ``scheme://`` and local paths do not match.
SCP_LIKE = re.compile(r"^(?:[^/@:\s]+@)?[^/@:\s]+:(?!//)")
SSH_SCHEMES = ("ssh://", "git+ssh://", "ssh+git://")

#: ``scheme://user:password@``, and any ``user@`` on http(s), where the user is often the token itself.
_URL_WITH_PASSWORD = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)[^/@:\s]*:[^/@\s]*@")
_HTTP_URL_WITH_USER = re.compile(r"(https?://)[^/@\s]+@")

DEFAULT_ROOT = "/data/vaults"
SHARED_KEY_ENV = "GIT_DEPLOY_KEY_B64"
NO_KEY = "-"

EXIT_INVALID = 2
EXIT_KEY_MISSING = 64


class ResolveError(Exception):
    """A problem with the declared vaults: a stable ``E_`` code, a message and an exit status."""

    def __init__(self, code: str, message: str, status: int = EXIT_INVALID) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.status = status


@dataclass(frozen=True)
class GitVault:
    """One vault the entrypoint has to clone or refresh."""

    slug: str
    repo: str
    branch: str
    path: str
    key_env: str | None

    def plan_line(self) -> str:
        return "\t".join((self.slug, self.repo, self.branch, self.path, self.key_env or NO_KEY))


def redact(text: str) -> str:
    """``text`` with the credentials of every URL in it replaced by ``***``; a plain ``ssh://git@host`` is kept."""
    return _HTTP_URL_WITH_USER.sub(r"\1***@", _URL_WITH_PASSWORD.sub(r"\1***@", text))


def needs_ssh_key(repo: str) -> bool:
    """Whether reading ``repo`` goes through ssh and therefore needs a deploy key."""
    return repo.startswith(SSH_SCHEMES) or SCP_LIKE.match(repo) is not None


def key_candidates(slug: str, declared: str | None) -> list[str]:
    """Environment variables that may hold the key of ``slug``, most specific first."""
    names = [declared] if declared else []
    names.append(f"{slug.upper().replace('-', '_')}_GIT_DEPLOY_KEY_B64")
    names.append(SHARED_KEY_ENV)
    return names


def _clean_text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResolveError("E_MANIFEST_INVALID", f"{where} must be a non-empty string")
    text = value.strip()
    if any(ch in text for ch in "\t\r\n\x00"):
        raise ResolveError("E_MANIFEST_INVALID", f"{where} must not contain tabs or line breaks")
    return text


def _load(raw: str) -> dict[str, Any]:
    import yaml

    try:
        loaded = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark is not None else ""
        raise ResolveError("E_MANIFEST_INVALID", f"KNOWLEDGE_VAULTS_YAML is not valid YAML{where}") from None
    if not isinstance(loaded, dict):
        raise ResolveError("E_MANIFEST_INVALID", "KNOWLEDGE_VAULTS_YAML must be a mapping with a 'vaults' list")
    return loaded


def _anchored(path: str, base: Path) -> str:
    candidate = Path(path).expanduser()
    return str(candidate if candidate.is_absolute() else base / candidate)


def _check_overlap(vaults: list[tuple[str, str]]) -> None:
    """Refuse two vaults whose directories are the same, or one inside the other, once symlinks and ``..`` are gone."""
    real = [(slug, path, os.path.realpath(path).rstrip(os.sep) or os.sep) for slug, path in vaults]
    for index, (slug, path, resolved) in enumerate(real):
        for other_slug, other_path, other in real[index + 1 :]:
            inside = resolved.startswith(other + os.sep) or other.startswith(resolved + os.sep)
            if resolved == other or inside:
                raise ResolveError(
                    "E_VAULTS_PATH_OVERLAP",
                    f"{slug} ({path}) and {other_slug} ({other_path}) overlap: a vault never shares its "
                    "directory with another vault, nor sits inside it",
                )


def _pick_key(slug: str, repo: str, declared: str | None, env: Mapping[str, str]) -> str | None:
    if not needs_ssh_key(repo):
        return None
    candidates = key_candidates(slug, declared)
    for name in candidates:
        if env.get(name, "").strip():
            return name
    raise ResolveError(
        "E_DEPLOY_KEY_MISSING",
        f"{slug} declares repo {redact(repo)} over ssh but none of {', '.join(candidates)} is set",
        EXIT_KEY_MISSING,
    )


def resolve(
    raw: str, env: Mapping[str, str], out_file: Path, *, root: str = DEFAULT_ROOT
) -> tuple[dict[str, Any], list[GitVault]]:
    """The resolved document and the git vaults to clone, in declaration order.

    ``out_file`` only anchors relative paths (the server anchors them at the
    directory of the ``vaults.yaml`` it reads); nothing is written here.
    """
    document = _load(raw)
    entries = document.get("vaults")
    if not isinstance(entries, list) or not entries:
        raise ResolveError("E_MANIFEST_INVALID", "vaults must be a non-empty list")

    base = out_file.resolve().parent
    seen: set[str] = set()
    plan: list[GitVault] = []
    resolved: list[Any] = []
    for index, entry in enumerate(entries):
        where = f"vaults[{index}]"
        if not isinstance(entry, dict):
            raise ResolveError("E_MANIFEST_INVALID", f"{where} must be a mapping")
        slug = entry.get("slug")
        if not isinstance(slug, str) or not SLUG_PATTERN.fullmatch(slug):
            raise ResolveError(
                "E_VAULT_SLUG_INVALID",
                f"{where}.slug must match {SLUG_PATTERN.pattern} (lowercase letters, digits and inner hyphens)",
            )
        if slug in seen:
            raise ResolveError("E_VAULT_SLUG_DUPLICATE", f"{where}.slug {slug!r} is already registered")
        seen.add(slug)

        repo_raw, path_raw = entry.get("repo"), entry.get("path")
        if repo_raw is None and path_raw is None:
            raise ResolveError("E_MANIFEST_INVALID", f"{where} needs repo (a git vault) or path (a mounted folder)")

        out = dict(entry)
        if repo_raw is None:
            path = _anchored(_clean_text(path_raw, f"{where}.path"), base)
            if not os.path.isdir(path):
                raise ResolveError(
                    "E_VAULT_PATH_NOT_DIR",
                    f"{slug}: {path} is not a directory; mount the folder there or declare repo",
                )
            out["path"] = path
        else:
            repo = _clean_text(repo_raw, f"{where}.repo")
            if repo.startswith("-"):
                raise ResolveError("E_MANIFEST_INVALID", f"{where}.repo must not start with '-'")
            git = entry.get("git")
            if git is not None and not isinstance(git, dict):
                raise ResolveError("E_MANIFEST_INVALID", f"{where}.git must be a mapping")
            branch = _clean_text((git or {}).get("branch", "main"), f"{where}.git.branch")
            if not BRANCH_PATTERN.fullmatch(branch):
                raise ResolveError("E_MANIFEST_INVALID", f"{where}.git.branch is not a valid branch name")
            declared = entry.get("deploy_key_env")
            if declared is not None and (not isinstance(declared, str) or not ENV_NAME_PATTERN.fullmatch(declared)):
                raise ResolveError("E_MANIFEST_INVALID", f"{where}.deploy_key_env must be an environment variable name")
            path = _anchored(
                _clean_text(path_raw, f"{where}.path") if path_raw is not None else f"{root.rstrip('/')}/{slug}",
                base,
            )
            out["path"] = path
            plan.append(GitVault(slug, repo, branch, path, _pick_key(slug, repo, declared, env)))
        resolved.append(out)

    _check_overlap([(str(entry["slug"]), str(entry["path"])) for entry in resolved])
    return {**document, "vaults": resolved}, plan


def main(argv: list[str], env: Mapping[str, str]) -> int:
    if len(argv) != 1:
        print("usage: resolve-vaults.py OUT_FILE", file=sys.stderr)
        return EXIT_INVALID
    raw = env.get("KNOWLEDGE_VAULTS_YAML", "")
    if not raw.strip():
        print("E_MANIFEST_INVALID: KNOWLEDGE_VAULTS_YAML is empty", file=sys.stderr)
        return EXIT_INVALID
    out_file = Path(argv[0])
    try:
        document, plan = resolve(raw, env, out_file, root=env.get("KNOWLEDGE_VAULTS_ROOT") or DEFAULT_ROOT)
    except ResolveError as exc:
        print(str(exc), file=sys.stderr)
        return exc.status

    import yaml

    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True), encoding="utf-8")
    for vault in plan:
        print(vault.plan_line())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], os.environ))
