"""Install the context packages a vault declares into the checkouts on this machine.

A vault declares in ``Contexts/config.json`` which repositories exist (the
registry :mod:`knowledge.repos` reads) and, for each one, the package under
``Contexts/<context>/`` that gives an AI client its working context in that
checkout: ``CLAUDE.md``, ``.mcp.json`` and the ``.claude/`` tree. This module
is the client side of that declaration, behind ``cloudfrog-knowledge init``.
A remote server cannot do any of it: it never sees the client's disk.

For each vault, :func:`init_vault`

1. decides where each root lives on this machine: a ``--root`` flag, what this
   machine already declares (environment variable or ``repos.local.json``),
   the root's ``default`` when that directory exists, a scan of the search
   directories for git checkouts whose ``origin`` names a declared remote, or
   an answer typed at a prompt. A root nothing locates is skipped;
2. decides which checkouts receive a package on this machine: the ``apply``
   list of ``repos.local.json``, changed by ``--repo ID``, ``--all`` and
   ``--drop ID``. A file without the list adopts the checkouts whose package is
   already installed, so a machine set up before the list existed keeps its
   setup; a machine with nothing to adopt and no flag is asked in a terminal
   and otherwise installs nothing and says how to choose;
3. writes ``<config home>/knowledge/<slug>/repos.local.json`` with those roots,
   the checkouts it found away from their declared path, the selection, the
   vault directory and the slug, so :mod:`knowledge.repos` finds the vault
   again without reading ``vaults.yaml``. Locations that come from the
   environment stay in the environment;
4. installs the package of every selected checkout that is on disk, by
   relative symlinks (``install_mode: link``, the recommended mode) or by copy
   (``copy``, kept for compatibility), adds the ignore entries to the checkout
   (``.git/info/exclude`` by default), reports every present checkout that is
   not selected as ``not-selected`` and skips every checkout that is not there.
   ``--drop ID`` removes the links of that checkout that point into its
   package and nothing else.

The selection concerns package installation only: graph targets and the
server read the registry without it.

Nothing is written inside a vault (ADR-044): the overlay lives in the user's
configuration directory and the links and copies in the checkouts, and a
write whose location falls inside a served vault is refused with
``E_WRITE_FENCED`` before anything happens. Pre-existing content is never
clobbered: link mode replaces only symlinks, and a real file or directory
needs ``force`` (moved aside to ``<name>.pre-ai-context.bak-<epoch>``) or, for
a copy that drifted from its package, ``refresh``. ``settings.local.json``,
``agent-memory/`` and ``worktrees/`` inside ``.claude`` are personal: never
linked or copied from a package, never removed from a checkout.

In link mode a ``.claude`` that is a real directory of the checkout stays one
(Claude Code refuses to create worktrees under a linked ``.claude``): each
entry of the package's ``.claude`` is linked inside it, and so is each entry
of a ``.claude/<collection>/`` the checkout tracks in git. Below that an entry
(a skill, an agent) is a unit. Whatever the checkout tracks in git is its own:
it is kept, reported as owned by the checkout, and never a conflict.

Every step is idempotent: a second run with nothing changed writes nothing and
reports every installed checkout as ``unchanged``.
"""

from __future__ import annotations

import filecmp
import json
import os
import shutil
import subprocess
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from knowledge import repos
from knowledge.errors import (
    E_CONTEXTS_INVALID,
    E_WRITE_FENCED,
    W_CONTEXTS_GITIGNORE,
    W_CONTEXTS_LINKED_NOT_SELECTED,
)

#: What a context package installs into a checkout, in install order.
MANAGED = (".claude", "CLAUDE.md", ".mcp.json")
#: Names inside a copied ``.claude`` that belong to the person, not to the package.
PERSONAL = ("settings.local.json", "worktrees", "agent-memory")
#: Suffix of the copy ``force`` keeps of what it replaced.
BACKUP_MARK = ".pre-ai-context.bak-"
#: How many directory levels link mode enters: ``.claude`` and ``.claude/<collection>``.
INTO_DEPTH = 2

#: Names the command a hint tells the person to run, for clients that wrap this one.
COMMAND_LABEL_ENV = "KNOWLEDGE_COMMAND_LABEL"
#: Replaced by the subcommand inside a label; a label without it is followed by the subcommand.
COMMAND_PLACEHOLDER = "{command}"
DEFAULT_COMMAND = "cloudfrog-knowledge"

EXCLUDE_LOCAL = "local"
EXCLUDE_GITIGNORE = "gitignore"
EXCLUDE_TARGETS = (EXCLUDE_LOCAL, EXCLUDE_GITIGNORE)
#: Anchored to the checkout root: an unanchored ``.claude`` also hides nested ones.
IGNORE_ENTRIES = tuple(f"/{name}" for name in MANAGED) + tuple(f"/{name}{BACKUP_MARK}*" for name in MANAGED)

DEFAULT_MAX_DEPTH = 4
_SCAN_SKIP = frozenset({"node_modules", "vendor", "__pycache__"})

INSTALLED = "installed"
UNCHANGED = "unchanged"
WOULD_INSTALL = "would-install"
SKIPPED = "skipped"
CONFLICT = "conflict"
ERROR = "error"
#: The checkout's origin could not be read, so nothing proves it is the declared repository.
UNVERIFIED = "unverified"
#: The checkout is on this machine and is not in the ``apply`` list: information, never a failure.
NOT_SELECTED = "not-selected"
FAILED = frozenset({CONFLICT, ERROR, UNVERIFIED})

#: What Claude Code writes through a linked ``.claude`` into ``Contexts/<package>/.claude/`` of the vault:
#: the entry a ``.gitignore`` rule names and a path inside it that the rule has to cover.
CLAUDE_WRITES = (
    ("settings.local.json", "settings.local.json"),
    ("agent-memory/", "agent-memory/probe"),
    ("worktrees/", "worktrees/probe"),
)
GITIGNORE_COVERED = "covered"
GITIGNORE_GAPS = "gaps"
GITIGNORE_NOT_APPLICABLE = "not-applicable"

#: Key of ``repos.local.json`` that lists the registry ids whose package ``init`` installs.
APPLY_KEY = "apply"
#: Where the selection of a run came from.
SELECTION_OVERLAY = "overlay"
SELECTION_ADOPTED = "adopted"
SELECTION_PROMPT = "prompt"
SELECTION_FLAGS = "flags"
SELECTION_UNSET = "unset"

VAULT_INITIALIZED = "initialized"
VAULT_NO_CONTEXTS = "no-contexts"
VAULT_NO_APPS = "no-apps"
VAULT_SKIPPED = "skipped"
VAULT_ERROR = "error"

#: Asks one question and returns the answer, or ``None`` for no answer.
Prompt = Callable[[str], "str | None"]



