"""Consolidation bridge: export stable memories into the vault.

Runs `memory obsidian-export` scoped to a project into the vault's memory
folder (config: memory.vault_folder), then normalises the exported files so
the knowledge index can fence them off from curated docs:

- frontmatter `type` (observation type) moves to `memory_type`
- `type: memory` and `source: memory` are set
- files whose observation type is not in the allowlist (config:
  memory.export_types) are removed, as are session/topic hub files
- files carrying a `knowledge_ref` are removed: the fact they hold is already
  written up in a curated document, so re-exporting it would leave the vault
  with two copies of the same knowledge and only one of them reviewed

The weekly bridge (scripts/weekly-memory-bridge.sh) commits and pushes the
memory folder afterwards in the vault's own repository, whose push webhook
makes the server pull the vault and reindex incrementally. A local stdio index picks the notes up
on its next `reindex` (target="vault").

Usage:
    uv run python scripts/export_memories.py [--project NAME] [--limit N]
        [--memory-bin PATH] [--dry-run]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import frontmatter

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from knowledge import config  # noqa: E402

_HUB_DIRS = {"_sessions", "_topics"}


def _is_promoted(metadata: dict) -> bool:
    """Report whether an exported memory already points at a curated document.

    `knowledge_ref` is the promotion mark of the memory <-> knowledge link: a
    human wrote the fact up in the vault and someone stamped the observation
    with the path. From that moment the memory folder must not carry its own
    copy — the curated document wins any domain conflict, and a stale twin in
    `90 - Memory/` is exactly how a superseded explanation keeps surfacing in
    search results.
    """
    return bool(str(metadata.get("knowledge_ref") or "").strip())


def export(memory_bin: str, project: str, limit: int | None, memory_root: Path) -> None:
    cmd = [memory_bin, "obsidian-export", "--vault", str(memory_root), "--project", project]
    if limit:
        cmd += ["--limit", str(limit)]
    # Flushed so the command line lands in the log before memory's own output
    # when stdout is a file, as under launchd.
    print(f"$ {' '.join(cmd)}", flush=True)
    subprocess.run(cmd, check=True, timeout=300)


def normalise(memory_root: Path, allowed_types: set[str], *, dry_run: bool = False) -> dict[str, int]:
    stats = {"kept": 0, "removed": 0, "rewritten": 0, "promoted": 0}
    export_root = memory_root / "memory"
    if not export_root.exists():
        print(f"nothing exported at {export_root}")
        return stats

    for md_path in sorted(export_root.rglob("*.md")):
        rel_parts = md_path.relative_to(export_root).parts
        is_hub = any(part in _HUB_DIRS for part in rel_parts)

        try:
            post = frontmatter.load(str(md_path))
        except Exception as exc:
            print(f"  ! unparseable, removing: {md_path.name} ({exc})")
            if not dry_run:
                md_path.unlink()
            stats["removed"] += 1
            continue

        obs_type = str(post.metadata.get("memory_type") or post.metadata.get("type") or "")
        if is_hub or obs_type not in allowed_types:
            if not dry_run:
                md_path.unlink()
            stats["removed"] += 1
            continue

        if _is_promoted(post.metadata):
            print(f"  - promoted, not exported: {md_path.name} -> {post.metadata['knowledge_ref']}")
            if not dry_run:
                md_path.unlink()
            stats["promoted"] += 1
            stats["removed"] += 1
            continue

        if post.metadata.get("type") == "memory" and post.metadata.get("source") == "memory":
            stats["kept"] += 1
            continue

        post.metadata["memory_type"] = obs_type
        post.metadata["type"] = "memory"
        post.metadata["source"] = "memory"
        updated = post.metadata.get("updated_at") or post.metadata.get("created_at")
        if updated and "last_updated" not in post.metadata:
            post.metadata["last_updated"] = str(updated).split(" ")[0]
        if not dry_run:
            md_path.write_text(frontmatter.dumps(post) + "\n")
        stats["kept"] += 1
        stats["rewritten"] += 1

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="knowledge")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--memory-bin", default=str(Path.home() / "Projects/memory/bin/memory"))
    parser.add_argument("--dry-run", action="store_true", help="report without writing/removing")
    args = parser.parse_args()

    memory_root = config.VAULT_PATH / config.MEMORY_FOLDER
    memory_root.mkdir(parents=True, exist_ok=True)

    export(args.memory_bin, args.project, args.limit, memory_root)
    stats = normalise(memory_root, set(config.MEMORY_EXPORT_TYPES), dry_run=args.dry_run)
    print(f"bridge done: {stats} -> {memory_root}")
    print("next: commit and push the memory folder; the push webhook reindexes the server")


if __name__ == "__main__":
    main()
