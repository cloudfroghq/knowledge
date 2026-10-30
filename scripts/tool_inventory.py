#!/usr/bin/env python3
"""Generate ``docs/tools/inventory.md`` from the tool catalog the server really serves.

The catalog is read the way a client reads it: a real ``initialize`` and a
real ``tools/list`` over the stdio transport, against the golden launcher
(``tests/golden_stdio_server.py``) on a copy of the fixture vault whose
``vault.yaml`` declares every extension that brings a tool (``flows``,
``governance``, ``confluence`` and ``jira``), so the tools of every gated
extension are listed too: the catalog of ``tools_list__features`` with
``sync_jira`` at the end. Nothing is parsed
from the source, so the inventory cannot describe a tool the server does not
register, or miss one it does. ``tools/list`` never builds the index, so the
launch is cheap and touches no network.

``--check`` regenerates in memory and exits 1 when the committed file differs:
the drift gate (``tests/test_tool_inventory.py`` runs the same comparison).

Usage::

    uv run python scripts/tool_inventory.py            # rewrite the inventory
    uv run python scripts/tool_inventory.py --check    # fail on drift
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INVENTORY = PROJECT_ROOT / "docs" / "tools" / "inventory.md"
FIXTURE_VAULT = PROJECT_ROOT / "tests" / "fixtures" / "vault"
GOLDEN_SERVER = PROJECT_ROOT / "tests" / "golden_stdio_server.py"
PLACEHOLDER_KEY = "placeholder-not-a-credential"
#: ``vault.yaml`` of the vault the catalog is read from: the fixture's
#: content with every extension that brings a tool declared, credentials left unset.
JIRA_MANIFEST = (
    "slug: cloudfrog\n"
    "features: [flows, governance, confluence, jira]\n"
    "memory:\n  project: cloudfrog\n"
    "integrations:\n"
    "  jira:\n"
    "    site: https://jira.invalid\n"
    "    projects: [EXMP]\n"
    "    secrets: {email_env: CLOUDFROG_JIRA_EMAIL, token_env: CLOUDFROG_JIRA_TOKEN}\n"
)
REGENERATE = "uv run python scripts/tool_inventory.py"


async def fetch_catalog() -> dict[str, Any]:
    """``serverInfo``, protocol version, ``instructions`` and the ``tools/list`` result, as wire JSON."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    with tempfile.TemporaryDirectory(prefix="knowledge-inventory-") as tmp:
        vault = Path(tmp) / "vault"
        shutil.copytree(FIXTURE_VAULT, vault)
        (vault / "vault.yaml").write_text(JIRA_MANIFEST, encoding="utf-8")
        env = {
            "PATH": os.environ.get("PATH", ""),
            "VAULT_PATH": str(vault),
            "KNOWLEDGE_INDEX_DIR": str(Path(tmp) / "index"),
            "OPENAI_API_KEY": PLACEHOLDER_KEY,
            "MCP_TRANSPORT": "stdio",
            "CONFLUENCE_URL": "",
            "CONFLUENCE_EMAIL": "",
            "CONFLUENCE_TOKEN": "",
            "CLOUDFROG_JIRA_EMAIL": "",
            "CLOUDFROG_JIRA_TOKEN": "",
            # The catalog of the default profile, whatever a local ``.env`` says.
            "KNOWLEDGE_PROFILE": "",
            # jira.invalid never resolves; the operator's list lets the manifest name it.
            "KNOWLEDGE_INTEGRATION_SITES": "jira.invalid",
            # The catalog of the defaults, whatever a local ``.env`` says.
            "KNOWLEDGE_STRUCTURED_OUTPUT": "",
            "KNOWLEDGE_TEXT_INDENT": "",
        }
        params = StdioServerParameters(
            command=sys.executable, args=[str(GOLDEN_SERVER)], env=env, cwd=str(PROJECT_ROOT)
        )
        with open(Path(tmp) / "server.log", "w", encoding="utf-8") as errlog:
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=120)) as session:
                    init = await session.initialize()
                    tools = await session.list_tools()
    return {
        "server_info": init.serverInfo.model_dump(mode="json", exclude_none=True),
        "protocol_version": init.protocolVersion,
        "instructions": init.instructions,
        "tools": tools.model_dump(mode="json", by_alias=True, exclude_none=True)["tools"],
    }


# --- rendering ----------------------------------------------------------------