def command_label(env: Mapping[str, str], command: str) -> str:
    """The command a hint names for ``command`` (``init``, ``doctor``).

    ``KNOWLEDGE_COMMAND_LABEL`` lets a wrapper name itself instead of this CLI:
    ``{command}`` inside it is replaced by the subcommand
    (``Contexts/scripts/{command}.sh``), and a label without it is followed by
    the subcommand. Unset or blank, the hint names ``cloudfrog-knowledge``.
    """
    label = (env.get(COMMAND_LABEL_ENV) or "").strip()
    if not label:
        return f"{DEFAULT_COMMAND} {command}"
    if COMMAND_PLACEHOLDER in label:
        return label.replace(COMMAND_PLACEHOLDER, command)
    return f"{label} {command}"


def overlay_readme(init_command: str) -> list[str]:
    """The ``_README`` of a ``repos.local.json`` written by ``init_command``."""
    return [
        "Where the checkouts of this vault live ON THIS MACHINE. Written by",
        f"`{init_command}`; edit it freely and run init again.",
        "",
        "  vault   the vault directory whose Contexts/config.json declares",
        "          what each checkout IS (id, remote, root, relative path).",
        "  roots   absolute directory of each root of that declaration.",
        "  repos   one checkout that does not sit at <root>/<declared path>.",
        "  apply   the ids of the checkouts that receive their context package",
        f"          on this machine; `{init_command} --repo ID`, `--all` and",
        "          `--drop ID` change it. Graph targets do not read it.",
        "",
        "KNOWLEDGE_<ROOT>_ROOT and KNOWLEDGE_REPO_<ID> override this file;",
        "CD_<ROOT>_ROOT and CD_REPO_<ID> still work and are deprecated.",
    ]


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Selection:
    """What the flags of one ``init`` run change in the ``apply`` list of a vault.

    ``add`` holds the ``--repo`` ids, ``drop`` the ``--drop`` ids and
    ``everything`` is ``--all``: every checkout on this machine with a package.
    A ``--drop`` wins over ``--repo`` and ``--all`` for the same id.
    """

    add: frozenset[str] = frozenset()
    drop: frozenset[str] = frozenset()
    everything: bool = False

    @property
    def given(self) -> bool:
        return bool(self.add or self.drop or self.everything)


@dataclass
class SelectionOutcome:
    """The ``apply`` list of one vault before and after a run, and where it came from.

    ``apply`` is ``None`` when the run leaves the list unset: a machine with
    nothing to adopt, no flag and no answer. ``previous`` is ``None`` when the
    file had no list. ``source`` is ``overlay``, ``adopted``, ``prompt``,
    ``flags`` or ``unset``.
    """

    apply: list[str] | None
    previous: list[str] | None
    source: str
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    unselected: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "apply": None if self.apply is None else list(self.apply),
            "previous": None if self.previous is None else list(self.previous),
            "source": self.source,
            "added": list(self.added),
            "removed": list(self.removed),
            "unselected": list(self.unselected),
        }


@dataclass(frozen=True)
class VaultTarget:
    """A registered vault: its slug and its directory, if one is declared."""

    slug: str
    path: Path | None


@dataclass
class RootChoice:
    """Where one root of a vault ended up on this machine, and how that was decided.

    ``kind`` is ``flag``, ``environment``, ``overlay``, ``default``,
    ``detected`` or ``prompt``; empty when nothing located the root.
    ``failed`` marks a ``--root`` that names no directory: an explicit
    instruction that could not be followed, never a quiet skip.
    """

    id: str
    path: str | None = None
    kind: str = ""
    source: str = ""
    detail: str = ""
    failed: bool = False

    def to_json(self) -> dict[str, Any]:
        status = "error" if self.failed else ("located" if self.path else "not-located")
        return {
            "id": self.id,
            "status": status,
            "path": self.path,
            "kind": self.kind,
            "source": self.source,
            "detail": self.detail,
        }


@dataclass
class AppOutcome:
    """What ``init`` did with one declared checkout."""

    id: str
    context: str | None
    mode: str | None
    status: str
    path: str | None = None
    detail: str = ""
    actions: list[str] = field(default_factory=list)
    #: An ``unverified`` checkout that is a git working tree with no origin remote
    #: (``doctor --client`` warns about it instead of failing). Not part of the JSON report.
    origin_missing: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "context": self.context,
            "mode": self.mode,
            "status": self.status,
            "path": self.path,
            "detail": self.detail,
            "actions": list(self.actions),
        }


@dataclass
class VaultOutcome:
    """What ``init`` did with one vault."""

    slug: str
    path: str | None
    status: str = VAULT_INITIALIZED
    declaration: str | None = None
    overlay: str | None = None
    overlay_action: str = ""
    detail: str = ""
    #: Whether the vault's own ``.gitignore`` covers what Claude Code writes through the links.
    gitignore: str = ""
    warnings: list[str] = field(default_factory=list)
    roots: list[RootChoice] = field(default_factory=list)
    apps: list[AppOutcome] = field(default_factory=list)
    selection: SelectionOutcome | None = None

    @property
    def ok(self) -> bool:
        return (
            self.status != VAULT_ERROR
            and not any(root.failed for root in self.roots)
            and all(app.status not in FAILED for app in self.apps)
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "path": self.path,
            "status": self.status,
            "ok": self.ok,
            "declaration": self.declaration,
            "overlay": self.overlay,
            "overlay_action": self.overlay_action,
            "detail": self.detail,
            "gitignore": self.gitignore,
            "warnings": list(self.warnings),
            "roots": [root.to_json() for root in self.roots],
            "apps": [app.to_json() for app in self.apps],
            "selection": None if self.selection is None else self.selection.to_json(),
        }


# ---------------------------------------------------------------------------
# Finding checkouts
# ---------------------------------------------------------------------------


def default_search_dirs(env: Mapping[str, str], known: Iterable[Path]) -> list[Path]:
    """``~/Projects`` and the parent of every known path, existing directories only, in order."""
    candidates: list[Path] = []
    home = (env.get("HOME") or "").strip()
    if home:
        candidates.append(Path(home) / "Projects")
    candidates.extend(Path(path).parent for path in known)
    out: list[Path] = []
    for candidate in candidates:
        if candidate not in out and candidate.is_dir():
            out.append(candidate)
    return out


