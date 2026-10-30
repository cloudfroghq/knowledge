"""Which vault's credentials a ``vault.yaml`` may name, and where they may travel.

A vault is curated content. Its ``integrations`` block names a site and the
variables the credentials come from, so the settings decide, before any
request, that one vault can never send another vault's credentials (or the
operator's) to a site it declares: every variable carries the vault's own
prefix (only the default vault may name the bare ``JIRA_*`` or
``CONFLUENCE_*`` ones), and the site is on Atlassian Cloud or on a host the
operator lists in ``KNOWLEDGE_INTEGRATION_SITES``.

Over HTTP the server never loads ``jira`` nor ``memory-bridge``, so the keys
only those read are reported as warnings there instead of stopping the
server; the site and credential checks stop it whatever the transport.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest
import yaml

from jira_fakes import FakeJira, jira_server
from knowledge import errors, jira_sync, settings
from knowledge.errors import KnowledgeConfigError, KnowledgeToolError

JIRA_BLOCK = (
    "features: [jira]\n"
    "memory:\n  project: {project}\n"
    "integrations:\n"
    "  jira:\n"
    "    site: {site}\n"
    "    projects: [ACME]\n"
    "    secrets: {{email_env: {email}, token_env: {token}}}\n"
)


def _registry(tmp_path: Path, manifests: dict[str, str], *, default: str, **env: str) -> settings.Settings:
    entries: list[dict[str, Any]] = []
    for slug, body in manifests.items():
        directory = tmp_path / slug
        directory.mkdir(parents=True)
        (directory / "vault.yaml").write_text(f"slug: {slug}\n" + textwrap.dedent(body), encoding="utf-8")
        entries.append({"slug": slug, "path": str(directory), "default": slug == default})
    vaults_file = tmp_path / "vaults.yaml"
    vaults_file.write_text(
        yaml.safe_dump({"version": 1, "data_dir": str(tmp_path / "data"), "vaults": entries}), encoding="utf-8"
    )
    base = {"KNOWLEDGE_VAULTS": str(vaults_file), "KNOWLEDGE_EMBEDDINGS_PROVIDER": "bag-of-words", **env}
    return settings.load_settings(base, project_root=tmp_path)


def _jira(slug: str, site: str = "https://example.atlassian.net", *, email: str = "", token: str = "") -> str:
    prefix = slug.upper().replace("-", "_")
    return JIRA_BLOCK.format(
        project=slug, site=site, email=email or f"{prefix}_JIRA_EMAIL", token=token or f"{prefix}_JIRA_TOKEN"
    )


# --- one vault never names another vault's variables ---------------------------------


def test_a_vault_naming_another_vaults_variables_is_refused(tmp_path: Path) -> None:
    """The diversion: ``personal`` names ``GLOBEX_JIRA_*`` with a site of its own."""
    manifests = {
        "globex": _jira("globex"),
        "personal": _jira("personal", email="GLOBEX_JIRA_EMAIL", token="GLOBEX_JIRA_TOKEN"),
    }

    with pytest.raises(KnowledgeConfigError) as excinfo:
        _registry(tmp_path, manifests, default="globex")

    assert excinfo.value.code == errors.E_MANIFEST_INVALID
    assert "personal/vault.yaml" in excinfo.value.message
    assert "integrations.jira.secrets.email_env" in excinfo.value.message
    assert "PERSONAL_JIRA_EMAIL" in excinfo.value.message


def test_only_the_default_vault_may_name_the_bare_variables(tmp_path: Path) -> None:
    manifests = {
        "globex": _jira("globex", email="JIRA_EMAIL", token="JIRA_API_TOKEN"),
        "personal": _jira("personal"),
    }
    loaded = _registry(tmp_path, manifests, default="globex")
    assert loaded.vault("globex").integrations["jira"]["secrets"]["token_env"] == "JIRA_API_TOKEN"

    swapped = tmp_path / "swapped"
    swapped.mkdir()
    with pytest.raises(KnowledgeConfigError) as excinfo:
        _registry(swapped, manifests, default="personal")
    assert "globex/vault.yaml" in excinfo.value.message
    assert "GLOBEX_JIRA_EMAIL" in excinfo.value.message


def test_a_slug_with_dashes_names_an_underscore_prefix(tmp_path: Path) -> None:
    loaded = _registry(tmp_path, {"cd-knowledge": _jira("cd-knowledge")}, default="cd-knowledge")

    assert loaded.default_vault().integrations["jira"]["secrets"]["email_env"] == "CD_KNOWLEDGE_JIRA_EMAIL"
    assert settings.env_prefix("cd-knowledge") == "CD_KNOWLEDGE"


@pytest.mark.parametrize("secrets", ["{token_env: PERSONAL_CONFLUENCE_TOKEN}", None])
def test_a_vault_that_is_not_the_default_never_borrows_the_operators_credentials(
    tmp_path: Path, secrets: str | None
) -> None:
    """A site of its own with a variable left out would fall back to ``CONFLUENCE_*``."""
    body = "integrations:\n  confluence:\n    site: https://personal.atlassian.net\n"
    if secrets is not None:
        body += f"    secrets: {secrets}\n"

    with pytest.raises(KnowledgeConfigError) as excinfo:
        _registry(tmp_path, {"globex": "", "personal": body}, default="globex")

    assert excinfo.value.code == errors.E_MANIFEST_INVALID
    assert "integrations.confluence.secrets" in excinfo.value.message


def test_the_default_vault_may_fall_back_to_the_operators_confluence_variables(tmp_path: Path) -> None:
    body = "integrations:\n  confluence:\n    site: https://globex.atlassian.net\n"

    loaded = _registry(tmp_path, {"globex": body, "personal": ""}, default="globex")

    assert loaded.default_vault().integrations["confluence"]["site"] == "https://globex.atlassian.net"


# --- where a credential may travel ---------------------------------------------------


@pytest.mark.parametrize(
    "site",
    [
        "https://collector.example.test",
        "https://atlassian.net",
        "https://example.atlassian.net.example.test",
        "https://example.atlassian.net@collector.example.test",
        "https://user:pass@example.atlassian.net",
        "http://example.atlassian.net",
    ],
)
def test_a_site_off_atlassian_cloud_is_refused(tmp_path: Path, site: str) -> None:
    with pytest.raises(KnowledgeConfigError) as excinfo:
        _registry(tmp_path, {"globex": _jira("globex", site)}, default="globex")

    assert excinfo.value.code == errors.E_MANIFEST_INVALID
    assert "integrations.jira.site" in excinfo.value.message
    assert settings.INTEGRATION_SITES_ENV in excinfo.value.message


@pytest.mark.parametrize(
    ("listed", "site"),
    [
        ("jira.corp.example", "https://jira.corp.example"),
        (".corp.example", "https://jira.corp.example/"),
        (" JIRA.corp.example. , other.example", "https://jira.corp.example:8443"),
    ],
)
def test_the_operator_lists_more_hosts(tmp_path: Path, listed: str, site: str) -> None:
    loaded = _registry(
        tmp_path, {"globex": _jira("globex", site)}, default="globex", KNOWLEDGE_INTEGRATION_SITES=listed
    )

    assert loaded.default_vault().integrations["jira"]["site"] == site


def test_a_suffix_never_matches_the_bare_domain() -> None:
    assert settings.integration_site_allowed("https://acme.atlassian.net")
    assert not settings.integration_site_allowed("https://atlassian.net")
    assert not settings.integration_site_allowed("https://evilatlassian.net")
    assert settings.integration_site_allowed("http://127.0.0.1:8080")


def test_the_client_refuses_an_unlisted_site_even_for_a_direct_caller() -> None:
    from knowledge import jira_client

    with pytest.raises(KnowledgeToolError) as excinfo:
        jira_client.JiraClient("https://collector.example.test", "bot@example.test", "not-a-credential")
    assert excinfo.value.code == errors.E_MANIFEST_INVALID

    listed = jira_client.JiraClient(
        "https://jira.corp.example", "bot@example.test", "not-a-credential", sites=settings.integration_sites(
            {"KNOWLEDGE_INTEGRATION_SITES": "jira.corp.example"}
        )
    )
    assert "jira.corp.example" in repr(listed)


def test_auto_reaches_only_a_site_the_settings_allowed(tmp_path: Path) -> None:
    """The session hook's run inherits the check: a vault off the list never loads, so it never runs."""
    state = FakeJira()
    with jira_server(state) as url:
        loaded = _registry(tmp_path, {"globex": _jira("globex", url)}, default="globex")
        assert [spec.slug for spec in loaded.vaults] == ["globex"]
    with pytest.raises(KnowledgeConfigError):
        _registry(tmp_path / "off", {"globex": _jira("globex", "https://collector.example.test")}, default="globex")


