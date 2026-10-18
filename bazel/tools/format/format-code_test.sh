#!/usr/bin/env bash
# Exercise the generated rules_lint runner against a tracked-file fixture.
set -euo pipefail

formatter="$(realpath "$1")"
fixture="$(mktemp -d "${TEST_TMPDIR:-/tmp}/format-code.XXXXXX")"
export BUILD_WORKSPACE_DIRECTORY="$fixture"
cd "$fixture"
git init -q
mkdir -p projects/oom-inference
printf 'projects/oom-inference\n' >.bazelignore
# Intentionally invalid Rust must be ignored even when tracked under a
# Bazel-ignored Cargo project. Restoring the Rust pass makes this check fail.
printf 'fn deliberately_invalid(\n' >projects/oom-inference/probe.rs
git add .bazelignore projects/oom-inference/probe.rs
"$formatter"
"$formatter" projects/oom-inference/probe.rs
echo 'PASS: tracked and explicit Cargo Rust files do not invoke a Bazel formatter'

# Each remaining configured formatter must both accept formatted source and
# reject drift. This also checks the generated labels and their runfiles.
files=(probe.go probe.js probe.py probe.sh probe.bzl)
formatted=(
	'package fixture\n'
	'const value = 1;\n'
	'VALUE = 1\n'
	'#!/usr/bin/env bash\necho fixture\n'
	'VALUE = 1\n'
)
unformatted=(
	'package fixture\nvar Value=1\n'
	'const value=1;\n'
	'VALUE=1\n'
	'#!/usr/bin/env bash\necho    fixture\n'
	'VALUE=1\n'
)
for i in "${!files[@]}"; do
	file="${files[$i]}"
	printf '%b' "${formatted[$i]}" >"$file"
	git add "$file"
	"$formatter" "$file"
	printf '%b' "${unformatted[$i]}" >"$file"
	if "$formatter" "$file"; then
		echo "FAIL: formatter accepted drift in $file" >&2
		exit 1
	fi
	printf '%b' "${formatted[$i]}" >"$file"
	echo "PASS: formatter accepts formatted $file and rejects drift"
done

"$formatter"
echo 'PASS: configured formatters accept the full fixture with invalid Rust present'
