#!/usr/bin/env bash
set -euo pipefail

base_ref="${CI_BASE_REF:-origin/main}"
query_args=(
	--config=ci
	--deleted_packages=bazel/tools/python
)
test_args=(
	"${query_args[@]}"
	--test_tag_filters=-external,-future
)

workspace="${BUILD_WORKSPACE_DIRECTORY:-}"
if [[ -z "$workspace" ]]; then
	workspace="$(git rev-parse --show-toplevel)"
fi
cd "$workspace"

while IFS= read -r buildbuddy_patch; do
	if [[ "$buildbuddy_patch" =~ ^[0-9a-f]{64}$ ]] &&
		[[ "$(head -n 1 -- "$buildbuddy_patch")" == "diff --git "* ]]; then
		rm -f -- "$buildbuddy_patch"
	fi
done < <(git ls-files --others --exclude-standard)

if [[ "$base_ref" == origin/* ]]; then
	git fetch origin "${base_ref#origin/}" --quiet
fi

# An empty affected set and explicit target overrides still check the manifest.
# bb remote may supply BUILD_WORKSPACE_DIRECTORY; this is an outer script.
env -u BUILD_WORKSPACE_DIRECTORY ./bazel/tools/ci/python-manifest.sh check "$base_ref"

targets_file="$(mktemp)"
trap 'rm -f "$targets_file"' EXIT
if [[ $# -gt 0 ]]; then
	printf '%s\n' "$@" >"$targets_file"
else
	./bazel/tools/ci/affected-targets.sh "$base_ref" HEAD -- "${query_args[@]}" >"$targets_file"
fi

target_count="$(grep -c . "$targets_file" || true)"
if [[ "$target_count" -eq 0 ]]; then
	echo "ci-local-affected: no Bazel targets affected; skipping bazel test"
	exit 0
fi

echo "ci-local-affected: running bazel test on $target_count affected target(s)"
test_status=0
bazel test "${test_args[@]}" --target_pattern_file="$targets_file" || test_status=$?
if [[ "$test_status" -eq 4 ]]; then
	echo "ci-local-affected: affected targets contain no tests; build succeeded"
	exit 0
fi
exit "$test_status"
