"""Registry of the repository checkouts a vault declares.

Two facts about a repository live in two different places on purpose:

* **what it is** — its identity, the group it belongs to, the git remote that
  proves it, the relative path it takes under a root and the context package
  that serves it. That is the same on every machine, so the vault declares it
  in ``Contexts/config.json`` under ``repos`` (the packages live next to it,
  under ``Contexts/<context>/``) and it is versioned with the vault.
* **where it is** — the absolute location of the roots on this disk. That is
  true for one person and false for everybody else, so it never enters a
  vault: it comes from ``<config home>/knowledge/<slug>/repos.local.json``,
  which ``cloudfrog-knowledge init`` writes, or from environment variables.
  ``<config home>`` is ``$XDG_CONFIG_HOME`` or ``$HOME/.config``, the
  directory that also holds the user's ``vaults.yaml``.

Where a root is, highest first:

1. ``KNOWLEDGE_<ROOT>_ROOT``;
2. the root's declared ``env`` and ``CD_<ROOT>_ROOT``, deprecated aliases
   that keep working and are reported as such;
3. ``roots.<id>`` of the machine's ``repos.local.json``;
4. the root's ``default`` in the declaration, only when that directory exists
   on this machine: the vault suggests a layout, and a suggestion nobody
   followed is not a location this machine declared.

One checkout can be pinned past its root, again highest first:
``KNOWLEDGE_REPO_<ID>``, then ``CD_REPO_<ID>`` (deprecated), then
``repos.<id>`` of ``repos.local.json``. A pin beats every root.

Which declaration is read: the one a caller names (a vault slug, a vault
directory or a declaration file); otherwise the only vault this machine
initialized. This module never reads
``vaults.yaml``: ``init`` records the vault directory and its slug inside each
``repos.local.json``, so the overlays alone say which vaults this machine
initialized and where their declarations are. That keeps the module free of
any YAML parser.

Nothing here derives a location from a directory name baked into the source.
A checkout whose root nobody declared is reported as *unconfigured*, which is
a different answer from *missing* and neither of them is a pass:

``unconfigured``
    No root and no override was supplied, so this module never learned where to
    look. Nothing on disk was inspected and nothing may be concluded from that.

``missing``
    A location was resolved and it is not there. The machine declared the root,
    so an absent checkout is a real gap the caller can act on.

``resolved``
    A location was resolved and the directory exists.

Collapsing the first two into a single falsy value is the failure this module
exists to prevent: it turns "I could not look" into "I looked and found
nothing", and a gate built on top of that reports green for a tree it never
opened.

The module imports nothing outside the standard library, so shell tooling can
run it as ``python3 -m knowledge.repos`` with ``PYTHONPATH=<repo>/src`` without
the project virtualenv. ``scripts/lib/repo-registry.sh`` is the only shell
reader and it does exactly that, which keeps one implementation of the
resolution rules rather than one per language.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Directory of a vault that holds its registry and its context packages.
CONTEXTS_DIR_NAME = "Contexts"
#: The registry inside :data:`CONTEXTS_DIR_NAME`.
DECLARATION_NAME = "config.json"
#: This machine's locations for one vault, under ``<config home>/knowledge/<slug>/``.
OVERLAY_NAME = "repos.local.json"

#: Environment variable prefix for a single checkout, e.g. ``KNOWLEDGE_REPO_MIDDLEWARE``.
REPO_ENV_PREFIX = "KNOWLEDGE_REPO_"
#: Deprecated alias of :data:`REPO_ENV_PREFIX`, still honoured.
LEGACY_REPO_ENV_PREFIX = "CD_REPO_"

#: The reason :func:`read_origin` gives for a checkout whose config names no origin remote.
NO_ORIGIN = "declares no origin remote"

STATUS_RESOLVED = "resolved"
STATUS_MISSING = "missing"
STATUS_UNCONFIGURED = "unconfigured"

#: How a checkout's context package reaches its working tree. "link" is the
#: usual mode; "copy" is reserved for a checkout that holds git worktrees
#: inside its own working tree, where a symlinked .claude would cancel Claude
#: Code's worktrees.
INSTALL_MODE_LINK = "link"
INSTALL_MODE_COPY = "copy"
INSTALL_MODES = (INSTALL_MODE_LINK, INSTALL_MODE_COPY)

#: Same rule as ``settings.SLUG_PATTERN``, restated so this module imports nothing internal.
SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}[a-z0-9]$")

#: A context package name: one directory under ``Contexts/``, never a path.
CONTEXT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class RegistryError(RuntimeError):
    """The registry could not be read — never degraded into an empty registry.

    An unreadable or malformed declaration is "I could not look". Returning an
    empty registry instead would let every consumer report that no repository
    is configured, which is the same answer a correctly empty registry gives
    and hides a broken file behind a plausible one.
    """


# ---------------------------------------------------------------------------
# Names and places
# ---------------------------------------------------------------------------


def _env_token(identifier: str) -> str:
    return identifier.replace("-", "_").replace(".", "_").upper()


def repo_env_var(repo_id: str) -> str:
    """Environment variable that pins one checkout, e.g. ``KNOWLEDGE_REPO_EXAMPLE_SERVICE``."""
    return f"{REPO_ENV_PREFIX}{_env_token(repo_id)}"


def legacy_repo_env_var(repo_id: str) -> str:
    """Deprecated alias of :func:`repo_env_var`, e.g. ``CD_REPO_EXAMPLE_SERVICE``."""
    return f"{LEGACY_REPO_ENV_PREFIX}{_env_token(repo_id)}"


def root_env_var(root_id: str) -> str:
    """Environment variable that pins a whole root, e.g. ``KNOWLEDGE_WORK_ROOT``."""
    return f"KNOWLEDGE_{_env_token(root_id)}_ROOT"


def config_home(env: Mapping[str, str] | None = None) -> Path | None:
    """``$XDG_CONFIG_HOME/knowledge`` or ``$HOME/.config/knowledge``; ``None`` without either.

    Same rule the settings apply to the user's ``vaults.yaml``, and only the
    given environment is read, so a hermetic ``env`` never reaches the
    developer's own configuration.
    """
    env = os.environ if env is None else env
    xdg = (env.get("XDG_CONFIG_HOME") or "").strip()
    if xdg:
        return Path(xdg).expanduser() / "knowledge"
    home = (env.get("HOME") or "").strip()
    if not home:
        return None
    return Path(home) / ".config" / "knowledge"


def overlay_path_for(slug: str, env: Mapping[str, str] | None = None) -> Path | None:
    """``<config home>/knowledge/<slug>/repos.local.json``; ``None`` without a config home."""
    if not SLUG_PATTERN.fullmatch(slug):
        raise RegistryError(f"{slug!r} is not a vault slug (lowercase letters, digits and inner hyphens)")
    base = config_home(env)
    return None if base is None else base / slug / OVERLAY_NAME


def contexts_dir(vault_dir: Path) -> Path:
    """``<vault>/Contexts``: the vault's registry and its context packages."""
    return vault_dir / CONTEXTS_DIR_NAME


