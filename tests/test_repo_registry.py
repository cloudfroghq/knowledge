"""The repository registry: the shape it must have, and the three answers it gives.

The registry is the one place that answers "where is repository X on this
machine". This server is project-agnostic: which repositories a vault declares
is that vault's business, in its ``Contexts/config.json``, so nothing here
asserts a particular repository, a particular slug or a particular directory —
every case below is an invariant that stays true whatever a vault declares.
The declaration cases read the fixture vault under
``tests/fixtures/vaults/contexts/``; no case reads a real vault.

**Portability.** The declaration says what a repository *is*: its identity, the
group it belongs to, the remote that proves it, the relative path it takes
under a root. None of that changes between machines, so it is versioned with
the vault. Where the roots *are* lives in the machine's ``repos.local.json``
and in ``KNOWLEDGE_*`` variables, and a test that reads a resolved value cannot
tell a portable declaration from one pinned to the machine the test runs on —
the resolved value necessarily contains this checkout's own location. So the
portability cases below read the declaration itself, which is false on every
machine the moment someone writes a home directory into it.

**Three answers, never two.** ``unconfigured`` means nobody said where to look
and nothing was inspected; ``missing`` means this machine said where to look
and the directory is not there. Collapsing them into one falsy value turns "I
could not look" into "I looked and found nothing", and every gate built on top
of that reports green for a tree it never opened.

**What each case reads.** The declaration cases read the fixture vault's
declaration and its package folders. The resolution and refusal cases own their
fixtures: a registry written into ``tmp_path`` and an explicit ``env`` mapping,
so they touch nothing the machine happens to have.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from knowledge import graph_targets, repos, settings

CONTEXTS_VAULT = Path(__file__).parent / "fixtures" / "vaults" / "contexts"
DECLARATION = CONTEXTS_VAULT / "Contexts" / "config.json"

# Prefixes that pin a declaration to one machine: an absolute POSIX path under
# a user's home, a root-owned tree, or a "~" the resolver would treat as a
# literal directory name.
_MACHINE_SPECIFIC = ("/Users/", "/home/", "/root/", "~")

# Keys the parser refuses an entry without. Kept here rather than imported so a
# field quietly dropped from ``_parse_entries`` fails this file too.
_REQUIRED_ENTRY_KEYS = ("id", "remote", "group", "root", "path")

# A git remote is declared as the bare repository name, because
# ``Registry.verify_remote`` compares it against the tail of an origin URL
# (``…/<remote>.git``, ``…:<remote>``). A URL, a path or a ".git" suffix
# written here would never match any checkout and the mismatch would be
# reported as a defect in the tree rather than in the declaration.
_REMOTE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# An environment variable name the shell can actually export.
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _declared() -> dict:
    return json.loads(DECLARATION.read_text(encoding="utf-8")).get("repos", {})


# --- the fixture declaration has the shape the parser requires --------------


def test_the_fixture_declares_a_repository_registry() -> None:
    """An empty registry would satisfy every case below while checking nothing."""
    block = _declared()
    assert block, f"{DECLARATION} declares no 'repos' block"
    assert block.get("roots"), "repos.roots is empty; the registry would resolve nothing"
    assert block.get("entries"), "repos.entries is empty; the registry would resolve nothing"


def test_the_fixture_declaration_loads(tmp_path: Path) -> None:
    """The fixture must be structurally valid, not decorative.

    An example nobody can load documents a shape that does not exist. This
    loads the declaration through the real parser with an empty environment
    and no overlay, so the only thing under test is the declaration itself.
    """
    registry = repos.load_registry(declaration_path=DECLARATION, overlay_path=tmp_path / "absent.json", env={})

    assert registry.entries, "the declaration parsed into an empty registry"
    for repo_id in registry.entries:
        # With no root declared anywhere, every entry must land on the answer
        # that says so — never on a plausible path nobody supplied.
        resolution = registry.resolve(repo_id)
        assert resolution.status == repos.STATUS_UNCONFIGURED
        assert resolution.path is None


def test_every_root_declares_an_id_and_an_exportable_override_variable() -> None:
    roots = _declared()["roots"]
    seen: set[str] = set()
    for root in roots:
        assert root.get("id"), f"a repos.roots entry declares no id: {root!r}"
        assert root["id"] not in seen, f"repos.roots declares {root['id']!r} twice"
        seen.add(root["id"])
        override = repos.root_env_var(root["id"])
        assert _ENV_NAME.match(override), (
            f"root {root['id']!r} derives the override {override!r}; a root override has to be a name "
            "a shell can export"
        )
        # ``env`` is the deprecated alias of the override: optional, but exportable when declared.
        if root.get("env"):
            assert _ENV_NAME.match(root["env"]), f"root {root['id']!r} declares env {root['env']!r}"

    overrides = [repos.root_env_var(root["id"]) for root in roots]
    assert len(set(overrides)) == len(overrides), f"two roots share one override variable: {overrides}"


def test_a_root_default_is_absolute_or_home_relative() -> None:
    """The vault suggests where a root usually lives; a relative suggestion would mean nothing."""
    for root in _declared()["roots"]:
        default = root.get("default")
        if default is None:
            continue
        assert str(default).startswith(("/", "~/")), f"root {root['id']!r} declares the relative default {default!r}"


def test_every_entry_declares_the_keys_the_parser_requires() -> None:
    for entry in _declared()["entries"]:
        missing = [key for key in _REQUIRED_ENTRY_KEYS if key not in entry]
        assert not missing, f"{entry.get('id', entry)!r} is missing {missing}"
        for key in _REQUIRED_ENTRY_KEYS:
            assert isinstance(entry[key], str) and entry[key], (
                f"{entry['id']!r} declares {key}={entry[key]!r}; it has to be a non-empty string"
            )


def test_every_entry_id_is_unique() -> None:
    """The id is the registry key, so a duplicate silently shadows a checkout."""
    ids = [entry["id"] for entry in _declared()["entries"]]
    duplicated = sorted({repo_id for repo_id in ids if ids.count(repo_id) > 1})
    assert not duplicated, f"repos.entries declares these ids more than once: {duplicated}"


def test_every_entry_id_yields_a_usable_override_variable() -> None:
    """``KNOWLEDGE_REPO_<ID>`` is the per-checkout pin; an id that mangles it is unusable."""
    for entry in _declared()["entries"]:
        var = repos.repo_env_var(entry["id"])
        assert _ENV_NAME.match(var), f"{entry['id']!r} derives the override {var!r}, which no shell can export"


def test_no_declared_path_is_machine_specific() -> None:
    """Reads the declaration, not a resolved value — see the module docstring."""
    offenders = [
        (entry["id"], entry["path"])
        for entry in _declared()["entries"]
        if str(entry["path"]).startswith(_MACHINE_SPECIFIC) or Path(str(entry["path"])).is_absolute()
    ]
    assert not offenders, (
        "declared paths are relative to a root so the same declaration works on every "
        f"machine; these are pinned to one filesystem: {offenders}"
    )


def test_every_remote_is_declared_as_a_bare_repository_name() -> None:
    """A URL here silently turns every checkout into a remote mismatch."""
    offenders = [
        (entry["id"], entry["remote"])
        for entry in _declared()["entries"]
        if not _REMOTE_NAME.match(str(entry["remote"])) or str(entry["remote"]).endswith(".git")
    ]
    assert not offenders, (
        "a remote is the bare repository name the origin URL ends with, never a URL, a "
        f"path or a '.git' suffix; these are not: {offenders}"
    )


def test_every_entry_hangs_off_a_declared_root_and_a_declared_group() -> None:
    block = _declared()
    root_ids = {root["id"] for root in block["roots"]}
    group_ids = set(block.get("groups", {}))

    for entry in block["entries"]:
        assert entry["root"] in root_ids, f"{entry['id']!r} hangs off undeclared root {entry['root']!r}"
        assert entry["group"] in group_ids, f"{entry['id']!r} is in undeclared group {entry['group']!r}"


def test_a_context_slug_names_a_real_context_package() -> None:
    """A declared slug has to match a package folder that sits beside the declaration.

    ``context: null`` is the explicit "this checkout has no AI-context package"
    case — a checkout installed inside another repository's dependency tree, or
    one nobody has written a package for. Declaring a slug it does not have
    would be more comfortable and less true.
    """
    contexts = DECLARATION.parent
    for entry in _declared()["entries"]:
        slug = entry.get("context")
        if slug is None:
            continue
        assert (contexts / slug).is_dir(), (
            f"{entry['id']!r} claims context {slug!r}, which is not a directory under {contexts.name}/"
        )


def test_install_mode_is_declared_and_valid_wherever_a_context_is() -> None:
    """Every materialized context says explicitly how it materializes.

    A silent default of "link" is how a checkout holding git worktrees ends up
    symlinked by mistake, cancelling Claude Code's own worktrees there. So the
    declaration must be explicit, and it must be one of the two modes the
    installer actually implements.
    """
    for entry in _declared()["entries"]:
        if entry.get("context") is None:
            continue
        mode = entry.get("install_mode")
        assert mode in repos.INSTALL_MODES, (
            f"{entry['id']!r} declares context {entry['context']!r} but install_mode is "
            f"{mode!r}, not one of {repos.INSTALL_MODES}"
        )


def test_a_context_null_entry_declares_no_install_mode() -> None:
    """A checkout with no context package has nothing to materialize."""
    for entry in _declared()["entries"]:
        if entry.get("context") is not None:
            continue
        assert entry.get("install_mode") is None, (
            f"{entry['id']!r} declares context: null but install_mode "
            f"{entry.get('install_mode')!r}; a checkout with no context package has "
            "nothing to materialize"
        )


# --- graph.targets asks the registry ----------------------------------------
#
# A graph target names a registry ``repo`` and a subpath under it, so the
# registry stays the single answer to "where is repository X". An empty target
# list is a legitimate configuration; a target naming a repository nobody
# declared is not. These cases make the fixture vault the default vault, so
# ``knowledge.graph_targets`` answers from its ``Contexts/config.json``.


@pytest.fixture()
def default_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    vault = tmp_path / "vault"
    shutil.copytree(CONTEXTS_VAULT, vault)
    manifest = tmp_path / "vaults.yaml"
    manifest.write_text(
        f"version: 1\nvaults:\n  - slug: contexts\n    path: {vault}\n    default: true\n", encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("KNOWLEDGE_VAULTS", str(manifest))
    monkeypatch.setenv("VAULT_PATH", "")
    monkeypatch.setenv("KNOWLEDGE_HOME", str(tmp_path / "data"))
    for root in ("WORK", "SIDE"):
        monkeypatch.delenv(f"KNOWLEDGE_{root}_ROOT", raising=False)
        monkeypatch.delenv(f"CD_{root}_ROOT", raising=False)
    settings.reset_current()
    monkeypatch.setattr(graph_targets, "_registry_cache", None)
    yield vault
    settings.reset_current()


def test_every_graph_target_repo_reference_names_a_declared_entry(default_vault: Path) -> None:
    declared_ids = {entry["id"] for entry in _declared()["entries"]}
    targets = json.loads(DECLARATION.read_text(encoding="utf-8")).get("graph", {}).get("targets", [])

    assert targets, "the fixture declares no graph.targets; this case would certify nothing"
    unknown = [entry["repo"] for entry in targets if entry.get("repo") and entry["repo"] not in declared_ids]
    assert not unknown, f"graph.targets references repositories the registry does not declare: {unknown}"


def test_an_unusable_graph_target_is_reported_rather_than_dropped(default_vault: Path) -> None:
    """Every declared target ends up either usable or explained. None vanish."""
    declared = len(json.loads(DECLARATION.read_text(encoding="utf-8")).get("graph", {}).get("targets", []))
    accounted = len(graph_targets._graph_targets_from_json) + len(graph_targets.GRAPH_TARGETS_UNRESOLVED)
    assert declared > 0
    assert accounted == declared, (
        f"{declared} graph targets are declared but only {accounted} were accounted for; "
        "a target that is neither usable nor explained is a silent hole"
    )
    # No root is configured on this machine, so every target is explained.
    assert len(graph_targets.GRAPH_TARGETS_UNRESOLVED) == declared
    for reason in graph_targets.GRAPH_TARGETS_UNRESOLVED:
        assert reason.strip(), "an unusable target was recorded with an empty reason"
        assert "unconfigured" in reason


def test_every_usable_graph_target_actually_exists(default_vault: Path, tmp_path: Path, monkeypatch) -> None:
    """GRAPH_TARGETS is what a Python consumer would walk; a phantom entry is the bug."""
    api = tmp_path / "work" / "ledger" / "api"
    api.mkdir(parents=True)
    monkeypatch.setenv("KNOWLEDGE_WORK_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("KNOWLEDGE_REPO_LEDGER_API", str(api))
    monkeypatch.setattr(graph_targets, "_registry_cache", None)

    usable = graph_targets._graph_targets_from_json

    assert usable == [str(api)]
    assert [p for p in usable if not Path(p).is_dir()] == []


def test_graph_targets_detail_keeps_every_entrys_identity(default_vault: Path) -> None:
    """The per-entry detail must not lose what ``GRAPH_TARGETS`` already lost.

    A consumer that has to report a per-target outcome (build-platform-graph.sh's
    provenance sidecar) needs the declared ``(repo, path)`` back, not just the
    split verdict ``GRAPH_TARGETS``/``GRAPH_TARGETS_UNRESOLVED`` keep.
    """
    declared = json.loads(DECLARATION.read_text(encoding="utf-8")).get("graph", {}).get("targets", [])
    detail = graph_targets.graph_targets_detail()

    assert len(detail) == len(declared) > 0, "every declared entry must produce exactly one detail row"
    for declared_entry, row in zip(declared, detail):
        assert row["repo"] == declared_entry.get("repo")
        assert row["path"] == str(declared_entry.get("path", "."))
        # exactly one of resolved_dir/reason is populated, never both and never neither
        assert (row["resolved_dir"] is None) != (not row["reason"]), (
            f"{row}: resolved_dir and reason must disagree about whether the entry resolved"
        )
        if row["resolved_dir"] is not None:
            assert Path(row["resolved_dir"]).is_dir()


def test_a_default_vault_without_a_declaration_has_no_targets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``Contexts/config.json`` means no registry and no targets — and the reason says so."""
    vault = tmp_path / "plain"
    vault.mkdir()
    (vault / "vault.yaml").write_text("slug: plain\n", encoding="utf-8")
    manifest = tmp_path / "vaults.yaml"
    manifest.write_text(
        f"version: 1\nvaults:\n  - slug: plain\n    path: {vault}\n    default: true\n", encoding="utf-8"
    )
    monkeypatch.setenv("KNOWLEDGE_VAULTS", str(manifest))
    monkeypatch.setenv("VAULT_PATH", "")
    monkeypatch.setenv("KNOWLEDGE_HOME", str(tmp_path / "data"))
    settings.reset_current()
    monkeypatch.setattr(graph_targets, "_registry_cache", None)
    try:
        registry, error = graph_targets.registry_state()

        assert registry is None
        assert "declares no Contexts/config.json" in error
        assert graph_targets.graph_targets_detail() == []
        assert graph_targets.repo_checkouts() == []
    finally:
        settings.reset_current()


