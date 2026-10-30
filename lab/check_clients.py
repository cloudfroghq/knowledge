"""Checks of the lab clients: ``cloudfrog-knowledge init`` end to end, on the host.

A client is a person with a vault cloned locally who talks to a remote server:
``alice`` (tenant acme) and ``bob`` (tenant globex). ``lab/seed.sh --clients``
builds each one under ``lab/.work/clients/<name>/`` with its own ``HOME`` and
``XDG_CONFIG_HOME``, its own ``vaults.yaml``, a local copy of each vault (a
clone of the bare repository for a git vault, the folder itself for the folder
vault) and the fake application checkouts that vault declares::

    alice   acme-web and acme-deploy are checked out, acme-api is not; a tree of
            about 2,000 directories (60 of them git checkouts) surrounds them
    bob     globex-core is checked out; his second vault, globex-notes, declares
            no apps (an empty registry), so there is nothing to link for it

Run it through ``lab/run.sh``. It starts by seeding the clients again, so every
run begins before ``init`` and can be repeated. Each client runs the same
story: ``doctor --brief`` asks for ``init``, ``init --dry-run`` writes nothing,
``init`` links the packages, a second ``init`` changes nothing, and ``doctor
--brief`` goes quiet. The process gets only ``PATH``, ``HOME`` and
``XDG_CONFIG_HOME`` of the client, the shared uv cache and an embeddings
provider that needs no key: nothing of the developer's own environment or home
reaches it.
"""

from __future__ import annotations

import hashlib
import json
import os
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from report import Row, print_table

LAB = Path(__file__).resolve().parent
ROOT = LAB.parent
WORK = LAB / ".work"
CLIENTS = WORK / "clients"
SEED = LAB / "seed.sh"
#: What a context package puts into a checkout.
LINKED = (".claude", "CLAUDE.md", ".mcp.json")
SCAN_RUNS = 5


@dataclass(frozen=True)
class App:
    """An application a vault declares; ``root`` and ``path`` are the ones of its Contexts/config.json."""

    id: str
    vault: str
    root: str
    path: str
    present: bool = True


@dataclass(frozen=True)
class Client:
    name: str
    vaults: dict[str, Path]  # slug -> the directory of this client's copy of the vault
    apps: tuple[App, ...]
    noisy: bool = False  # surrounded by the synthetic tree that times the scan

    @property
    def home(self) -> Path:
        return CLIENTS / self.name

    @property
    def xdg(self) -> Path:
        return self.home / ".config"

    @property
    def vaults_yaml(self) -> Path:
        return self.xdg / "knowledge" / "vaults.yaml"

    @property
    def projects(self) -> Path:
        return self.home / "Projects"

    def checkout(self, app: App) -> Path:
        return self.projects / app.root / app.path

    def overlay(self, slug: str) -> Path:
        return self.xdg / "knowledge" / slug / "repos.local.json"

    @property
    def vaults_with_apps(self) -> list[str]:
        return [slug for slug in self.vaults if any(app.vault == slug for app in self.apps)]

    @property
    def vaults_without_apps(self) -> list[str]:
        return [slug for slug in self.vaults if slug not in self.vaults_with_apps]


def clients() -> list[Client]:
    alice, bob = CLIENTS / "alice", CLIENTS / "bob"
    return [
        Client(
            "alice",
            {"acme-handbook": alice / "Vaults" / "acme-handbook", "acme-ops": WORK / "vaults" / "acme-ops"},
            (
                App("acme-web", "acme-handbook", "acme", "web"),
                App("acme-api", "acme-handbook", "acme", "services/api", present=False),
                App("acme-deploy", "acme-ops", "acme-infra", "deploy"),
            ),
            noisy=True,
        ),
        Client(
            "bob",
            {"globex-docs": bob / "Vaults" / "globex-docs", "globex-notes": WORK / "vaults" / "globex-notes"},
            (App("globex-core", "globex-docs", "globex", "core"),),
        ),
    ]


