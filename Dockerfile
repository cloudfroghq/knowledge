# syntax=docker/dockerfile:1.7

# ---------------------------------------------------------------------------
# Stage 1: builder — install deps with uv into a self-contained venv
# ---------------------------------------------------------------------------
FROM python:3.12-slim-trixie AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

RUN pip install --no-cache-dir uv

WORKDIR /app

# Copy dependency manifests first to maximize layer cache reuse
COPY pyproject.toml uv.lock ./

# Install runtime dependencies into /app/.venv
RUN uv sync --frozen --no-dev --no-install-project

# Install the project itself, non-editable, so /app/.venv/bin carries the
# cloudfrog-knowledge console script ([project.scripts]). The venv sits at the
# same path in both stages, so the script's shebang (/app/.venv/bin/python)
# resolves in the runtime stage. PYTHONPATH=/app/src there still puts the copied
# source ahead of site-packages: the code imported is the same as before.
#
# pyproject.toml declares readme = "README.md", which hatchling reads while it
# builds the wheel, so the file has to be in the builder's context. It stays out
# of the dependency-only layer above: editing it must not invalidate that cache.
COPY README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

# ---------------------------------------------------------------------------
# Stage 2: runtime — slim image with git + curl for webhook + healthcheck
# ---------------------------------------------------------------------------
FROM python:3.12-slim-trixie AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl openssh-client \
    && rm -rf /var/lib/apt/lists/*

# Where each vault's index lives (index.sqlite and its sidecars):
#
# * KNOWLEDGE_HOME=/data/index is the data root: a vaults.yaml deployment keeps
#   one directory per vault under it (/data/index/<slug>).
# * CHROMA_PERSIST_DIR=/data/chroma stays for deployments whose compose is not
#   in this repository and mounts its volume there: with no KNOWLEDGE_INDEX_DIR
#   the index is written beside chroma.sqlite3, inside that volume, instead of
#   in an unmounted directory that a redeploy would empty (and re-embed).
# * The composes in infra/ set KNOWLEDGE_INDEX_DIR=/data/index/<slug> on a
#   volume of its own, which wins over both; /data/chroma is then only where
#   migrate-index copies usage statistics and learned link weights from.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PATH=/app/.venv/bin:$PATH \
    MCP_TRANSPORT=http \
    VAULT_PATH=/data/vault \
    KNOWLEDGE_HOME=/data/index \
    CHROMA_PERSIST_DIR=/data/chroma

WORKDIR /app

# Copy the prebuilt venv from the builder stage
COPY --from=builder /app/.venv /app/.venv

# Copy the application source. No config.json ships in the image: review
# thresholds and the rest of a vault's settings come from the vault's own
# vault.yaml, or from a file named by KNOWLEDGE_CONFIG_JSON.
COPY src ./src

# Entrypoint: gets the vaults into place (git clone only for a vault that
# declares a remote: VAULT_REMOTE, or `repo` in KNOWLEDGE_VAULTS_YAML), runs
# migrate-index (no embedding) and then starts the server. The resolver turns
# KNOWLEDGE_VAULTS_YAML into the vaults.yaml the server reads.
COPY infra/bootstrap-vault.sh /usr/local/bin/knowledge-bootstrap
COPY infra/resolve-vaults.py /usr/local/bin/knowledge-resolve-vaults
RUN chmod 0755 /usr/local/bin/knowledge-bootstrap /usr/local/bin/knowledge-resolve-vaults

# Persistent data mounts
RUN mkdir -p /data/index /data/chroma /data/vault

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["knowledge-bootstrap"]
