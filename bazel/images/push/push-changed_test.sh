#!/usr/bin/env bash
# Unit tests for push-changed.sh, the main-branch image publisher.
#
# The script decides NOT to do things (skip a push), and a wrong skip leaves a
# chart pinned to a digest that was never uploaded, which surfaces in prod as
# ImagePullBackOff rather than as a red build. So the cases pinned here are
# mostly the fail-closed ones: an unknown digest, a registry that will not
# answer, and an image the manifest does not cover at all.
#
# bazel and crane are both stubbed. The real ones are not needed to test the
# decision logic, and stubbing them is what makes the registry-error and
# manifest-drift paths reachable at all.

set -o errexit -o nounset -o pipefail

SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/push-changed.sh"
FAILURES=0

pass() { echo "  PASS: $1"; }
fail() {
	echo "  FAIL: $1"
	echo "$2" | sed 's/^/        /'
	FAILURES=$((FAILURES + 1))
}

# Build a throwaway workspace: a generated-shaped bazel/images/BUILD, a digest
# manifest, and stub bazel/crane binaries that record what they were asked to
# do. PUBLISHED is a newline-separated list of "repo@digest" refs crane will
# claim already exist; CRANE_BROKEN makes every lookup fail instead.
setup() {
	local manifest="$1" published="$2" broken="${3:-}"
	WORK=$(mktemp -d)
	mkdir -p "$WORK/bazel/images" "$WORK/bazel-bin/bazel/images/digests" "$WORK/bin"

	# The signoz-addons pair is not decoration. Subtracting push_charts from
	# push_all with `comm` reported that chart, which is in BOTH lists, as an
	# uncovered image: comm compares in the ambient locale while the inputs were
	# sorted LC_ALL=C, and under en_GB.UTF-8 the two disagree. These are the
	# exact labels that reproduced it, so case 6 is the regression test.
	cat >"$WORK/bazel/images/BUILD" <<'BUILD'
multirun(
    name = "push_all",
    commands = [
        "//projects/alpha:image.push",
        "//projects/alpha/chart:chart.push",
        "//projects/beta:image.push",
        "//projects/gamma:image.push",
        "//projects/platform/signoz-addons/dashboard-sidecar:chart.push",
        "//projects/platform/signoz-addons/dashboard-sidecar/cmd:image.push",
    ],
    jobs = 0,
    visibility = ["//visibility:public"],
)

multirun(
    name = "push_charts",
    commands = [
        "//projects/alpha/chart:chart.push",
        "//projects/platform/signoz-addons/dashboard-sidecar:chart.push",
    ],
    jobs = 0,
    visibility = ["//visibility:public"],
)
BUILD

	printf '%s' "$manifest" >"$WORK/bazel-bin/bazel/images/digests/manifest.txt"

	# Stub bazel: `run` appends its label to run.log, everything else is a no-op.
	# argv.log keeps the WHOLE command line as well. The two are separate on
	# purpose: most cases assert on labels alone and compare them exactly, so
	# widening run.log would break them, while the flags are what case 9 needs.
	cat >"$WORK/bin/bazel" <<'STUB'
#!/usr/bin/env bash
if [ "${1:-}" = "run" ]; then
	echo "$2" >>"$STUB_RUN_LOG"
	echo "$*" >>"$STUB_ARGV_LOG"
fi
exit 0
STUB

	if [ "$broken" = "broken" ]; then
		printf '#!/usr/bin/env bash\nexit 1\n' >"$WORK/bin/crane"
	else
		cat >"$WORK/bin/crane" <<STUB
#!/usr/bin/env bash
# \$1 is "manifest", \$2 is the repo@digest ref.
printf '%s\n' "$published" | grep -qxF "\$2"
STUB
	fi

	chmod +x "$WORK/bin/bazel" "$WORK/bin/crane"
	export STUB_RUN_LOG="$WORK/run.log"
	export STUB_ARGV_LOG="$WORK/argv.log"
	: >"$STUB_RUN_LOG"
	: >"$STUB_ARGV_LOG"
}

teardown() { rm -rf "$WORK"; }

run_script() {
	BUILD_WORKSPACE_DIRECTORY="$WORK" BAZEL="$WORK/bin/bazel" CRANE="$WORK/bin/crane" \
		bash "$SCRIPT" 2>&1
}