def _type_of(schema: dict[str, Any]) -> str:
    if "type" in schema:
        return str(schema["type"])
    if "anyOf" in schema:
        return " \\| ".join(_type_of(s) for s in schema["anyOf"])
    return "any"


def _one_line(text: str) -> str:
    return " ".join(text.split())


#: End of the first sentence: a point and a space, except after an abbreviation.
_SENTENCE_END = re.compile(r"(?<!e\.g)(?<!i\.e)\. ")


def _summary(description: str) -> str:
    """First sentence of the description, as the catalog states it, safe inside a table cell."""
    flat = _one_line(description)
    first = _SENTENCE_END.split(flat, maxsplit=1)[0].rstrip(".") + "."
    return first.replace("|", "\\|")


def _params(tool: dict[str, Any]) -> list[tuple[str, str, bool, str]]:
    schema = tool.get("inputSchema", {})
    required = set(schema.get("required", []))
    out = []
    for name, prop in schema.get("properties", {}).items():
        default = "—" if "default" not in prop else f"`{json.dumps(prop['default'], ensure_ascii=False)}`"
        out.append((name, _type_of(prop), name in required, default))
    return out


def _output(tool: dict[str, Any]) -> str:
    schema = tool.get("outputSchema")
    if not schema:
        return "no declara `outputSchema`"
    result = schema.get("properties", {}).get("result", {})
    return f"`outputSchema` derivado de la anotación de retorno (`result`: `{_type_of(result)}`)"


#: Prefix of the first ``instructions`` line that names the contract version.
CONTRACT_LINE_PREFIX = "knowledge-contract: "


def declared_contract_version(instructions: str | None) -> str | None:
    """The contract version the handshake announces on the first ``instructions`` line."""
    first = (instructions or "").splitlines()[:1]
    if first and first[0].startswith(CONTRACT_LINE_PREFIX):
        return first[0].removeprefix(CONTRACT_LINE_PREFIX).strip() or None
    return None


def _contract_section(catalog: dict[str, Any]) -> str:
    info = catalog["server_info"]
    sdk = (
        f"`serverInfo` dice `name: {info.get('name')}`, `version: {info.get('version')}`, pero esa versión es la "
        "del SDK `mcp` (FastMCP la rellena con la versión del paquete cuando el servidor no declara una), no la "
        f"del contrato. Protocolo negociado: `{catalog['protocol_version']}`."
    )
    version = declared_contract_version(catalog.get("instructions"))
    if version is None:
        return (
            "**Ninguna declarada.** El servidor no anuncia una versión del contrato de tools: ni el catálogo ni "
            f"ninguna tool la llevan. {sdk}"
        )
    return (
        f"**{version}.** El servidor la anuncia en la primera línea de `instructions` del `initialize` "
        f"(`{CONTRACT_LINE_PREFIX}{version}`); su historial vive en `docs/contract/CHANGELOG.md`. {sdk}"
    )


def _since(name: str) -> str:
    """The contract version a tool first appeared in (``knowledge.contract.TOOL_SINCE``)."""
    from knowledge import contract

    return contract.TOOL_SINCE.get(name, "—")


def _extension(name: str) -> str:
    """The extension a tool belongs to, as the summary table shows it; ``núcleo`` for the core tools."""
    from knowledge import contract

    extension = contract.TOOL_EXTENSION.get(name)
    return f"`{extension}`" if extension else "núcleo"


def _gated(name: str) -> str | None:
    """The note of a tool that only a vault declaring its extension brings."""
    from knowledge import extensions

    for ext in extensions.EXTENSIONS:
        if ext.gated and name in ext.tools:
            transports = " y ".join(sorted(ext.transports))
            legacy = " El perfil `legacy` la registra sin mirar `features`." if ext.legacy_always else ""
            return (
                f"Solo aparece cuando algún vault servido declara la extensión `{ext.name}` en `features` "
                f"(transporte {transports}); sin ella el catálogo no la lista y llamarla responde "
                f"`E_FEATURE_DISABLED`.{legacy}"
            )
    return None


