#!/bin/bash
# First-time setup for the Knowledge MCP server
# Installs dependencies, creates .env and prepares the vault's index directory

set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

echo "=== Knowledge MCP Setup ==="

# 1. Install Python dependencies
echo "Installing dependencies..."
uv sync

# 2. Check .env
if [ ! -f .env ]; then
    echo ""
    echo "Creating .env from template..."
    cp .env.example .env
    echo "WARNING: Edit .env and add your OPENAI_API_KEY"
    echo ""
fi

# 3. Prepare the index directory. Nothing is downloaded or embedded here: the
# index rebuilds from the vault on the first tool call (or now, with
# `uv run cloudfrog-knowledge migrate-index --build`). A Chroma directory left
# by an older version under .data/chroma gives its usage statistics and learned
# link weights to the new index.
echo "Preparing the index directory..."
uv run cloudfrog-knowledge migrate-index || echo "migrate-index failed: check .env (VAULT_PATH, KNOWLEDGE_VAULTS) and run it again"

echo ""
echo "=== Setup complete ==="
echo ""
echo "To use with Claude Code, add to ~/.claude/settings.json:"
echo '  "cloudfrog-knowledge": {'
echo '    "type": "stdio",'
echo '    "command": "uv",'
echo "    \"args\": [\"run\", \"--directory\", \"$PROJECT_DIR\", \"python\", \"-m\", \"knowledge.server\"]"
echo '  }'
