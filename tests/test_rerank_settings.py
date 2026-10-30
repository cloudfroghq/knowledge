"""The ``rerank`` block of a ``vault.yaml`` and the operator's ``KNOWLEDGE_RERANK*`` variables.

A vault is curated content, so its block names the judge and the variable the
key comes from and never the key; the settings decide, before any request, who
may be sent a key:

* a ``key_env`` outside the provider's own names, one that belongs to another
  vault, a ``base_url`` the key could leak through and a credential written in
  the file stop the server, over stdio and over HTTP;
* any other mistake leaves only that vault without a judge, with a
  ``W_RERANK_INVALID`` warning, so a typo in one vault's file never stops a
  process that serves four;
* a declared provider whose key variable is empty starts the server too, with
  ``W_RERANK_KEY_MISSING`` naming the variable, over stdio and over HTTP;
* the operator switches judges off, narrows them or retargets them with
  variables a vault cannot set.

Every case builds its settings from an explicit ``env`` mapping and vaults under
``tmp_path``: nothing reads the developer's environment, and no judge is called.
"""

from __future__ import annotations

import dataclasses
import textwrap
from pathlib import Path
from typing import Any

import pytest
import yaml

from knowledge import errors, settings
from knowledge.errors import KnowledgeConfigError
from knowledge.registry import VaultRegistry

#: A value shaped like a real credential. It must never reach a message.
_TOKEN_SHAPED = "sk-proj-THISISNOTAREALKEYbutlookslikeone0123456789"
#: What a key variable holds in these cases; it must never reach a message or a repr.
_KEY_VALUE = "typesafe-test-key-value-0001"

_JEV = """
rerank:
  provider: jev
  default: off
  model: jev-1.13.0
  key_env: {key_env}
"""


def _jev(key_env: str = "TYPESAFE_API_KEY", extra: str = "") -> str:
    return _JEV.format(key_env=key_env) + textwrap.indent(textwrap.dedent(extra), "  ")


def _registry(
    tmp_path: Path, manifests: dict[str, str], *, default: str | None = None, **env: str
) -> settings.Settings:
    """A ``vaults.yaml`` over one directory per manifest; the first vault is the default unless told otherwise."""
    entries: list[dict[str, Any]] = []
    default = default or next(iter(manifests))
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


def _one(tmp_path: Path, block: str, **env: str) -> settings.Settings:
    return _registry(tmp_path, {"acme": block}, **env)


def _warnings(loaded: settings.Settings, code: str) -> list[str]:
    return [w for w in loaded.load_warnings if w.startswith(f"{code}:")]


# --- a vault without a block, and a valid one ------------------------------------------


def test_a_vault_without_a_rerank_block_has_no_judge(tmp_path: Path) -> None:
    rerank = _one(tmp_path, "").default_vault().rerank

    assert rerank == settings.RerankSettings()
    assert rerank.provider == "none"
    assert rerank.configured is False
    assert rerank.default_on is False


def test_a_valid_block_is_read_with_its_defaults(tmp_path: Path) -> None:
    loaded = _one(tmp_path, _jev(), TYPESAFE_API_KEY=_KEY_VALUE)
    rerank = loaded.default_vault().rerank

    assert (rerank.provider, rerank.model, rerank.key_env) == ("jev", "jev-1.13.0", "TYPESAFE_API_KEY")
    assert rerank.configured is True
    assert rerank.default_on is False
    assert (rerank.candidates, rerank.units_per_doc, rerank.timeout_ms) == (10, 2, 1500)
    assert (rerank.max_input_tokens, rerank.max_usd_per_day) == (20_000, 1.0)
    assert (rerank.conditional_gap, rerank.never_send, rerank.base_url) == (None, (), None)
    assert rerank.api_key == _KEY_VALUE
    assert loaded.load_warnings == ()


@pytest.mark.parametrize(
    ("spelling", "expected"),
    [("on", True), ("off", False), ('"on"', True), ('"off"', False)],
)
def test_default_reads_both_yaml_spellings_of_on_and_off(tmp_path: Path, spelling: str, expected: bool) -> None:
    """YAML 1.1 turns a bare ``on`` and ``off`` into booleans; the quoted words mean the same."""
    block = _jev().replace("default: off", f"default: {spelling}")

    assert _one(tmp_path, block).default_vault().rerank.default_on is expected