# --- what HTTP ignores ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("extra", "where"),
    [
        ("    kind_map: {Story: story}\n", "integrations.jira.kind_map"),
        ("    status_map: {Done: closed}\n", "integrations.jira.status_map"),
        ("    target: jira\n", "integrations.jira.target"),
    ],
)
def test_http_warns_about_the_keys_only_the_jira_sync_reads(tmp_path: Path, extra: str, where: str) -> None:
    body = _jira("globex") + extra

    loaded = _registry(tmp_path, {"globex": body}, default="globex", MCP_TRANSPORT="http")

    warnings = [w for w in loaded.load_warnings if w.startswith(errors.W_EXTENSION_UNAVAILABLE_HTTP)]
    assert len(warnings) == 1 and where in warnings[0]

    stdio = tmp_path / "stdio"
    stdio.mkdir()
    with pytest.raises(KnowledgeConfigError) as excinfo:
        _registry(stdio, {"globex": body}, default="globex")
    assert where in excinfo.value.message


def test_a_sync_refuses_those_keys_whatever_loaded_the_settings(tmp_path: Path) -> None:
    body = _jira("globex") + "    kind_map: {Story: story}\n"
    loaded = _registry(tmp_path, {"globex": body}, default="globex", MCP_TRANSPORT="http")

    with pytest.raises(KnowledgeToolError) as excinfo:
        jira_sync.sync(loaded.default_vault(), env={}, memory=loaded.memory, dry_run=True)

    assert excinfo.value.code == errors.E_MANIFEST_INVALID
    assert "integrations.jira.kind_map" in excinfo.value.message


