"""graphify onboarding targets, resolved through the repository registry.

ADR-039 retires code embeddings from this server and renames the former
``code.paths`` into ``graph.targets``: the pattern of repositories graphify
onboards, consumed by the ``bootstrap-graph`` skill and
``scripts/build-platform-graph.sh``. Each entry keeps only its registry
``repo`` and ``path`` — no extension or directory filter travels with it,
because graphify decides what a target reads through its own
``.graphifyignore``. The default vault's ``Contexts/config.json`` declares the
``graph.targets`` and the registry that are read; a default vault that declares
none has no targets.

The registry (``knowledge.repos``) is the one place that answers "where is
repository X on this machine". Nothing else may answer it: a second answer
spelled as a path relative to this project root assumes every declared
checkout is a sibling of this one, and in a layout where the knowledge lives
under one tree and the services under another, every such path resolves to a
directory that does not exist without anything saying so. So a
``graph.targets`` entry names a registry ``repo`` and a subpath under it, and
the registry answers where that repository is. An entry with no ``repo`` is
anchored to this project root: it is not a repository, or it is one nobody has
located yet.

Nothing here runs at import. The registry is read on first use, and
``REPO_REGISTRY``, ``REPO_REGISTRY_ERROR``, ``GRAPH_TARGETS`` and
``GRAPH_TARGETS_UNRESOLVED`` resolve lazily as module attributes; reloading the
module drops the cached reading.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from knowledge import repos, settings

_resolve_relative_path = settings.resolve_relative_path

_registry_cache: tuple[repos.Registry | None, str] | None = None


def _contexts_vault() -> settings.VaultSpec | None:
    """The default vault when it declares ``Contexts/config.json``; then its registry is the one read."""
    spec = settings.current().default_vault()
    if spec.path is not None and repos.contexts_declaration(spec.path).is_file():
        return spec
    return None


def registry_state() -> tuple[repos.Registry | None, str]:
    """``(registry, error)``: the registry, or ``None`` and why it could not be read.

    The registry of the default vault: its ``Contexts/config.json``, with the
    ``repos.local.json`` this machine wrote for it. A default vault that
    declares none has no registry, and the reason says so.

    Deliberately not fatal: the MCP server serves the vault, and the registry
    only governs graphify onboarding targets. But it is never silent either —
    the reason travels into every unresolved entry, so nobody reads "no
    targets configured" when the truth is "the registry is broken".
    """
    global _registry_cache
    if _registry_cache is None:
        # Loading the settings first reads the project's .env, so a root
        # override declared there reaches the registry the same way an export does.
        settings.current()
        spec = _contexts_vault()
        if spec is None:
            _registry_cache = (
                None,
                f"the default vault declares no {repos.CONTEXTS_DIR_NAME}/{repos.DECLARATION_NAME}, "
                "so there is no repository registry",
            )
        else:
            try:
                _registry_cache = (repos.load_registry(vault_dir=spec.path, vault=spec.slug), "")
            except repos.RegistryError as exc:
                _registry_cache = (None, str(exc))
    return _registry_cache


def _declared_targets() -> list[dict[str, Any]]:
    spec = _contexts_vault()
    if spec is None:
        return []
    registry, _error = registry_state()
    if registry is not None:
        return [dict(entry) for entry in registry.graph_targets]
    # The registry did not load: read the targets as declared, so each
    # one is reported unresolved with that reason instead of vanishing.
    try:
        raw = json.loads(repos.contexts_declaration(spec.path or Path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    graph_cfg = raw.get("graph", {}) if isinstance(raw, Mapping) else {}
    if not isinstance(graph_cfg, Mapping):
        return []
    targets = graph_cfg.get("targets", [])
    return [dict(entry) for entry in targets] if isinstance(targets, list) else []


def _resolve_graph_target(entry: Mapping[str, Any]) -> tuple[str | None, str]:
    """Resolve one ``graph.targets`` entry to a usable directory.

    Returns ``(path, "")`` when the directory is there, and ``(None, reason)``
    when it is not. The reason is kept rather than discarded: a root that could
    not be resolved and a root that resolved to nothing are different facts.
    """
    subpath = str(entry.get("path", "."))
    repo_id = entry.get("repo")
    name = f"{repo_id}:{subpath}" if repo_id else subpath

    if repo_id:
        registry, registry_error = registry_state()
        if registry is None:
            return None, f"{name}: the repository registry could not be read — {registry_error}"
        try:
            resolution = registry.resolve(str(repo_id))
        except KeyError as exc:
            return None, f"{name}: {exc}"
        if not resolution.ok:
            return None, f"{name} [{resolution.status}]: {resolution.reason}"
        base = resolution.path or ""
        full = base if subpath in ("", ".") else os.path.join(base, subpath)
    else:
        full = _resolve_relative_path(subpath)

    if not Path(full).is_dir():
        return None, f"{name} [missing]: {full} is declared but is not a directory on this machine"
    return full, ""


def _split_graph_targets(entries: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    usable: list[str] = []
    unusable: list[str] = []
    for entry in entries:
        path, reason = _resolve_graph_target(entry)
        if path is None:
            unusable.append(reason)
        else:
            usable.append(path)
    return usable, unusable


def graph_targets_detail() -> list[dict[str, Any]]:
    """Every declared ``graph.targets`` entry, resolved or not, identity kept.

    ``GRAPH_TARGETS``/``GRAPH_TARGETS_UNRESOLVED`` split the declaration into two
    flat lists and lose which entry produced which path -- fine for a consumer
    that only wants directories to walk, useless for one that has to report a
    per-entry outcome. ``scripts/build-platform-graph.sh`` is the second kind:
    it names, in its provenance sidecar, exactly which declared ``(repo, path)``
    was fused and which was not and why, so it needs the entry back, not just
    the split verdict.

    Returns one dict per declared entry, in declaration order:
    ``{"repo": str | None, "path": str, "resolved_dir": str | None, "reason": str}``.
    ``resolved_dir`` is ``None`` exactly when ``reason`` is non-empty.
    """
    detail: list[dict[str, Any]] = []
    for entry in _declared_targets():
        resolved_dir, reason = _resolve_graph_target(entry)
        detail.append(
            {
                "repo": entry.get("repo"),
                "path": str(entry.get("path", ".")),
                "resolved_dir": resolved_dir,
                "reason": reason,
            }
        )
    return detail


def repo_checkouts() -> list[dict[str, Any]]:
    """Every repository the registry declares, with the git remote it copies.

    ``graph_targets_detail()`` answers "where do I read this surface from"; this
    answers "what else on this machine is a copy of the same repository". Only the
    registry can: a checkout can name itself through git, never its siblings, and
    one repository may be declared under several deployment slots at once.

    The platform graph reads one checkout per repository, so the copies it does not
    read are the working trees its artifact silently excludes. Returning them lets
    that exclusion be printed instead of assumed.

    Returns one dict per registry entry, in declaration order:
    ``{"id": str, "remote": str, "resolved_dir": str | None, "reason": str}``.
    ``resolved_dir`` is ``None`` exactly when ``reason`` is non-empty.
    """
    registry, _error = registry_state()
    if registry is None:
        return []
    out: list[dict[str, Any]] = []
    for resolution in registry.resolve_all():
        out.append(
            {
                "id": resolution.entry.id,
                "remote": resolution.entry.remote,
                "resolved_dir": resolution.path if resolution.ok else None,
                "reason": "" if resolution.ok else f"[{resolution.status}] {resolution.reason}",
            }
        )
    return out


def __getattr__(name: str) -> Any:
    """Lazy module attributes kept for callers that read the split lists."""
    if name == "REPO_REGISTRY":
        return registry_state()[0]
    if name == "REPO_REGISTRY_ERROR":
        return registry_state()[1]
    if name in ("GRAPH_TARGETS", "_graph_targets_from_json", "GRAPH_TARGETS_UNRESOLVED"):
        usable, unusable = _split_graph_targets(_declared_targets())
        return unusable if name == "GRAPH_TARGETS_UNRESOLVED" else usable
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
