# Repository registry

No repository path is written in the code. The repository-to-path mapping lives in one place, and every consumer reads it from there.

| File | What it declares | Versioned? |
|---|---|---|
| `<vault>/Contexts/config.json`, keys `repos` (and `graph`) | What each repository **is**: its identifier, the group it belongs to, the remote that proves it, the relative path it takes under a root, and the context package (`Contexts/<context>/`) that belongs to it. The same on every machine. | Yes, with the vault |
| `~/.config/knowledge/<slug>/repos.local.json` | Where the roots **are** on this machine, and the vault directory. Absolute paths. Also `apply`, the list of identifiers whose context packages `init` installs on this machine (`--repo`, `--all` and `--drop` change it; neither the graph targets nor the server read it). Written by `cloudfrog-knowledge init` (under `$XDG_CONFIG_HOME` if defined). | No: it lives outside every repository |

The server is not tied to any project: the checkouts it can reach are declared by each vault in its `Contexts/config.json`, one entry per checkout. The fixture `tests/fixtures/vaults/contexts/Contexts/config.json` shows the shape, and [ADR-047](adr/ADR-047.md) explains each field the parser requires.

**The key is the repository's identity**, never the name of the local directory. The two can differ (a renamed clone, a worktree, a second copy) and only the identifier survives that difference. The folder name is a value of the registry (`path`), never a key.

**Roots and their groups.** A root is the directory the declared paths hang from, and the difference between groups is not cosmetic: checkouts that come down together as submodules of the same parent arrive with a single `git submodule update --init`; `standalone` ones are independent clones, and cloning the parent brings none of them; `vendored` ones appear only when the host checkout's package manager installs them in its gitignored dependency tree. A tool that treats them as one set reports a whole platform from half of it.

**Precedence**, highest first: `KNOWLEDGE_REPO_<ID>` (or the deprecated `CD_REPO_<ID>`), then `repos.<id>` in `repos.local.json`, then `KNOWLEDGE_<ROOT>_ROOT` (or the deprecated `CD_<ROOT>_ROOT` and the root's `env`), then `roots.<id>` in `repos.local.json`, then the `default` the root declares in `Contexts/config.json`, only if that directory exists; otherwise nothing, and the checkout is *unconfigured*.

**Which declaration is read.** The one of the vault you name (`--vault SLUG` or `--vault-dir DIR` in `python3 -m knowledge.repos`); if you name none, the one of the only vault initialized on this machine; with several, you must name one; with none, the registry answers that you must run `cloudfrog-knowledge init`. The module does not read `vaults.yaml` (it does not need PyYAML): each `repos.local.json` that `init` writes stores the vault directory and its slug.

**Three answers, never two.** `unconfigured` means nobody said where to look and nothing was inspected; `missing` means this machine did say where to look and the directory is not there. Neither one is a pass, and confusing them turns "I could not look" into "I looked and there was nothing".

```bash
# Resolution table, verifying each checkout's remote
PYTHONPATH=src python3 -m knowledge.repos --report --verify-remotes

# From bash: repo_path / repo_status / repo_require
. scripts/lib/repo-registry.sh
repo_path ledger-api
repo_status ledger-api
```

Consumer: `graph.targets` of the default vault's `Contexts/config.json` (each entry names a registry `repo` and is the roster of repositories that `graphify` onboards). The `Makefile` (`make help`) wraps `init` and the graph builds over that registry.

> A `CD_*` variable set in `.env` reaches the Python side, because `load_dotenv()` runs before the registry is read. The bash side reads the real environment, so there they must be exported. For stable configuration, `repos.local.json` serves both.
