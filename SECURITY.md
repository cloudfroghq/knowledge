# Security policy

## Supported versions

Security fixes go to the latest minor release of `cloudfrog-knowledge` (PyPI) and `@cloudfrog/knowledge` (npm), which are published with the same version number (ADR-050). The project is at 0.x, so older minors are not patched: upgrade to the latest release.

The tool contract is versioned separately (currently `2.0.0`, see `docs/contract/CHANGELOG.md`). The `legacy` tool profile (`serve --profile legacy`) exists only as a compatibility window of one release and is planned for removal in contract 2.1.0 (ADR-046); it is not a long-term supported surface.

| Version | Supported |
|---|---|
| Latest `0.x` minor release | Yes |
| Earlier releases | No |

<!-- OWNER: confirm the support window above. The repository does not state one beyond "latest release". -->

## Reporting a vulnerability

Do not open a public issue, pull request or discussion for a vulnerability.

Email **admin@cloudfrog.com.mx** with the subject `SECURITY: cloudfrog-knowledge`. This is the organization address already published in the package metadata (`plugin/claude-code/.claude-plugin/plugin.json` and `npm/package.json`).

If GitHub private vulnerability reporting is enabled for this repository, you can use it instead: open the **Security** tab and choose **Report a vulnerability** (`https://github.com/cloudfroghq/knowledge/security/advisories/new`). If the button is not there, use the email address above.

Please include:

- The affected version (`knowledge --version`), install method, transport (`stdio` or `http`) and profile.
- What you found and its impact: what an attacker can read, write or reach, and with which credentials or scope (`read`, `admin`, none).
- Reproduction steps or a proof of concept, ideally against `tests/fixtures/vault/` rather than real data.
- Relevant `doctor` output, with tokens and keys removed. Never paste a real bearer token, `OPENAI_API_KEY` or other credential.

## What is in scope

- HTTP authentication and authorization: bearer resolution, the `read` and `admin` scopes (`MCP_API_KEY`, `MCP_ADMIN_KEY`, `bearers` in `vaults.yaml`), and scope enforcement on `reindex`, `sync_confluence` and `/webhook/reindex`.
- The reindex webhook: signature verification with `MCP_WEBHOOK_SECRET`, and handling of pushes to other branches or refs.
- The vault write fence (ADR-044): any path by which the server writes inside a served vault outside `90 - Memory/`, or by which `sync_confluence` or `confluence export` writes into a vault.
- Secret handling: errors, logs, `doctor` output or tool responses that echo a bearer token, an OpenAI key, a judge key or another credential; credential-shaped values accepted in `vault.yaml` (`E_SECRET_IN_MANIFEST`).
- The memory server integration: writes or reads that leave loopback, or that bypass the `MEMORY_HTTP_TOKEN` requirement.
- Path traversal or vault-boundary escapes in `file_path`, `get_document` and the indexer.
- Supply-chain integrity of the published artifacts: the npm wrapper's `uv` download and SHA-256 verification, and the release workflows.

## What is out of scope

- Findings that need a malicious vault owner or an operator who already controls the server's environment and configuration (the vault is curated content, edited through reviewed pull requests).
- Running the HTTP transport without TLS or without a bearer token, or exposing it on a public network against the documented guidance.
- Denial of service through very large vaults or deliberately expensive queries, unless a small request causes disproportionate damage.
- Vulnerabilities in third-party dependencies with no demonstrated impact on this project (report those upstream).
- Social engineering, physical access, and issues in services you deploy this server next to.

## Coordinated disclosure

We ask reporters to give us reasonable time to investigate and release a fix before any public disclosure. We aim to acknowledge a report within a few business days, to share an assessment after triage, and to publish a fix and a GitHub security advisory crediting the reporter (if they wish) once a patched release is available. These are goals, not guarantees.

<!-- OWNER: confirm the response-time goals above. -->

## Safe harbor

Good-faith research that follows this policy, avoids privacy violations and service disruption, and uses only your own data and deployments will not be pursued by the maintainers.