def contexts_declaration(vault_dir: Path) -> Path:
    """``<vault>/Contexts/config.json``: what the vault's checkouts are."""
    return contexts_dir(vault_dir) / DECLARATION_NAME


def remote_matches(url: str, expected: str) -> bool:
    """Whether an origin URL names the bare repository ``expected`` (``…/<x>.git``, ``…:<x>``)."""
    url = url.strip().rstrip("/")
    return any(url.endswith(f"{sep}{expected}{tail}") for sep in ("/", ":") for tail in (".git", ""))


#: Answer of :func:`_origin_from_config` when only git itself can be trusted with the repository.
_ASK_GIT = object()
#: A config file larger than this is not parsed here.
_CONFIG_MAX_BYTES = 1 << 20
_SECTION = re.compile(r'\[\s*([A-Za-z0-9.-]+)(?:\s+"([^"\\]*)")?\s*\](.*)$')


def _git_dir(path: Path) -> Path | None:
    """The git directory of the working tree at ``path``: ``.git`` itself or the target of its ``gitdir:`` file."""
    dot_git = path / ".git"
    try:
        if dot_git.is_dir():
            return dot_git
        if dot_git.is_file():
            lines = dot_git.read_text(encoding="utf-8", errors="replace").splitlines()
            if lines and lines[0].startswith("gitdir:"):
                target = Path(lines[0][len("gitdir:") :].strip())
                target = target if target.is_absolute() else dot_git.parent / target
                return target if target.is_dir() else None
    except OSError:
        return None
    return None


def _origin_in_config(text: str) -> str | None | object:
    """The last ``remote.origin.url`` of a git config file, ``None`` when it has none, or :data:`_ASK_GIT`.

    Only the plain shape is vouched for: section headers alone on their line,
    unquoted values without comments or continuation lines, no ``include`` and
    no ``includeIf``. Anything else is :data:`_ASK_GIT`, so a repository whose
    config uses a feature this reader does not model is answered by git.
    """
    in_origin = False
    url: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if raw.rstrip().endswith("\\"):
            return _ASK_GIT
        if not line or line[0] in "#;":
            continue
        if line[0] == "[":
            match = _SECTION.fullmatch(line)
            if match is None:
                return _ASK_GIT
            name, subsection, rest = match.groups()
            name = name.lower()
            if rest.strip() or name in ("include", "includeif") or name.startswith("remote."):
                return _ASK_GIT
            in_origin = name == "remote" and subsection == "origin"
            continue
        key, equals, value = line.partition("=")
        key, value = key.strip().lower(), value.strip()
        if key == "worktreeconfig":
            return _ASK_GIT
        if in_origin and key == "url":
            if not equals or any(char in value for char in "\"\\#;"):
                return _ASK_GIT
            url = value
    return url