# --- resolution: the three answers ------------------------------------------


@pytest.fixture()
def synthetic(tmp_path: Path):
    """A registry declared from scratch, so the cases below own their fixtures.

    Two roots, because the split is part of the shape: a checkout that comes
    down with its parent and one cloned on its own resolve through different
    overrides, and a fixture with a single root cannot tell them apart.
    """
    declaration = tmp_path / "config.json"
    declaration.write_text(
        json.dumps(
            {
                "repos": {
                    "roots": [
                        {"id": "parent", "env": "CD_PARENT_ROOT"},
                        {"id": "extras", "env": "CD_EXTRAS_ROOT"},
                    ],
                    "groups": {"submodule": "", "standalone": ""},
                    "entries": [
                        {
                            "id": "inner-service",
                            "remote": "inner-service",
                            "group": "submodule",
                            "root": "parent",
                            "path": "apps/inner",
                        },
                        {
                            "id": "outer-service",
                            "remote": "outer-service",
                            "group": "standalone",
                            "root": "extras",
                            "path": "outer-checkout",
                        },
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    return declaration, tmp_path


def test_a_root_nobody_declared_is_unconfigured_and_names_its_override(synthetic) -> None:
    declaration, tmp_path = synthetic
    registry = repos.load_registry(
        declaration_path=declaration, overlay_path=tmp_path / "absent.json", env={}
    )

    resolution = registry.resolve("inner-service")

    assert resolution.status == repos.STATUS_UNCONFIGURED
    assert resolution.path is None, "an unconfigured checkout must not hand back a plausible path"
    assert "KNOWLEDGE_PARENT_ROOT" in resolution.reason
    assert not resolution.ok


def test_a_declared_root_whose_directory_is_absent_is_missing_not_unconfigured(synthetic) -> None:
    """The distinction the whole module exists for.

    Same repository, same registry — the only change is that this machine now
    says where to look. The answer has to change from "nothing was inspected"
    to "it is not there", and it has to carry the path so the operator can act.
    """
    declaration, tmp_path = synthetic
    registry = repos.load_registry(
        declaration_path=declaration,
        overlay_path=tmp_path / "absent.json",
        env={"CD_PARENT_ROOT": str(tmp_path / "nowhere")},
    )

    resolution = registry.resolve("inner-service")

    assert resolution.status == repos.STATUS_MISSING
    assert resolution.path == str(tmp_path / "nowhere" / "apps" / "inner")
    assert "not a directory" in resolution.reason
    assert not resolution.ok
    assert resolution.status != repos.STATUS_UNCONFIGURED


def test_a_present_checkout_resolves(synthetic) -> None:
    declaration, tmp_path = synthetic
    (tmp_path / "parent" / "apps" / "inner").mkdir(parents=True)
    registry = repos.load_registry(
        declaration_path=declaration,
        overlay_path=tmp_path / "absent.json",
        env={"CD_PARENT_ROOT": str(tmp_path / "parent")},
    )

    resolution = registry.resolve("inner-service")

    assert resolution.status == repos.STATUS_RESOLVED
    assert resolution.ok
    assert Path(resolution.path).is_dir()
    assert resolution.reason == ""


def test_the_overlay_supplies_roots_and_the_environment_overrides_it(synthetic) -> None:
    declaration, tmp_path = synthetic
    from_overlay = tmp_path / "from-overlay" / "apps" / "inner"
    from_env = tmp_path / "from-env" / "apps" / "inner"
    from_overlay.mkdir(parents=True)
    from_env.mkdir(parents=True)

    overlay = tmp_path / "repos.local.json"
    overlay.write_text(json.dumps({"roots": {"parent": str(tmp_path / "from-overlay")}}))

    from_file = repos.load_registry(declaration_path=declaration, overlay_path=overlay, env={})
    assert from_file.resolve("inner-service").path == str(from_overlay)

    overridden = repos.load_registry(
        declaration_path=declaration,
        overlay_path=overlay,
        env={"CD_PARENT_ROOT": str(tmp_path / "from-env")},
    )
    assert overridden.resolve("inner-service").path == str(from_env)


def test_a_single_checkout_can_be_pinned_past_its_root(synthetic) -> None:
    """The escape hatch for a clone that does not sit where its root says.

    It is also why the registry is keyed by repository identity and never by
    the local directory name: the two disagree here, and only the id survives
    a move.
    """
    declaration, tmp_path = synthetic
    elsewhere = tmp_path / "somewhere" / "Renamed-Checkout"
    elsewhere.mkdir(parents=True)

    registry = repos.load_registry(
        declaration_path=declaration,
        overlay_path=tmp_path / "absent.json",
        env={
            "CD_EXTRAS_ROOT": str(tmp_path / "extras"),
            "CD_REPO_OUTER_SERVICE": str(elsewhere),
        },
    )

    resolution = registry.resolve("outer-service")
    assert resolution.path == str(elsewhere)
    assert resolution.source == "CD_REPO_OUTER_SERVICE"


def test_the_per_checkout_variable_name_is_derived_the_same_way_everywhere() -> None:
    assert repos.repo_env_var("service") == "KNOWLEDGE_REPO_SERVICE"
    assert repos.repo_env_var("outer-service") == "KNOWLEDGE_REPO_OUTER_SERVICE"
    assert repos.repo_env_var("slot_00") == "KNOWLEDGE_REPO_SLOT_00"
    assert repos.legacy_repo_env_var("outer-service") == "CD_REPO_OUTER_SERVICE"
    assert repos.root_env_var("work") == "KNOWLEDGE_WORK_ROOT"


# --- "I could not look" is never an empty registry --------------------------


def test_a_malformed_declaration_raises_instead_of_resolving_nothing(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"repos": {"roots": [], "entries": [],}')

    with pytest.raises(repos.RegistryError, match="not valid JSON"):
        repos.load_registry(declaration_path=path, overlay_path=tmp_path / "absent.json", env={})


def test_a_missing_declaration_raises(tmp_path: Path) -> None:
    with pytest.raises(repos.RegistryError, match="is missing"):
        repos.load_registry(
            declaration_path=tmp_path / "config.json",
            overlay_path=tmp_path / "absent.json",
            env={},
        )


def test_a_malformed_overlay_raises_instead_of_being_ignored(synthetic) -> None:
    declaration, tmp_path = synthetic
    overlay = tmp_path / "repos.local.json"
    overlay.write_text("not json at all")

    with pytest.raises(repos.RegistryError, match="not valid JSON"):
        repos.load_registry(declaration_path=declaration, overlay_path=overlay, env={})


def test_an_overlay_pinning_an_unknown_checkout_is_refused(synthetic) -> None:
    """A typo here would silently keep the declared location instead."""
    declaration, tmp_path = synthetic
    overlay = tmp_path / "repos.local.json"
    overlay.write_text(json.dumps({"repos": {"inner-servic": "/tmp/typo"}}))

    with pytest.raises(repos.RegistryError, match="does not declare"):
        repos.load_registry(declaration_path=declaration, overlay_path=overlay, env={})


def test_a_duplicate_entry_id_is_refused(tmp_path: Path) -> None:
    """The id is the registry key; a second one would shadow the first in silence."""
    entry = {
        "id": "x",
        "remote": "x",
        "group": "standalone",
        "root": "extras",
        "path": "x",
    }
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "repos": {
                    "roots": [{"id": "extras", "env": "CD_EXTRAS_ROOT"}],
                    "groups": {"standalone": ""},
                    "entries": [entry, {**entry, "path": "y"}],
                }
            }
        )
    )

    with pytest.raises(repos.RegistryError, match="twice"):
        repos.load_registry(declaration_path=path, overlay_path=tmp_path / "absent.json", env={})


def test_an_entry_hanging_off_an_undeclared_root_is_refused(tmp_path: Path) -> None:
    """Otherwise the checkout would be permanently unconfigured with no way to say so."""
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "repos": {
                    "roots": [{"id": "extras", "env": "CD_EXTRAS_ROOT"}],
                    "groups": {"standalone": ""},
                    "entries": [
                        {
                            "id": "x",
                            "remote": "x",
                            "group": "standalone",
                            "root": "nowhere",
                            "path": "x",
                        }
                    ],
                }
            }
        )
    )

    with pytest.raises(repos.RegistryError, match="repos.roots does not declare"):
        repos.load_registry(declaration_path=path, overlay_path=tmp_path / "absent.json", env={})