SIDECAR="//projects/platform/signoz-addons/dashboard-sidecar/cmd:image.push"

MANIFEST_3=$(
	printf '%s\t%s\t%s\n' \
		"//projects/alpha:image.push" "ghcr.io/jomcgi/homelab/alpha" "sha256:aaa" \
		"//projects/beta:image.push" "ghcr.io/jomcgi/homelab/beta" "sha256:bbb" \
		"$SIDECAR" "ghcr.io/jomcgi/homelab/sidecar" "sha256:ddd" \
		"//projects/gamma:image.push" "ghcr.io/jomcgi/homelab/gamma" "sha256:ccc"
)

ALL_PUBLISHED=$(printf '%s\n%s\n%s\n%s' \
	"ghcr.io/jomcgi/homelab/alpha@sha256:aaa" \
	"ghcr.io/jomcgi/homelab/beta@sha256:bbb" \
	"ghcr.io/jomcgi/homelab/gamma@sha256:ccc" \
	"ghcr.io/jomcgi/homelab/sidecar@sha256:ddd")

echo "push-changed.sh"

# 1. An image whose digest is already in the registry must not be pushed. This
#    is the entire point of the script; everything else guards it.
setup "$MANIFEST_3" "ghcr.io/jomcgi/homelab/alpha@sha256:aaa"
OUT=$(run_script)
LOG=$(cat "$STUB_RUN_LOG")
if grep -q "alpha:image.push" <<<"$LOG"; then
	fail "skips an image already in the registry" "$LOG"
elif grep -q "beta:image.push" <<<"$LOG" && grep -q "gamma:image.push" <<<"$LOG"; then
	pass "skips an image already in the registry, pushes the other two"
else
	fail "skips an image already in the registry" "$LOG"
fi
teardown

# 2. Nothing changed at all: every image is skipped and only the charts run.
#    This is the common case on main, where most commits touch no image input.
#
#    Also the regression test for the trailing-newline bug: MANIFEST_3 comes
#    from a command substitution, so its last line has no newline, and a plain
#    `read` loop drops that line at EOF. gamma is last, so before the fix it was
#    never compared and could never be skipped.
setup "$MANIFEST_3" "$ALL_PUBLISHED"
OUT=$(run_script)
LOG=$(cat "$STUB_RUN_LOG")
if [ "$LOG" = "//bazel/images:push_charts" ]; then
	pass "pushes no images when every digest is already published"
else
	fail "pushes no images when every digest is already published" "$LOG"
fi
teardown

# 3. FAIL CLOSED: a registry that will not answer must not be read as
#    "already there". A broken crane pushes everything.
setup "$MANIFEST_3" "" broken
OUT=$(run_script)
LOG=$(cat "$STUB_RUN_LOG")
if grep -q "alpha:image.push" <<<"$LOG" &&
	grep -q "beta:image.push" <<<"$LOG" &&
	grep -q "gamma:image.push" <<<"$LOG"; then
	pass "pushes everything when the registry lookup fails"
else
	fail "pushes everything when the registry lookup fails" "$LOG"
fi
teardown

# 4. FAIL CLOSED: a blank digest is not a match, even against a stub crane that
#    would happily say yes to "repo@".
setup "$(printf '%s\t%s\t%s\n' "//projects/alpha:image.push" "ghcr.io/jomcgi/homelab/alpha" "")" \
	"ghcr.io/jomcgi/homelab/alpha@"
OUT=$(run_script)
if grep -q "alpha:image.push" "$STUB_RUN_LOG"; then
	pass "pushes an image whose manifest digest is blank"
else
	fail "pushes an image whose manifest digest is blank" "$(cat "$STUB_RUN_LOG")"
fi
teardown

# 5. FAIL CLOSED: manifest drift. gamma is in push_all but absent from the
#    manifest, so it must be pushed unconditionally and say so. Without this
#    the deploy would look green while never publishing gamma.
setup "$(printf '%s\t%s\t%s\n' \
	"//projects/alpha:image.push" "ghcr.io/jomcgi/homelab/alpha" "sha256:aaa" \
	"//projects/beta:image.push" "ghcr.io/jomcgi/homelab/beta" "sha256:bbb" \
	"$SIDECAR" "ghcr.io/jomcgi/homelab/sidecar" "sha256:ddd")" \
	"$ALL_PUBLISHED"