# --- running the CLI as the client --------------------------------------------


@dataclass(frozen=True)
class Run:
    code: int
    out: str
    err: str
    seconds: float

    def report(self) -> dict[str, Any]:
        return json.loads(self.out)


def uv_cache() -> str:
    done = subprocess.run(["uv", "cache", "dir"], capture_output=True, text=True, check=True)
    return done.stdout.strip()


def client_env(client: Client, cache: str) -> dict[str, str]:
    """The whole environment of the client's process; the developer's own never leaks in."""
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(client.home),
        "XDG_CONFIG_HOME": str(client.xdg),
        "UV_CACHE_DIR": cache,
        "KNOWLEDGE_EMBEDDINGS_PROVIDER": "bag-of-words",
        "OPENAI_API_KEY": "",
        "VAULT_PATH": "",
    }


def cli(client: Client, cache: str, *args: str) -> Run:
    command = ["uv", "run", "--no-sync", "--project", str(ROOT), "cloudfrog-knowledge", *args]
    started = time.perf_counter()
    done = subprocess.run(
        command, cwd=client.home, env=client_env(client, cache), capture_output=True, text=True, timeout=300
    )
    return Run(done.returncode, done.stdout, done.stderr, time.perf_counter() - started)


def init_args(client: Client, *extra: str) -> list[str]:
    return [
        "init",
        "--vaults",
        str(client.vaults_yaml),
        "--yes",
        # Every present checkout receives its package: the lab client chooses all of them.
        "--all",
        "--search-dir",
        str(client.projects),
        "--json",
        *extra,
    ]


# --- looking at the disk -------------------------------------------------------


def snapshot(root: Path) -> dict[str, tuple[Any, ...]]:
    """Every entry under ``root`` with its kind, mtime and content hash (or link target)."""
    entries: dict[str, tuple[Any, ...]] = {}
    for current, directories, files in os.walk(root):
        directories.sort()
        for name in [*directories, *sorted(files)]:
            path = Path(current) / name
            stat = path.lstat()
            key = str(path.relative_to(root))
            if path.is_symlink():
                entries[key] = ("link", os.readlink(path), stat.st_mtime_ns)
            elif path.is_dir():
                entries[key] = ("dir", stat.st_mtime_ns)
            else:
                entries[key] = ("file", stat.st_size, stat.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
    return entries


def changed(before: dict[str, tuple[Any, ...]], after: dict[str, tuple[Any, ...]]) -> list[str]:
    return sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))


def digest(root: Path) -> str:
    return hashlib.sha256(repr(sorted(snapshot(root).items())).encode()).hexdigest()


def count_directories(root: Path) -> int:
    """Directories a scan can enter: the root and every one not dot-prefixed below it."""
    total = 1
    for _, directories, _ in os.walk(root):
        directories[:] = [d for d in directories if not d.startswith(".")]
        total += len(directories)
    return total


def origin_of(path: Path) -> str:
    done = subprocess.run(
        ["git", "-C", str(path), "config", "--get", "remote.origin.url"], capture_output=True, text=True
    )
    return done.stdout.strip()


# --- the story of one client ---------------------------------------------------