def test_http_warns_about_a_memory_hub_outside_the_vault(tmp_path: Path) -> None:
    body = "memory:\n  project: globex\n  hub: ../Hub.md\n"

    loaded = _registry(tmp_path, {"globex": body}, default="globex", MCP_TRANSPORT="http")

    assert loaded.default_vault().memory_hub is None
    assert any("memory.hub" in w and w.startswith(errors.W_EXTENSION_UNAVAILABLE_HTTP) for w in loaded.load_warnings)


@pytest.mark.parametrize(
    "body",
    [
        _jira("globex", "https://collector.example.test"),
        _jira("globex", token="OPENAI_API_KEY"),
        _jira("globex", token="PERSONAL_JIRA_TOKEN"),
    ],
    ids=["site", "server-secret", "foreign-prefix"],
)
def test_http_still_refuses_a_site_or_a_variable_it_may_not_use(tmp_path: Path, body: str) -> None:
    with pytest.raises(KnowledgeConfigError) as excinfo:
        _registry(tmp_path, {"globex": body}, default="globex", MCP_TRANSPORT="http")

    assert excinfo.value.code == errors.E_MANIFEST_INVALID


def test_vault_path_binds_the_served_slug(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    env = {"VAULT_PATH": str(vault), "CHROMA_COLLECTION": "acme"}
    (vault / "vault.yaml").write_text(_jira("acme"), encoding="utf-8")
    loaded = settings.load_settings(env, project_root=tmp_path)
    assert loaded.default_vault().integrations["jira"]["secrets"]["token_env"] == "ACME_JIRA_TOKEN"

    (vault / "vault.yaml").write_text(_jira("acme", token="INITECH_JIRA_TOKEN"), encoding="utf-8")
    with pytest.raises(KnowledgeConfigError) as excinfo:
        settings.load_settings(env, project_root=tmp_path)
    assert "integrations.jira.secrets.token_env" in excinfo.value.message


def test_vault_path_binds_the_slug_the_vault_yaml_declares(tmp_path: Path) -> None:
    """With no ``CHROMA_COLLECTION``, the vault's own ``slug`` is the served one, so its own variables bind."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "vault.yaml").write_text("slug: personal\n" + _jira("personal"), encoding="utf-8")
    loaded = settings.load_settings({"VAULT_PATH": str(vault)}, project_root=tmp_path)
    assert loaded.default_vault().slug == "personal"
    assert loaded.default_vault().integrations["jira"]["secrets"]["token_env"] == "PERSONAL_JIRA_TOKEN"

    (vault / "vault.yaml").write_text("slug: personal\n" + _jira("personal", token="INITECH_JIRA_TOKEN"), encoding="utf-8")
    with pytest.raises(KnowledgeConfigError) as excinfo:
        settings.load_settings({"VAULT_PATH": str(vault)}, project_root=tmp_path)
    assert "integrations.jira.secrets.token_env" in excinfo.value.message
