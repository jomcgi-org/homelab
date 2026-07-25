#!/usr/bin/env bash
# Fast format script - runs all formatters in parallel using standalone binaries
# Used by both pre-commit and CI for identical formatting
#
# Usage:
#   fast-format.sh          # Format entire repo (CI mode)
#   fast-format.sh --staged # Format only staged files (pre-commit mode)
set -euo pipefail

cd "${BUILD_WORKSPACE_DIRECTORY:-$(git rev-parse --show-toplevel)}"

# Ensure .tools/bin is on PATH (pre-commit doesn't run through direnv)
if [[ -d "$PWD/.tools/bin" ]]; then
	export PATH="$PWD/.tools/bin:$PATH"
fi

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

log() { echo -e "${GREEN}▶${NC} $1"; }
err() { echo -e "${RED}✗${NC} $1" >&2; }

# Parse flags
STAGED=false
if [[ "${1:-}" == "--staged" ]]; then
	STAGED=true
fi

# Verify required tools exist
# buildifier and gazelle are CI-only (run via Bazel)
MISSING=()
for tool in ruff shfmt prettier gofumpt; do
	if ! command -v "$tool" &>/dev/null; then
		MISSING+=("$tool")
	fi
done
if [ ${#MISSING[@]} -gt 0 ]; then
	err "Missing tools: ${MISSING[*]}"
	err "Run './bootstrap.sh' to install dev tools"
	err "In a worktree? Run 'direnv allow' to symlink tools from the main repo"
	exit 1
fi

# In --staged mode, collect staged files by extension and only format those
if $STAGED; then
	mapfile -t STAGED_FILES < <(git diff --cached --name-only --diff-filter=ACMR)
	if [ ${#STAGED_FILES[@]} -eq 0 ]; then
		log "No staged files to format"
		exit 0
	fi

	# Partition staged files by type
	PY_FILES=()
	SH_FILES=()
	GO_FILES=()
	STARLARK_FILES=()
	PRETTIER_FILES=()
	BUILD_FILES=()
	for f in "${STAGED_FILES[@]}"; do
		case "$f" in
		*.py) PY_FILES+=("$f") ;;
		*.sh) SH_FILES+=("$f") ;;
		*.go) GO_FILES+=("$f") ;;
		BUILD | BUILD.bazel | *.bzl | WORKSPACE | WORKSPACE.bazel)
			STARLARK_FILES+=("$f")
			BUILD_FILES+=("$f")
			;;
		*/BUILD | */BUILD.bazel)
			STARLARK_FILES+=("$f")
			BUILD_FILES+=("$f")
			;;
		*.js | *.jsx | *.ts | *.tsx | *.json | *.yaml | *.yml | *.md | *.css | *.html | *.svelte)
			PRETTIER_FILES+=("$f")
			;;
		esac
	done

	log "Formatting ${#STAGED_FILES[@]} staged files..."
	PIDS=()
	PRETTIER_CONFIG="${PRETTIER_CONFIG:-bazel/tools/format/prettier.config.cjs}"

	if [ ${#PY_FILES[@]} -gt 0 ]; then
		ruff format "${PY_FILES[@]}" 2>/dev/null &
		PIDS+=($!)
	fi
	if [ ${#SH_FILES[@]} -gt 0 ]; then
		(shfmt -w "${SH_FILES[@]}" 2>/dev/null || true) &
		PIDS+=($!)
	fi
	if [ ${#STARLARK_FILES[@]} -gt 0 ] && command -v buildifier &>/dev/null; then
		(buildifier "${STARLARK_FILES[@]}" 2>/dev/null || true) &
		PIDS+=($!)
	fi
	if [ ${#GO_FILES[@]} -gt 0 ]; then
		(gofumpt -w "${GO_FILES[@]}" 2>/dev/null || true) &
		PIDS+=($!)
	fi
	if [ ${#PRETTIER_FILES[@]} -gt 0 ]; then
		(prettier --config "$PRETTIER_CONFIG" --write "${PRETTIER_FILES[@]}" 2>/dev/null || true) &
		PIDS+=($!)
	fi

	# Doc/config generators — ONE shared list (also run by the CI format multirun
	# via :run_generators), so local and CI cannot drift. Runs unconditionally;
	# each generator is idempotent and fast. || true: never block a commit.
	(./bazel/tools/format/run-generators.sh 2>/dev/null || true) &
	PIDS+=($!)

	# Atlas migration checksums — only if migration SQL files are staged
	for f in "${STAGED_FILES[@]}"; do
		case "$f" in
		*/migrations/*.sql)
			if command -v atlas &>/dev/null; then
				./bazel/tools/format/update-atlas-sum.sh 2>/dev/null &
				PIDS+=($!)
			fi
			break
			;;
		esac
	done

	# Sync homelab-library dependency versions (local-only: needs the helm CLI,
	# which is not in the CI format runner; charts have their own version gates)
	./bazel/tools/format/sync-helm-deps.sh 2>/dev/null &
	PIDS+=($!)

	for pid in "${PIDS[@]}"; do wait "$pid" 2>/dev/null || true; done

	# Gazelle only if Go or Python files changed
	if [ "${SKIP_GAZELLE:-0}" != "1" ]; then
		if [ ${#GO_FILES[@]} -gt 0 ] || [ ${#PY_FILES[@]} -gt 0 ] || [ ${#BUILD_FILES[@]} -gt 0 ]; then
			log "Running gazelle..."
			gazelle 2>/dev/null || true
		fi
	fi

	# Re-stage any files that were modified by formatting
	git add "${STAGED_FILES[@]}" 2>/dev/null || true

	log "Done!"
	exit 0
fi

# Full repo mode (CI and manual runs)
log "Formatting..."
PIDS=()

# Python
ruff format . 2>/dev/null &
PIDS+=($!)

# Shell
(find . -name '*.sh' -not -path './bazel-*' -not -path './.git/*' -not -path './.claude/worktrees/*' -print0 |
	xargs -0 shfmt -w 2>/dev/null || true) &
PIDS+=($!)

# Starlark — buildifier is optional (CI provides it via Bazel)
if command -v buildifier &>/dev/null; then
	(find . \( -name BUILD -o -name BUILD.bazel -o -name '*.bzl' -o -name WORKSPACE -o -name WORKSPACE.bazel \) \
		-not -path './bazel-*' -not -path './.claude/worktrees/*' -print0 |
		xargs -0 buildifier 2>/dev/null || true) &
	PIDS+=($!)
fi

# Go
(find . -name '*.go' -not -path './bazel-*' -not -path './.git/*' -not -path './.claude/worktrees/*' -print0 |
	xargs -0 gofumpt -w 2>/dev/null || true) &
PIDS+=($!)

# Prettier (JS/TS/JSON/YAML/MD/Svelte); --config loads prettier-plugin-svelte
PRETTIER_CONFIG="${PRETTIER_CONFIG:-bazel/tools/format/prettier.config.cjs}"
prettier --config "$PRETTIER_CONFIG" --write . 2>/dev/null &
PIDS+=($!)

# Doc/config generators — ONE shared list (also run by the CI format multirun via
# :run_generators), so local and CI cannot drift. || true: a local generator
# hiccup must never block a commit; CI is authoritative.
(./bazel/tools/format/run-generators.sh 2>/dev/null || true) &
PIDS+=($!)

# Sync homelab-library dependency versions (local-only: needs the helm CLI, which
# is not in the CI format runner; charts have their own version gates)
./bazel/tools/format/sync-helm-deps.sh 2>/dev/null &
PIDS+=($!)

# Atlas migration checksums
if command -v atlas &>/dev/null; then
	./bazel/tools/format/update-atlas-sum.sh 2>/dev/null &
	PIDS+=($!)
fi

# Wait for all parallel tasks
for pid in "${PIDS[@]}"; do wait "$pid" 2>/dev/null || true; done

# Gazelle (generates BUILD files for Go/Python)
# Run after formatters complete since it needs formatted files
# SKIP_GAZELLE=1 allows CI to use bazel run //:gazelle instead
if [ "${SKIP_GAZELLE:-0}" != "1" ]; then
	log "Running gazelle..."
	gazelle 2>/dev/null || true
fi

log "Done!"
