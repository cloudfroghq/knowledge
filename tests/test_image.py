"""The container image built from the Dockerfile: sqlite-vec, FTS5 and the start sequence.

Everything here runs inside the image, on its own Python and its own SQLite,
because those are what a deployment gets and neither is the interpreter that
runs this suite:

* ``sqlite_vec`` loads, a ``vec0`` table answers a KNN query written with
  ``k = ?`` (the only form older SQLite releases accept) and filters on a
  metadata column inside it.
* FTS5 with ``unicode61 remove_diacritics 2`` matches ``autenticacion`` against
  ``autenticación``.
* ``migrate-index`` copies usage statistics and learned weights from a Chroma
  directory mounted read-only, and a second run writes nothing.
* The entrypoint runs ``migrate-index`` before ``serve`` and keeps stdout for
  the protocol: a stdio session with the ``none`` provider answers ``search``.

Marked ``image``: the default run deselects it (``addopts`` in pyproject.toml)
because building the image takes minutes. Ring 1 runs it in its own job on
amd64 (``uv run pytest -m image``); locally it runs on the host's architecture.
Without a reachable docker daemon every test skips.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

pytestmark = pytest.mark.image

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_VAULT = ROOT / "tests" / "fixtures" / "vault"
IMAGE_TAG = "cloudfrog-knowledge:image-test"
BUILD_TIMEOUT = 1800
RUN_TIMEOUT = 300

#: Run inside the image with its Python: one JSON object on stdout.
_SQLITE_PROBE = """
import json, sqlite3
import sqlite_vec

con = sqlite3.connect(":memory:")
con.enable_load_extension(True)
sqlite_vec.load(con)
con.enable_load_extension(False)
out = {"sqlite": sqlite3.sqlite_version, "vec": con.execute("select vec_version()").fetchone()[0]}

con.execute("create virtual table f using fts5(text, tokenize='unicode61 remove_diacritics 2')")
con.execute("insert into f(text) values ('Flujo de autenticación del middleware')")
out["fts_hits"] = con.execute("select count(*) from f where f match 'autenticacion'").fetchone()[0]

con.execute(
    "create virtual table v using vec0("
    "id integer primary key, embedding float[3] distance_metric=cosine, type text)"
)
rows = [(1, [1.0, 0.0, 0.0], "doc"), (2, [0.0, 1.0, 0.0], "doc"), (3, [1.0, 0.1, 0.0], "memory")]
for rid, vec, kind in rows:
    con.execute(
        "insert into v(id, embedding, type) values (?, ?, ?)", (rid, sqlite_vec.serialize_float32(vec), kind)
    )
