# Makefile — one entry point for the pipeline that installs the AI contexts a vault declares
# (ADR-036, ADR-047) and builds the code graphs (ADR-023, ADR-037) across the declared checkouts.
#
# Every target is a facade over cloudfrog-knowledge init or scripts/*.sh. Nothing here
# reimplements what they already do, and no script is moved to suit this file: the GitHub
# workflows name several of them by path, and tests/test_pipeline_contract.py fails the suite
# the moment one of those paths stops existing.
#
# The registry (the Contexts/config.json of the vault this machine initialized, resolved by
# scripts/lib/repo-registry.sh) is the only answer to "which checkouts are there and where".
# It is sourced, never parsed here: two answers to that question drift apart the first time an
# override is added to one.
#
# Recipes are backslash-continued single logical lines because the make macOS ships is
# 3.81, which has neither .ONESHELL nor .SHELLFLAGS. A `#` inside such a line would comment
# out everything after it on the same line, so every explanation lives above its target.
SHELL := /bin/bash

ROOT := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
SCRIPTS := $(ROOT)/scripts
REGISTRY := $(SCRIPTS)/lib/repo-registry.sh

# One checkout instead of all of them: `make graphs REPO=<id from repos.entries>`.
REPO ?=
# Extra arguments for cloudfrog-knowledge init, e.g. `make ai-context INIT_ARGS="--vault acme --yes"`.
INIT_ARGS ?=
# FORCE=1 rebuilds a graph that already matches its repository HEAD.
FORCE ?=

FORCE_FLAG := $(if $(FORCE),--force,)
IDS = $(if $(REPO),$(REPO),$$(repo_ids))

# graph.targets may name one repository several times, once per surface read out of it.
# The graph is built per checkout, so the ids are deduplicated and each checkout is
# visited once however many surfaces its entry declares.
TARGET_IDS_PY := from knowledge import graph_targets; print("\n".join(dict.fromkeys(t["repo"] for t in graph_targets.graph_targets_detail() if t["repo"])))
REGISTERED_PY := import json, os; p = os.path.expanduser("~/.claude.json"); c = json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {}; print("\n".join(k for k, v in (c.get("projects") or {}).items() if "graphify" in ((v or {}).get("mcpServers") or {})))
BUILT_AT_PY := import json, sys; print(json.load(open(sys.argv[1], encoding="utf-8")).get("built_at_commit") or "")

.PHONY: help setup ai-context ai-context-check graphs platform-graph doctor

help:
	@echo "Targets:"
	@echo "  setup             ai-context, then the graphs, then platform-graph, then doctor."
	@echo "                    Incremental: a graph already built at its repository HEAD is"
	@echo "                    skipped, so only the first run is slow."
	@echo "  ai-context        Run cloudfrog-knowledge init: install each vault's Contexts/<context>"
	@echo "                    package into the checkouts present on this machine."
	@echo "  ai-context-check  Run cloudfrog-knowledge init --dry-run: fail when a checkout"
	@echo "                    holds a copy that drifted or a file init does not manage."
	@echo "  graphs            Build the code graph of every declared graph target."
	@echo "  platform-graph    Merge the per-repo graphs into one."
	@echo "  doctor            Report context, graph freshness and MCP registration."
	@echo "                    Read-only, and exits 0 even when it finds gaps."
	@echo ""
	@echo "Variables:"
	@echo "  REPO=<id>         Restrict graphs and doctor to one checkout."
	@echo "  INIT_ARGS=...     Extra arguments for ai-context and ai-context-check."
	@echo "  FORCE=1           Rebuild a graph that already matches its repository HEAD."

setup:
	@echo "setup: building every missing or stale graph. The first run takes tens of minutes"
	@echo "       and leaves hundreds of MB under graphify-out/; later runs skip what is current."
	@$(MAKE) --no-print-directory ai-context
	@$(MAKE) --no-print-directory graphs
	@$(MAKE) --no-print-directory platform-graph
	@$(MAKE) --no-print-directory doctor