def scan_checkouts(search_dirs: Iterable[Path], *, max_depth: int = DEFAULT_MAX_DEPTH) -> list[tuple[Path, str]]:
    """Git checkouts under ``search_dirs``, at most ``max_depth`` levels down, with their origin URL.

    Dot-prefixed directories, ``node_modules``, ``vendor`` and
    ``__pycache__`` are not entered, symbolic links are not followed, and a
    checkout without an origin remote is not reported: nothing could match it.
    """
    found: dict[Path, str] = {}
    seen: set[Path] = set()
    for base in search_dirs:
        try:
            root = base.expanduser().resolve()
        except OSError:
            continue
        if not root.is_dir():
            continue
        for dirpath, dirnames, _files in os.walk(root):
            current = Path(dirpath)
            depth = len(current.parts) - len(root.parts)
            if current not in seen:
                seen.add(current)
                if (current / ".git").exists():
                    url, _reason = repos.read_origin(current)
                    if url:
                        found[current] = url
            if depth >= max_depth:
                dirnames[:] = []
            else:
                dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in _SCAN_SKIP)
    return sorted(found.items())


def memoized_scan(
    scan: Callable[..., list[tuple[Path, str]]] | None = None,
) -> Callable[..., list[tuple[Path, str]]]:
    """``scan`` (``scan_checkouts`` by default) that walks a given set of search directories once.

    Every vault of one ``init`` run looks for the same checkouts, so the second
    vault reuses what the first found instead of walking the tree and reading
    every repository again. The key is the directories and the depth; each
    caller gets its own copy of the list.
    """
    inner = scan if scan is not None else scan_checkouts
    cache: dict[tuple[tuple[str, ...], int], list[tuple[Path, str]]] = {}

    def cached(search_dirs: Iterable[Path], *, max_depth: int = DEFAULT_MAX_DEPTH) -> list[tuple[Path, str]]:
        dirs = list(search_dirs)
        key = (tuple(str(d) for d in dirs), max_depth)
        if key not in cache:
            cache[key] = inner(dirs, max_depth=max_depth)
        return list(cache[key])

    return cached


def _strip_suffix(path: Path, relative: str) -> Path | None:
    """``path`` without its trailing ``relative`` segments, or ``None`` when it does not end with them."""
    parts = Path(relative).parts if relative not in ("", ".") else ()
    if not parts:
        return path
    if len(path.parts) > len(parts) and path.parts[-len(parts) :] == parts:
        return Path(*path.parts[: -len(parts)])
    return None


def _declared_location(choice: RootChoice | None, entry: repos.RepoEntry) -> Path | None:
    if choice is None or not choice.path:
        return None
    base = Path(choice.path)
    return base if entry.path in ("", ".") else base / entry.path


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return Path(os.path.abspath(path))


# ---------------------------------------------------------------------------
# The write fence
# ---------------------------------------------------------------------------


def _location(path: Path) -> Path:
    """Where a write to ``path`` lands: its parent resolved, its own name kept.

    A symbolic link is judged by where it sits, never by what it points to:
    a link inside a checkout that points into the vault writes nothing there.
    """
    return _resolved(path.parent) / path.name


def fenced(path: Path, fence: Sequence[Path]) -> Path | None:
    """The served vault a write to ``path`` would land in, or ``None``."""
    location = _location(path)
    for vault in fence:
        root = _resolved(vault)
        if location == root or location.is_relative_to(root):
            return vault
    return None


# ---------------------------------------------------------------------------
# Installing one package
# ---------------------------------------------------------------------------


class _InstallError(RuntimeError):
    pass


