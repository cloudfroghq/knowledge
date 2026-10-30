"""``repos.read_origin``: the origin of a checkout, read from git's own config files.

Spawning ``git config`` for every repository a scan meets costs about 13 ms
each; reading the file costs microseconds. The file is parsed only when that
is safe, and every shape it cannot vouch for goes to ``git config --get`` for
that one repository, so the answer is always the one git gives.

What is pinned:

* the answer equals ``git config --get remote.origin.url`` over a table of
  config shapes, and for a plain repository, a linked worktree and a
  ``.git`` file (``gitdir:``) no git process is started;
* ``include``, ``includeIf``, quoting, line continuations, inline comments,
  the ``[remote.origin]`` spelling and ``extensions.worktreeConfig`` fall
  back to git, once per repository;
* a directory that is not a repository, or whose ``.git`` file points
  nowhere, behaves as before (the git answer, never an exception).

Every case builds its repositories under ``tmp_path``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from knowledge import repos

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.test",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.test",
}


def _git(*args: str, cwd: Path, home: Path) -> str:
    done = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "HOME": str(home), **GIT_ENV},
    )
    return done.stdout.strip()


def _repo(root: Path, name: str = "repo", origin: str | None = "git@h.example:team/app.git") -> Path:
    path = root / name
    path.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=path, home=root)
    if origin is not None:
        _git("remote", "add", "origin", origin, cwd=path, home=root)
    return path


def _expected(path: Path, home: Path) -> str | None:
    """What git itself answers, computed before any spy is installed."""
    done = subprocess.run(
        ["git", "-C", str(path), "config", "--get", "remote.origin.url"],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "HOME": str(home)},
    )
    return done.stdout.strip() or None if done.returncode == 0 else None


@pytest.fixture(autouse=True)
def hermetic_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No global or system git config reaches ``git config --get`` (a real ``~/.gitconfig`` can make it fail)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)


class Spawns:
    """Counts the processes ``repos`` starts, still running them."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[list[str]] = []
        real = subprocess.run

        def spy(command: list[str], *args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(list(command))
            return real(command, *args, **kwargs)  # type: ignore[call-overload]

        monkeypatch.setattr(repos.subprocess, "run", spy)


# --- no process for a repository it can read -------------------------------------


def test_a_plain_repository_is_read_without_starting_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _repo(tmp_path)
    spawns = Spawns(monkeypatch)

    assert repos.read_origin(path) == ("git@h.example:team/app.git", "")
    assert spawns.calls == []


def test_a_repository_without_origin_is_reported_without_starting_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _repo(tmp_path, origin=None)
    spawns = Spawns(monkeypatch)

    url, reason = repos.read_origin(path)

    assert url is None and "declares no origin remote" in reason
    assert spawns.calls == []


def test_a_linked_worktree_reads_the_origin_of_the_repository_it_belongs_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = _repo(tmp_path)
    (main / "f.txt").write_text("x")
    _git("add", "-A", cwd=main, home=tmp_path)
    _git("commit", "-q", "-m", "init", cwd=main, home=tmp_path)
    linked = tmp_path / "linked"
    _git("worktree", "add", "-q", str(linked), cwd=main, home=tmp_path)
    assert (linked / ".git").is_file()
    spawns = Spawns(monkeypatch)

    assert repos.read_origin(linked) == ("git@h.example:team/app.git", "")
    assert spawns.calls == []


def test_a_git_file_pointing_at_a_separate_git_dir_is_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "sub"
    path.mkdir()
    (tmp_path / "modules").mkdir()
    _git("init", "-q", "-b", "main", f"--separate-git-dir={tmp_path / 'modules' / 'sub'}", cwd=path, home=tmp_path)
    _git("remote", "add", "origin", "git@h.example:team/sub.git", cwd=path, home=tmp_path)
    assert (path / ".git").read_text().startswith("gitdir:")
    spawns = Spawns(monkeypatch)

    assert repos.read_origin(path) == ("git@h.example:team/sub.git", "")
    assert spawns.calls == []


def test_a_submodule_reads_the_origin_its_own_config_declares(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bare = tmp_path / "up.git"
    _git("init", "-q", "--bare", "-b", "main", str(bare), cwd=tmp_path, home=tmp_path)
    seed = _repo(tmp_path, "seed", origin=None)
    (seed / "f.txt").write_text("x")
    _git("add", "-A", cwd=seed, home=tmp_path)
    _git("commit", "-q", "-m", "init", cwd=seed, home=tmp_path)
    _git("push", "-q", str(bare), "main", cwd=seed, home=tmp_path)
    parent = _repo(tmp_path, "parent", origin="git@h.example:team/parent.git")
    _git("-c", "protocol.file.allow=always", "submodule", "add", "-q", str(bare), "sub", cwd=parent, home=tmp_path)
    assert (parent / "sub" / ".git").is_file()
    expected = _expected(parent / "sub", tmp_path)
    spawns = Spawns(monkeypatch)

    assert repos.read_origin(parent / "sub") == (expected, "")
    assert spawns.calls == []


# --- the answer is git's, over a table of config shapes ---------------------------


CONFIGS = {
    "plain": '[remote "origin"]\n\turl = git@h.example:a/b.git\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n',
    "mixed case names": '[Remote "origin"]\n\tURL = git@h.example:a/b.git\n',
    "last url wins": '[remote "origin"]\n\turl = git@h.example:a/first.git\n\turl = git@h.example:a/last.git\n',
    "split section": '[remote "origin"]\n\turl = git@h.example:a/one.git\n[core]\n\tbare = false\n[remote "origin"]\n\turl = git@h.example:a/two.git\n',
    "other remote ignored": '[remote "upstream"]\n\turl = git@h.example:a/up.git\n[remote "origin"]\n\turl = git@h.example:a/b.git\n',
    "only another remote": '[remote "upstream"]\n\turl = git@h.example:a/up.git\n',
    "comments and blanks": '# top\n\n[remote "origin"]\n; note\n\n  url   =   git@h.example:a/b.git   \n',
    "https url": '[remote "origin"]\n\turl = https://h.example/a/b.git\n',
    "empty url": '[remote "origin"]\n\turl =\n',
}


@pytest.mark.parametrize("name", sorted(CONFIGS))
def test_the_answer_equals_git_config_get_without_a_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    path = _repo(tmp_path, origin=None)
    (path / ".git" / "config").write_text('[core]\n\trepositoryformatversion = 0\n' + CONFIGS[name], encoding="utf-8")
    expected = _expected(path, tmp_path)
    spawns = Spawns(monkeypatch)

    url, _reason = repos.read_origin(path)

    assert url == expected
    assert spawns.calls == []


# --- shapes it cannot vouch for go to git ---------------------------------------------


FALLBACKS = {
    "include": '[include]\n\tpath = extra\n',
    "includeIf": '[includeIf "gitdir:**"]\n\tpath = extra\n',
    "quoted value": '[remote "origin"]\n\turl = "git@h.example:a/b.git"\n',
    "backslash": '[remote "origin"]\n\turl = git@h.example:a/\\\nb.git\n',
    "inline comment": '[remote "origin"]\n\turl = git@h.example:a/b.git # mine\n',
    "old section spelling": '[remote.origin]\n\turl = git@h.example:a/b.git\n',
    "key on the header line": '[remote "origin"] url = git@h.example:a/b.git\n',
    "worktree config": '[extensions]\n\tworktreeConfig = true\n[remote "origin"]\n\turl = git@h.example:a/b.git\n',
}


@pytest.mark.parametrize("name", sorted(FALLBACKS))
def test_a_shape_it_cannot_vouch_for_asks_git_once_and_gets_git_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    path = _repo(tmp_path, origin=None)
    (path / ".git" / "config").write_text(
        '[core]\n\trepositoryformatversion = 0\n' + FALLBACKS[name], encoding="utf-8"
    )
    (path / ".git" / "extra").write_text('[remote "origin"]\n\turl = git@h.example:a/included.git\n', encoding="utf-8")
    expected = _expected(path, tmp_path)
    spawns = Spawns(monkeypatch)

    url, _reason = repos.read_origin(path)

    assert url == expected
    assert len(spawns.calls) == 1 and spawns.calls[0][:2] == ["git", "-C"]


def test_an_included_origin_is_found_through_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _repo(tmp_path, origin=None)
    (path / ".git" / "config").write_text('[include]\n\tpath = extra\n', encoding="utf-8")
    (path / ".git" / "extra").write_text('[remote "origin"]\n\turl = git@h.example:a/included.git\n', encoding="utf-8")
    spawns = Spawns(monkeypatch)

    assert repos.read_origin(path) == ("git@h.example:a/included.git", "")
    assert len(spawns.calls) == 1


# --- not a repository -------------------------------------------------------------------


def test_a_directory_that_is_not_a_repository_still_asks_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    spawns = Spawns(monkeypatch)

    url, reason = repos.read_origin(plain)

    assert url is None and "declares no origin remote" in reason
    assert len(spawns.calls) == 1


def test_a_git_file_pointing_nowhere_never_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / ".git").write_text("gitdir: ../does-not-exist\n", encoding="utf-8")
    spawns = Spawns(monkeypatch)

    url, reason = repos.read_origin(broken)

    assert url is None and "declares no origin remote" in reason
    assert len(spawns.calls) == 1