def test_every_key_of_the_block_is_read(tmp_path: Path) -> None:
    block = _jev(
        extra="""
        candidates: 8
        units_per_doc: 3
        timeout_ms: 900
        max_input_tokens: 12000
        max_usd_per_day: 0
        conditional_gap: 0.03
        never_send: ["Personal/", "Finance/Q4/"]
        """
    )

    rerank = _one(tmp_path, block).default_vault().rerank

    assert (rerank.candidates, rerank.units_per_doc, rerank.timeout_ms) == (8, 3, 900)
    assert (rerank.max_input_tokens, rerank.max_usd_per_day, rerank.conditional_gap) == (12_000, 0.0, 0.03)
    assert rerank.never_send == ("Personal/", "Finance/Q4/")


def test_kev_defaults_to_its_alias_and_takes_a_loopback_url(tmp_path: Path) -> None:
    block = """
    rerank:
      provider: kev
      key_env: KEV_API_KEY
      base_url: http://127.0.0.1:8787/
      timeout_ms: 30000
    """

    rerank = _one(tmp_path, block).default_vault().rerank

    assert (rerank.provider, rerank.model) == ("kev", "kev-latest")
    assert rerank.base_url == "http://127.0.0.1:8787"
    assert rerank.timeout_ms == 30_000


def test_the_key_never_reaches_a_repr(tmp_path: Path) -> None:
    loaded = _one(tmp_path, _jev(), TYPESAFE_API_KEY=_KEY_VALUE)

    assert _KEY_VALUE not in repr(loaded.default_vault().rerank)
    assert _KEY_VALUE not in repr(loaded.default_vault())
    assert "TYPESAFE_API_KEY" in repr(loaded.default_vault().rerank)


def test_a_vault_path_deployment_reads_the_block_too(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "vault.yaml").write_text(_jev(), encoding="utf-8")
    env = {
        "VAULT_PATH": str(vault),
        "KNOWLEDGE_HOME": str(tmp_path / "home"),
        "KNOWLEDGE_EMBEDDINGS_PROVIDER": "bag-of-words",
        "TYPESAFE_API_KEY": _KEY_VALUE,
    }

    rerank = settings.load_settings(env, project_root=tmp_path).default_vault().rerank

    assert (rerank.provider, rerank.api_key) == ("jev", _KEY_VALUE)


def test_a_provider_none_block_declares_no_judge(tmp_path: Path) -> None:
    rerank = _one(tmp_path, "rerank:\n  provider: none\n").default_vault().rerank

    assert rerank.configured is False


# --- what stops the server: the places a key could leak to ----------------------------


@pytest.mark.parametrize(
    ("block", "where"),
    [
        pytest.param(_jev("OPENAI_API_KEY"), "rerank.key_env", id="openai-key"),
        pytest.param(_jev("MCP_ADMIN_KEY"), "rerank.key_env", id="server-secret"),
        pytest.param(_jev("KEV_API_KEY"), "rerank.key_env", id="another-providers-key"),
        pytest.param(_jev("typesafe_api_key"), "rerank.key_env", id="lower-case"),
        pytest.param(_jev(extra="base_url: http://127.0.0.1:9\n"), "rerank.base_url", id="base-url-with-jev"),
        pytest.param(
            "rerank:\n  provider: kev\n  key_env: KEV_API_KEY\n  base_url: https://collector.example.test\n",
            "rerank.base_url",
            id="kev-url-off-loopback",
        ),
        pytest.param(
            "rerank:\n  provider: kev\n  key_env: KEV_API_KEY\n  base_url: http://user:pw@127.0.0.1:8787\n",
            "rerank.base_url",
            id="kev-url-with-credentials",
        ),
        pytest.param(
            "rerank:\n  provider: kev\n  key_env: KEV_API_KEY\n  base_url: http://127.0.0.1.example.test:8787\n",
            "rerank.base_url",
            id="kev-url-lookalike-host",
        ),
    ],
)
def test_a_key_that_could_travel_to_the_wrong_place_stops_the_server(
    tmp_path: Path, block: str, where: str
) -> None:
    with pytest.raises(KnowledgeConfigError) as excinfo:
        _one(tmp_path, block)

    assert excinfo.value.code == errors.E_MANIFEST_INVALID
    assert where in excinfo.value.message
    assert "acme/vault.yaml" in excinfo.value.message


def test_a_secret_written_in_the_block_stops_the_server_without_echoing_it(tmp_path: Path) -> None:
    with pytest.raises(KnowledgeConfigError) as excinfo:
        _one(tmp_path, _jev(extra=f"api_key: {_TOKEN_SHAPED}\n"))
    assert excinfo.value.code == errors.E_SECRET_IN_MANIFEST
    assert _TOKEN_SHAPED not in str(excinfo.value)

    with pytest.raises(KnowledgeConfigError) as excinfo:
        _one(tmp_path / "again", _jev(key_env=_TOKEN_SHAPED))
    assert excinfo.value.code == errors.E_SECRET_IN_MANIFEST
    assert _TOKEN_SHAPED not in str(excinfo.value)


