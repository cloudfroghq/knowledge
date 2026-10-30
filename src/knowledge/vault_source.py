"""A vault read from a directory, behind :class:`~knowledge.ports.VaultSource`.

How the directory is kept fresh is injected: :class:`DirectoryVaultSource`
never runs git itself. Without a ``sync`` it reports that nothing was pulled.
"""

from __future__ import annotations

from pathlib import Path

from knowledge.ports import SYNC_PULLED, SYNC_SKIPPED_NO_REPO, SYNC_UP_TO_DATE, VaultSync


def is_hidden(relative: Path) -> bool:
    """Whether a vault-relative path crosses a segment that starts with ``.``.

    Dot-prefixed directories hold tooling, not curated content: ``.obsidian``,
    ``.git`` and the ``.claude`` tree of a context package under
    ``Contexts/<app>/``. The rule is the one ``vault-lint`` walks the vault
    with, so the indexer and the linter read the same set of notes.
    """
    return any(part.startswith(".") for part in relative.parts)


class DirectoryVaultSource:
    """Every ``*.md`` file under ``root`` outside dot-prefixed segments, in a stable sorted order.

    ``Contexts/<app>/CLAUDE.md`` is a note like any other; nothing under
    ``Contexts/<app>/.claude/`` is.
    """

    def __init__(self, root: Path, *, sync: VaultSync | None = None) -> None:
        self.root = root
        self._sync = sync

    def __repr__(self) -> str:
        return f"DirectoryVaultSource(root={str(self.root)!r}, sync={self._sync is not None})"

    def markdown_files(self) -> list[Path]:
        return [path for path in sorted(self.root.rglob("*.md")) if not is_hidden(path.relative_to(self.root))]

    def sync(self) -> bool:
        """Refresh the directory; ``False`` when there is no refresh or nothing was pulled."""
        if self._sync is None:
            return False
        return self._sync(self.root)

    def sync_outcome(self) -> str:
        """Refresh the directory and say how it ended (``ports.SYNC_OUTCOMES``).

        Without a refresh there is nothing to pull from: ``skipped_no_repo``.
        A refresh that cannot report its outcome is read through its boolean,
        which means "something was pulled".
        """
        if self._sync is None:
            return SYNC_SKIPPED_NO_REPO
        report = getattr(self._sync, "outcome", None)
        if callable(report):
            return str(report(self.root))
        return SYNC_PULLED if self._sync(self.root) else SYNC_UP_TO_DATE