# init installs the package of every checkout this machine has and skips the absent ones, so
# there is nothing to loop over here. It asks about a root it cannot locate unless INIT_ARGS
# carries --yes; a copy-mode checkout that drifted needs INIT_ARGS=--refresh.
ai-context:
	@uv run cloudfrog-knowledge init $(INIT_ARGS)

# A gate: --dry-run writes nothing and exits 1 for a checkout whose copy drifted from its
# package or whose .claude, CLAUDE.md or .mcp.json is a file of its own.
ai-context-check:
	@uv run cloudfrog-knowledge init --dry-run $(INIT_ARGS)

graphs:
	@. "$(REGISTRY)"; \
	targets="$$(PYTHONPATH="$(ROOT)/src" "$$REPO_REGISTRY_PYTHON" -c '$(TARGET_IDS_PY)')"; \
	if [ -z "$$targets" ]; then \
	  echo "graphs: graph.targets yielded nothing; zero targets inspected is not a pass" >&2; \
	  exit 1; \
	fi; \
	for id in $$targets; do \
	  if [ -n "$(REPO)" ] && [ "$$id" != "$(REPO)" ]; then continue; fi; \
	  if [ "$$(repo_status "$$id")" = "unconfigured" ]; then \
	    echo "skip: $$id has no root configured on this machine"; continue; fi; \
	  echo "==> $$id"; \
	  "$(SCRIPTS)/bootstrap-graphify-repo.sh" --target "$$(repo_path "$$id")" $(FORCE_FLAG) || exit 1; \
	done

platform-graph:
	@"$(SCRIPTS)/build-platform-graph.sh"

# Diagnosis, never a gate: it writes nothing and exits 0 even when every line is a gap.
doctor:
	@. "$(REGISTRY)"; \
	registered="$$("$$REPO_REGISTRY_PYTHON" -c '$(REGISTERED_PY)')"; \
	mcp_bin="$$(command -v graphify-mcp 2>/dev/null || echo "$$HOME/.local/bin/graphify-mcp")"; \
	for id in $(IDS); do \
	  status="$$(repo_status "$$id")"; \
	  if [ "$$status" != "resolved" ]; then printf '%-32s %s\n' "$$id" "$$status"; continue; fi; \
	  dir="$$(repo_path "$$id")"; \
	  slug="$$(repo_context "$$id")"; \
	  mode="$$(repo_install_mode "$$id")"; \
	  if [ -z "$$slug" ]; then context="none declared"; \
	  elif [ -L "$$dir/.claude" ]; then context="linked"; \
	  elif [ -d "$$dir/.claude" ]; then context="copy"; \
	  else context="NOT installed"; fi; \
	  case "$$mode:$$context" in \
	    link:linked | copy:copy | :* | *:"NOT installed") : ;; \
	    *) context="$$context!=$$mode" ;; \
	  esac; \
	  graph_json="$$dir/graphify-out/graph.json"; \
	  if [ ! -f "$$graph_json" ]; then graph="none"; \
	  else \
	    built="$$("$$REPO_REGISTRY_PYTHON" -c '$(BUILT_AT_PY)' "$$graph_json" 2>/dev/null || true)"; \
	    head_commit="$$(git -C "$$dir" rev-parse HEAD 2>/dev/null || true)"; \
	    if [ -z "$$built" ]; then graph="no built_at_commit"; \
	    elif [ "$$built" = "$$head_commit" ]; then graph="fresh"; \
	    else graph="STALE"; fi; \
	  fi; \
	  mcp="registered"; \
	  printf '%s\n' "$$registered" | rg -qxF "$$dir" || mcp="NOT registered"; \
	  printf '%-32s context=%-14s graph=%-18s mcp=%s\n' \
	    "$$id" "$$context" "$$graph" "$$mcp"; \
	  if [ "$$mcp" != "registered" ] && [ -f "$$graph_json" ]; then \
	    printf '  register it with: claude mcp add graphify -s local -- %s --graph %s --transport stdio\n' \
	      "$$mcp_bin" "$$graph_json"; \
	  fi; \
	done