def test_a_vault_never_names_another_vaults_key(tmp_path: Path) -> None:
    manifests = {"acme": _jev(), "personal": _jev("ACME_TYPESAFE_API_KEY")}

    with pytest.raises(KnowledgeConfigError) as excinfo:
        _registry(tmp_path, manifests, default="acme")

    assert excinfo.value.code == errors.E_MANIFEST_INVALID
    assert "personal/vault.yaml" in excinfo.value.message
    assert "rerank.key_env" in excinfo.value.message
    assert "PERSONAL_TYPESAFE_API_KEY" in excinfo.value.message


def test_only_the_default_vault_may_name_the_bare_variable(tmp_path: Path) -> None:
    manifests = {"acme": _jev(), "personal": _jev("PERSONAL_TYPESAFE_API_KEY")}
    loaded = _registry(tmp_path, manifests, default="acme")
    assert loaded.vault("acme").rerank.key_env == "TYPESAFE_API_KEY"
    assert loaded.vault("personal").rerank.key_env == "PERSONAL_TYPESAFE_API_KEY"

    swapped = tmp_path / "swapped"
    swapped.mkdir()
    with pytest.raises(KnowledgeConfigError) as excinfo:
        _registry(swapped, manifests, default="personal")
    assert "acme/vault.yaml" in excinfo.value.message
    assert "ACME_TYPESAFE_API_KEY" in excinfo.value.message


def test_a_slug_with_dashes_names_an_underscore_prefix(tmp_path: Path) -> None:
    manifests = {"acme": "", "cd-knowledge": _jev("CD_KNOWLEDGE_TYPESAFE_API_KEY")}

    loaded = _registry(tmp_path, manifests, default="acme")

    assert loaded.vault("cd-knowledge").rerank.key_env == "CD_KNOWLEDGE_TYPESAFE_API_KEY"


def test_the_binding_check_names_variables_of_this_vault() -> None:
    rerank = settings.RerankSettings(provider="jev", key_env="OTHER_TYPESAFE_API_KEY")

    assert settings.rerank_binding_problems(rerank, "initech", is_default=False)[0][0] == "rerank.key_env"
    assert settings.rerank_binding_problems(rerank, "other", is_default=False) == []
    assert settings.rerank_binding_problems(settings.RerankSettings(), "initech", is_default=False) == []


# --- what leaves only that vault without a judge ---------------------------------------


@pytest.mark.parametrize(
    ("block", "where"),
    [
        pytest.param("rerank:\n  provider: openai\n", "rerank.provider", id="unknown-provider"),
        pytest.param(_jev().replace("jev-1.13.0", "jev-latest"), "rerank.model", id="alias-model"),
        pytest.param(_jev().replace("jev-1.13.0", "gpt-4"), "rerank.model", id="foreign-model"),
        pytest.param(_jev().replace("  model: jev-1.13.0\n", ""), "rerank.model", id="jev-without-a-model"),
        pytest.param(_jev().replace("  key_env: TYPESAFE_API_KEY\n", ""), "rerank.key_env", id="no-key-env"),
        pytest.param(_jev(extra="candidates: 13\n"), "rerank.candidates", id="candidates-above"),
        pytest.param(_jev(extra="candidates: 0\n"), "rerank.candidates", id="candidates-below"),
        pytest.param(_jev(extra="candidates: true\n"), "rerank.candidates", id="candidates-bool"),
        pytest.param(_jev(extra="units_per_doc: 4\n"), "rerank.units_per_doc", id="units-above"),
        pytest.param(_jev(extra="timeout_ms: 100\n"), "rerank.timeout_ms", id="timeout-below"),
        pytest.param(_jev(extra="timeout_ms: 60000\n"), "rerank.timeout_ms", id="jev-timeout-above"),
        pytest.param(_jev(extra="max_input_tokens: 32000\n"), "rerank.max_input_tokens", id="tokens-above"),
        pytest.param(_jev(extra="max_usd_per_day: -1\n"), "rerank.max_usd_per_day", id="negative-budget"),
        pytest.param(_jev(extra="conditional_gap: 0\n"), "rerank.conditional_gap", id="gap-zero"),
        pytest.param(_jev(extra="never_send: Personal/\n"), "rerank.never_send", id="never-send-not-a-list"),
        pytest.param(_jev(extra='never_send: ["/etc"]\n'), "rerank.never_send", id="never-send-absolute"),
        pytest.param(_jev(extra='never_send: ["../x"]\n'), "rerank.never_send", id="never-send-dotdot"),
        pytest.param(_jev(extra="speed: fast\n"), "rerank.speed", id="unknown-key"),
        pytest.param(_jev().replace("default: off", "default: sometimes"), "rerank.default", id="bad-default"),
        pytest.param("rerank: jev\n", "rerank", id="not-a-mapping"),
    ],
)
def test_a_malformed_block_leaves_only_that_vault_without_a_judge(tmp_path: Path, block: str, where: str) -> None:
    manifests = {"acme": block, "personal": _jev("PERSONAL_TYPESAFE_API_KEY")}

    loaded = _registry(tmp_path, manifests, default="acme", PERSONAL_TYPESAFE_API_KEY=_KEY_VALUE)

    assert loaded.vault("acme").rerank.configured is False
    assert loaded.vault("personal").rerank.configured is True, "the other vault keeps its judge"
    found = _warnings(loaded, errors.W_RERANK_INVALID)
    assert len(found) == 1
    assert found[0].startswith(f"{errors.W_RERANK_INVALID}: acme: {where} ")
    assert "searches without a judge" in found[0]
    loaded.validate()


