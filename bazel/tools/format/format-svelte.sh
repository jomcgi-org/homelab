#!/usr/bin/env bash
# Format all tracked *.svelte files with the hermetic prettier binary.
#
# aspect_rules_lint format_multirun has no Svelte language dialect (JavaScript
# includes Vue but not Svelte), so CI Format would otherwise skip these files.
# This step is wired into //bazel/tools/format:format so local and CI share one
# path once prettier-plugin-svelte is in the hermetic config.
set -euo pipefail

ROOT="${BUILD_WORKSPACE_DIRECTORY:-$(git rev-parse --show-toplevel)}"
cd "$ROOT"

# Locate this binary's runfiles (bazel run / multirun).
RUNFILES="${RUNFILES_DIR:-}"
if [[ -z "$RUNFILES" ]]; then
	if [[ -d "${0}.runfiles" ]]; then
		RUNFILES="${0}.runfiles"
	elif [[ -d "$(dirname "$0")/format_svelte.runfiles" ]]; then
		RUNFILES="$(dirname "$0")/format_svelte.runfiles"
	fi
fi

PRETTIER=""
if [[ -n "$RUNFILES" ]]; then
	for candidate in \
		"$RUNFILES/_main/bazel/tools/format/prettier_/prettier" \
		"$RUNFILES/homelab/bazel/tools/format/prettier_/prettier"; do
		if [[ -x "$candidate" ]]; then
			PRETTIER="$candidate"
			break
		fi
	done
	if [[ -z "$PRETTIER" ]]; then
		PRETTIER=$(find "$RUNFILES" -path '*/prettier_/prettier' \( -type f -o -type l \) 2>/dev/null | head -1 || true)
	fi
fi

if [[ -z "${PRETTIER:-}" || ! -x "$PRETTIER" ]]; then
	echo "ERROR: hermetic prettier not found in runfiles (RUNFILES=${RUNFILES:-unset})" >&2
	exit 1
fi

mapfile -t files < <(git ls-files '*.svelte' | grep -v '^\.claude/' || true)
if [[ ${#files[@]} -eq 0 ]]; then
	exit 0
fi

# Batch to avoid ARG_MAX on large trees; prettier is fine with many args but
# keep chunks modest for older hosts.
batch_size=200
for ((i = 0; i < ${#files[@]}; i += batch_size)); do
	chunk=("${files[@]:i:batch_size}")
	"$PRETTIER" --write "${chunk[@]}"
done
