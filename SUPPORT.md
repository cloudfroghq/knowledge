# Support

## Questions and usage help

- Start with the [README](README.md) and the [installation guide](docs/install.md) (plugin, `uvx`, npm, Docker and source installs).
- For anything else, open a [GitHub issue](https://github.com/cloudfroghq/knowledge/issues/new/choose) and pick the closest template.

## Reporting a bug

Open an issue with the **Bug report** template. Please include:

- Package version: `knowledge --version` (or `cloudfrog-knowledge --version`).
- Install method (PyPI/`uvx`, npm, plugin, Docker, source), transport (`stdio` or `http`) and profile (`all` or `legacy`).
- Which vault `features` you use (`flows`, `governance`, `confluence`, `jira`, ...).
- OS and Python version, and whether `sqlite-vec` loaded (`vector_backend` in `doctor` output: `sqlite-vec` or `python`).
- The output of `cloudfrog-knowledge doctor --brief` (or the full `doctor` JSON).
- Steps to reproduce, expected result and actual result. Error codes (`E_...`) are stable, so quote them.

Remove bearer tokens, API keys and private vault content from anything you paste.

## Proposing changes

- New features: use the **Feature request** template.
- Anything that changes the response shape of a tool: use the **Tool contract change** template. This project versions its tool contract strictly (`docs/contract/CHANGELOG.md`).

## Security issues

Do not file these as public issues. Follow [SECURITY.md](SECURITY.md).