def test_the_alias_message_says_why_it_is_refused(tmp_path: Path) -> None:
    loaded = _one(tmp_path, _jev().replace("jev-1.13.0", "jev-latest"))

    assert "alias that changes without notice" in _warnings(loaded, errors.W_RERANK_INVALID)[0]


def test_a_malformed_block_keeps_the_variable_check_fatal(tmp_path: Path) -> None:
    """Being forgiven for a typo never forgives a key sent where it must not go."""
    block = _jev("OPENAI_API_KEY", extra="candidates: 99\n")

    with pytest.raises(KnowledgeConfigError) as excinfo:
        _one(tmp_path, block)

    assert excinfo.value.code == errors.E_MANIFEST_INVALID


# --- a declared judge whose key is missing ---------------------------------------------


def test_a_missing_key_warns_with_the_variable_name_and_the_server_starts(tmp_path: Path) -> None:
    loaded = _one(tmp_path, _jev(extra=""))

    warnings = [w for w in loaded.validate() if w.startswith(f"{errors.W_RERANK_KEY_MISSING}:")]

    assert len(warnings) == 1
    assert "acme" in warnings[0]
    assert "TYPESAFE_API_KEY" in warnings[0]
    assert _KEY_VALUE not in warnings[0]


def test_a_present_key_does_not_warn(tmp_path: Path) -> None:
    loaded = _one(tmp_path, _jev(), TYPESAFE_API_KEY=_KEY_VALUE)

    assert not [w for w in loaded.validate() if errors.W_RERANK_KEY_MISSING in w]


@pytest.mark.parametrize("default", ["on", "off"])
def test_over_http_a_missing_key_never_stops_the_server(tmp_path: Path, default: str) -> None:
    """A deployment that autodeploys must not exit 2 because a key is not loaded yet."""
    block = _jev().replace("default: off", f"default: {default}")
    loaded = _one(tmp_path, block, MCP_TRANSPORT="http", MCP_API_KEY="read-only-bearer")

    warnings = loaded.validate()

    assert any(w.startswith(f"{errors.W_RERANK_KEY_MISSING}:") for w in warnings)


def test_a_vault_without_a_judge_never_warns_about_a_key(tmp_path: Path) -> None:
    loaded = _one(tmp_path, "")

    assert not [w for w in loaded.validate() if errors.W_RERANK_KEY_MISSING in w]


def test_a_judge_the_operator_switched_off_needs_no_key(tmp_path: Path) -> None:
    loaded = _one(tmp_path, _jev(), KNOWLEDGE_RERANK="off")

    assert not [w for w in loaded.validate() if errors.W_RERANK_KEY_MISSING in w]


# --- the operator -----------------------------------------------------------------------


def test_the_operator_defaults_allow_every_provider() -> None:
    operator = settings.load_settings({"VAULT_PATH": "x"}, project_root=Path("/nonexistent")).rerank_operator

    assert operator == settings.RerankOperator()
    assert operator.allows("jev") and operator.allows("kev")
    assert not operator.allows("none")


def test_knowledge_rerank_off_switches_every_judge_off(tmp_path: Path) -> None:
    loaded = _one(tmp_path, _jev(), TYPESAFE_API_KEY=_KEY_VALUE, KNOWLEDGE_RERANK="off")

    assert loaded.rerank_operator.off is True
    assert not loaded.rerank_operator.allows("jev")
    assert loaded.load_warnings == ()