@pytest.mark.parametrize("missing", _REQUIRED_ENTRY_KEYS)
def test_an_entry_missing_a_required_key_is_refused(tmp_path: Path, missing: str) -> None:
    """Every required field is refused by name, so the message says what to write."""
    entry = {
        "id": "x",
        "remote": "x",
        "group": "standalone",
        "root": "extras",
        "path": "x",
    }
    del entry[missing]
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "repos": {
                    "roots": [{"id": "extras", "env": "CD_EXTRAS_ROOT"}],
                    "groups": {"standalone": ""},
                    "entries": [entry],
                }
            }
        )
    )

    with pytest.raises(repos.RegistryError, match=f"missing {missing!r}"):
        repos.load_registry(declaration_path=path, overlay_path=tmp_path / "absent.json", env={})


@pytest.mark.parametrize("declared", ["/opt/checkouts/x", "~/checkouts/x"])
def test_a_declared_path_that_pins_a_machine_is_refused(tmp_path: Path, declared: str) -> None:
    """The parser refuses at load time what the portability case only observes.

    Both shapes pin a declaration to one filesystem: an absolute path, and a
    "~" the resolver would join as a literal directory name rather than expand.
    Refusing them here means a declaration that would only work on the machine
    it was written on cannot even be loaded — the check does not depend on
    somebody remembering to run the portability case.
    """
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "repos": {
                    "roots": [{"id": "extras", "env": "CD_EXTRAS_ROOT"}],
                    "groups": {"standalone": ""},
                    "entries": [
                        {
                            "id": "x",
                            "remote": "x",
                            "group": "standalone",
                            "root": "extras",
                            "path": declared,
                        }
                    ],
                }
            }
        )
    )

    with pytest.raises(repos.RegistryError, match="absolute path"):
        repos.load_registry(declaration_path=path, overlay_path=tmp_path / "absent.json", env={})