query = sqlite_vec.serialize_float32([1.0, 0.05, 0.0])
out["knn"] = [
    r[0]
    for r in con.execute(
        "select id from v where embedding match ? and k = ? and type != 'memory' order by distance",
        (query, 2),
    )
]
print(json.dumps(out))
"""


def _docker() -> str | None:
    docker = shutil.which("docker")
    if docker is None:
        return None
    probe = subprocess.run([docker, "info"], capture_output=True, text=True, timeout=60, check=False)
    return docker if probe.returncode == 0 else None


@pytest.fixture(scope="module")
def docker() -> str:
    found = _docker()
    if found is None:
        pytest.skip("no reachable docker daemon")
    return found


@pytest.fixture(scope="module")
def image(docker: str) -> str:
    built = subprocess.run(
        [docker, "build", "--tag", IMAGE_TAG, str(ROOT)],
        capture_output=True,
        text=True,
        timeout=BUILD_TIMEOUT,
        check=False,
    )
    assert built.returncode == 0, built.stderr[-4000:]
    return IMAGE_TAG


def _run(docker: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [docker, "run", "--rm", *args], capture_output=True, text=True, timeout=RUN_TIMEOUT, check=False
    )


def test_sqlite_vec_and_fts5_work_inside_the_image(docker: str, image: str) -> None:
    done = _run(docker, "--entrypoint", "python", image, "-c", _SQLITE_PROBE)
    assert done.returncode == 0, done.stderr
    probe = json.loads(done.stdout.strip().splitlines()[-1])
    assert probe["vec"] == "v0.1.9"
    assert probe["fts_hits"] == 1, "remove_diacritics 2 must fold the accent away"
    assert probe["knn"] == [1, 2], "KNN with k = ? must filter type inside vec0 and order by distance"


def test_the_cli_console_script_is_on_path_inside_the_image(docker: str, image: str) -> None:
    done = _run(docker, "--entrypoint", "cloudfrog-knowledge", image, "--help")
    assert done.returncode == 0, done.stderr
    assert "usage:" in done.stdout


def _chroma_source(tmp_path: Path) -> Path:
    """A Chroma directory as an older image left it: its marker file and two sidecars."""
    source = tmp_path / "chroma"
    source.mkdir()
    (source / "chroma.sqlite3").write_bytes(b"")
    (source / "usage_stats.json").write_text(json.dumps({"files": {"Runbooks/x.md": {"uses": 3.0}}}))
    (source / "link_graph.json").write_text(
        json.dumps({"outgoing": {"a.md": ["b.md"]}, "incoming": {}, "weights": {"a.md||b.md": 1.5}})
    )
    return source


def test_migrate_index_copies_the_sidecars_once_and_then_writes_nothing(
    docker: str, image: str, tmp_path: Path
) -> None:
    source = _chroma_source(tmp_path)
    script = (
        "python -m knowledge migrate-index && python -m knowledge migrate-index"
        " && python -c 'import json,sys; d=\"/data/index/vault\";"
        " print(json.dumps({\"usage\": json.load(open(d+\"/usage_stats.json\")),"
        " \"graph\": json.load(open(d+\"/link_graph.json\"))}))'"
    )
    done = _run(
        docker,
        "--env", "KNOWLEDGE_EMBEDDINGS_PROVIDER=none",
        "--env", "VAULT_PATH=/data/vault",
        "--env", "KNOWLEDGE_INDEX_DIR=/data/index/vault",
        "--volume", f"{FIXTURE_VAULT}:/data/vault:ro",
        "--volume", f"{source}:/data/chroma:ro",
        "--entrypoint", "sh",
        image,
        "-c",
        script,
    )
    assert done.returncode == 0, done.stderr
    lines = done.stdout.strip().splitlines()
    first, second, sidecars = (json.loads(line) for line in lines[-3:])
    assert first == {"vault": "vault", "outcome": "migrated", "index_version": 1, "from": "chroma"}
    assert second == {"vault": "vault", "outcome": "up_to_date", "index_version": 1, "from": None}
    assert sidecars["usage"]["files"]["Runbooks/x.md"]["uses"] == 3.0
    assert sidecars["graph"] == {"weights": {"a.md||b.md": 1.5}}


async def _stdio_search(docker: str, image: str, errlog_path: Path) -> list[dict[str, object]]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=docker,
        args=[
            "run", "--rm", "--interactive",
            "--env", "MCP_TRANSPORT=stdio",
            "--env", "KNOWLEDGE_EMBEDDINGS_PROVIDER=none",
            "--env", "VAULT_PATH=/data/vault",
            "--env", "KNOWLEDGE_INDEX_DIR=/data/index/vault",
            "--volume", f"{FIXTURE_VAULT}:/data/vault:ro",
            image,
        ],
    )
    with errlog_path.open("w", encoding="utf-8") as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=RUN_TIMEOUT)) as session:
                await session.initialize()
                result = await session.call_tool(
                    "search",
                    {"query": "middleware outage", "limit": 3, "fields": ["file_path", "title", "score"]},
                )
    assert not result.isError, result.content
    return [json.loads(block.text) for block in result.content if getattr(block, "text", None)]


def test_the_entrypoint_migrates_then_serves_search_over_stdio(docker: str, image: str, tmp_path: Path) -> None:
    errlog = tmp_path / "server.log"
    rows = asyncio.run(_stdio_search(docker, image, errlog))
    assert rows, "search over stdio returned no rows"
    assert all(isinstance(row.get("file_path"), str) and row.get("title") for row in rows)
    assert "Middleware" in rows[0]["file_path"] or "Middleware" in str(rows[0]["title"])
    log = errlog.read_text(encoding="utf-8")
    assert '"outcome": "created"' in log, "the entrypoint must run migrate-index (on stderr) before serve"
