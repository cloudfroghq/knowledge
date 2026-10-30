"""``cloudfrog-knowledge init``: a vault's context packages, linked into the checkouts of this machine.

Each case builds a lab under ``tmp_path``: a copy of the ``contexts`` fixture
vault served through its own ``vaults.yaml``, a fake home and config home, and
fake checkouts made with ``git init`` and an ``origin`` remote. Nothing reads
or writes the developer's home, the real vaults or the real checkouts.

What is pinned:

* a dry run reports everything and writes nothing;
* a run writes ``<config home>/knowledge/<slug>/repos.local.json``, links the
  ``link`` packages with relative symlinks, copies the ``copy`` ones, adds
  the ignore entries, skips the checkouts that are not on the machine and
  ends by naming every file it touched;
* a second run changes nothing;
* pre-existing content is never clobbered without ``--force``, a drifted
  copy needs ``--refresh``, a checkout whose origin is not its declared
  remote is refused, and nothing is ever written inside a vault;
* a real ``.claude`` of the checkout stays real and receives one link per
  package entry (and per entry of a ``.claude/<collection>/`` the checkout
  tracks); an untracked real entry conflicts until ``--force`` moves it aside,
  and whatever the checkout tracks in git is kept as its own;
* ``doctor`` reports a pending init as a warning, exit status 0, and its
  brief line names the command; ``doctor --client`` checks only what the
  client owns and reports each checkout;
* every hint names ``KNOWLEDGE_COMMAND_LABEL`` when it is set;
* only the checkouts of the ``apply`` list receive a package: ``--repo``,
  ``--all`` and ``--drop`` change it (``--drop`` removes only the links into
  that package), a new machine is asked in a terminal and otherwise installs
  nothing, a file without the list adopts what is installed, an id the
  registry no longer declares is dropped with a notice, a dry run writes
  nothing, and ``doctor --client`` tells a checkout that is not selected
  apart from one that is selected and not linked, one that is linked and not
  selected, and a file with no list.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from knowledge import cli, contexts, repos

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTEXTS_VAULT = REPO_ROOT / "tests" / "fixtures" / "vaults" / "contexts"
APP_IDS = ("ledger-api", "ledger-web", "notes-cli")


@dataclass
class Lab:
    root: Path
    vault: Path
    home: Path
    work: Path
    manifest: Path

    @property
    def overlay(self) -> Path:
        return self.root / "xdg" / "knowledge" / "contexts" / "repos.local.json"

    @property
    def api(self) -> Path:
        return self.work / "ledger" / "api"

    @property
    def web(self) -> Path:
        return self.work / "ledger" / "web"


def _checkout(path: Path, remote: str) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "remote", "add", "origin", f"git@example.test:team/{remote}.git"],
        check=True,
        capture_output=True,
    )
    return path


def _tree(root: Path) -> dict[str, str]:
    """Every path under ``root`` (links by their target, directories as such) mapped to what it holds."""
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            out[rel] = "link:" + str(path.readlink())
        elif path.is_dir():
            out[rel] = "dir"
        else:
            out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


@pytest.fixture()
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Lab:
    vault = tmp_path / "vault"
    shutil.copytree(CONTEXTS_VAULT, vault)
    manifest = tmp_path / "vaults.yaml"
    manifest.write_text(
        f"version: 1\nvaults:\n  - slug: contexts\n    path: {vault}\n    default: true\n", encoding="utf-8"
    )
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(cli, "load_dotenv", lambda *_a, **_k: False)
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("KNOWLEDGE_VAULTS", str(manifest))
    monkeypatch.setenv("VAULT_PATH", "")
    monkeypatch.setenv("KNOWLEDGE_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("MCP_TRANSPORT", "stdio")
    for root in ("WORK", "SIDE"):
        monkeypatch.delenv(f"KNOWLEDGE_{root}_ROOT", raising=False)
        monkeypatch.delenv(f"CD_{root}_ROOT", raising=False)
    for app in APP_IDS:
        token = app.replace("-", "_").upper()
        monkeypatch.delenv(f"KNOWLEDGE_REPO_{token}", raising=False)
        monkeypatch.delenv(f"CD_REPO_{token}", raising=False)
    work = tmp_path / "work"
    lab = Lab(root=tmp_path, vault=vault, home=home, work=work, manifest=manifest)
    _checkout(lab.api, "ledger-api")
    _checkout(lab.web, "ledger-web")
    return lab


def _run(lab: Lab, capsys: pytest.CaptureFixture[str], *flags: str) -> tuple[int, dict]:
    """``init --json --yes`` with exactly ``flags``: no selection unless they carry one."""
    rc = cli.main(["init", "--json", "--yes", "--search-dir", str(lab.work), *flags])
    return rc, json.loads(capsys.readouterr().out)


def _init(lab: Lab, capsys: pytest.CaptureFixture[str], *flags: str) -> tuple[int, dict]:
    """``init`` that selects every checkout on the machine (``--all``), the way most cases want it."""
    return _run(lab, capsys, "--all", *flags)


def _apps(report: dict) -> dict[str, dict]:
    return {app["id"]: app for app in report["vaults"][0]["apps"]}


def _exclude(checkout: Path) -> list[str]:
    return (checkout / ".git" / "info" / "exclude").read_text(encoding="utf-8").splitlines()


# --- dry run ----------------------------------------------------------------------


def test_a_dry_run_reports_everything_and_writes_nothing(lab: Lab, capsys: pytest.CaptureFixture[str]) -> None:
    before = _tree(lab.root)

    rc, report = _init(lab, capsys, "--dry-run")

    assert rc == 0
    assert report["dry_run"] is True
    vault = report["vaults"][0]
    assert (vault["status"], vault["overlay"], vault["overlay_action"]) == (
        "initialized",
        str(lab.overlay),
        "would-create",
    )
    roots = {root["id"]: root for root in vault["roots"]}
    assert (roots["work"]["path"], roots["work"]["kind"]) == (str(lab.work.resolve()), "detected")
    assert roots["side"]["status"] == "not-located"
    apps = _apps(report)
    assert {app: apps[app]["status"] for app in APP_IDS} == {
        "ledger-api": "would-install",
        "ledger-web": "would-install",
        "notes-cli": "skipped",
    }
    assert _tree(lab.root) == before, "a dry run writes nothing anywhere"


# --- a run --------------------------------------------------------------------------


def test_init_writes_the_overlay_links_copies_and_skips(lab: Lab, capsys: pytest.CaptureFixture[str]) -> None:
    vault_before = _tree(lab.vault)

    rc, report = _init(lab, capsys)

    assert rc == 0 and report["ok"] is True
    overlay = json.loads(lab.overlay.read_text(encoding="utf-8"))
    assert overlay["slug"] == "contexts"
    assert overlay["vault"] == str(lab.vault)
    assert overlay["roots"] == {"work": str(lab.work.resolve())}
    assert overlay["repos"] == {}
    assert overlay["_README"], "the file explains itself to the person who opens it"

    apps = _apps(report)
    assert {app: apps[app]["status"] for app in APP_IDS} == {
        "ledger-api": "installed",
        "ledger-web": "installed",
        "notes-cli": "skipped",
    }
    assert "root 'side' is not located" in apps["notes-cli"]["detail"]

    package = lab.vault / "Contexts" / "ledger-api"
    for name in (".claude", "CLAUDE.md", ".mcp.json"):
        link = lab.api / name
        assert link.is_symlink(), name
        assert not Path(link.readlink()).is_absolute(), "links are relative, so a moved tree keeps them"
        assert link.resolve() == (package / name).resolve()
    assert (lab.api / ".claude" / "agents" / "x.md").is_file()

    copied = lab.vault / "Contexts" / "ledger-web"
    for name in ("CLAUDE.md", ".mcp.json"):
        assert not (lab.web / name).is_symlink()
        assert (lab.web / name).read_bytes() == (copied / name).read_bytes()
    assert not (lab.web / ".claude").is_symlink()
    assert (lab.web / ".claude" / "agents" / "x.md").read_bytes() == (copied / ".claude" / "agents" / "x.md").read_bytes()

    for checkout in (lab.api, lab.web):
        assert set(contexts.IGNORE_ENTRIES) <= set(_exclude(checkout))
    assert _tree(lab.vault) == vault_before, "init never writes inside a vault"

    files = report["files"]
    assert files["vaults_yaml"] == str(lab.manifest)
    assert files["repos_local"] == [{"vault": "contexts", "path": str(lab.overlay), "action": "created"}]
    assert [c["id"] for c in files["checkouts"]] == ["ledger-api", "ledger-web"]


def test_the_text_report_ends_with_where_every_file_lives(lab: Lab, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["init", "--yes", "--all", "--search-dir", str(lab.work)])
    out = capsys.readouterr().out

    assert rc == 0
    tail = out.split("Where everything lives (edit these files to change it):\n", 1)[1].splitlines()
    assert tail[0] == f"  vaults.yaml       {lab.manifest}"
    assert tail[1].startswith(f"  repos.local.json  {lab.overlay}  (contexts, created)")
    assert tail[2].startswith(f"  checkout          {lab.api}  (contexts/ledger-api, installed)")
    assert tail[3].startswith(f"  checkout          {lab.web}  (contexts/ledger-web, installed)")


def test_a_second_run_changes_nothing(lab: Lab, capsys: pytest.CaptureFixture[str]) -> None:
    _init(lab, capsys)
    before = _tree(lab.root)

    rc, report = _init(lab, capsys)

    assert rc == 0
    assert report["vaults"][0]["overlay_action"] == "unchanged"
    assert {app["id"]: app["status"] for app in report["vaults"][0]["apps"]} == {
        "ledger-api": "unchanged",
        "ledger-web": "unchanged",
        "notes-cli": "skipped",
    }
    assert _tree(lab.root) == before
    assert _exclude(lab.api).count("/.claude") == 1


def test_a_second_run_does_not_touch_a_linked_checkout(lab: Lab, capsys: pytest.CaptureFixture[str]) -> None:
    """The symlink probe is only for a checkout that still has to receive a link.

    Creating and removing the probe file bumps the directory's mtime, so a run
    that has nothing to do must not create it. The mtime is parked far in the
    past first: a file system with a coarse clock could hide a touch that
    happened right after the first run.
    """
    _init(lab, capsys)
    parked = 1_000_000_000 * 10**9
    os.utime(lab.api, ns=(parked, parked))

    rc, report = _init(lab, capsys)

    assert rc == 0
    assert _apps(report)["ledger-api"]["status"] == "unchanged"
    assert lab.api.stat().st_mtime_ns == parked
    assert not list(lab.api.glob(".ai-context-symlink-probe.*"))


def test_a_memoized_scan_runs_once_per_search_and_depth(tmp_path: Path) -> None:
    seen: list[tuple[tuple[str, ...], int]] = []

    def scan(search_dirs: list[Path], *, max_depth: int = contexts.DEFAULT_MAX_DEPTH) -> list[tuple[Path, str]]:
        seen.append((tuple(str(d) for d in search_dirs), max_depth))
        return [(tmp_path / "repo", "git@h.example:team/repo.git")]

    cached = contexts.memoized_scan(scan)
    first = cached([tmp_path], max_depth=4)
    assert cached([tmp_path], max_depth=4) == first
    cached([tmp_path], max_depth=2)
    cached([tmp_path / "elsewhere"], max_depth=4)

    assert len(seen) == 3, "a different depth or a different directory is a different scan"
    first.clear()
    assert cached([tmp_path], max_depth=4), "a caller cannot empty the cache"


def test_several_vaults_share_one_scan_of_the_search_directories(
    lab: Lab, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    second = lab.root / "vault-b"
    shutil.copytree(CONTEXTS_VAULT, second)
    manifest = second / "vault.yaml"
    manifest.write_text(manifest.read_text(encoding="utf-8").replace("slug: contexts", "slug: contexts-b"), encoding="utf-8")
    with lab.manifest.open("a", encoding="utf-8") as handle:
        handle.write(f"  - slug: contexts-b\n    path: {second}\n")
    decoy = _checkout(lab.work / "decoy", "unrelated")
    visited: list[Path] = []
    real = repos.read_origin

    def counting(path: Path | str) -> tuple[str | None, str]:
        visited.append(Path(path).resolve())
        return real(path)

    monkeypatch.setattr(repos, "read_origin", counting)

    rc, report = _init(lab, capsys)

    assert rc == 0 and [v["slug"] for v in report["vaults"]] == ["contexts", "contexts-b"]
    # Only a scan ever looks at the decoy: no vault declares it, so nothing verifies it.
    assert visited.count(decoy.resolve()) == 1


def test_a_checkout_that_is_not_on_the_machine_is_skipped(lab: Lab, capsys: pytest.CaptureFixture[str]) -> None:
    shutil.rmtree(lab.web)

    rc, report = _init(lab, capsys)

    assert rc == 0, "a checkout this machine does not have is skipped, never a failure"
    web = _apps(report)["ledger-web"]
    assert web["status"] == "skipped"
    assert "not on this machine" in web["detail"]
    assert _apps(report)["ledger-api"]["status"] == "installed"


def test_a_vault_default_locates_its_root_and_is_recorded(lab: Lab, capsys: pytest.CaptureFixture[str]) -> None:
    notes = _checkout(lab.home / "side-projects" / "notes-cli", "notes-cli")

    rc, report = _init(lab, capsys)

    assert rc == 0
    roots = {root["id"]: root for root in report["vaults"][0]["roots"]}
    assert roots["side"]["kind"] == "default"
    assert _apps(report)["notes-cli"]["status"] == "installed"
    assert (notes / "CLAUDE.md").is_symlink()
    assert json.loads(lab.overlay.read_text(encoding="utf-8"))["roots"]["side"] == str(lab.home / "side-projects")


def test_a_checkout_away_from_its_declared_path_is_pinned(lab: Lab, capsys: pytest.CaptureFixture[str]) -> None:
    shutil.rmtree(lab.web)
    clone = _checkout(lab.work / "clones" / "web-renamed", "ledger-web")

    rc, report = _init(lab, capsys)

    assert rc == 0
    assert json.loads(lab.overlay.read_text(encoding="utf-8"))["repos"] == {"ledger-web": str(clone.resolve())}
    web = _apps(report)["ledger-web"]
    assert (web["status"], web["path"]) == ("installed", str(clone.resolve()))


def test_a_root_flag_wins_and_an_unknown_root_writes_nothing(
    lab: Lab, capsys: pytest.CaptureFixture[str]
) -> None:
    notes = _checkout(lab.root / "elsewhere" / "notes-cli", "notes-cli")

    rc = cli.main(["init", "--yes", "--search-dir", str(lab.work), "--root", "nowhere=/tmp"])
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.err.startswith("E_ROOT_UNKNOWN: ")
    assert not lab.overlay.exists()

    rc, report = _init(lab, capsys, "--root", f"contexts:side={notes.parent}")
    assert rc == 0
    side = {root["id"]: root for root in report["vaults"][0]["roots"]}["side"]
    assert (side["kind"], side["path"]) == ("flag", str(notes.parent))
    assert _apps(report)["notes-cli"]["status"] == "installed"


def test_a_root_flag_that_names_no_directory_fails(lab: Lab, capsys: pytest.CaptureFixture[str]) -> None:
    rc, report = _init(lab, capsys, "--root", f"side={lab.root / 'missing'}")

    assert rc == 1
    side = {root["id"]: root for root in report["vaults"][0]["roots"]}["side"]
    assert side["status"] == "error"


# --- never clobber, never into a vault --------------------------------------------


def test_a_real_file_is_a_conflict_until_force_moves_it_aside(
    lab: Lab, capsys: pytest.CaptureFixture[str]
) -> None:
    (lab.api / "CLAUDE.md").write_text("# mine\n", encoding="utf-8")

    rc, report = _init(lab, capsys)
    api = _apps(report)["ledger-api"]
    assert rc == 1
    assert api["status"] == "conflict"
    assert "--force" in api["detail"]
    assert (lab.api / "CLAUDE.md").read_text(encoding="utf-8") == "# mine\n"
    assert not (lab.api / ".claude").exists(), "a conflict installs nothing in that checkout"

    rc, report = _init(lab, capsys, "--force")
    assert rc == 0
    assert (lab.api / "CLAUDE.md").is_symlink()
    [backup] = list(lab.api.glob("CLAUDE.md.pre-ai-context.bak-*"))
    assert backup.read_text(encoding="utf-8") == "# mine\n"


def test_a_drifted_copy_needs_refresh_and_keeps_the_personal_files(
    lab: Lab, capsys: pytest.CaptureFixture[str]
) -> None:
    _init(lab, capsys)
    agent = lab.web / ".claude" / "agents" / "x.md"
    agent.write_text("drifted\n", encoding="utf-8")
    personal = lab.web / ".claude" / "settings.local.json"
    personal.write_text('{"mine": true}\n', encoding="utf-8")
    extra = lab.web / ".claude" / "agents" / "stale.md"
    extra.write_text("not in the package\n", encoding="utf-8")

    rc, report = _init(lab, capsys)
    assert rc == 1
    assert _apps(report)["ledger-web"]["status"] == "conflict"
    assert agent.read_text(encoding="utf-8") == "drifted\n"

    rc, report = _init(lab, capsys, "--refresh")
    assert rc == 0
    assert _apps(report)["ledger-web"]["status"] == "installed"
    package = lab.vault / "Contexts" / "ledger-web" / ".claude" / "agents" / "x.md"
    assert agent.read_bytes() == package.read_bytes()
    assert not extra.exists(), "a refreshed copy mirrors the package"
    assert personal.read_text(encoding="utf-8") == '{"mine": true}\n'
    assert list(lab.web.glob("*.pre-ai-context.bak-*")) == [], "a refresh keeps no backup"


def _declare(lab: Lab, **changes: dict) -> None:
    """Rewrite entries of the lab vault's declaration: ``changes`` maps an entry id to the keys to replace."""
    path = lab.vault / "Contexts" / "config.json"
    declaration = json.loads(path.read_text(encoding="utf-8"))
    for entry in declaration["repos"]["entries"]:
        entry.update(changes.get(entry["id"].replace("-", "_"), {}))
    path.write_text(json.dumps(declaration), encoding="utf-8")