def scenario(client: Client, cache: str) -> tuple[list[Row], list[str]]:
    rows: list[Row] = []
    notes: list[str] = []

    def add(name: str, ok: bool, detail: str) -> None:
        rows.append(Row(f"{client.name}: {name}", bool(ok), detail))

    present = [a for a in client.apps if a.present]
    absent = [a for a in client.apps if not a.present]
    slugs = list(client.vaults)
    vault_before = {slug: digest(path) for slug, path in client.vaults.items()}
    baseline = cli(client, cache, "init", "--help").seconds

    # 1. Before init, doctor asks for it.
    before = cli(client, cache, "doctor", "--brief", "--vaults", str(client.vaults_yaml))
    wanted = f"init pending for vault(s) {', '.join(client.vaults_with_apps)}; run:"
    add(
        'doctor --brief says "init pending" before init',
        before.code == 0 and wanted in before.out,
        before.out.strip()[:100],
    )

    # 2. A dry run reports the plan and writes nothing, not even a config directory.
    frozen = snapshot(client.home)
    dry = cli(client, cache, *init_args(client, "--dry-run"))
    try:
        planned = {a["id"]: a["status"] for v in dry.report()["vaults"] for a in v["apps"]}
        dry_ok = dry.code == 0 and dry.report()["dry_run"] is True
    except (ValueError, KeyError):
        planned, dry_ok = {}, False
    touched = changed(frozen, snapshot(client.home))
    expected_plan = {a.id: ("would-install" if a.present else "skipped") for a in client.apps}
    add(
        "init --dry-run plans the links and writes nothing",
        dry_ok and not touched and planned == expected_plan,
        f"plan={planned} changed={len(touched)}"
        + (f" {touched[:2]}" if touched else "")
        + (f" err={dry.err.strip()[:60]}" if not dry_ok else ""),
    )

    # 3. The real run.
    first = cli(client, cache, *init_args(client))
    try:
        report = first.report()
        run_ok = first.code == 0 and report["ok"] is True
    except (ValueError, KeyError):
        report, run_ok = {"vaults": []}, False
    outcomes = {a["id"]: a["status"] for v in report["vaults"] for a in v["apps"]}
    expected_outcome = {a.id: ("installed" if a.present else "skipped") for a in client.apps}
    add(
        "init exits 0 and reports every present app installed",
        run_ok and outcomes == expected_outcome,
        f"rc={first.code} {outcomes}" + ("" if run_ok else f" err={first.err.strip()[:80]}"),
    )

    # 4. Where this machine's locations are written.
    overlays: list[str] = []
    for slug in client.vaults_with_apps:
        vault = client.vaults[slug]
        path = client.overlay(slug)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            roots = data["roots"]
            good = (
                data["slug"] == slug
                and Path(data["vault"]) == vault
                and bool(roots)
                and all(Path(d).is_dir() for d in roots.values())
            )
        except (OSError, ValueError, KeyError):
            good = False
        overlays.append(f"{slug}={'ok' if good else 'BAD'}")
    add(
        "init writes <xdg>/knowledge/<slug>/repos.local.json",
        all(entry.endswith("=ok") for entry in overlays),
        " ".join(overlays),
    )

    # 4b. A vault that declares no apps has nothing to link: reported, not pending, nothing written for it.
    if client.vaults_without_apps:
        statuses_by_vault = {v["slug"]: v for v in report["vaults"]}
        diagnosis = cli(client, cache, "doctor", "--vaults", str(client.vaults_yaml))
        problems: list[str] = []
        try:
            doctor = diagnosis.report()
            pending = doctor["init"]["pending"]
            warned = [w for w in doctor["warnings"] if w.startswith("W_CONTEXTS_INIT_PENDING")]
            counted = {row["slug"]: row.get("contexts", {}).get("apps") for row in doctor["vaults"]}
        except (ValueError, KeyError):
            pending, warned, counted = ["?"], ["?"], {}
            problems.append(f"doctor rc={diagnosis.code}")
        for slug in client.vaults_without_apps:
            outcome = statuses_by_vault.get(slug, {})
            if outcome.get("status") != "no-apps" or outcome.get("apps") != [] or outcome.get("roots") != []:
                problems.append(f"{slug}: init says {outcome.get('status')}")
            if client.overlay(slug).exists():
                problems.append(f"{slug}: init wrote an overlay")
            if slug in pending or counted.get(slug) != 0:
                problems.append(f"{slug}: doctor pending={slug in pending} apps={counted.get(slug)}")
        if warned:
            problems.append(f"doctor warns {warned}")
        add(
            "a vault with no apps is reported no-apps (rc 0), writes nothing, is not init pending",
            first.code == 0 and run_ok and not problems and diagnosis.code == 0,
            "; ".join(problems)
            or f"{', '.join(client.vaults_without_apps)}: no-apps, no overlay, doctor apps=0, pending={pending}",
        )

    # 4c. The vault copies that are git repositories ignore what Claude Code writes through a link.
    gitignore = {v["slug"]: (v["gitignore"], v["warnings"]) for v in report["vaults"] if v["status"] == "initialized"}
    expected_gitignore = {
        slug: ("covered" if client.home in client.vaults[slug].parents else "not-applicable")
        for slug in client.vaults_with_apps
    }
    add(
        "init finds the vault .gitignore covering what Claude writes (clone: covered, folder: n/a)",
        {slug: state for slug, (state, _w) in gitignore.items()} == expected_gitignore
        and not any(w for _s, w in gitignore.values()),
        " ".join(f"{slug}={state}" for slug, (state, _w) in gitignore.items()),
    )

    # 5. The packages are linked into the checkouts, pointing at this client's vault copy.
    links, good_links = 0, 0
    for app in present:
        for name in LINKED:
            links += 1
            link = client.checkout(app) / name
            source = client.vaults[app.vault] / "Contexts" / app.id / name
            if link.is_symlink() and link.resolve() == source.resolve() and source.exists():
                good_links += 1
    readable = all(
        app.id in (client.checkout(app) / "CLAUDE.md").read_text(encoding="utf-8")
        for app in present
        if (client.checkout(app) / "CLAUDE.md").exists()
    )
    add(
        "present apps: CLAUDE.md, .mcp.json and .claude link into the vault copy",
        good_links == links and readable,
        f"{good_links}/{links} links resolve into {', '.join(f'{s}' for s in slugs)}",
    )

    # 6. An app that is not on this machine is skipped and nothing is invented for it.
    if absent:
        skipped = [
            a.id
            for a in absent
            if outcomes.get(a.id) == "skipped"
            and not client.checkout(a).exists()
            and not client.checkout(a).parent.exists()
        ]
        add("absent app is reported skipped and nothing is created", len(skipped) == len(absent), f"skipped={skipped}")

    # 7. The links do not dirty the checkouts (they sit in .git/info/exclude).
    dirty = [
        a.id
        for a in present
        if subprocess.run(
            ["git", "-C", str(client.checkout(a)), "status", "--porcelain"], capture_output=True, text=True
        ).stdout.strip()
    ]
    add(
        "checkouts stay git-clean (links hidden by .git/info/exclude)",
        not dirty,
        f"dirty={dirty}" if dirty else f"{len(present)} checkouts clean",
    )

    # 8. Nothing was written inside the vault copies.
    vault_after = {slug: digest(path) for slug, path in client.vaults.items()}
    rewritten = [slug for slug in slugs if vault_after[slug] != vault_before[slug]]
    add(
        "init writes nothing inside the vault copies (tree hash)",
        not rewritten,
        f"rewritten={rewritten}" if rewritten else f"{len(slugs)} vault tree(s) identical",
    )

    # 9. A second run reports everything unchanged and modifies nothing.
    settled = snapshot(client.home)
    second = cli(client, cache, *init_args(client))
    try:
        again = second.report()
        statuses = {a["id"]: a["status"] for v in again["vaults"] for a in v["apps"]}
        overlay_actions = {v["slug"]: v["overlay_action"] for v in again["vaults"] if v["status"] == "initialized"}
        quiet = second.code == 0 and again["ok"] is True
    except (ValueError, KeyError):
        statuses, overlay_actions, quiet = {}, {}, False
    expected_again = {a.id: ("unchanged" if a.present else "skipped") for a in client.apps}
    touched = changed(settled, snapshot(client.home))
    vaults_still = all(digest(path) == vault_before[slug] for slug, path in client.vaults.items())
    add(
        "second run: all unchanged, no file modified (mtimes + hashes)",
        quiet
        and statuses == expected_again
        and set(overlay_actions.values()) == {"unchanged"}
        and not touched
        and vaults_still,
        f"{statuses} overlays={sorted(set(overlay_actions.values()))} changed={len(touched)}"
        + (f" {touched[:2]}" if touched else ""),
    )

    # 10. The vault copies lint clean: Contexts/ has no frontmatter and index.exclude covers it.
    lint_detail: list[str] = []
    lint_ok = True
    for slug, path in client.vaults.items():
        linted = cli(client, cache, "vault-lint", str(path))
        try:
            result = linted.report()
            clean = linted.code == 0 and result["ok"] is True and not result["errors"]
            lint_detail.append(
                f"{slug}: {result['files']} files, {len(result['errors'])} errors, {len(result['warnings'])} warnings"
            )
        except (ValueError, KeyError):
            clean = False
            lint_detail.append(f"{slug}: rc={linted.code} {linted.err.strip()[:60]}")
        lint_ok = lint_ok and clean
    untouched = all(digest(path) == vault_before[slug] for slug, path in client.vaults.items())
    add(
        "vault-lint passes on the vault copies (Contexts/ is index.exclude)",
        lint_ok and untouched,
        "; ".join(lint_detail) + ("" if untouched else "; the lint rewrote a vault"),
    )

    # 11. After init, doctor has nothing to say (the session hook stays silent).
    after = cli(client, cache, "doctor", "--brief", "--vaults", str(client.vaults_yaml))
    add(
        "doctor --brief is ok after init (the hook stays silent)",
        after.code == 0 and after.out.startswith("knowledge doctor: ok"),
        after.out.strip()[:100],
    )

    # 12. The autodetect scan over the synthetic tree.
    if client.noisy:
        from knowledge import contexts

        timings: list[float] = []
        found: list[tuple[Path, str]] = []
        for _ in range(SCAN_RUNS):
            started = time.perf_counter()
            found = contexts.scan_checkouts([client.projects])
            timings.append(time.perf_counter() - started)
        urls = {url for _, url in found}
        wanted_urls = {origin_of(client.checkout(a)) for a in present}
        directories = count_directories(client.projects)
        best, median = min(timings) * 1000, statistics.median(timings) * 1000
        add(
            f"scan over ~2,000 directories finds the {len(present)} app checkouts",
            wanted_urls <= urls and directories >= 2000,
            f"{directories} dirs, {len(found)} repos, best {best:.0f} ms / median {median:.0f} ms of {SCAN_RUNS}",
        )
        notes.append(
            f"scan timing ({client.name}): scan_checkouts over {directories} directories and "
            f"{len(found)} git checkouts takes {best:.0f} ms best / {median:.0f} ms median of {SCAN_RUNS} runs."
        )
        notes.append(
            f"init timing ({client.name}, {len(slugs)} vaults): first run {first.seconds:.2f} s, "
            f"second run {second.seconds:.2f} s, `init --help` baseline {baseline:.2f} s (uv + interpreter start)."
        )
    return rows, notes


def main() -> int:
    if not (WORK / "repos").is_dir():
        print(f"{WORK / 'repos'} is missing: run lab/run.sh first", file=sys.stderr)
        return 2
    subprocess.run([str(SEED), "--clients"], check=True, capture_output=True)
    cache = uv_cache()
    rows: list[Row] = []
    notes: list[str] = []
    for client in clients():
        try:
            client_rows, client_notes = scenario(client, cache)
        except Exception as exc:  # a story that cannot run is a failed story
            client_rows, client_notes = (
                [Row(f"{client.name}: scenario", False, f"{type(exc).__name__}: {exc}"[:140])],
                [],
            )
        rows += client_rows
        notes += client_notes
    print_table("CLIENT CHECKS (cloudfrog-knowledge init on the host, one person per tenant)", rows)
    for note in notes:
        print(note)
    return 0 if all(r.ok for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