def _origin_from_config(path: Path) -> str | None | object:
    """The origin of the working tree at ``path`` read from git's config file, or :data:`_ASK_GIT`.

    Follows a ``.git`` file (a submodule, or a ``--separate-git-dir``) to its
    git directory and a linked worktree to the common directory that holds the
    config. Reading the file costs microseconds; starting ``git config`` costs
    about 13 ms, which dominates a scan of many repositories.
    """
    git_dir = _git_dir(path)
    if git_dir is None:
        return _ASK_GIT
    try:
        common = git_dir
        if (git_dir / "commondir").is_file():
            common = git_dir / (git_dir / "commondir").read_text(encoding="utf-8").strip()
        config = common / "config"
        if (git_dir / "config.worktree").exists() or config.stat().st_size > _CONFIG_MAX_BYTES:
            return _ASK_GIT
        return _origin_in_config(config.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return _ASK_GIT


def read_origin(path: Path | str) -> tuple[str | None, str]:
    """``(url, "")`` for the origin remote of the checkout at ``path``, or ``(None, reason)``.

    The answer is the one of ``git config --get remote.origin.url``. A plain
    config file is read directly; ``include``/``includeIf``, quoting,
    comments after a value, worktree-specific config and any directory whose
    git directory cannot be found go to git, for that one repository.
    """
    local = _origin_from_config(Path(path))
    if local is not _ASK_GIT:
        if isinstance(local, str) and local:
            return local, ""
        return None, f"{path} {NO_ORIGIN}"
    try:
        completed = subprocess.run(
            ["git", "-C", str(path), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"git could not be run against {path}: {exc}"
    url = completed.stdout.strip()
    if completed.returncode != 0 or not url:
        return None, f"{path} {NO_ORIGIN}"
    return url, ""


def lacks_origin(path: Path | str) -> bool:
    """Is ``path`` a git working tree whose config names no origin remote?

    Such a checkout cannot be proved to be the declared repository, and it cannot
    be proved to be another one either. A plain directory, or one whose git
    directory cannot be found, is not a checkout and answers ``False``.
    """
    if _git_dir(Path(path)) is None:
        return False
    url, reason = read_origin(path)
    return url is None and reason.endswith(NO_ORIGIN)


def _expand(raw: str, env: Mapping[str, str]) -> str | None:
    """An absolute, normalised path from ``raw``; ``~`` expands from ``env``'s ``HOME``.

    ``None`` when ``raw`` starts with ``~`` and ``env`` has no ``HOME``: there
    is nothing to expand it against, and the process's own home is not part
    of the environment the caller handed in.
    """
    if raw == "~" or raw.startswith("~/"):
        home = (env.get("HOME") or "").strip()
        if not home:
            return None
        raw = home + raw[1:]
    return os.path.normpath(os.path.abspath(raw))


# ---------------------------------------------------------------------------
# The declaration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Root:
    """A directory the registry hangs relative paths off.

    A declaration names one root per directory its checkouts hang off, and
    the split is not cosmetic: checkouts that come down together as submodules
    of one parent and checkouts each cloned on its own behave differently.
    Cloning the parent brings the first set and none of the second, so a caller
    that treats them as one will report a complete platform while half of it
    was never on the disk.

    ``env`` is the variable an older declaration named for the root; it stays
    honoured as a deprecated alias of :attr:`override`. ``default`` is where
    the vault suggests the root lives (``~/…`` or absolute), used only when
    that directory exists on this machine.
    """

    id: str
    env: str | None = None
    description: str = ""
    default: str | None = None

    @property
    def override(self) -> str:
        return root_env_var(self.id)

    @property
    def legacy_overrides(self) -> tuple[str, ...]:
        names: list[str] = []
        for name in (self.env, f"CD_{_env_token(self.id)}_ROOT"):
            if name and name != self.override and name not in names:
                names.append(name)
        return tuple(names)


@dataclass(frozen=True)
class RepoEntry:
    """One repository checkout, declared portably."""

    id: str
    remote: str
    group: str
    root: str
    path: str
    context: str | None = None
    install_mode: str | None = None
    description: str = ""


@dataclass(frozen=True)
class Resolution:
    """What the registry could say about one checkout, and how it knows."""

    entry: RepoEntry
    status: str
    path: str | None = None
    source: str = ""
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.status == STATUS_RESOLVED

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.entry.id,
            "context": self.entry.context,
            "install_mode": self.entry.install_mode,
            "remote": self.entry.remote,
            "group": self.entry.group,
            "root": self.entry.root,
            "declared_path": self.entry.path,
            "status": self.status,
            "path": self.path,
            "source": self.source,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Binding:
    """A vault this machine initialized: its slug, its directory and its overlay."""

    slug: str
    vault_dir: Path
    overlay_path: Path


def _read_json_object(path: Path, *, what: str) -> tuple[dict, bool]:
    """Read a JSON object, keeping "absent" and "unreadable" apart.

    Same contract as ``config._load_config_json``: a file that is not there is
    a legitimate state the caller handles; a file that is there and cannot be
    parsed raises, because falling back to a default while the real settings
    sit one unreadable byte away is how a gate passes for the wrong reason.
    """
    if not path.exists():
        return {}, False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RegistryError(f"{what} {path} exists but could not be read: {exc}") from exc
    except ValueError as exc:
        raise RegistryError(f"{what} {path} exists but is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RegistryError(
            f"{what} {path} must hold a JSON object, got {type(payload).__name__}"
        )
    return payload, True


def read_overlay(path: Path) -> dict[str, Any]:
    """The JSON object of a ``repos.local.json``: empty when the file is absent, raising when unreadable."""
    payload, _ = _read_json_object(path, what="the local overlay")
    return payload


@dataclass
class Registry:
    """The declaration plus whatever this machine knows about where things are."""

    roots: dict[str, Root] = field(default_factory=dict)
    entries: dict[str, RepoEntry] = field(default_factory=dict)
    root_paths: dict[str, tuple[str, str]] = field(default_factory=dict)
    repo_paths: dict[str, tuple[str, str]] = field(default_factory=dict)
    declaration_found: bool = False
    overlay_found: bool = False
    #: The file the declaration was read from.
    declaration_path: Path = Path(DECLARATION_NAME)
    #: The file this machine's locations were read from (or would be).
    overlay_path: Path | None = None
    #: The vault the declaration belongs to; ``None`` for a declaration file named outright.
    vault_dir: Path | None = None
    slug: str | None = None
    #: Directory holding the context packages: ``<vault>/Contexts``, or the
    #: directory of a declaration file named outright.
    contexts_root: Path = Path(CONTEXTS_DIR_NAME)
    #: ``graph.targets`` of the declaration, as declared.
    graph_targets: tuple[dict[str, Any], ...] = ()
    #: Deprecated variables this resolution used, one sentence each.
    warnings: list[str] = field(default_factory=list)

    @property
    def source(self) -> str:
        """``vault`` when the declaration is a vault's ``Contexts/config.json``, else ``file``."""
        return "vault" if self.vault_dir is not None else "file"

    # -- resolution ---------------------------------------------------------

    def resolve(self, repo_id: str) -> Resolution:
        entry = self.entries.get(repo_id)
        if entry is None:
            raise KeyError(
                f"{repo_id!r} is not declared in {self.declaration_path} under repos.entries; "
                f"known ids: {', '.join(sorted(self.entries))}"
            )

        pinned = self.repo_paths.get(repo_id)
        if pinned is not None:
            return self._verify(entry, pinned[0], pinned[1])

        root_location = self.root_paths.get(entry.root)
        if root_location is None:
            root = self.roots.get(entry.root)
            override = root.override if root else root_env_var(entry.root)
            where = str(self.overlay_path) if self.overlay_path is not None else OVERLAY_NAME
            init = " (cloudfrog-knowledge init writes it)" if self.vault_dir is not None else ""
            return Resolution(
                entry=entry,
                status=STATUS_UNCONFIGURED,
                reason=(
                    f"root {entry.root!r} is not declared on this machine; "
                    f"export {override}, or set roots.{entry.root} in {where}{init}, "
                    f"or pin this checkout with {repo_env_var(repo_id)}"
                ),
            )

        base, source = root_location
        joined = os.path.normpath(base if entry.path in ("", ".") else os.path.join(base, entry.path))
        if os.path.commonpath([os.path.normpath(base), joined]) != os.path.normpath(base):
            return Resolution(
                entry=entry,
                status=STATUS_MISSING,
                path=joined,
                source=source,
                reason=f"{joined} is outside its root {base}",
            )
        return self._verify(entry, joined, f"{source} + declared path")

    def _verify(self, entry: RepoEntry, path: str, source: str) -> Resolution:
        if Path(path).is_dir():
            return Resolution(entry=entry, status=STATUS_RESOLVED, path=path, source=source)
        return Resolution(
            entry=entry,
            status=STATUS_MISSING,
            path=path,
            source=source,
            reason=f"{path} was resolved but is not a directory on this machine",
        )

    def resolve_all(self) -> list[Resolution]:
        return [self.resolve(repo_id) for repo_id in self.entries]

    def context_dir(self, repo_id: str) -> Path | None:
        """The package directory of one checkout's context, or ``None`` when it declares none."""
        entry = self.entries.get(repo_id)
        if entry is None:
            raise KeyError(f"{repo_id!r} is not declared in {self.declaration_path} under repos.entries")
        return None if entry.context is None else self.contexts_root / entry.context

    # -- remote verification -------------------------------------------------

    def verify_remote(self, resolution: Resolution) -> tuple[bool | None, str]:
        """Does the checkout at this location carry the declared git remote?

        Returns ``(None, reason)`` when the question could not be answered —
        the path never resolved, or git refused to read the checkout. That is
        deliberately not ``False``: "the remote is wrong" and "I could not read
        the remote" are different findings and only the first one names a
        defect in the tree.
        """
        if not resolution.ok or resolution.path is None:
            return None, f"not inspected: {resolution.status}"
        url, reason = read_origin(resolution.path)
        if url is None:
            return None, reason
        return remote_matches(url, resolution.entry.remote), url


def _text(item: Mapping[str, Any], key: str) -> str | None:
    value = item.get(key)
    return None if value in (None, "") else str(value)


def _parse_roots(raw: Any, *, allow_empty: bool = False) -> dict[str, Root]:
    if not isinstance(raw, list):
        raise RegistryError("repos.roots must be a JSON array of root objects")
    roots: dict[str, Root] = {}
    for item in raw:
        if not isinstance(item, dict) or not item.get("id"):
            raise RegistryError(f"repos.roots entry needs an 'id': {item!r}")
        default = _text(item, "default")
        if default is not None and not (default == "~" or default.startswith("~/") or Path(default).is_absolute()):
            raise RegistryError(
                f"repos.roots {item['id']!r} declares default {default!r}; a default is absolute "
                "or starts with '~/', since a relative one has nothing to be relative to"
            )
        root = Root(
            id=str(item["id"]),
            env=_text(item, "env"),
            description=str(item.get("description", "")),
            default=default,
        )
        if root.id in roots:
            raise RegistryError(f"repos.roots declares {root.id!r} twice")
        roots[root.id] = root
    if not roots and not allow_empty:
        raise RegistryError("repos.roots is empty; the registry would resolve nothing")
    return roots


def _parse_entries(raw: Any, roots: Mapping[str, Root], *, allow_empty: bool = False) -> dict[str, RepoEntry]:
    if not isinstance(raw, list):
        raise RegistryError("repos.entries must be a JSON array of checkout objects")
    entries: dict[str, RepoEntry] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise RegistryError(f"repos.entries holds a non-object: {item!r}")
        for key in ("id", "remote", "group", "root", "path"):
            if key not in item:
                raise RegistryError(f"repos.entries entry is missing {key!r}: {item!r}")
        context = _text(item, "context")
        install_mode = _text(item, "install_mode")
        entry = RepoEntry(
            id=str(item["id"]),
            remote=str(item["remote"]),
            group=str(item["group"]),
            root=str(item["root"]),
            path=str(item["path"]),
            context=context,
            install_mode=install_mode,
            description=str(item.get("description", "")),
        )
        if entry.id in entries:
            raise RegistryError(f"repos.entries declares {entry.id!r} twice")
        # A checkout with no context package has nothing to materialize, so it
        # must declare no install_mode either; a missing slug is the explicit
        # "none" case, never an implicit default. A checkout with a context
        # package must say how it materializes — defaulting silently to "link"
        # is how a checkout that holds git worktrees ends up symlinked by
        # mistake.
        if context is None:
            if install_mode is not None:
                raise RegistryError(
                    f"{entry.id!r} declares context: null but install_mode {install_mode!r}; "
                    "a checkout with no context package has nothing to materialize"
                )
        else:
            if not CONTEXT_PATTERN.fullmatch(context) or context in (".", ".."):
                raise RegistryError(
                    f"{entry.id!r} declares context {context!r}; a context names one package "
                    "directory, never a path"
                )
            if install_mode not in INSTALL_MODES:
                raise RegistryError(
                    f"{entry.id!r} declares context {context!r} but install_mode is "
                    f"{install_mode!r}; it must be one of {INSTALL_MODES}"
                )
        if entry.root not in roots:
            raise RegistryError(
                f"{entry.id!r} hangs off root {entry.root!r}, which repos.roots does not declare"
            )
        if Path(entry.path).is_absolute() or entry.path.startswith("~"):
            raise RegistryError(
                f"{entry.id!r} declares an absolute path {entry.path!r}; declared paths are "
                "relative to a root so the same declaration works on every machine"
            )
        if ".." in entry.path.replace("\\", "/").split("/"):
            raise RegistryError(
                f"{entry.id!r} declares path {entry.path!r}, which leaves its root; "
                "a declared path stays inside the root it hangs off"
            )
        entries[entry.id] = entry
    if not entries and not allow_empty:
        raise RegistryError("repos.entries is empty; the registry would resolve nothing")
    variables: dict[str, str] = {}
    for kind, names in (
        ("root", {rid: root_env_var(rid) for rid in roots}),
        ("checkout", {eid: repo_env_var(eid) for eid in entries}),
    ):
        for ident, var in names.items():
            other = variables.setdefault(var, f"{kind} {ident!r}")
            if other != f"{kind} {ident!r}":
                raise RegistryError(f"{other} and {kind} {ident!r} would both be pinned by {var}; rename one")
    return entries


def _parse_graph_targets(raw: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(raw, Mapping):
        return ()
    targets = raw.get("targets", [])
    return tuple(dict(entry) for entry in targets if isinstance(entry, Mapping)) if isinstance(targets, list) else ()


def _first_env(env: Mapping[str, str], names: Iterable[str]) -> tuple[str, str] | None:
    for name in names:
        value = (env.get(name) or "").strip()
        if value:
            return name, value
    return None


def _locations_from(
    overlay: Mapping[str, Any],
    env: Mapping[str, str],
    roots: Mapping[str, Root],
    entries: Mapping[str, RepoEntry],
    *,
    overlay_label: str = OVERLAY_NAME,
    declaration_label: str = DECLARATION_NAME,
) -> tuple[dict[str, tuple[str, str]], dict[str, tuple[str, str]], list[str]]:
    """Where this machine says things are. Environment wins over the overlay, the overlay over a default."""
    warnings: list[str] = []
    root_paths: dict[str, tuple[str, str]] = {}
    overlay_roots = overlay.get("roots", {})
    if overlay_roots and not isinstance(overlay_roots, dict):
        raise RegistryError(f"{overlay_label}: 'roots' must be a JSON object")
    for root_id, root in roots.items():
        hit = _first_env(env, (root.override, *root.legacy_overrides))
        if hit is not None:
            name, value = hit
            location = _expand(value, env)
            if location is not None:
                root_paths[root_id] = (location, name)
                if name != root.override:
                    warnings.append(f"{name} is deprecated; export {root.override} instead")
                continue
        from_overlay = str(overlay_roots.get(root_id, "")).strip() if isinstance(overlay_roots, dict) else ""
        if from_overlay:
            location = _expand(from_overlay, env)
            if location is not None:
                root_paths[root_id] = (location, f"{overlay_label}:roots.{root_id}")
                continue
        if root.default:
            location = _expand(root.default, env)
            # A default nobody followed is not a location this machine
            # declared: the root stays unconfigured rather than missing.
            if location is not None and Path(location).is_dir():
                root_paths[root_id] = (location, f"{declaration_label}:roots.{root_id}.default")

    repo_paths: dict[str, tuple[str, str]] = {}
    overlay_repos = overlay.get("repos", {})
    if overlay_repos and not isinstance(overlay_repos, dict):
        raise RegistryError(f"{overlay_label}: 'repos' must be a JSON object")
    for repo_id in entries:
        hit = _first_env(env, (repo_env_var(repo_id), legacy_repo_env_var(repo_id)))
        if hit is not None:
            name, value = hit
            location = _expand(value, env)
            if location is not None:
                repo_paths[repo_id] = (location, name)
                if name != repo_env_var(repo_id):
                    warnings.append(f"{name} is deprecated; export {repo_env_var(repo_id)} instead")
                continue
        from_overlay = str(overlay_repos.get(repo_id, "")).strip() if isinstance(overlay_repos, dict) else ""
        if from_overlay:
            location = _expand(from_overlay, env)
            if location is not None:
                repo_paths[repo_id] = (location, f"{overlay_label}:repos.{repo_id}")

    if isinstance(overlay_repos, dict):
        unknown = sorted(set(overlay_repos) - set(entries))
        if unknown:
            raise RegistryError(
                f"{overlay_label} pins checkouts the registry does not declare: {unknown}. "
                "A typo here would silently keep the declared location instead."
            )
    if isinstance(overlay_roots, dict):
        unknown_roots = sorted(set(overlay_roots) - set(roots))
        if unknown_roots:
            raise RegistryError(
                f"{overlay_label} declares roots the registry does not know: {unknown_roots}"
            )

    return root_paths, repo_paths, warnings


# ---------------------------------------------------------------------------
# Which vaults this machine initialized
# ---------------------------------------------------------------------------


def initialized_vaults(env: Mapping[str, str] | None = None) -> list[Binding]:
    """Every ``<config home>/knowledge/<slug>/repos.local.json``, sorted by slug.

    Each overlay ``init`` writes names its vault directory under ``vault``;
    one that does not, or that cannot be parsed, raises: it is a file this
    machine meant as an initialization, and skipping it would answer "not
    initialized" for a vault that is.
    """
    env = os.environ if env is None else env
    base = config_home(env)
    if base is None or not base.is_dir():
        return []
    bindings: list[Binding] = []
    for child in sorted(base.iterdir()):
        overlay = child / OVERLAY_NAME
        if not SLUG_PATTERN.fullmatch(child.name) or not overlay.is_file():
            continue
        payload, _ = _read_json_object(overlay, what="the local overlay")
        vault = payload.get("vault")
        if not isinstance(vault, str) or not vault.strip():
            raise RegistryError(
                f"{overlay} names no 'vault' directory; run cloudfrog-knowledge init to write it again"
            )
        location = _expand(vault.strip(), env)
        if location is None:
            raise RegistryError(f"{overlay}: 'vault' {vault!r} cannot be expanded without HOME")
        bindings.append(Binding(slug=child.name, vault_dir=Path(location), overlay_path=overlay))
    return bindings


def _same_dir(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return os.path.normpath(a) == os.path.normpath(b)


def _select(
    env: Mapping[str, str], *, vault: str | None, vault_dir: Path | None
) -> tuple[Path, Path | None, Path | None, str | None]:
    """``(declaration, overlay, vault_dir, slug)`` for a caller that named no declaration file."""
    if vault is not None:
        overlay = overlay_path_for(vault, env)
        if vault_dir is not None:
            return contexts_declaration(vault_dir), overlay, vault_dir, vault
        for binding in initialized_vaults(env):
            if binding.slug == vault:
                return contexts_declaration(binding.vault_dir), binding.overlay_path, binding.vault_dir, vault
        raise RegistryError(
            f"vault {vault!r} is not initialized on this machine ({overlay or 'no config home'} "
            "is missing); run: cloudfrog-knowledge init"
        )
    bindings = initialized_vaults(env)
    if vault_dir is not None:
        for binding in bindings:
            if _same_dir(binding.vault_dir, vault_dir):
                return contexts_declaration(vault_dir), binding.overlay_path, vault_dir, binding.slug
        return contexts_declaration(vault_dir), None, vault_dir, None
    if len(bindings) == 1:
        only = bindings[0]
        return contexts_declaration(only.vault_dir), only.overlay_path, only.vault_dir, only.slug
    if len(bindings) > 1:
        raise RegistryError(
            "several vaults are initialized on this machine "
            f"({', '.join(b.slug for b in bindings)}); name one with --vault SLUG"
        )
    raise RegistryError("no vault is initialized on this machine; run: cloudfrog-knowledge init")


def load_registry(
    *,
    declaration_path: Path | None = None,
    overlay_path: Path | None = None,
    env: Mapping[str, str] | None = None,
    vault_dir: Path | None = None,
    vault: str | None = None,
    overlay: Mapping[str, Any] | None = None,
) -> Registry:
    """Build the registry from a declaration and this machine's overlay.

    ``declaration_path`` names the declaration file outright (the environment
    alone says where things are unless ``overlay_path`` names a file too);
    ``vault_dir`` names a vault, whose ``Contexts/config.json`` is read with
    the overlay ``init`` wrote for it; ``vault`` names an initialized vault by
    slug. With none of them, the only initialized vault is read.
    ``overlay`` replaces the overlay file's content (a dry run plans with it).
    """
    env = os.environ if env is None else env
    slug: str | None = vault
    selected_overlay: Path | None
    if declaration_path is not None:
        selected_overlay = overlay_path
        if vault_dir is None and declaration_path.parent.name == CONTEXTS_DIR_NAME:
            vault_dir = declaration_path.parent.parent
    else:
        declaration_path, found_overlay, vault_dir, slug = _select(env, vault=vault, vault_dir=vault_dir)
        selected_overlay = overlay_path if overlay_path is not None else found_overlay

    settings, declaration_found = _read_json_object(declaration_path, what="the declaration")
    if not declaration_found:
        raise RegistryError(
            f"{declaration_path} is missing; the repository registry is declared there and "
            "nothing can be resolved without it"
        )
    raw = settings.get("repos")
    if not isinstance(raw, dict):
        raise RegistryError(f"{declaration_path} declares no 'repos' object")

    # A vault may declare that it has no apps (``roots: []``, ``entries: []``);
    # a declaration file named outright never means that, so an empty one is a broken file.
    allow_empty = vault_dir is not None
    roots = _parse_roots(raw.get("roots"), allow_empty=allow_empty)
    entries = _parse_entries(raw.get("entries"), roots, allow_empty=allow_empty)

    if overlay is not None:
        overlay_payload, overlay_found = dict(overlay), True
    elif selected_overlay is not None:
        overlay_payload, overlay_found = _read_json_object(selected_overlay, what="the local overlay")
    else:
        overlay_payload, overlay_found = {}, False
    root_paths, repo_paths, warnings = _locations_from(
        overlay_payload,
        env,
        roots,
        entries,
        overlay_label=str(selected_overlay) if selected_overlay is not None else OVERLAY_NAME,
        declaration_label=str(declaration_path),
    )

    return Registry(
        roots=roots,
        entries=entries,
        root_paths=root_paths,
        repo_paths=repo_paths,
        declaration_found=declaration_found,
        overlay_found=overlay_found,
        declaration_path=declaration_path,
        overlay_path=selected_overlay,
        vault_dir=vault_dir,
        slug=slug,
        contexts_root=contexts_dir(vault_dir) if vault_dir is not None else declaration_path.parent,
        graph_targets=_parse_graph_targets(settings.get("graph")),
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# CLI — the shell reader's only entry point
# ---------------------------------------------------------------------------


def _format_table(resolutions: Iterable[Resolution]) -> str:
    rows = list(resolutions)
    width = max((len(r.entry.id) for r in rows), default=0)
    lines = []
    for res in rows:
        detail = res.path or res.reason
        lines.append(f"{res.entry.id.ljust(width)}  {res.status.ljust(12)}  {detail}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m knowledge.repos",
        description="Resolve the declared repository checkouts from the registry.",
    )
    parser.add_argument("--vault", metavar="SLUG", help="read the registry of this initialized vault")
    parser.add_argument(
        "--vault-dir", metavar="DIR", help="read the registry of the vault at DIR (its Contexts/config.json)"
    )
    parser.add_argument("--path", metavar="ID", help="print the resolved path of one checkout")
    parser.add_argument("--status", metavar="ID", help="print the status word of one checkout")
    parser.add_argument(
        "--context", metavar="ID", help="print the context package name of one checkout, or nothing"
    )
    parser.add_argument(
        "--context-dir", metavar="ID", help="print the context package directory of one checkout, or nothing"
    )
    parser.add_argument(
        "--install-mode",
        metavar="ID",
        help="print the declared install_mode ('link'/'copy') of one checkout, or nothing",
    )
    parser.add_argument("--ids", action="store_true", help="print every declared checkout id")
    parser.add_argument("--report", action="store_true", help="print the resolution table")
    parser.add_argument("--json", action="store_true", help="print every resolution as JSON")
    parser.add_argument(
        "--verify-remotes",
        action="store_true",
        help="also read each resolved checkout's origin remote and compare it to the declaration",
    )
    args = parser.parse_args(argv)

    try:
        registry = load_registry(
            vault=args.vault, vault_dir=Path(args.vault_dir).expanduser() if args.vault_dir else None
        )
    except RegistryError as exc:
        print(f"ERROR: {exc}", flush=True)
        return 2

    if args.ids:
        print("\n".join(registry.entries))
        return 0

    declarative = (args.context, args.context_dir, args.install_mode)
    if any(value is not None for value in declarative):
        # Declarative facts from the declaration, not tied to this machine's
        # location — unlike --path/--status these never need registry.resolve.
        repo_id = next(value for value in declarative if value is not None)
        entry = registry.entries.get(repo_id)
        if entry is None:
            print(
                f"ERROR: {repo_id!r} is not declared in {registry.declaration_path} under "
                f"repos.entries; known ids: {', '.join(sorted(registry.entries))}",
                flush=True,
            )
            return 2
        if args.context is not None:
            value = entry.context
        elif args.context_dir is not None:
            package = registry.context_dir(repo_id)
            value = None if package is None else str(package)
        else:
            value = entry.install_mode
        if value:
            print(value)
        return 0

    if args.path or args.status:
        repo_id = args.path or args.status
        try:
            resolution = registry.resolve(repo_id)
        except KeyError as exc:
            print(f"ERROR: {exc}", flush=True)
            return 2
        if args.status:
            print(resolution.status)
            return 0
        if resolution.ok:
            print(resolution.path)
            return 0
        # Nothing on stdout for a checkout that did not resolve. A caller
        # writing `cd "$(... --path x)"` must not receive a plausible string
        # for a directory nobody has; the reason travels on stderr and the
        # exit code, and `--status` answers which of the two failures it was.
        print(resolution.reason, file=sys.stderr)
        return 3

    resolutions = registry.resolve_all()
    if args.json or not args.report:
        entries = [r.as_dict() for r in resolutions]
        if args.verify_remotes:
            for item, res in zip(entries, resolutions):
                matches, detail = registry.verify_remote(res)
                item["remote_matches"] = matches
                item["remote_detail"] = detail
        payload: dict[str, Any] = {
            "source": registry.source,
            "vault": None if registry.vault_dir is None else str(registry.vault_dir),
            "slug": registry.slug,
            "declaration": str(registry.declaration_path),
            "overlay": None if registry.overlay_path is None else str(registry.overlay_path),
            "overlay_found": registry.overlay_found,
            "contexts": str(registry.contexts_root),
            "roots": {
                root_id: {
                    "env": root.override,
                    "legacy_env": list(root.legacy_overrides),
                    "default": root.default,
                    "path": registry.root_paths.get(root_id, (None, ""))[0],
                    "source": registry.root_paths.get(root_id, (None, ""))[1],
                }
                for root_id, root in registry.roots.items()
            },
            "entries": entries,
            "warnings": registry.warnings,
        }
        print(json.dumps(payload, indent=2))
    else:
        for warning in registry.warnings:
            print(f"warning: {warning}", file=sys.stderr)
        print(_format_table(resolutions))
        if args.verify_remotes:
            print()
            for res in resolutions:
                matches, detail = registry.verify_remote(res)
                verdict = {True: "MATCH", False: "MISMATCH", None: "UNVERIFIED"}[matches]
                print(f"{res.entry.id}: {verdict} ({detail})")

    return 0 if all(r.ok for r in resolutions) else 1


if __name__ == "__main__":  # pragma: no cover - exercised through the CLI
    raise SystemExit(main())