def test_a_path_that_escapes_its_root_installs_nothing_outside_it(lab: Lab, capsys: pytest.CaptureFixture[str]) -> None:
    escaped = lab.root / "escaped"
    escaped.mkdir()
    subprocess.run(["git", "init", "-q", str(escaped)], check=True, capture_output=True)
    _declare(lab, ledger_api={"path": "../escaped"})

    rc, report = _init(lab, capsys)

    assert rc == 1
    [vault] = report["vaults"]
    assert vault["status"] == "error" and vault["detail"].startswith("E_CONTEXTS_INVALID: ")
    assert "leaves its root" in vault["detail"]
    assert list(escaped.iterdir()) == [escaped / ".git"], "nothing was installed outside the root"
    assert not lab.overlay.exists()


def test_a_checkout_whose_origin_cannot_be_verified_is_not_installed_without_force(
    lab: Lab, capsys: pytest.CaptureFixture[str]
) -> None:
    subprocess.run(["git", "-C", str(lab.api), "remote", "remove", "origin"], check=True, capture_output=True)

    rc, report = _init(lab, capsys, "--dry-run")
    api = _apps(report)["ledger-api"]
    assert rc == 1 and api["status"] == "unverified"
    assert "declares no origin remote" in api["detail"] and "--force" in api["detail"]

    rc, report = _init(lab, capsys)
    assert rc == 1 and _apps(report)["ledger-api"]["status"] == "unverified"
    assert not (lab.api / "CLAUDE.md").exists() and not (lab.api / ".claude").exists()
    assert _apps(report)["ledger-web"]["status"] == "installed", "the other checkouts are not held back"

    rc, report = _init(lab, capsys, "--force")
    api = _apps(report)["ledger-api"]
    assert rc == 0 and api["status"] == "installed"
    assert "not verified" in api["detail"]
    assert (lab.api / "CLAUDE.md").is_symlink()