def test_an_empty_switch_leaves_it_to_the_vaults(tmp_path: Path) -> None:
    loaded = _one(tmp_path, _jev(), KNOWLEDGE_RERANK="")

    assert loaded.rerank_operator.off is False


def test_a_switch_that_is_not_off_still_switches_off_and_says_so(tmp_path: Path) -> None:
    """A typo in the variable meant to stop content leaving the machine must not leave rerank running."""
    loaded = _one(tmp_path, _jev(), KNOWLEDGE_RERANK="of")

    assert loaded.rerank_operator.off is True
    assert len(_warnings(loaded, errors.W_RERANK_INVALID)) == 1


def test_the_operator_narrows_the_providers_a_vault_may_use(tmp_path: Path) -> None:
    loaded = _one(tmp_path, _jev(), KNOWLEDGE_RERANK_PROVIDERS="kev")

    assert loaded.rerank_operator.providers == frozenset({"kev"})
    assert loaded.rerank_operator.allows("kev")
    assert not loaded.rerank_operator.allows("jev")
    assert loaded.default_vault().rerank.provider == "jev", "the declaration stays; the process decides"


def test_a_providers_list_with_nothing_known_allows_no_provider(tmp_path: Path) -> None:
    loaded = _one(tmp_path, _jev(), KNOWLEDGE_RERANK_PROVIDERS="gpt,claude")

    assert loaded.rerank_operator.providers == frozenset()
    assert not loaded.rerank_operator.allows("jev")
    assert len(_warnings(loaded, errors.W_RERANK_INVALID)) == 1
    assert "gpt" not in _warnings(loaded, errors.W_RERANK_INVALID)[0]


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:9100", "http://localhost:9100/", "http://[::1]:9100", "https://api.typesafe.ai"]
)
def test_the_operator_may_retarget_the_judge_to_loopback_or_typesafe(tmp_path: Path, url: str) -> None:
    loaded = _one(tmp_path, "", KNOWLEDGE_RERANK_BASE_URL=url)

    assert loaded.rerank_operator.base_url == url.rstrip("/")
    assert loaded.load_warnings == ()


@pytest.mark.parametrize(
    "url",
    [
        "https://collector.example.test",
        "http://api.typesafe.ai",
        "https://api.typesafe.ai/v1/systemone",
        "https://api.typesafe.ai@collector.example.test",
        "http://169.254.169.254",
    ],
)
def test_an_operator_url_a_key_could_leak_through_is_ignored_with_a_warning(tmp_path: Path, url: str) -> None:
    loaded = _one(tmp_path, "", KNOWLEDGE_RERANK_BASE_URL=url)

    assert loaded.rerank_operator.base_url is None
    assert len(_warnings(loaded, errors.W_RERANK_INVALID)) == 1


@pytest.mark.parametrize(("raw", "expected"), [("800", 800), ("60000", 60_000), ("100", None), ("fast", None)])
def test_the_operator_timeout_is_bounded(tmp_path: Path, raw: str, expected: int | None) -> None:
    loaded = _one(tmp_path, "", KNOWLEDGE_RERANK_TIMEOUT_MS=raw)

    assert loaded.rerank_operator.timeout_ms == expected
    assert bool(_warnings(loaded, errors.W_RERANK_INVALID)) is (expected is None)


# --- composition --------------------------------------------------------------------------


def test_the_suite_pins_every_rerank_variable_empty() -> None:
    """``conftest`` leaves nothing of the developer's shell that could address a judge."""
    import os

    for name in (
        "TYPESAFE_API_KEY",
        "KEV_API_KEY",
        "KNOWLEDGE_RERANK",
        "KNOWLEDGE_RERANK_PROVIDERS",
        "KNOWLEDGE_RERANK_BASE_URL",
        "KNOWLEDGE_RERANK_TIMEOUT_MS",
    ):
        assert os.environ.get(name) == "", name
    assert settings.current().rerank_operator == settings.RerankOperator()


def test_a_declared_judge_does_not_change_what_the_registry_builds(tmp_path: Path) -> None:
    """The settings only describe a judge; composing one is for the factory the server injects."""
    loaded = _one(tmp_path, _jev(), TYPESAFE_API_KEY=_KEY_VALUE)
    registry = VaultRegistry.from_settings(loaded)

    runtime = registry.default

    assert runtime.spec.rerank.provider == "jev"
    assert runtime.judge.name == "none"
    assert dataclasses.is_dataclass(runtime.spec.rerank)