def test_install_mode_without_a_context_is_refused(tmp_path: Path) -> None:
    """A checkout with nothing to materialize must declare no install_mode."""
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "repos": {
                    "roots": [{"id": "extras", "env": "CD_EXTRAS_ROOT"}],
                    "groups": {"standalone": ""},
                    "entries": [
                        {
                            "id": "x",
                            "remote": "x",
                            "group": "standalone",
                            "root": "extras",
                            "path": "x",
                            "context": None,
                            "install_mode": "link",
                        }
                    ],
                }
            }
        )
    )

    with pytest.raises(repos.RegistryError, match="nothing to materialize"):
        repos.load_registry(declaration_path=path, overlay_path=tmp_path / "absent.json", env={})


@pytest.mark.parametrize("install_mode", [None, "", "symlink", "COPY"])
def test_a_context_without_a_valid_install_mode_is_refused(tmp_path: Path, install_mode) -> None:
    """A context package must say explicitly how it materializes; no silent default."""
    entry: dict = {
        "id": "x",
        "remote": "x",
        "group": "standalone",
        "root": "extras",
        "path": "x",
        "context": "x",
    }
    if install_mode is not None:
        entry["install_mode"] = install_mode
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "repos": {
                    "roots": [{"id": "extras", "env": "CD_EXTRAS_ROOT"}],
                    "groups": {"standalone": ""},
                    "entries": [entry],
                }
            }
        )
    )

    with pytest.raises(repos.RegistryError, match="install_mode"):
        repos.load_registry(declaration_path=path, overlay_path=tmp_path / "absent.json", env={})