def test_a_plain_directory_is_not_installed_into_without_force_either(
    lab: Lab, capsys: pytest.CaptureFixture[str]
) -> None:
    shutil.rmtree(lab.api / ".git")

    rc, report = _init(lab, capsys)

    assert rc == 1 and _apps(report)["ledger-api"]["status"] == "unverified"
    assert not (lab.api / "CLAUDE.md").exists()


def test_a_link_of_the_user_is_a_conflict_until_force_moves_it_aside(
    lab: Lab, capsys: pytest.CaptureFixture[str]
) -> None:
    mine = lab.root / "dotfiles" / "claude"
    mine.mkdir(parents=True)
    (lab.api / ".claude").symlink_to(mine)

    rc, report = _init(lab, capsys)
    api = _apps(report)["ledger-api"]
    assert rc == 1 and api["status"] == "conflict"
    assert (lab.api / ".claude").readlink() == mine, "the user's link is untouched"
    assert not (lab.api / "CLAUDE.md").exists(), "a conflict installs nothing in that checkout"

    rc, _report = _init(lab, capsys, "--force")
    assert rc == 0
    assert (lab.api / ".claude").readlink() != mine
    [backup] = list(lab.api.glob(".claude.pre-ai-context.bak-*"))
    assert backup.readlink() == mine


def test_a_link_init_made_for_a_vault_that_moved_is_relinked_without_force(
    lab: Lab, capsys: pytest.CaptureFixture[str]
) -> None:
    _init(lab, capsys)
    stale = lab.root / "old-vault" / "Contexts" / "ledger-api" / ".claude"
    (lab.api / ".claude").unlink()
    (lab.api / ".claude").symlink_to(stale)

    rc, report = _init(lab, capsys)

    assert rc == 0 and _apps(report)["ledger-api"]["status"] == "installed"
    assert (lab.api / ".claude").resolve() == (lab.vault / "Contexts" / "ledger-api" / ".claude").resolve()


# --- the vault must ignore what Claude Code writes through a linked .claude -------------

GITIGNORE_FULL = (
    "Contexts/*/.claude/settings.local.json\nContexts/*/.claude/agent-memory/\nContexts/*/.claude/worktrees/\n"
)


def _git_vault(lab: Lab, gitignore: str | None) -> None:
    subprocess.run(["git", "init", "-q", str(lab.vault)], check=True, capture_output=True)
    if gitignore is not None:
        (lab.vault / ".gitignore").write_text(gitignore, encoding="utf-8")