def _git(checkout: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(["git", "-C", str(checkout), *args], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    out = completed.stdout.strip()
    return out if completed.returncode == 0 and out else None


def _exclude_file(checkout: Path, exclude: str) -> Path:
    if exclude == EXCLUDE_GITIGNORE:
        return checkout / ".gitignore"
    # info/exclude lives in the common git directory, which a linked
    # worktree does not share with its own: --git-path answers for both.
    out = _git(checkout, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude")
    if out is None:
        out = _git(checkout, "rev-parse", "--git-path", "info/exclude")
    if out is None:
        raise _InstallError(
            f"{checkout} is not a git checkout, so its ignore entries have nowhere to go; "
            "pass --exclude gitignore to write them into its .gitignore"
        )
    path = Path(out)
    return path if path.is_absolute() else checkout / path


def _missing_ignores(exclude_file: Path, entries: Sequence[str]) -> list[str]:
    try:
        present = {line.strip() for line in exclude_file.read_text(encoding="utf-8").splitlines()}
    except FileNotFoundError:
        present = set()
    return [entry for entry in entries if entry not in present]


def _tracked(checkout: Path, rel: str) -> bool:
    """Whether git tracks ``rel`` (or, for a directory, anything under it) in ``checkout``; read-only."""
    try:
        done = subprocess.run(
            ["git", "-C", str(checkout), "ls-files", "--error-unmatch", "--", rel],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def _write_ignores(exclude_file: Path, missing: Sequence[str]) -> None:
    if not missing:
        return
    exclude_file.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude_file.read_text(encoding="utf-8") if exclude_file.exists() else ""
    # A last line without its line break would swallow the first entry.
    prefix = "\n" if existing and not existing.endswith("\n") else ""
    with exclude_file.open("a", encoding="utf-8") as handle:
        handle.write(prefix + "".join(f"{entry}\n" for entry in missing))


def _can_symlink(checkout: Path) -> bool:
    if os.name == "nt":
        return False
    probe = checkout / f".ai-context-symlink-probe.{os.getpid()}"
    try:
        os.symlink(".", probe)
    except OSError:
        return False
    probe.unlink()
    return True


def _backup_path(target: Path) -> Path:
    base = f"{target.name}{BACKUP_MARK}{int(time.time())}"
    candidate = target.with_name(base)
    counter = 1
    while candidate.exists() or candidate.is_symlink():
        candidate = target.with_name(f"{base}-{counter}")
        counter += 1
    return candidate


def _top_level_personal(root: Path) -> Callable[[str, list[str]], list[str]]:
    top = os.fspath(root)

    def ignore(directory: str, names: list[str]) -> list[str]:
        return [name for name in names if directory == top and name in PERSONAL]

    return ignore


def _files(root: Path) -> dict[str, Path]:
    """Every file under ``root`` by relative POSIX path, without the top-level personal names."""
    out: dict[str, Path] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        if current == root:
            dirnames[:] = [d for d in dirnames if d not in PERSONAL]
            filenames = [f for f in filenames if f not in PERSONAL]
        for name in filenames:
            path = current / name
            out[path.relative_to(root).as_posix()] = path
    return out


def _same(source: Path, target: Path) -> bool:
    """Whether the real file or directory ``target`` already holds exactly what ``source`` holds."""
    if target.is_symlink():
        return False
    if source.is_dir():
        if not target.is_dir():
            return False
        ours, theirs = _files(source), _files(target)
        return ours.keys() == theirs.keys() and all(filecmp.cmp(ours[rel], theirs[rel], shallow=False) for rel in ours)
    return target.is_file() and filecmp.cmp(source, target, shallow=False)


def _copy(source: Path, target: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, target, ignore=_top_level_personal(source), dirs_exist_ok=True)
    else:
        shutil.copy2(source, target)


def _mirror(source: Path, target: Path) -> None:
    """Make the real directory ``target`` an exact copy of ``source``, personal names kept."""
    for child in target.iterdir():
        if child.name in PERSONAL:
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    _copy(source, target)


@dataclass
class _Step:
    name: str
    kind: str
    source: Path
    target: Path
    link: str = ""

    def describe(self) -> str:
        verbs = {
            "keep": "already in place",
            "link": f"-> {self.link}",
            "relink": f"-> {self.link} (replaces another link)",
            "backup-link": f"-> {self.link} (the previous content is kept as {self.name}{BACKUP_MARK}<epoch>)",
            "copy": "copied",
            "replace-link": "copied (replaces a link)",
            "refresh": "refreshed from the package",
            "backup-copy": f"copied (the previous content is kept as {self.name}{BACKUP_MARK}<epoch>)",
            "conflict": "exists and is not managed by init",
            "owned": "is tracked by the checkout and stays as it is (owned by the checkout)",
            "into": "is a directory of the checkout; the package entries are linked inside it",
        }
        return f"{self.name} {verbs[self.kind]}"

    def apply(self) -> None:
        is_dir = self.source.is_dir()
        if self.kind in PASSIVE:
            return
        if self.kind in ("relink", "replace-link"):
            self.target.unlink()
        backup: Path | None = None
        if self.kind in ("backup-link", "backup-copy"):
            backup = _backup_path(self.target)
            os.rename(self.target, backup)
        if self.kind in ("link", "relink", "backup-link"):
            os.symlink(self.link, self.target, target_is_directory=is_dir)
            return
        if self.kind == "refresh" and is_dir:
            _mirror(self.source, self.target)
            return
        _copy(self.source, self.target)
        if backup is not None and is_dir:
            for keep in PERSONAL:
                kept = backup / keep
                if kept.is_dir() and not kept.is_symlink():
                    shutil.copytree(kept, self.target / keep, symlinks=True, dirs_exist_ok=True)
                elif kept.exists() or kept.is_symlink():
                    shutil.copy2(kept, self.target / keep, follow_symlinks=False)


#: Steps that change nothing in the checkout.
PASSIVE = frozenset({"keep", "owned", "into"})
#: Steps that create a symbolic link.
LINKING = frozenset({"link", "relink", "backup-link"})


def _is_context_link(destination: str, rel: str) -> bool:
    """Whether a symlink points at ``.../Contexts/<package>/<rel>``: what ``init`` makes, wherever the vault is."""
    parts = Path(destination).parts
    tail = Path(rel).parts
    if len(parts) < len(tail) + 2:
        return False
    return parts[-len(tail) - 2] == repos.CONTEXTS_DIR_NAME and parts[-len(tail) :] == tail


def _plan_link(package: Path, checkout: Path, rel: str, *, force: bool, depth: int = 0) -> list[_Step]:
    """Link ``package/rel`` at ``checkout/rel``, or link its entries inside a real directory of the checkout."""
    source = package / rel
    target = checkout / rel
    link = os.path.relpath(_resolved(package) / rel, _resolved(target.parent))
    if target.is_symlink():
        current = os.readlink(target)
        if current == link:
            return [_Step(rel, "keep", source, target, link)]
        if _is_context_link(current, rel):
            return [_Step(rel, "relink", source, target, link)]
        # A link somebody made for their own reasons is theirs, like a file of their own.
        return [_Step(rel, "backup-link" if force else "conflict", source, target, link)]
    if not target.exists():
        return [_Step(rel, "link", source, target, link)]
    if (
        depth < INTO_DEPTH
        and source.is_dir()
        and target.is_dir()
        # The top-level .claude holds the person's worktrees and settings, so it is
        # entered even untracked; a collection under it only when the checkout versions it.
        and (depth == 0 or _tracked(checkout, rel))
    ):
        steps = [_Step(rel, "into", source, target)]
        for child in sorted(source.iterdir(), key=lambda path: path.name):
            if depth == 0 and child.name in PERSONAL:
                continue
            steps.extend(_plan_link(package, checkout, f"{rel}/{child.name}", force=force, depth=depth + 1))
        return steps
    if _tracked(checkout, rel):
        return [_Step(rel, "owned", source, target)]
    return [_Step(rel, "backup-link" if force else "conflict", source, target, link)]


def _plan(package: Path, checkout: Path, name: str, mode: str, *, force: bool, refresh: bool) -> list[_Step]:
    if mode == repos.INSTALL_MODE_LINK:
        return _plan_link(package, checkout, name, force=force)
    source = package / name
    target = checkout / name
    if target.is_symlink():
        if _is_context_link(os.readlink(target), name):
            return [_Step(name, "replace-link", source, target)]
        return [_Step(name, "backup-copy" if force else "conflict", source, target)]
    if not target.exists():
        return [_Step(name, "copy", source, target)]
    if _same(source, target):
        return [_Step(name, "keep", source, target)]
    if _tracked(checkout, name):
        return [_Step(name, "owned", source, target)]
    if refresh and source.is_dir() == target.is_dir():
        return [_Step(name, "refresh", source, target)]
    return [_Step(name, "backup-copy" if force else "conflict", source, target)]


def _ignores(steps: Sequence[_Step]) -> list[str]:
    """What the checkout ignores: every path init links or copies, and the backups ``force`` leaves beside it.

    A directory init only enters is the checkout's, and so is what the checkout
    tracks: neither is ignored, so a new file the person adds there still shows.
    """
    names = [step.name for step in steps if step.kind not in ("owned", "into")]
    return [f"/{name}" for name in names] + [f"/{name}{BACKUP_MARK}*" for name in names]


def install_package(
    package: Path,
    checkout: Path,
    mode: str,
    *,
    force: bool = False,
    refresh: bool = False,
    exclude: str = EXCLUDE_LOCAL,
    dry_run: bool = False,
    fence: Sequence[Path] = (),
    init_command: str = f"{DEFAULT_COMMAND} init",
) -> tuple[str, list[str], str]:
    """Install ``package`` into ``checkout``: ``(status, actions, detail)``.

    Plans every item first and changes nothing when one of them conflicts,
    so a checkout is never left half installed. ``status`` is ``installed``,
    ``unchanged``, ``would-install`` (dry run), ``conflict`` or ``error``.
    ``init_command`` is the command the conflict hint tells the person to run.
    """
    if not (package / "CLAUDE.md").is_file():
        return ERROR, [], f"the package {package} has no CLAUDE.md"
    items = [name for name in MANAGED if (package / name).exists()]
    try:
        exclude_file = _exclude_file(checkout, exclude)
    except _InstallError as exc:
        return ERROR, [], str(exc)

    def plan(how: str) -> list[_Step]:
        return [step for name in items for step in _plan(package, checkout, name, how, force=force, refresh=refresh)]

    # Planning only reads, so the fence can judge every place a step would write.
    steps = plan(mode)
    targets = [checkout / name for name in items] + [step.target for step in steps if step.kind not in PASSIVE]
    for path in [*targets, exclude_file]:
        vault = fenced(path, fence)
        if vault is not None:
            return (
                ERROR,
                [],
                (
                    f"{E_WRITE_FENCED}: {path} is inside the served vault at {vault}; "
                    "a checkout cannot live inside a vault"
                ),
            )

    effective = mode
    notes: list[str] = []
    # The probe writes into the checkout root, so it runs only when a link is
    # still to be created: a package already linked proves the file system holds them.
    needs_probe = any(step.kind in LINKING for step in steps)
    if mode == repos.INSTALL_MODE_LINK and not dry_run and needs_probe and not _can_symlink(checkout):
        effective = repos.INSTALL_MODE_COPY
        notes.append("this filesystem cannot hold symbolic links, so the package was copied")
        steps = plan(effective)
    conflicts = [step for step in steps if step.kind == "conflict"]
    if conflicts:
        hint = f"run {init_command} --force to move it aside (kept as <name>.pre-ai-context.bak-<epoch>)"
        if effective == repos.INSTALL_MODE_COPY:
            hint += f", or {init_command} --refresh when it is a copy init made that drifted from the package"
        return CONFLICT, [step.describe() for step in conflicts], hint
    owned = [step.name for step in steps if step.kind == "owned"]
    if owned:
        notes.append(f"owned by the checkout, kept as it is: {', '.join(owned)}")
    detail = "; ".join(notes)
    missing = _missing_ignores(exclude_file, _ignores(steps))
    changed = any(step.kind not in PASSIVE for step in steps) or bool(missing)
    actions = [step.describe() for step in steps]
    if missing:
        actions.append(f"{len(missing)} ignore entr{'y' if len(missing) == 1 else 'ies'} for {exclude_file}")
    if dry_run:
        return (WOULD_INSTALL if changed else UNCHANGED), actions, detail

    for step in steps:
        step.apply()
    claude = checkout / "CLAUDE.md"
    if not claude.is_file() or claude.stat().st_size == 0:
        return ERROR, actions, f"CLAUDE.md is not readable through {checkout} after the install"
    for name in items:
        if not (checkout / name).exists():
            return ERROR, actions, f"{name} does not resolve through {checkout} after the install"
    _write_ignores(exclude_file, missing)
    return (INSTALLED if changed else UNCHANGED), actions, detail


# ---------------------------------------------------------------------------
# What a checkout already holds of its package
# ---------------------------------------------------------------------------


def _points_into(destination: str, context: str, rel: str) -> bool:
    """Whether a symlink destination is ``.../Contexts/<context>/<rel>``, wherever the vault is."""
    parts = Path(destination).parts
    tail = Path(rel).parts
    if len(parts) < len(tail) + 2:
        return False
    return (
        parts[-len(tail) - 2] == repos.CONTEXTS_DIR_NAME
        and parts[-len(tail) - 1] == context
        and parts[-len(tail) :] == tail
    )


def package_links(checkout: Path, context: str) -> list[str]:
    """Paths, relative to ``checkout``, of the symlinks that point into ``Contexts/<context>/``.

    Looks where ``init`` links: the managed names at the checkout root and,
    through real directories only, the entries of ``.claude`` and of its
    collections. The personal names of ``.claude`` are never entered.
    """
    found: list[str] = []

    def visit(rel: str, depth: int) -> None:
        target = checkout / rel
        if target.is_symlink():
            if _points_into(os.readlink(target), context, rel):
                found.append(rel)
            return
        if depth >= INTO_DEPTH or not target.is_dir():
            return
        try:
            children = sorted(target.iterdir(), key=lambda path: path.name)
        except OSError:
            return
        for child in children:
            if depth == 0 and child.name in PERSONAL:
                continue
            visit(f"{rel}/{child.name}", depth + 1)

    for name in MANAGED:
        visit(name, 0)
    return found


def _copy_installed(checkout: Path) -> bool:
    """Whether a copy mode install is in place: a real managed item that an ignore entry of ``init`` names."""
    exclude_files = [checkout / ".gitignore"]
    out = _git(checkout, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude")
    if out is not None:
        exclude_files.append(Path(out))
    entries: set[str] = set()
    for exclude_file in exclude_files:
        try:
            entries.update(line.strip() for line in exclude_file.read_text(encoding="utf-8").splitlines())
        except OSError:
            continue
    return any(
        f"/{name}" in entries and (checkout / name).exists() and not (checkout / name).is_symlink()
        for name in MANAGED
    )


def package_installed(checkout: Path, context: str, mode: str | None) -> bool:
    """Whether ``checkout`` already holds the package ``context``: links into it, or a copy ``init`` made."""
    if package_links(checkout, context):
        return True
    return mode == repos.INSTALL_MODE_COPY and _copy_installed(checkout)


def drop_links(checkout: Path, context: str, *, dry_run: bool, fence: Sequence[Path] = ()) -> list[str]:
    """Remove the links of ``checkout`` that point into ``Contexts/<context>/``; what was (or would be) removed.

    Only those links go: a copy, a file of the checkout, a link of somebody
    else and the ignore entries stay where they are.
    """
    actions: list[str] = []
    for rel in package_links(checkout, context):
        target = checkout / rel
        if fenced(target, fence) is not None:
            continue
        if not dry_run:
            target.unlink()
        actions.append(f"{rel} link {'would be removed' if dry_run else 'removed'}")
    return actions


# ---------------------------------------------------------------------------
# One vault
# ---------------------------------------------------------------------------


def check_vault_gitignore(vault_dir: Path, packages: Sequence[str]) -> tuple[str, list[str]]:
    """Whether the vault's ``.gitignore`` covers what Claude Code writes into ``packages``: ``(state, uncovered)``.

    Only a vault that is itself the root of a git repository is checked: the rules of a repository
    around it say nothing about what the vault publishes, and a plain folder publishes nothing.
    ``git check-ignore --no-index`` reads the rules without touching the index or the tree.
    """
    top = _git(vault_dir, "rev-parse", "--show-toplevel")
    if top is None or _resolved(Path(top)) != _resolved(vault_dir):
        return GITIGNORE_NOT_APPLICABLE, []

    def probe(package: str, rel: str) -> str:
        return f"{repos.CONTEXTS_DIR_NAME}/{package}/.claude/{rel}"

    probes = [probe(package, rel) for package in packages for _label, rel in CLAUDE_WRITES]
    try:
        done = subprocess.run(
            ["git", "-C", str(vault_dir), "check-ignore", "--no-index", "-z", "--stdin"],
            input="\0".join(probes),
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return GITIGNORE_NOT_APPLICABLE, []
    if done.returncode not in (0, 1):
        return GITIGNORE_NOT_APPLICABLE, []
    ignored = set(done.stdout.split("\0"))
    uncovered = [
        label for label, rel in CLAUDE_WRITES if any(probe(package, rel) not in ignored for package in packages)
    ]
    return (GITIGNORE_GAPS, uncovered) if uncovered else (GITIGNORE_COVERED, [])


def declared_roots(vault_dir: Path | None) -> frozenset[str]:
    """Root ids the vault's ``Contexts/config.json`` declares; empty without one or when it does not parse."""
    if vault_dir is None:
        return frozenset()
    declaration = repos.contexts_declaration(vault_dir)
    if not declaration.is_file():
        return frozenset()
    try:
        registry = repos.load_registry(declaration_path=declaration, overlay={}, env={}, vault_dir=vault_dir)
    except repos.RegistryError:
        return frozenset()
    return frozenset(registry.roots)


def declared_ids(vault_dir: Path | None) -> frozenset[str]:
    """Checkout ids the vault's ``Contexts/config.json`` declares; empty without one or when it does not parse."""
    if vault_dir is None:
        return frozenset()
    declaration = repos.contexts_declaration(vault_dir)
    if not declaration.is_file():
        return frozenset()
    try:
        registry = repos.load_registry(declaration_path=declaration, overlay={}, env={}, vault_dir=vault_dir)
    except repos.RegistryError:
        return frozenset()
    return frozenset(registry.entries)


def declared_apps(vault_dir: Path | None) -> int | None:
    """How many apps the vault's ``Contexts/config.json`` declares; ``None`` without one or when it does not parse."""
    if vault_dir is None:
        return None
    declaration = repos.contexts_declaration(vault_dir)
    if not declaration.is_file():
        return None
    try:
        registry = repos.load_registry(declaration_path=declaration, overlay={}, env={}, vault_dir=vault_dir)
    except repos.RegistryError:
        return None
    return len(registry.entries)


def _kind_of(root: repos.Root, source: str) -> str:
    if source == root.override or source in root.legacy_overrides:
        return "environment"
    if source.endswith(".default"):
        return "default"
    return "overlay"


def _ask_root(prompt: Prompt, slug: str, root_id: str, members: Sequence[repos.RepoEntry]) -> str | None:
    holds = ", ".join(entry.path for entry in members[:4]) + (", ..." if len(members) > 4 else "")
    question = f"Directory of root {root_id!r} of vault {slug!r} (it holds {holds}); empty to skip: "
    for _attempt in range(3):
        answer = (prompt(question) or "").strip()
        if not answer:
            return None
        location = os.path.abspath(os.path.expanduser(answer))
        if Path(location).is_dir():
            return location
        question = f"{location} is not a directory. {question}"
    return None


def _sanitized(previous: Mapping[str, Any], registry: repos.Registry) -> tuple[dict[str, Any], list[str]]:
    """``previous`` without the roots, pins and selected ids its declaration no longer knows, and what was dropped.

    An ``apply`` that is not a list of ids is a malformed file; a missing one
    stays missing, which is what tells a run to adopt what is installed.
    """
    out = dict(previous)
    dropped: list[str] = []
    for key, known in (("roots", registry.roots), ("repos", registry.entries)):
        block = previous.get(key, {})
        if block in (None, ""):
            block = {}
        if not isinstance(block, dict):
            raise repos.RegistryError(f"'{key}' must be a JSON object")
        kept = {name: value for name, value in block.items() if name in known}
        dropped += [f"{key}.{name}" for name in block if name not in known]
        out[key] = kept
    if APPLY_KEY in previous:
        selected = previous[APPLY_KEY]
        if not isinstance(selected, list) or not all(isinstance(item, str) for item in selected):
            raise repos.RegistryError(f"'{APPLY_KEY}' must be a JSON array of checkout ids")
        out[APPLY_KEY] = sorted({item for item in selected if item in registry.entries})
        dropped += [f"{APPLY_KEY}.{item}" for item in dict.fromkeys(selected) if item not in registry.entries]
    return out, dropped


def _ask_apply(prompt: Prompt, slug: str, present: Sequence[str]) -> list[str] | None:
    """The checkouts a person picks at a prompt; ``None`` to decide later."""
    question = (
        f"Vault {slug!r}: checkouts on this machine with a context package: {', '.join(present)}. "
        "Install into which? Ids separated by commas or spaces, 'all', 'none', or empty to decide later: "
    )
    for _attempt in range(3):
        answer = (prompt(question) or "").strip()
        if not answer:
            return None
        if answer.lower() == "all":
            return list(present)
        if answer.lower() == "none":
            return []
        picked = [item for item in answer.replace(",", " ").split() if item]
        unknown = [item for item in picked if item not in present]
        if not unknown:
            return sorted(set(picked))
        question = f"{', '.join(unknown)} is not one of them. {question}"
    return None


def _overlay_text(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def _write_atomically(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    scratch.write_text(text, encoding="utf-8")
    os.replace(scratch, path)


def init_vault(
    target: VaultTarget,
    *,
    env: Mapping[str, str],
    root_flags: Mapping[str, Path] | None = None,
    search_dirs: Sequence[Path] = (),
    max_depth: int = DEFAULT_MAX_DEPTH,
    prompt: Prompt | None = None,
    fence: Sequence[Path] = (),
    dry_run: bool = False,
    force: bool = False,
    refresh: bool = False,
    exclude: str = EXCLUDE_LOCAL,
    scan: Callable[..., list[tuple[Path, str]]] = scan_checkouts,
    selection: Selection | None = None,
) -> VaultOutcome:
    """Locate the roots of one vault, settle its selection, write its ``repos.local.json`` and install its packages.

    ``prompt`` asks for a root nothing locates and, on a machine with no
    selection, nothing to adopt and no flag, for the checkouts to install
    into (never in a dry run). ``selection`` carries ``--repo``, ``--all`` and ``--drop``.
    """
    flags_given = selection if selection is not None else Selection()
    init_command = command_label(env, "init")
    outcome = VaultOutcome(slug=target.slug, path=None if target.path is None else str(target.path))
    if target.path is None or not target.path.is_dir():
        outcome.status = VAULT_SKIPPED
        outcome.detail = "no vault directory on this machine"
        return outcome
    vault_dir = Path(os.path.abspath(target.path))
    declaration = repos.contexts_declaration(vault_dir)
    if not declaration.is_file():
        outcome.status = VAULT_NO_CONTEXTS
        outcome.detail = f"no {repos.CONTEXTS_DIR_NAME}/{repos.DECLARATION_NAME}; nothing to initialize"
        return outcome
    outcome.declaration = str(declaration)
    try:
        parsed = repos.load_registry(declaration_path=declaration, overlay={}, env={}, vault_dir=vault_dir)
    except repos.RegistryError as exc:
        return _failed(outcome, f"{E_CONTEXTS_INVALID}: {exc}")
    if not parsed.entries:
        # Nothing to locate or link, so there is nothing of this machine to record.
        outcome.status = VAULT_NO_APPS
        outcome.detail = f"{repos.CONTEXTS_DIR_NAME}/{repos.DECLARATION_NAME} declares no apps; nothing to link"
        return outcome
    try:
        overlay_path = repos.overlay_path_for(target.slug, env)
    except repos.RegistryError as exc:
        return _failed(outcome, str(exc))
    if overlay_path is None:
        return _failed(outcome, "there is no configuration directory: set HOME or XDG_CONFIG_HOME")
    outcome.overlay = str(overlay_path)
    hit = fenced(overlay_path, fence)
    if hit is not None:
        return _failed(
            outcome,
            f"{E_WRITE_FENCED}: {overlay_path} is inside the served vault at {hit}; "
            "point XDG_CONFIG_HOME outside every vault",
        )

    def load(overlay: Mapping[str, Any]) -> repos.Registry:
        return repos.load_registry(
            declaration_path=declaration,
            overlay_path=overlay_path,
            overlay=overlay,
            env=env,
            vault_dir=vault_dir,
            vault=target.slug,
        )

    try:
        bare = load({})
        previous_text = overlay_path.read_text(encoding="utf-8") if overlay_path.is_file() else None
        previous, dropped = _sanitized(repos.read_overlay(overlay_path), bare)
        current = load(previous)
    except (repos.RegistryError, OSError) as exc:
        return _failed(outcome, f"{E_CONTEXTS_INVALID}: {exc}")

    flags = dict(root_flags or {})
    scanned: list[tuple[Path, str]] | None = None

    def checkouts() -> list[tuple[Path, str]]:
        nonlocal scanned
        if scanned is None:
            scanned = scan(search_dirs, max_depth=max_depth) if search_dirs else []
        return scanned

    choices: dict[str, RootChoice] = {}
    for root_id, root in current.roots.items():
        if root_id in flags:
            location = os.path.abspath(os.path.expanduser(str(flags[root_id])))
            if Path(location).is_dir():
                choices[root_id] = RootChoice(root_id, location, "flag", f"--root {root_id}")
            else:
                choices[root_id] = RootChoice(
                    root_id, None, "flag", f"--root {root_id}", f"{location} is not a directory", failed=True
                )
            continue
        declared = current.root_paths.get(root_id)
        if declared is not None:
            location, source = declared
            if Path(location).is_dir():
                choices[root_id] = RootChoice(root_id, location, _kind_of(root, source), source)
                continue
        members = [entry for entry in current.entries.values() if entry.root == root_id]
        votes: Counter[str] = Counter()
        for path, url in checkouts():
            for entry in members:
                if repos.remote_matches(url, entry.remote):
                    base = _strip_suffix(path, entry.path)
                    if base is not None:
                        votes[str(base)] += 1
        ranked = votes.most_common()
        if ranked and (len(ranked) == 1 or ranked[0][1] > ranked[1][1]):
            choices[root_id] = RootChoice(
                root_id,
                ranked[0][0],
                "detected",
                "scan",
                f"{ranked[0][1]} checkout(s) whose origin matches a declared remote",
            )
            continue
        detail = (
            "several directories hold its checkouts: " + ", ".join(path for path, _ in ranked)
            if ranked
            else "no checkout under the search directories has a matching origin"
        )
        if declared is not None:
            detail = f"{declared[0]} ({declared[1]}) is not a directory; {detail}"
        answer = _ask_root(prompt, target.slug, root_id, members) if prompt is not None else None
        if answer is not None:
            choices[root_id] = RootChoice(root_id, answer, "prompt", "prompt")
            continue
        choices[root_id] = RootChoice(root_id, None, "", "", f"{detail}; pass --root {root_id}=DIR")
    outcome.roots = list(choices.values())

    # A checkout away from its declared path is pinned, but only when exactly
    # one unclaimed checkout carries its remote: two would be a guess.
    def expected(entry: repos.RepoEntry) -> Path | None:
        pinned = current.repo_paths.get(entry.id)
        return Path(pinned[0]) if pinned is not None else _declared_location(choices.get(entry.root), entry)

    claimed: set[Path] = set()
    for entry in current.entries.values():
        place = expected(entry)
        if place is not None and place.is_dir():
            claimed.add(_resolved(place))
    pins: dict[str, str] = {}
    pin_notes: dict[str, str] = {}
    for entry in current.entries.values():
        place = expected(entry)
        if place is not None and place.is_dir():
            continue
        candidates = [
            path for path, url in checkouts() if repos.remote_matches(url, entry.remote) and path not in claimed
        ]
        if len(candidates) == 1:
            pins[entry.id] = str(candidates[0])
            claimed.add(candidates[0])
        elif candidates:
            pin_notes[entry.id] = "several checkouts carry its remote: " + ", ".join(map(str, candidates))

    roots_out = dict(previous.get("roots", {}))
    for choice in choices.values():
        if choice.path and choice.kind in ("flag", "detected", "prompt", "default", "overlay"):
            roots_out[choice.id] = choice.path
    repos_out = dict(previous.get("repos", {}))
    repos_out.update(pins)
    payload: dict[str, Any] = {
        "_README": overlay_readme(init_command),
        "slug": target.slug,
        "vault": str(vault_dir),
        "roots": dict(sorted(roots_out.items())),
        "repos": dict(sorted(repos_out.items())),
    }
    final = load(payload)
    resolutions = final.resolve_all()
    present = [
        resolution.entry.id
        for resolution in resolutions
        if resolution.entry.context is not None
        and resolution.status == repos.STATUS_RESOLVED
        and resolution.path is not None
    ]
    chosen, outcome.selection = _settle_selection(
        resolutions,
        present,
        previous.get(APPLY_KEY),
        flags_given,
        prompt=None if dry_run else prompt,
        slug=target.slug,
    )
    if outcome.selection.apply is not None:
        payload[APPLY_KEY] = outcome.selection.apply
    for key, value in previous.items():
        payload.setdefault(key, value)
    text = _overlay_text(payload)
    if previous_text == text:
        outcome.overlay_action = "unchanged"
    else:
        verb = "update" if previous_text is not None else "create"
        outcome.overlay_action = f"would-{verb}" if dry_run else f"{verb}d"
        if not dry_run:
            try:
                _write_atomically(overlay_path, text)
            except OSError as exc:
                return _failed(outcome, f"{overlay_path} could not be written: {exc}")
    if dropped:
        outcome.detail = f"dropped from {overlay_path.name}, no longer declared: {', '.join(dropped)}"

    for resolution in resolutions:
        entry = resolution.entry
        app = AppOutcome(entry.id, entry.context, entry.install_mode, SKIPPED, resolution.path)
        outcome.apps.append(app)
        if entry.context is None:
            app.detail = "declares no context package"
            continue
        if resolution.status == repos.STATUS_UNCONFIGURED:
            app.detail = f"root {entry.root!r} is not located on this machine"
            continue
        if resolution.status == repos.STATUS_MISSING:
            app.detail = f"not on this machine (no checkout at {resolution.path})"
            if entry.id in pin_notes:
                app.detail += f"; {pin_notes[entry.id]}"
            continue
        if entry.id not in chosen:
            _not_selected(outcome, app, entry, flags_given, init_command, dry_run=dry_run, fence=fence)
            continue
        matches, remote_detail = final.verify_remote(resolution)
        if matches is False:
            app.status = ERROR
            app.detail = (
                f"its origin {remote_detail} is not the declared remote {entry.remote!r}; "
                f"correct the root or pin repos.{entry.id} in {overlay_path}"
            )
            continue
        if matches is None and not force:
            # A package ships a .mcp.json, which declares commands to run: it goes only
            # into a checkout that is provably the declared repository, or one the person vouches for.
            app.status = UNVERIFIED
            app.origin_missing = resolution.path is not None and repos.lacks_origin(resolution.path)
            app.detail = (
                f"its origin could not be verified ({remote_detail}); install anyway with {init_command} --force "
                "once you know the checkout is the declared repository"
            )
            continue
        package = final.context_dir(entry.id)
        if package is None or resolution.path is None or not package.is_dir():
            app.status = ERROR
            app.detail = f"the vault declares context {entry.context!r} and {package} is not a directory"
            continue
        app.status, app.actions, app.detail = install_package(
            package,
            Path(resolution.path),
            entry.install_mode or repos.INSTALL_MODE_LINK,
            force=force,
            refresh=refresh,
            exclude=exclude,
            dry_run=dry_run,
            fence=fence,
            init_command=init_command,
        )
        if matches is None:
            note = f"origin not verified: {remote_detail} (installed because of --force)"
            app.detail = f"{app.detail}; {note}" if app.detail else note
    linked = sorted(
        {
            app.context
            for app in outcome.apps
            if app.context
            and app.mode == repos.INSTALL_MODE_LINK
            and app.status in (INSTALLED, UNCHANGED, WOULD_INSTALL)
        }
    )
    outcome.gitignore = GITIGNORE_NOT_APPLICABLE
    if linked:
        outcome.gitignore, uncovered = check_vault_gitignore(vault_dir, linked)
        if uncovered:
            outcome.warnings.append(
                f"{W_CONTEXTS_GITIGNORE}: the .gitignore of vault {target.slug!r} does not cover "
                f"{', '.join(uncovered)} under {repos.CONTEXTS_DIR_NAME}/*/.claude/; Claude Code writes them "
                "through a linked package, into the vault. Add "
                + ", ".join(f"{repos.CONTEXTS_DIR_NAME}/*/.claude/{label}" for label, _rel in CLAUDE_WRITES)
                + " to it, by pull request of the vault"
            )
    return outcome


def _settle_selection(
    resolutions: Sequence[repos.Resolution],
    present: Sequence[str],
    previous: list[str] | None,
    flags: Selection,
    *,
    prompt: Prompt | None,
    slug: str,
) -> tuple[set[str], SelectionOutcome]:
    """The checkouts that receive a package in this run, and the ``apply`` list to record.

    A recorded list is the base. Without one, the checkouts whose package is
    already installed are adopted; with nothing to adopt and no flag, a
    prompt (when there is one) picks, and otherwise the list stays unset and
    nothing is installed. ``--repo`` and ``--all`` add, ``--drop`` removes.
    """
    declared = {resolution.entry.id for resolution in resolutions}
    adopted: list[str] = []
    if previous is None:
        by_id = {resolution.entry.id: resolution for resolution in resolutions}
        for repo_id in present:
            resolution = by_id[repo_id]
            entry = resolution.entry
            if entry.context is not None and resolution.path is not None:
                if package_installed(Path(resolution.path), entry.context, entry.install_mode):
                    adopted.append(repo_id)
    chosen = set(previous if previous is not None else adopted)
    chosen |= {repo_id for repo_id in flags.add if repo_id in declared}
    if flags.everything:
        chosen |= set(present)
    chosen -= flags.drop
    if previous is not None:
        source = SELECTION_OVERLAY
    elif adopted:
        source = SELECTION_ADOPTED
    elif flags.given:
        source = SELECTION_FLAGS
    else:
        source = SELECTION_UNSET
        answer = _ask_apply(prompt, slug, present) if prompt is not None and present else None
        if answer is not None:
            chosen, source = set(answer), SELECTION_PROMPT
    apply = None if source == SELECTION_UNSET else sorted(chosen)
    reference = set(previous or [])
    return chosen, SelectionOutcome(
        apply=apply,
        previous=None if previous is None else sorted(previous),
        source=source,
        added=sorted(set(apply or []) - reference),
        removed=sorted(reference - set(apply or [])),
        unselected=[repo_id for repo_id in present if repo_id not in chosen],
    )


def _not_selected(
    outcome: VaultOutcome,
    app: AppOutcome,
    entry: repos.RepoEntry,
    flags: Selection,
    init_command: str,
    *,
    dry_run: bool,
    fence: Sequence[Path],
) -> None:
    """Report a checkout on this machine that is not selected; with ``--drop``, remove its links."""
    app.status = NOT_SELECTED
    checkout = Path(app.path or "")
    context = entry.context or ""
    if entry.id in flags.drop:
        app.actions = drop_links(checkout, context, dry_run=dry_run, fence=fence)
        app.detail = f"dropped from {APPLY_KEY}; " + (
            "its links into the package go" if app.actions else "it holds no link into the package"
        )
        if entry.install_mode == repos.INSTALL_MODE_COPY and _copy_installed(checkout):
            app.detail += "; the copied files stay"
        return
    app.detail = f"not selected on this machine; install it with {init_command} --repo {entry.id}"
    if package_links(checkout, context):
        outcome.warnings.append(
            f"{W_CONTEXTS_LINKED_NOT_SELECTED}: {outcome.slug}: {entry.id} ({app.path}) holds links into "
            f"{repos.CONTEXTS_DIR_NAME}/{context}/ and is not in {APPLY_KEY}; keep it with "
            f"{init_command} --repo {entry.id} or remove the links with {init_command} --drop {entry.id}"
        )


def _failed(outcome: VaultOutcome, detail: str) -> VaultOutcome:
    outcome.status = VAULT_ERROR
    outcome.detail = detail
    return outcome