OUT=$(run_script)
LOG=$(cat "$STUB_RUN_LOG")
if grep -q "gamma:image.push" <<<"$LOG" && grep -q "not in the digest manifest" <<<"$OUT"; then
	pass "pushes an image missing from the manifest and warns"
else
	fail "pushes an image missing from the manifest and warns" "$LOG"$'\n'"$OUT"
fi
teardown

# 6. A chart target is not an image: it must never be treated as one, and it
#    must not appear in the uncovered-images warning. REGRESSION TEST for the
#    comm collation bug, which reported the signoz-addons chart (present in both
#    push_all and push_charts) as uncovered and pushed it a second time.
setup "$MANIFEST_3" "$ALL_PUBLISHED"
OUT=$(run_script)
if grep -q "chart.push" <<<"$OUT"; then
	fail "never treats a chart target as an uncovered image" "$OUT"
else
	pass "never treats a chart target as an uncovered image"
fi
teardown

# 7. Charts publish LAST. A chart pins image digests, so publishing it before
#    its images have finished uploading is the race 819a36cc2 had to work
#    around when push_all ran everything concurrently.
setup "$MANIFEST_3" ""
OUT=$(run_script)
if [ "$(tail -1 "$STUB_RUN_LOG")" = "//bazel/images:push_charts" ]; then
	pass "publishes charts after every image push"
else
	fail "publishes charts after every image push" "$(cat "$STUB_RUN_LOG")"
fi
teardown

# 8. An empty manifest means the build did not produce what we asked for.
#    Guessing would either skip everything or push everything; both are worse
#    than stopping, because the format stage that would catch it runs later.
setup "" ""
if run_script >/dev/null 2>&1; then
	fail "fails loudly on an empty manifest" "expected a non-zero exit"
else
	pass "fails loudly on an empty manifest"
fi
teardown

# 9. An image push must build its runfiles tree. preset.bazelrc sets
#    `common --nobuild_runfile_links` repo-wide, and apko_push.sh.tpl resolves
#    IMAGE_DIR through rlocation, which without a tree falls back to the
#    manifest and cannot resolve a DIRECTORY. Without the flag every push dies
#    on `jq: Could not open file .../image/index.json` (#4685).
#
#    Pinned as a command line rather than as behaviour because the failure needs
#    a bazel-built binary on a remote runner, which no local test reproduces.
#    The reason it needs pinning at all is that this branch does not run when
#    every image is content-identical, so a regression here stays invisible
#    until the next commit that genuinely publishes something.
setup "$MANIFEST_3" ""
OUT=$(run_script)
IMAGE_ARGV=$(grep "alpha:image.push" "$STUB_ARGV_LOG" || true)
if [ -z "$IMAGE_ARGV" ]; then
	fail "image push builds its runfiles tree" "no image push was recorded"
elif grep -q -- "--build_runfile_links" <<<"$IMAGE_ARGV"; then
	pass "image push builds its runfiles tree"
else
	fail "image push builds its runfiles tree" "$IMAGE_ARGV"
fi
teardown

# 10. And it must NOT carry a download-mode override. 57c2dd16c added
#     --remote_download_outputs=all on the theory that the layout was stranded
#     in CAS. It was not the cause and it did not fix anything, but it does pull
#     every layer of every changed image to the runner, which is exactly the
#     BuildBuddy egress push-changed.sh exists to cut (PR #4586, 488 GB/day).
#     Measured on a runner: --build_runfile_links alone materialises the layout.
setup "$MANIFEST_3" ""
OUT=$(run_script)
IMAGE_ARGV=$(grep "alpha:image.push" "$STUB_ARGV_LOG" || true)
if grep -q -- "--remote_download_outputs" <<<"$IMAGE_ARGV"; then
	fail "image push does not pay for a download-mode override" "$IMAGE_ARGV"
else
	pass "image push does not pay for a download-mode override"
fi
teardown

echo ""
if [ "$FAILURES" -ne 0 ]; then
	echo "$FAILURES test(s) failed"
	exit 1
fi
echo "All push-changed.sh tests passed"