def test_a_git_vault_that_ignores_what_claude_writes_has_nothing_to_warn_about(
    lab: Lab, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_vault(lab, GITIGNORE_FULL)
    before = _tree(lab.vault)

    rc, report = _init(lab, capsys)

    [vault] = report["vaults"]
    assert rc == 0 and vault["gitignore"] == "covered" and vault["warnings"] == []
    assert _tree(lab.vault) == before, "checking the ignore rules writes nothing in the vault"


def test_a_git_vault_that_does_not_ignore_it_gets_a_warning_that_blocks_nothing(
    lab: Lab, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_vault(lab, "Contexts/*/.claude/settings.local.json\n")

    rc, report = _init(lab, capsys)

    [vault] = report["vaults"]
    assert rc == 0 and vault["gitignore"] == "gaps"
    assert _apps(report)["ledger-api"]["status"] == "installed", "a warning does not hold the install back"
    [warning] = vault["warnings"]
    assert warning.startswith("W_CONTEXTS_GITIGNORE: ")
    assert "agent-memory" in warning and "worktrees" in warning and "settings.local.json" not in warning.split(";")[0]

    assert cli.main(["init", "--yes", "--search-dir", str(lab.work)]) == 0
    assert "W_CONTEXTS_GITIGNORE" in capsys.readouterr().out


def test_a_dry_run_warns_about_the_ignore_rules_too(lab: Lab, capsys: pytest.CaptureFixture[str]) -> None:
    _git_vault(lab, None)

    rc, report = _init(lab, capsys, "--dry-run")

    assert rc == 0 and report["vaults"][0]["gitignore"] == "gaps"


def test_a_vault_folder_that_is_not_a_git_repository_is_not_checked(
    lab: Lab, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, report = _init(lab, capsys)

    assert rc == 0 and report["vaults"][0]["gitignore"] == "not-applicable"
    assert report["vaults"][0]["warnings"] == []


def test_a_vault_folder_inside_another_repository_is_not_checked(
    lab: Lab, capsys: pytest.CaptureFixture[str]
) -> None:
    """The repository around it is not the vault's: its rules say nothing about what the vault publishes."""
    subprocess.run(["git", "init", "-q", str(lab.root)], check=True, capture_output=True)

    rc, report = _init(lab, capsys)

    assert rc == 0 and report["vaults"][0]["gitignore"] == "not-applicable"


def test_only_a_vault_that_is_linked_is_checked(lab: Lab, capsys: pytest.CaptureFixture[str]) -> None:
    """A copied package is written inside the checkout, never inside the vault."""
    _git_vault(lab, None)
    shutil.rmtree(lab.api)

    rc, report = _init(lab, capsys)

    assert rc == 0 and _apps(report)["ledger-web"]["status"] == "installed"
    assert report["vaults"][0]["gitignore"] == "not-applicable"


def test_agent_memory_in_a_copied_claude_is_personal(lab: Lab, capsys: pytest.CaptureFixture[str]) -> None:
    _init(lab, capsys)
    memory = lab.web / ".claude" / "agent-memory" / "x" / "MEMORY.md"
    memory.parent.mkdir(parents=True)
    memory.write_text("what the agent learned\n", encoding="utf-8")

    rc, report = _init(lab, capsys)
    assert rc == 0 and _apps(report)["ledger-web"]["status"] == "unchanged", "agent memory is not a drift"

    (lab.web / ".claude" / "agents" / "x.md").write_text("drifted\n", encoding="utf-8")
    rc, report = _init(lab, capsys, "--refresh")
    assert rc == 0 and _apps(report)["ledger-web"]["status"] == "installed"
    assert memory.read_text(encoding="utf-8") == "what the agent learned\n", "--refresh never deletes it"


def test_a_checkout_whose_origin_is_another_repository_is_refused(
    lab: Lab, capsys: pytest.CaptureFixture[str]
) -> None:
    subprocess.run(
        ["git", "-C", str(lab.api), "remote", "set-url", "origin", "git@example.test:team/something-else.git"],
        check=True,
    )

    rc, report = _init(lab, capsys, "--root", f"work={lab.work}")

    api = _apps(report)["ledger-api"]
    assert rc == 1
    assert api["status"] == "error"
    assert "is not the declared remote 'ledger-api'" in api["detail"]
    assert not (lab.api / "CLAUDE.md").exists()


def test_nothing_is_written_inside_a_served_vault(lab: Lab, capsys: pytest.CaptureFixture[str]) -> None:
    """A root pointed into the vault puts a checkout there: init refuses before writing."""
    inside = _checkout(lab.vault / "ledger" / "api", "ledger-api")
    before = _tree(lab.vault)

    rc, report = _init(lab, capsys, "--root", f"work={lab.vault}")

    api = _apps(report)["ledger-api"]
    assert rc == 1
    assert api["status"] == "error"
    assert api["detail"].startswith("E_WRITE_FENCED: ")
    assert not (inside / "CLAUDE.md").exists()
    assert _tree(lab.vault) == before


def test_a_config_home_inside_a_vault_is_fenced(
    lab: Lab, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(lab.vault / "config"))
    before = _tree(lab.vault)

    rc, report = _init(lab, capsys)

    assert rc == 1
    assert report["vaults"][0]["status"] == "error"
    assert report["vaults"][0]["detail"].startswith("E_WRITE_FENCED: ")
    assert _tree(lab.vault) == before


def test_a_vault_without_contexts_has_nothing_to_initialize(
    lab: Lab, capsys: pytest.CaptureFixture[str]
) -> None:
    shutil.rmtree(lab.vault / "Contexts")

    rc = cli.main(["init", "--yes", "--all", "--search-dir", str(lab.work)])
    out = capsys.readouterr().out

    assert rc == 0
    assert "no-contexts" in out
    assert "nothing to initialize" in out
    assert not lab.overlay.exists()


def _declare_no_apps(lab: Lab) -> None:
    (lab.vault / "Contexts" / "config.json").write_text(
        json.dumps({"repos": {"roots": [], "groups": {}, "entries": []}, "graph": {"targets": []}}), encoding="utf-8"
    )


def test_a_vault_that_declares_no_apps_is_reported_and_writes_nothing(
    lab: Lab, capsys: pytest.CaptureFixture[str]
) -> None:
    _declare_no_apps(lab)
    before = _tree(lab.root)

    rc, report = _init(lab, capsys)

    assert rc == 0 and report["ok"] is True
    [vault] = report["vaults"]
    assert (vault["slug"], vault["status"], vault["apps"], vault["roots"]) == ("contexts", "no-apps", [], [])
    assert report["files"]["repos_local"] == []
    assert not lab.overlay.exists()
    assert _tree(lab.root) == before

    assert cli.main(["init", "--yes", "--search-dir", str(lab.work)]) == 0
    out = capsys.readouterr().out
    assert "no-apps" in out and "declares no apps" in out
    assert "No registered vault declares Contexts/config.json" not in out


def test_doctor_does_not_ask_for_init_when_the_vault_declares_no_apps(
    lab: Lab, capsys: pytest.CaptureFixture[str]
) -> None:
    _declare_no_apps(lab)

    assert cli.main(["doctor", "--brief"]) == 0
    assert capsys.readouterr().out.startswith("knowledge doctor: ok, ")
    assert cli.main(["doctor"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["init"]["pending"] == []
    assert not any(w.startswith("W_CONTEXTS_INIT_PENDING") for w in report["warnings"])
    assert report["vaults"][0]["contexts"]["apps"] == 0


def test_a_malformed_declaration_is_still_a_coded_error_even_without_apps(
    lab: Lab, capsys: pytest.CaptureFixture[str]
) -> None:
    (lab.vault / "Contexts" / "config.json").write_text(
        json.dumps({"repos": {"roots": {}, "entries": []}}), encoding="utf-8"
    )

    rc, report = _init(lab, capsys)

    assert rc == 1
    assert report["vaults"][0]["status"] == "error"
    assert report["vaults"][0]["detail"].startswith("E_CONTEXTS_INVALID: ")


def test_a_broken_declaration_is_a_coded_error(lab: Lab, capsys: pytest.CaptureFixture[str]) -> None:
    (lab.vault / "Contexts" / "config.json").write_text("{not json", encoding="utf-8")

    rc, report = _init(lab, capsys)

    assert rc == 1
    assert report["vaults"][0]["status"] == "error"
    assert report["vaults"][0]["detail"].startswith("E_CONTEXTS_INVALID: ")


def test_a_root_nothing_locates_is_asked_for(lab: Lab) -> None:
    """``init`` in a terminal asks; an empty answer skips, a directory answers."""
    side = lab.root / "side"
    _checkout(side / "notes-cli", "notes-cli")
    questions: list[str] = []

    def answer(question: str) -> str | None:
        questions.append(question)
        return str(side)

    outcome = contexts.init_vault(
        contexts.VaultTarget("contexts", lab.vault),
        env={"HOME": str(lab.home), "XDG_CONFIG_HOME": str(lab.root / "xdg")},
        search_dirs=[lab.work],
        prompt=answer,
        selection=contexts.Selection(everything=True),
    )

    assert len(questions) == 1 and "'side'" in questions[0] and "notes-cli" in questions[0]
    side_choice = {root.id: root for root in outcome.roots}["side"]
    assert (side_choice.kind, side_choice.path) == ("prompt", str(side))
    assert {app.id: app.status for app in outcome.apps}["notes-cli"] == contexts.INSTALLED


# --- doctor ---------------------------------------------------------------------------


def test_doctor_reports_a_pending_init_without_failing(lab: Lab, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["doctor"])
    report = json.loads(capsys.readouterr().out)

    assert rc == 0, "a pending init is a warning: the vault serves all the same"
    assert report["ok"] is True
    assert report["init"]["pending"] == ["contexts"]
    [row] = report["vaults"]
    assert row["contexts"] == {
        "config": str(lab.vault / "Contexts" / "config.json"),
        "overlay": str(lab.overlay),
        "initialized": False,
        "apps": 3,
    }
    assert any(w.startswith("W_CONTEXTS_INIT_PENDING: contexts: ") for w in report["warnings"])

    assert cli.main(["doctor", "--brief"]) == 0
    line = capsys.readouterr().out
    assert line == "knowledge doctor: init pending for vault(s) contexts; run: cloudfrog-knowledge init\n"
    assert len(line.rstrip("\n").encode("utf-8")) <= cli.BRIEF_MAX_BYTES


def test_doctor_is_quiet_once_init_ran(lab: Lab, capsys: pytest.CaptureFixture[str]) -> None:
    _init(lab, capsys)

    assert cli.main(["doctor", "--brief"]) == 0
    assert capsys.readouterr().out.startswith("knowledge doctor: ok, ")
    assert cli.main(["doctor"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["init"]["pending"] == []
    assert report["vaults"][0]["contexts"]["initialized"] is True


def test_an_http_deployment_is_never_told_to_run_init(
    lab: Lab, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server behind HTTP has no checkouts: the fact is reported, the advice is not."""
    monkeypatch.setenv("MCP_TRANSPORT", "http")
    monkeypatch.setenv("MCP_API_KEY", "doctor-http-placeholder-not-a-credential")

    assert cli.main(["doctor"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["init"]["pending"] == ["contexts"]
    assert not any(w.startswith("W_CONTEXTS_INIT_PENDING") for w in report["warnings"])
    assert cli.main(["doctor", "--brief"]) == 0
    assert capsys.readouterr().out.startswith("knowledge doctor: ok, ")


def test_the_paths_a_machine_already_names_come_from_its_vaults_and_overlays(lab: Lab) -> None:
    """``init`` scans around what the vaults and the overlays already name."""
    overlay = lab.overlay
    overlay.parent.mkdir(parents=True)
    overlay.write_text(
        json.dumps({"roots": {"work": str(lab.work)}, "repos": {"ledger-api": str(lab.api)}}), encoding="utf-8"
    )
    spec = SimpleNamespace(slug="contexts", path=lab.vault)

    known = cli._known_paths([spec], {"XDG_CONFIG_HOME": str(lab.root / "xdg"), "HOME": str(lab.home)})

    assert known == [lab.vault, lab.work, lab.api]


# --- a real directory of the checkout receives the package entry by entry --------------


def _package(lab: Lab, app: str = "ledger-api") -> Path:
    return lab.vault / "Contexts" / app


def _real_claude(checkout: Path) -> dict[str, str]:
    """A real ``.claude`` holding what Claude Code and the person keep there; returns its tree."""
    claude = checkout / ".claude"
    (claude / "agent-memory").mkdir(parents=True)
    (claude / "agent-memory" / "notes.md").write_text("mine\n", encoding="utf-8")
    (claude / "worktrees" / "wt-1").mkdir(parents=True)
    (claude / "worktrees" / "wt-1" / "file.txt").write_text("worktree\n", encoding="utf-8")
    (claude / "settings.local.json").write_text('{"mine": true}\n', encoding="utf-8")
    return _tree(claude)


def _track(checkout: Path, *paths: str) -> None:
    subprocess.run(["git", "-C", str(checkout), "add", "--", *paths], check=True, capture_output=True)


def test_a_real_claude_directory_receives_one_link_per_package_entry(
    lab: Lab, capsys: pytest.CaptureFixture[str]
) -> None:
    package = _package(lab)
    (package / ".claude" / "settings.json").write_text("{}\n", encoding="utf-8")
    # Written through an earlier linked .claude into the vault: personal, never linked back out.
    (package / ".claude" / "settings.local.json").write_text('{"vault": true}\n', encoding="utf-8")
    personal = _real_claude(lab.api)

    rc, report = _init(lab, capsys)

    api = _apps(report)["ledger-api"]
    assert rc == 0 and api["status"] == "installed", api
    claude = lab.api / ".claude"
    assert claude.is_dir() and not claude.is_symlink(), "the checkout keeps its real directory"
    for child in ("agents", "settings.json"):
        link = claude / child
        assert link.is_symlink(), child
        assert not Path(link.readlink()).is_absolute()
        assert link.resolve() == (package / ".claude" / child).resolve()
    assert (claude / "agents" / "x.md").is_file()
    after = _tree(claude)
    assert {rel: after[rel] for rel in personal} == personal, "what the checkout holds is never touched"
    assert (claude / "settings.local.json").read_text(encoding="utf-8") == '{"mine": true}\n'
    for name in ("CLAUDE.md", ".mcp.json"):
        assert (lab.api / name).is_symlink()
    exclude = _exclude(lab.api)
    assert "/.claude/agents" in exclude and "/.claude/settings.json" in exclude
    assert "/.claude" not in exclude, "the real directory is the checkout's: only the links are ignored"
    assert "/.claude/settings.local.json" not in exclude


def test_a_real_untracked_entry_is_a_conflict_until_force_moves_it_aside(
    lab: Lab, capsys: pytest.CaptureFixture[str]
) -> None:
    _real_claude(lab.api)
    old = lab.api / ".claude" / "agents"
    old.mkdir()
    (old / "x.md").write_text("an old copy\n", encoding="utf-8")
    before = _tree(lab.api)

    rc, report = _init(lab, capsys)
    api = _apps(report)["ledger-api"]
    assert rc == 1 and api["status"] == "conflict"
    assert any(action.startswith(".claude/agents ") for action in api["actions"]), api["actions"]
    assert "--force" in api["detail"]
    assert {rel: digest for rel, digest in _tree(lab.api).items() if not rel.startswith(".git/")} == {
        rel: digest for rel, digest in before.items() if not rel.startswith(".git/")
    }, "a conflict installs nothing in that checkout"

    rc, report = _init(lab, capsys, "--force")
    assert rc == 0 and _apps(report)["ledger-api"]["status"] == "installed"
    assert old.is_symlink()
    [backup] = list((lab.api / ".claude").glob("agents.pre-ai-context.bak-*"))
    assert (backup / "x.md").read_text(encoding="utf-8") == "an old copy\n", "moved aside, never deleted"
    assert (lab.api / ".claude" / "settings.local.json").read_text(encoding="utf-8") == '{"mine": true}\n'


def test_an_entry_the_checkout_tracks_is_its_own_and_is_kept(lab: Lab, capsys: pytest.CaptureFixture[str]) -> None:
    package = _package(lab)
    for skill in ("nc-upgrade", "release"):
        (package / ".claude" / "skills" / skill).mkdir(parents=True)
        (package / ".claude" / "skills" / skill / "SKILL.md").write_text(f"vault {skill}\n", encoding="utf-8")
    (package / ".claude" / "settings.json").write_text('{"vault": true}\n', encoding="utf-8")
    ours = lab.api / ".claude" / "skills" / "nc-upgrade" / "SKILL.md"
    ours.parent.mkdir(parents=True)
    ours.write_text("the checkout's skill\n", encoding="utf-8")
    (lab.api / ".claude" / "settings.json").write_text('{"checkout": true}\n', encoding="utf-8")
    _track(lab.api, ".claude/skills/nc-upgrade/SKILL.md", ".claude/settings.json")

    rc, report = _init(lab, capsys)

    api = _apps(report)["ledger-api"]
    assert rc == 0 and api["status"] == "installed", api
    assert ours.read_text(encoding="utf-8") == "the checkout's skill\n"
    assert not ours.parent.is_symlink()
    assert (lab.api / ".claude" / "settings.json").read_text(encoding="utf-8") == '{"checkout": true}\n'
    release = lab.api / ".claude" / "skills" / "release"
    assert release.is_symlink(), "a tracked directory still receives the package entries it lacks"
    assert release.resolve() == (package / ".claude" / "skills" / "release").resolve()
    assert (lab.api / ".claude" / "agents").is_symlink()
    owned = [action for action in api["actions"] if "tracked by the checkout" in action]
    assert {action.split(" ", 1)[0] for action in owned} == {".claude/settings.json", ".claude/skills/nc-upgrade"}
    assert "owned by the checkout" in api["detail"]
    exclude = _exclude(lab.api)
    assert "/.claude/skills/release" in exclude
    assert "/.claude/skills/nc-upgrade" not in exclude and "/.claude/settings.json" not in exclude


def test_a_tracked_top_level_file_is_kept_and_an_untracked_one_still_conflicts(
    lab: Lab, capsys: pytest.CaptureFixture[str]
) -> None:
    for checkout in (lab.api, lab.web):
        (checkout / ".mcp.json").write_text('{"checkout": true}\n', encoding="utf-8")
        _track(checkout, ".mcp.json")

    rc, report = _init(lab, capsys)

    api = _apps(report)["ledger-api"]
    assert rc == 0 and api["status"] == "installed", api
    assert not (lab.api / ".mcp.json").is_symlink()
    assert (lab.api / ".mcp.json").read_text(encoding="utf-8") == '{"checkout": true}\n'
    assert ".mcp.json is tracked by the checkout" in " ".join(api["actions"])
    assert (lab.api / "CLAUDE.md").is_symlink() and (lab.api / ".claude").is_symlink()
    assert "/.mcp.json" not in _exclude(lab.api)

    assert _apps(report)["ledger-web"]["status"] == "installed", "copy mode keeps it too"
    assert (lab.web / "CLAUDE.md").is_file()
    assert (lab.web / ".mcp.json").read_text(encoding="utf-8") == '{"checkout": true}\n'


def test_linking_into_a_real_directory_is_idempotent(lab: Lab, capsys: pytest.CaptureFixture[str]) -> None:
    _real_claude(lab.api)
    (lab.api / ".mcp.json").write_text('{"checkout": true}\n', encoding="utf-8")
    _track(lab.api, ".mcp.json")
    _init(lab, capsys)
    before = _tree(lab.root)
    parked = 1_000_000_000 * 10**9
    os.utime(lab.api, ns=(parked, parked))

    rc, report = _init(lab, capsys)

    assert rc == 0 and _apps(report)["ledger-api"]["status"] == "unchanged"
    assert _tree(lab.root) == before
    assert lab.api.stat().st_mtime_ns == parked, "nothing to link, so the symlink probe does not run"
    assert _exclude(lab.api).count("/.claude/agents") == 1


def test_a_dry_run_into_a_real_directory_writes_nothing(lab: Lab, capsys: pytest.CaptureFixture[str]) -> None:
    _real_claude(lab.api)
    before = _tree(lab.root)

    rc, report = _init(lab, capsys, "--dry-run")

    api = _apps(report)["ledger-api"]
    assert rc == 0 and api["status"] == "would-install"
    assert any(action.startswith(".claude/agents -> ") for action in api["actions"]), api["actions"]
    assert _tree(lab.root) == before


def test_an_entry_link_init_made_for_a_vault_that_moved_is_relinked(
    lab: Lab, capsys: pytest.CaptureFixture[str]
) -> None:
    _real_claude(lab.api)
    _init(lab, capsys)
    agents = lab.api / ".claude" / "agents"
    agents.unlink()
    agents.symlink_to(lab.root / "old-vault" / "Contexts" / "ledger-api" / ".claude" / "agents")

    rc, report = _init(lab, capsys)

    assert rc == 0 and _apps(report)["ledger-api"]["status"] == "installed"
    assert agents.resolve() == (_package(lab) / ".claude" / "agents").resolve()


# --- doctor --client ---------------------------------------------------------------------

SERVER_ONLY = ("auth", "knowledge_home", "memory", "vector_backend")


def test_doctor_client_skips_the_server_checks(
    lab: Lab, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A machine that consumes a remote server has no embeddings key, index or bearer to check."""
    monkeypatch.setenv("KNOWLEDGE_EMBEDDINGS_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("MCP_TRANSPORT", "http")
    monkeypatch.delenv("MCP_API_KEY", raising=False)
    assert cli.main(["doctor"]) == 1, "the server doctor still checks what a server needs"
    capsys.readouterr()
    before = _tree(lab.root)

    rc = cli.main(["doctor", "--client"])
    report = json.loads(capsys.readouterr().out)

    assert rc == 0, report["problems"]
    assert report["mode"] == "client"
    assert all(key not in report for key in SERVER_ONLY)
    assert set(SERVER_ONLY) <= set(report["skipped"])
    [row] = report["vaults"]
    assert row["slug"] == "contexts" and row["readable"] is True
    assert "provider" not in row and "index" not in row
    assert row["contexts"]["initialized"] is False
    assert report["init"] == {"pending": ["contexts"], "command": "cloudfrog-knowledge init"}
    assert any(w.startswith("W_CONTEXTS_INIT_PENDING: contexts: ") for w in report["warnings"])
    assert _tree(lab.root) == before, "doctor writes nothing"

    assert cli.main(["doctor", "--client", "--brief"]) == 0
    assert capsys.readouterr().out == "knowledge doctor: init pending for vault(s) contexts; run: cloudfrog-knowledge init\n"


def test_doctor_client_reports_each_app_and_its_conflicts(lab: Lab, capsys: pytest.CaptureFixture[str]) -> None:
    (lab.api / "CLAUDE.md").write_text("# mine\n", encoding="utf-8")
    _init(lab, capsys)

    rc = cli.main(["doctor", "--client"])
    report = json.loads(capsys.readouterr().out)

    assert rc == 1
    apps = {app["id"]: app for app in report["vaults"][0]["contexts"]["checkouts"]}
    assert {app: apps[app]["status"] for app in APP_IDS} == {
        "ledger-api": "conflict",
        "ledger-web": "unchanged",
        "notes-cli": "skipped",
    }
    [problem] = report["problems"]
    assert problem["code"] == "E_CONTEXTS_CONFLICT" and problem["vault"] == "contexts"
    assert "ledger-api" in problem["message"] and "CLAUDE.md" in problem["message"]
    assert report["init"]["pending"] == []

    assert cli.main(["doctor", "--client", "--brief"]) == 1
    assert capsys.readouterr().out == (
        "knowledge doctor: E_CONTEXTS_CONFLICT (contexts); run: cloudfrog-knowledge doctor --client\n"
    )


def test_doctor_client_is_quiet_once_every_app_is_installed(lab: Lab, capsys: pytest.CaptureFixture[str]) -> None:
    _init(lab, capsys)

    assert cli.main(["doctor", "--client", "--brief"]) == 0
    assert capsys.readouterr().out == "knowledge doctor: ok, client, 1 vault(s), default contexts\n"


def test_doctor_client_counts_a_new_app_to_install_as_pending(lab: Lab, capsys: pytest.CaptureFixture[str]) -> None:
    _init(lab, capsys)
    (lab.api / "CLAUDE.md").unlink()

    rc = cli.main(["doctor", "--client"])
    report = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert report["init"]["pending"] == ["contexts"]
    apps = {app["id"]: app for app in report["vaults"][0]["contexts"]["checkouts"]}
    assert apps["ledger-api"]["status"] == "would-install"


def test_doctor_client_warns_about_a_checkout_without_origin(lab: Lab, capsys: pytest.CaptureFixture[str]) -> None:
    """A checkout with no origin remote cannot be proved wrong: the client is told, not failed."""
    _init(lab, capsys)
    subprocess.run(["git", "-C", str(lab.api), "remote", "remove", "origin"], check=True, capture_output=True)

    rc = cli.main(["doctor", "--client"])
    report = json.loads(capsys.readouterr().out)

    assert rc == 0, report["problems"]
    assert report["ok"] is True and report["problems"] == []
    apps = {app["id"]: app for app in report["vaults"][0]["contexts"]["checkouts"]}
    assert apps["ledger-api"]["status"] == "unverified", "the checkout row keeps what init reports"
    [warning] = [w for w in report["warnings"] if w.startswith("W_CONTEXTS_CHECKOUT_UNVERIFIED: ")]
    assert warning.startswith("W_CONTEXTS_CHECKOUT_UNVERIFIED: contexts: ledger-api (")
    assert "declares no origin remote" in warning
    assert report["init"]["pending"] == []

    assert cli.main(["doctor", "--client", "--brief"]) == 0
    assert capsys.readouterr().out == (
        "knowledge doctor: ok, client, 1 vault(s), default contexts, 1 checkout(s) unverified\n"
    )

    rc, init_report = _init(lab, capsys, "--dry-run")
    assert rc == 1 and _apps(init_report)["ledger-api"]["status"] == "unverified", "init still holds it back"


def test_doctor_client_fails_a_checkout_whose_origin_is_another_repository(
    lab: Lab, capsys: pytest.CaptureFixture[str]
) -> None:
    _init(lab, capsys)
    subprocess.run(
        ["git", "-C", str(lab.api), "remote", "set-url", "origin", "git@example.test:team/something-else.git"],
        check=True,
        capture_output=True,
    )

    rc = cli.main(["doctor", "--client"])
    report = json.loads(capsys.readouterr().out)

    assert rc == 1
    [problem] = report["problems"]
    assert problem["code"] == "E_CONTEXTS_CHECKOUT_INVALID" and "ledger-api" in problem["message"]
    assert not any(w.startswith("W_CONTEXTS_CHECKOUT_UNVERIFIED") for w in report["warnings"])


def test_doctor_client_fails_a_plain_directory_where_a_checkout_is_declared(
    lab: Lab, capsys: pytest.CaptureFixture[str]
) -> None:
    _init(lab, capsys)
    shutil.rmtree(lab.api / ".git")

    rc = cli.main(["doctor", "--client"])
    report = json.loads(capsys.readouterr().out)

    assert rc == 1
    assert [problem["code"] for problem in report["problems"]] == ["E_CONTEXTS_CHECKOUT_INVALID"]


def test_doctor_client_flags_a_declaration_that_does_not_load(
    lab: Lab, capsys: pytest.CaptureFixture[str]
) -> None:
    (lab.vault / "Contexts" / "config.json").write_text("{not json", encoding="utf-8")

    rc = cli.main(["doctor", "--client"])
    report = json.loads(capsys.readouterr().out)

    assert rc == 1
    assert [problem["code"] for problem in report["problems"]] == ["E_CONTEXTS_INVALID"]


def test_doctor_client_flags_a_vault_that_is_not_there(
    lab: Lab, capsys: pytest.CaptureFixture[str]
) -> None:
    shutil.rmtree(lab.vault)

    rc = cli.main(["doctor", "--client"])
    report = json.loads(capsys.readouterr().out)

    assert rc == 1
    assert report["vaults"][0]["readable"] is False
    assert report["problems"][0]["vault"] == "contexts"


# --- the command a hint names ------------------------------------------------------------


def test_every_hint_names_the_command_label_when_one_is_set(
    lab: Lab, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KNOWLEDGE_COMMAND_LABEL", "Contexts/scripts/{command}.sh")

    assert cli.main(["doctor", "--brief"]) == 0
    assert capsys.readouterr().out == "knowledge doctor: init pending for vault(s) contexts; run: Contexts/scripts/init.sh\n"
    assert cli.main(["doctor"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["init"]["command"] == "Contexts/scripts/init.sh"
    assert all("cloudfrog-knowledge" not in w for w in report["warnings"])

    (lab.api / "CLAUDE.md").write_text("# mine\n", encoding="utf-8")
    rc, report = _init(lab, capsys)
    assert rc == 1
    assert "Contexts/scripts/init.sh --force" in _apps(report)["ledger-api"]["detail"]
    readme = "\n".join(json.loads(lab.overlay.read_text(encoding="utf-8"))["_README"])
    assert "Contexts/scripts/init.sh" in readme and "cloudfrog-knowledge" not in readme

    assert cli.main(["doctor", "--client", "--brief"]) == 1
    assert capsys.readouterr().out.endswith("; run: Contexts/scripts/doctor.sh\n")

    rc = cli.main(["init", "--yes", "--search-dir", str(lab.work), "--dry-run"])
    assert capsys.readouterr().out.startswith("Contexts/scripts/init.sh (dry run: nothing is written)")


def test_a_label_without_a_placeholder_is_followed_by_the_subcommand(
    lab: Lab, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KNOWLEDGE_COMMAND_LABEL", "acme-knowledge")

    assert cli.main(["doctor", "--client", "--brief"]) == 0
    assert capsys.readouterr().out == "knowledge doctor: init pending for vault(s) contexts; run: acme-knowledge init\n"
    assert contexts.command_label({}, "doctor") == "cloudfrog-knowledge doctor"
    assert contexts.command_label({"KNOWLEDGE_COMMAND_LABEL": "  "}, "init") == "cloudfrog-knowledge init"


# --- which checkouts this machine selects: the apply list ------------------------------------


def _overlay(lab: Lab) -> dict:
    return json.loads(lab.overlay.read_text(encoding="utf-8"))


def _set_apply(lab: Lab, apply: list[str] | None) -> None:
    """Rewrite the ``apply`` list of the overlay by hand; ``None`` removes it, as in a file older than the list."""
    payload = _overlay(lab)
    if apply is None:
        payload.pop("apply", None)
    else:
        payload["apply"] = apply
    lab.overlay.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _statuses(report: dict) -> dict[str, str]:
    return {app_id: app["status"] for app_id, app in _apps(report).items()}


def _installed(checkout: Path) -> bool:
    return any((checkout / name).exists() or (checkout / name).is_symlink() for name in contexts.MANAGED)


def test_a_new_machine_without_flags_installs_nothing_and_says_how_to_choose(
    lab: Lab, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, report = _run(lab, capsys)

    assert rc == 0 and report["ok"] is True
    assert _statuses(report) == {"ledger-api": "not-selected", "ledger-web": "not-selected", "notes-cli": "skipped"}
    assert "--repo ledger-api" in _apps(report)["ledger-api"]["detail"]
    selection = report["vaults"][0]["selection"]
    assert selection == {
        "apply": None,
        "previous": None,
        "source": "unset",
        "added": [],
        "removed": [],
        "unselected": ["ledger-api", "ledger-web"],
    }
    assert "apply" not in _overlay(lab), "nobody chose, so no choice is recorded"
    assert not _installed(lab.api) and not _installed(lab.web)
    assert report["files"]["checkouts"] == []

    assert cli.main(["init", "--yes", "--search-dir", str(lab.work)]) == 0
    out = capsys.readouterr().out
    assert "apply  not set" in out
    assert "on this machine and not selected: ledger-api, ledger-web" in out
    assert "cloudfrog-knowledge init --repo ID" in out and "cloudfrog-knowledge init --all" in out


def test_without_a_terminal_a_new_machine_is_not_asked(lab: Lab, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["init", "--json", "--search-dir", str(lab.work)])
    report = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert report["vaults"][0]["selection"]["source"] == "unset"
    assert not _installed(lab.api)


def _init_with_answer(lab: Lab, answer: str | None) -> tuple[contexts.VaultOutcome, list[str]]:
    questions: list[str] = []

    def ask(question: str) -> str | None:
        if question.startswith("Directory of root"):
            return None
        questions.append(question)
        return answer

    outcome = contexts.init_vault(
        contexts.VaultTarget("contexts", lab.vault),
        env={"HOME": str(lab.home), "XDG_CONFIG_HOME": str(lab.root / "xdg")},
        search_dirs=[lab.work],
        prompt=ask,
    )
    return outcome, questions


def test_a_new_machine_in_a_terminal_is_asked_which_checkouts_to_install(lab: Lab) -> None:
    outcome, questions = _init_with_answer(lab, "ledger-api")

    [question] = questions
    assert "ledger-api, ledger-web" in question and "notes-cli" not in question
    assert outcome.selection is not None
    assert (outcome.selection.source, outcome.selection.apply) == ("prompt", ["ledger-api"])
    assert {app.id: app.status for app in outcome.apps}["ledger-api"] == contexts.INSTALLED
    assert {app.id: app.status for app in outcome.apps}["ledger-web"] == contexts.NOT_SELECTED
    assert _overlay(lab)["apply"] == ["ledger-api"]


@pytest.mark.parametrize(
    ("answer", "apply"),
    [("all", ["ledger-api", "ledger-web"]), ("none", []), ("ledger-web, ledger-api", ["ledger-api", "ledger-web"])],
)
def test_every_answer_of_the_prompt_is_recorded(lab: Lab, answer: str, apply: list[str]) -> None:
    outcome, _questions = _init_with_answer(lab, answer)

    assert outcome.selection is not None and outcome.selection.apply == apply
    assert _overlay(lab)["apply"] == apply


def test_an_empty_or_wrong_answer_leaves_the_choice_for_later(lab: Lab) -> None:
    outcome, _questions = _init_with_answer(lab, None)
    assert outcome.selection is not None and outcome.selection.apply is None
    assert "apply" not in _overlay(lab)

    outcome, questions = _init_with_answer(lab, "nope")
    assert len(questions) == 3 and questions[1].startswith("nope is not one of them.")
    assert outcome.selection is not None and outcome.selection.apply is None


def test_a_dry_run_never_asks(lab: Lab) -> None:
    outcome = contexts.init_vault(
        contexts.VaultTarget("contexts", lab.vault),
        env={"HOME": str(lab.home), "XDG_CONFIG_HOME": str(lab.root / "xdg")},
        search_dirs=[lab.work],
        prompt=lambda question: None if question.startswith("Directory of root") else pytest.fail("a dry run asked"),
        dry_run=True,
    )
    assert outcome.selection is not None and outcome.selection.source == "unset"


def test_repo_installs_only_that_checkout_and_the_choice_sticks(lab: Lab, capsys: pytest.CaptureFixture[str]) -> None:
    rc, report = _run(lab, capsys, "--repo", "ledger-api")

    assert rc == 0
    assert _statuses(report) == {"ledger-api": "installed", "ledger-web": "not-selected", "notes-cli": "skipped"}
    assert report["vaults"][0]["selection"]["added"] == ["ledger-api"]
    assert _overlay(lab)["apply"] == ["ledger-api"]
    assert (lab.api / "CLAUDE.md").is_symlink() and not _installed(lab.web)

    before = _tree(lab.root)
    rc, report = _run(lab, capsys)
    assert rc == 0
    assert _statuses(report) == {"ledger-api": "unchanged", "ledger-web": "not-selected", "notes-cli": "skipped"}
    assert report["vaults"][0]["overlay_action"] == "unchanged"
    assert report["vaults"][0]["selection"]["source"] == "overlay"
    assert _tree(lab.root) == before

    rc, report = _run(lab, capsys, "--repo", "contexts:ledger-web")
    assert rc == 0 and _overlay(lab)["apply"] == ["ledger-api", "ledger-web"]
    assert _statuses(report)["ledger-web"] == "installed"


def test_all_selects_every_checkout_on_the_machine_with_a_package(
    lab: Lab, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, report = _run(lab, capsys, "--all")

    assert rc == 0
    assert _overlay(lab)["apply"] == ["ledger-api", "ledger-web"], "a checkout that is not here is not added"
    assert report["vaults"][0]["selection"]["source"] == "flags"


def test_a_checkout_can_be_selected_before_it_is_cloned(lab: Lab, capsys: pytest.CaptureFixture[str]) -> None:
    rc, report = _run(lab, capsys, "--repo", "notes-cli")

    assert rc == 0
    assert _overlay(lab)["apply"] == ["notes-cli"]
    assert _statuses(report)["notes-cli"] == "skipped"


@pytest.mark.parametrize("flag", ["--repo", "--drop"])
@pytest.mark.parametrize("value", ["nope", "other:ledger-api"])
def test_an_id_no_vault_declares_writes_nothing(
    lab: Lab, capsys: pytest.CaptureFixture[str], flag: str, value: str
) -> None:
    before = _tree(lab.root)

    rc = cli.main(["init", "--yes", "--search-dir", str(lab.work), flag, value])

    err = capsys.readouterr().err
    assert rc == 2
    assert err.startswith(f"E_CONTEXTS_REPO_UNKNOWN: {flag} ")
    assert _tree(lab.root) == before


def test_a_malformed_repo_flag_is_a_usage_error(lab: Lab, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["init", "--repo", "contexts:"])
    assert exc.value.code == 2
    assert "is not [SLUG:]ID" in capsys.readouterr().err


def test_drop_removes_the_id_and_only_the_links_into_its_package(
    lab: Lab, capsys: pytest.CaptureFixture[str]
) -> None:
    _init(lab, capsys)
    (lab.api / "notes.txt").write_text("mine\n", encoding="utf-8")
    elsewhere = lab.root / "elsewhere.md"
    elsewhere.write_text("# not a package\n", encoding="utf-8")
    (lab.api / "OTHER.md").symlink_to(elsewhere)
    vault_before = _tree(lab.vault)
    exclude_before = _exclude(lab.api)

    rc, report = _run(lab, capsys, "--drop", "ledger-api")

    assert rc == 0
    assert _overlay(lab)["apply"] == ["ledger-web"]
    app = _apps(report)["ledger-api"]
    assert app["status"] == "not-selected"
    assert sorted(app["actions"]) == [".claude link removed", ".mcp.json link removed", "CLAUDE.md link removed"]
    assert not _installed(lab.api)
    assert (lab.api / "notes.txt").read_text(encoding="utf-8") == "mine\n"
    assert (lab.api / "OTHER.md").is_symlink(), "a link that does not point into the package stays"
    assert _exclude(lab.api) == exclude_before
    assert _tree(lab.vault) == vault_before
    assert report["vaults"][0]["selection"]["removed"] == ["ledger-api"]
    assert report["vaults"][0]["warnings"] == []

    rc, report = _run(lab, capsys)
    assert rc == 0 and _statuses(report)["ledger-api"] == "not-selected"


def test_drop_inside_a_real_claude_removes_the_entry_links_and_keeps_the_directory(
    lab: Lab, capsys: pytest.CaptureFixture[str]
) -> None:
    (lab.api / ".claude").mkdir()
    (lab.api / ".claude" / "settings.local.json").write_text("{}\n", encoding="utf-8")
    _init(lab, capsys)
    assert (lab.api / ".claude" / "agents").is_symlink()

    rc, report = _run(lab, capsys, "--drop", "ledger-api")

    assert rc == 0
    assert ".claude/agents link removed" in _apps(report)["ledger-api"]["actions"]
    assert (lab.api / ".claude").is_dir() and not (lab.api / ".claude").is_symlink()
    assert not (lab.api / ".claude" / "agents").exists()
    assert (lab.api / ".claude" / "settings.local.json").is_file()


def test_drop_leaves_the_files_of_a_copy(lab: Lab, capsys: pytest.CaptureFixture[str]) -> None:
    _init(lab, capsys)

    rc, report = _run(lab, capsys, "--drop", "ledger-web")

    assert rc == 0
    app = _apps(report)["ledger-web"]
    assert app["actions"] == [] and "the copied files stay" in app["detail"]
    assert (lab.web / "CLAUDE.md").is_file()
    assert _overlay(lab)["apply"] == ["ledger-api"]


def test_a_dry_run_shows_the_selection_changes_and_writes_nothing(
    lab: Lab, capsys: pytest.CaptureFixture[str]
) -> None:
    before = _tree(lab.root)
    rc, report = _run(lab, capsys, "--dry-run", "--repo", "ledger-api")
    assert rc == 0
    vault = report["vaults"][0]
    assert vault["overlay_action"] == "would-create"
    assert vault["selection"]["apply"] == ["ledger-api"] and vault["selection"]["added"] == ["ledger-api"]
    assert _statuses(report)["ledger-api"] == "would-install"
    assert _tree(lab.root) == before

    _init(lab, capsys)
    before = _tree(lab.root)
    rc, report = _run(lab, capsys, "--dry-run", "--drop", "ledger-api")
    assert rc == 0
    vault = report["vaults"][0]
    assert vault["overlay_action"] == "would-update"
    assert vault["selection"]["removed"] == ["ledger-api"]
    assert "CLAUDE.md link would be removed" in _apps(report)["ledger-api"]["actions"]
    assert _tree(lab.root) == before

    assert cli.main(["init", "--yes", "--search-dir", str(lab.work), "--dry-run", "--drop", "ledger-api"]) == 0
    assert "would remove: ledger-api" in capsys.readouterr().out


def test_a_file_without_apply_adopts_the_installed_checkouts(lab: Lab, capsys: pytest.CaptureFixture[str]) -> None:
    """The first run of this version keeps every machine as it was: what is installed stays selected."""
    _init(lab, capsys)
    _set_apply(lab, None)
    checkouts_before = {"api": _tree(lab.api), "web": _tree(lab.web)}

    rc, report = _run(lab, capsys, "--dry-run")
    assert rc == 0
    assert report["vaults"][0]["overlay_action"] == "would-update"
    assert report["vaults"][0]["selection"]["source"] == "adopted"
    assert "apply" not in _overlay(lab), "the dry run of a migration writes nothing"

    rc, report = _run(lab, capsys)

    assert rc == 0
    selection = report["vaults"][0]["selection"]
    assert (selection["source"], selection["apply"], selection["previous"]) == (
        "adopted",
        ["ledger-api", "ledger-web"],
        None,
    )
    assert _statuses(report) == {"ledger-api": "unchanged", "ledger-web": "unchanged", "notes-cli": "skipped"}
    assert _overlay(lab)["apply"] == ["ledger-api", "ledger-web"]
    assert {"api": _tree(lab.api), "web": _tree(lab.web)} == checkouts_before, "nobody's setup changes"


def test_adoption_takes_only_what_is_installed(lab: Lab, capsys: pytest.CaptureFixture[str]) -> None:
    _run(lab, capsys, "--repo", "ledger-api")
    _set_apply(lab, None)

    rc, report = _run(lab, capsys)

    assert rc == 0
    assert _overlay(lab)["apply"] == ["ledger-api"]
    assert _statuses(report)["ledger-web"] == "not-selected"
    assert not _installed(lab.web)


def test_ids_the_registry_no_longer_declares_are_dropped_with_a_notice(
    lab: Lab, capsys: pytest.CaptureFixture[str]
) -> None:
    _init(lab, capsys)
    _set_apply(lab, ["gone", "ledger-api", "ledger-web"])

    rc, report = _run(lab, capsys)

    assert rc == 0
    assert "apply.gone" in report["vaults"][0]["detail"]
    assert _overlay(lab)["apply"] == ["ledger-api", "ledger-web"]


def test_an_apply_that_is_not_a_list_of_ids_is_a_coded_error(lab: Lab, capsys: pytest.CaptureFixture[str]) -> None:
    _init(lab, capsys)
    _set_apply(lab, "ledger-api")  # type: ignore[arg-type]

    rc, report = _run(lab, capsys)

    assert rc == 1
    assert report["vaults"][0]["detail"].startswith("E_CONTEXTS_INVALID: ")


def test_a_linked_checkout_that_is_not_selected_is_a_warning(lab: Lab, capsys: pytest.CaptureFixture[str]) -> None:
    _init(lab, capsys)
    _set_apply(lab, [])

    rc, report = _run(lab, capsys)

    assert rc == 0
    assert _statuses(report)["ledger-api"] == "not-selected"
    assert (lab.api / "CLAUDE.md").is_symlink(), "init does not remove links it was not asked to remove"
    [warning] = report["vaults"][0]["warnings"]
    assert warning.startswith("W_CONTEXTS_LINKED_NOT_SELECTED: contexts: ledger-api (")
    assert "--repo ledger-api" in warning and "--drop ledger-api" in warning

    rc = cli.main(["doctor", "--client"])
    doctor = json.loads(capsys.readouterr().out)
    assert rc == 0 and doctor["problems"] == []
    assert [w for w in doctor["warnings"] if w.startswith("W_CONTEXTS_LINKED_NOT_SELECTED: ")] == [warning]
    assert cli.main(["doctor", "--client", "--brief"]) == 0
    assert capsys.readouterr().out == (
        "knowledge doctor: ok, client, 1 vault(s), default contexts, 1 checkout(s) linked and not selected\n"
    )


def test_doctor_client_reports_an_unselected_checkout_as_information(
    lab: Lab, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(lab, capsys, "--repo", "ledger-api")

    rc = cli.main(["doctor", "--client"])
    report = json.loads(capsys.readouterr().out)

    assert rc == 0 and report["problems"] == [] and report["init"]["pending"] == []
    contexts_row = report["vaults"][0]["contexts"]
    apps = {app["id"]: app["status"] for app in contexts_row["checkouts"]}
    assert apps == {"ledger-api": "unchanged", "ledger-web": "not-selected", "notes-cli": "skipped"}
    assert contexts_row["selection"]["apply"] == ["ledger-api"]
    assert contexts_row["selection"]["unselected"] == ["ledger-web"]
    assert not any(w.startswith("W_CONTEXTS_") for w in report["warnings"])
    assert cli.main(["doctor", "--client", "--brief"]) == 0
    assert capsys.readouterr().out == "knowledge doctor: ok, client, 1 vault(s), default contexts\n"


def test_doctor_client_counts_a_selected_checkout_without_links_as_pending(
    lab: Lab, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(lab, capsys, "--repo", "ledger-api")
    _set_apply(lab, ["ledger-api", "ledger-web"])

    rc = cli.main(["doctor", "--client"])
    report = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert report["init"]["pending"] == ["contexts"]
    apps = {app["id"]: app["status"] for app in report["vaults"][0]["contexts"]["checkouts"]}
    assert apps["ledger-web"] == "would-install"
    assert any(w.startswith("W_CONTEXTS_INIT_PENDING: contexts: checkout(s) ledger-web") for w in report["warnings"])


def test_doctor_client_warns_when_the_file_has_no_selection(lab: Lab, capsys: pytest.CaptureFixture[str]) -> None:
    _init(lab, capsys)
    _set_apply(lab, None)
    before = _tree(lab.root)

    rc = cli.main(["doctor", "--client"])
    report = json.loads(capsys.readouterr().out)

    assert rc == 0 and report["ok"] is True
    [warning] = [w for w in report["warnings"] if w.startswith("W_CONTEXTS_SELECTION_MISSING: ")]
    assert warning.startswith(f"W_CONTEXTS_SELECTION_MISSING: contexts: {lab.overlay} has no 'apply' list")
    assert report["init"]["pending"] == [], "the installed checkouts are adopted, so nothing is pending"
    assert cli.main(["doctor", "--client", "--brief"]) == 0
    assert capsys.readouterr().out == (
        "knowledge doctor: ok, client, 1 vault(s), default contexts, selection missing in 1 vault(s)\n"
    )
    assert _tree(lab.root) == before, "doctor writes nothing"

    _run(lab, capsys)
    assert cli.main(["doctor", "--client"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert not any(w.startswith("W_CONTEXTS_SELECTION_MISSING") for w in report["warnings"])


def test_the_overlay_readme_explains_apply(lab: Lab, capsys: pytest.CaptureFixture[str]) -> None:
    _init(lab, capsys)

    readme = "\n".join(_overlay(lab)["_README"])

    assert "apply" in readme and "--repo ID" in readme and "--drop ID" in readme
    assert "Graph targets do not read it" in readme


def test_the_selection_does_not_change_what_the_registry_resolves(
    lab: Lab, capsys: pytest.CaptureFixture[str]
) -> None:
    """Graph targets and the server read the registry; ``apply`` only decides where packages go."""
    _run(lab, capsys, "--repo", "ledger-api")
    env = {"HOME": str(lab.home), "XDG_CONFIG_HOME": str(lab.root / "xdg")}

    registry = repos.load_registry(vault_dir=lab.vault, vault="contexts", env=env)

    statuses = {resolution.entry.id: resolution.status for resolution in registry.resolve_all()}
    assert statuses["ledger-api"] == statuses["ledger-web"] == repos.STATUS_RESOLVED