def _profiles_section() -> list[str]:
    """What each public profile registers and the tools the contract retired."""
    from knowledge import contract

    lines = [
        "## Perfiles",
        "",
        f"`{contract.DEFAULT_PROFILE}` (el default) registra las tools del núcleo y, después, las de cada extensión "
        f"que algún vault servido declara: es el catálogo de arriba. `{contract.LEGACY_PROFILE}` registra el "
        "catálogo de 1.4.0, nombre por nombre y en su orden, con las respuestas de este contrato: las tools de "
        "`flows`, `governance` y `confluence` sin mirar `features`, y las tools retiradas. Se elige con "
        "`serve --profile` o `KNOWLEDGE_PROFILE` (la bandera gana).",
        "",
        "## Retiradas",
        "",
        "| Tool | Retirada en | Bajo `all` |",
        "|---|---|---|",
    ]
    for name, version in contract.TOOL_REMOVED.items():
        lines.append(
            f"| `{name}` | {version} | `E_TOOL_RETIRED`; solo el perfil `{contract.LEGACY_PROFILE}` la lista |"
        )
    return lines


def _deprecation(tool: dict[str, Any]) -> str | None:
    meta = tool.get("_meta") or {}
    if not meta.get("knowledge/deprecated"):
        return None
    removal = meta.get("knowledge/removal")
    return f"Obsoleta (`_meta.knowledge/deprecated`); se retira en {removal}." if removal else "Obsoleta."


def _output_mode(tools: list[dict[str, Any]]) -> str:
    if all(tool.get("outputSchema") for tool in tools):
        return (
            "Salida estructurada activa (`KNOWLEDGE_STRUCTURED_OUTPUT=1`): cada tool declara `outputSchema` y "
            "cada respuesta trae `structuredContent`, el mismo JSON que sus bloques de texto."
        )
    return (
        "Salida de texto (`KNOWLEDGE_STRUCTURED_OUTPUT=0`, el default): los bloques de texto son la respuesta "
        "canónica de cada tool. Con `1` cada tool declara además `outputSchema` y cada respuesta trae "
        "`structuredContent`, el mismo JSON; el orden del catálogo no cambia."
    )


def render(catalog: dict[str, Any]) -> str:
    tools = catalog["tools"]
    lines = [
        "# Inventario de tools",
        "",
        f"<!-- Generated by `{REGENERATE}` from the live tools/list catalog. Do not edit by hand. -->",
        "",
        "Catálogo que el servidor anuncia por `tools/list`, leído por stdio con un handshake real (no se deriva "
        "del código fuente). `tests/test_tool_inventory.py` falla si este archivo y el catálogo divergen; se "
        f"regenera con `{REGENERATE}`.",
        "",
        "## Versión de contrato",
        "",
        _contract_section(catalog),
        "",
        _output_mode(tools),
        "",
        f"## Resumen ({len(tools)} tools, en orden de registro)",
        "",
        "| # | Tool | Desde | Extensión | Parámetros (requeridos en negrita) | Resumen |",
        "|---|---|---|---|---|---|",
    ]
    for i, tool in enumerate(tools, start=1):
        params = ", ".join(f"**{n}**" if req else n for n, _, req, _ in _params(tool)) or "—"
        lines.append(
            f"| {i} | `{tool['name']}` | {_since(tool['name'])} | {_extension(tool['name'])} | {params} | "
            f"{_summary(tool.get('description', ''))} |"
        )
    lines += ["", *_profiles_section()]

    for tool in tools:
        lines += ["", f"## `{tool['name']}`", "", _one_line(tool.get("description", "")) or "(sin descripción)", ""]
        lines += [f"Desde el contrato {_since(tool['name'])}.", ""]
        deprecation = _deprecation(tool)
        if deprecation:
            lines += [deprecation, ""]
        gated = _gated(tool["name"])
        if gated:
            lines += [gated, ""]
        params = _params(tool)
        if params:
            lines += ["| Parámetro | Tipo | Requerido | Default |", "|---|---|---|---|"]
            lines += [f"| `{n}` | {t} | {'sí' if req else 'no'} | {d} |" for n, t, req, d in params]
        else:
            lines.append("Sin parámetros.")
        lines += ["", f"Respuesta: {_output(tool)}."]
    return "\n".join(lines) + "\n"


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if the committed inventory is out of date")
    parser.add_argument("--output", type=Path, default=INVENTORY)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    rendered = render(asyncio.run(fetch_catalog()))
    if args.check:
        current = args.output.read_text(encoding="utf-8") if args.output.is_file() else ""
        if current != rendered:
            print(f"inventory: {args.output} is out of date; run `{REGENERATE}`", file=sys.stderr)
            return 1
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