def test_an_unknown_repository_id_raises_rather_than_returning_nothing(synthetic) -> None:
    declaration, tmp_path = synthetic
    registry = repos.load_registry(
        declaration_path=declaration, overlay_path=tmp_path / "absent.json", env={}
    )

    with pytest.raises(KeyError, match="not declared"):
        registry.resolve("does-not-exist")


def test_a_remote_that_could_not_be_read_is_not_reported_as_a_mismatch(synthetic) -> None:
    """``None`` and ``False`` are different findings; only ``False`` is a defect."""
    declaration, root = synthetic
    registry = repos.load_registry(
        declaration_path=declaration, overlay_path=root / "absent.json", env={}
    )

    unresolved = registry.resolve("inner-service")
    matches, detail = registry.verify_remote(unresolved)

    assert matches is None
    assert "not inspected" in detail

def test_a_registry_that_cannot_load_reports_every_declared_target_unresolved(default_vault: Path) -> None:
    """The declared targets are read as they stand, so each one carries the reason instead of vanishing."""
    declaration = default_vault / "Contexts" / "config.json"
    raw = json.loads(declaration.read_text(encoding="utf-8"))
    del raw["repos"]
    declaration.write_text(json.dumps(raw), encoding="utf-8")

    registry, error = graph_targets.registry_state()
    detail = graph_targets.graph_targets_detail()

    assert registry is None and "declares no 'repos' object" in error
    assert [row["repo"] for row in detail] == ["ledger-api", "ledger-web"]
    assert all(row["resolved_dir"] is None and "could not be read" in row["reason"] for row in detail)


def test_a_declaration_that_is_not_json_yields_no_targets_and_says_why(default_vault: Path) -> None:
    (default_vault / "Contexts" / "config.json").write_text("{not json", encoding="utf-8")

    registry, error = graph_targets.registry_state()

    assert registry is None and "not valid JSON" in error
    assert graph_targets.graph_targets_detail() == []
