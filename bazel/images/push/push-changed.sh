#!/usr/bin/env bash
# push-changed.sh: publish only the images whose content is not already in the
# registry, then publish the charts. Replaces
# `bazel run //bazel/images:push_all --config=ci --stamp` on main.
#
# WHY. `bazel run` materialises every command's runfiles on the runner BEFORE
# the first command executes, so the push_all multirun drags all ~24 dual-arch
# images out of CAS even when every action is a cache hit. PR #4586 fixed that
# for PR branches by switching them to `bazel build`; main kept `bazel run`
# because main is where images actually publish. Measured on 2026-08-10 the
# `deploy` runner was 488 GB/day of BuildBuddy download, 29.6% of everything
# this repo moves and its single largest source.
#
# HOW. Image digests are content-stable: --stamp feeds oci_push's remote_tags,
# never the image itself, which is the same property the missed-chart-bump
# guard already relies on. So an image whose manifest digest is already in the
# registry has nothing to publish. The only thing a push would add is one more
# build-timestamped tag over identical bytes, and nothing reads those tags:
# every chart here deploys `repository@digest`. The two tag-only image
# references in the repo (embervm servingEnvoy, context-forge toolRefresh) are
# upstream images this never pushes.
#
# FAIL CLOSED. An image is pushed whenever we cannot prove it is unnecessary:
# no manifest entry, a blank repository or digest, or any registry lookup that
# does not clearly answer "already present". The failure we are protecting
# against is a skipped push leaving a chart pinned to a digest that does not
# exist, which is an ImagePullBackOff, so the tie always goes to pushing.

set -o errexit -o nounset -o pipefail

WORKSPACE="${BUILD_WORKSPACE_DIRECTORY:-$(git rev-parse --show-toplevel)}"
cd "$WORKSPACE"

BAZEL="${BAZEL:-bazel}"
BAZEL_ARGS=(--config=ci --stamp)

# Extract the `.push` labels listed in one multirun target of the generated
# bazel/images/BUILD. Used only to cross-check the manifest's coverage, so it
# reads the same file the manifest was generated from.
_multirun_labels() {
	awk -v want="$1" '
		$0 ~ "name = \"" want "\"," { inside = 1; next }
		inside && /^\)/ { inside = 0 }
		inside && match($0, /"\/\/[^"]*"/) {
			label = substr($0, RSTART + 1, RLENGTH - 2)
			if (label ~ /\.push$/) print label
		}
	' bazel/images/BUILD | LC_ALL=C sort -u
}

echo "==> Building every image (layers stay in CAS, nothing is staged here)"
"$BAZEL" build //bazel/images:push_all "${BAZEL_ARGS[@]}"

# --remote_download_outputs=toplevel overrides the --remote_download_minimal
# that --config=ci sets, for this one build only. The manifest is three short
# strings per image, so materialising it costs nothing and is the whole point:
# it is the cheap half of the data the expensive runfiles would have carried.
echo "==> Materialising the image digest manifest"
"$BAZEL" build //bazel/images/digests:manifest "${BAZEL_ARGS[@]}" \
	--remote_download_outputs=toplevel

# Read output paths from the bazel-bin convenience symlink, NOT from
# `bazel info bazel-bin --config=ci`, which fails to resolve
# @@buildbuddy_toolchain on the workflow runners. Same trap as
# bazel/helm/ci-diff-manifests.sh.
BAZEL_BIN="$WORKSPACE/bazel-bin"
MANIFEST="$BAZEL_BIN/bazel/images/digests/manifest.txt"

if [ ! -s "$MANIFEST" ]; then
	echo "ERROR: $MANIFEST is missing or empty; refusing to guess what to push." >&2
	exit 1
fi

echo "==> Building crane"
"$BAZEL" build @multitool//tools/crane --config=ci
CRANE="${CRANE:-$(find -L "$BAZEL_BIN/external" -name "crane" -type f -perm /111 2>/dev/null | head -1)}"
if [ -z "$CRANE" ]; then
	echo "ERROR: crane not found under $BAZEL_BIN/external" >&2
	exit 1
fi

echo ""
echo "==> Deciding which images need a push"

COVERED=$(mktemp)
TO_PUSH=$(mktemp)
trap 'rm -f "$COVERED" "$TO_PUSH"' EXIT

SKIPPED=0
# `|| [ -n "$label" ]` so a manifest whose last line has no trailing newline
# still gets its last image compared. Without it `read` returns non-zero at EOF
# and the final image drops out of the comparison silently. The coverage check
# below would still push it, so this was never a correctness hole, but it made
# the last image in the manifest permanently un-skippable.
while IFS=$'\t' read -r label repository digest || [ -n "${label:-}" ]; do
	[ -n "${label:-}" ] || continue
	echo "$label" >>"$COVERED"

	if [ -n "${repository:-}" ] && [ -n "${digest:-}" ] &&
		"$CRANE" manifest "${repository}@${digest}" >/dev/null 2>&1; then
		echo "  skip  $label  ($digest already published)"
		SKIPPED=$((SKIPPED + 1))
		continue
	fi

	echo "  push  $label"
	echo "$label" >>"$TO_PUSH"
done <"$MANIFEST"

# Coverage cross-check. The manifest and push_all are generated from the same
# list, so they can only disagree if one was hand-edited or the generator did
# not re-run. main's format stage would catch that, but it runs AFTER this
# script (deliberately: a formatter hiccup must not block a deploy), so an
# uncovered image is pushed here rather than silently never published.
LC_ALL=C sort -u -o "$COVERED" "$COVERED"
UNCOVERED=$(comm -23 <(comm -23 <(_multirun_labels push_all) <(_multirun_labels push_charts)) "$COVERED")
if [ -n "$UNCOVERED" ]; then
	echo "  WARNING: these images are in push_all but not in the digest manifest;" >&2
	echo "           pushing them unconditionally. Re-run 'format' to regenerate." >&2
	while IFS= read -r label; do
		[ -n "$label" ] || continue
		echo "  push  $label  (not in manifest)"
		echo "$label" >>"$TO_PUSH"
	done <<<"$UNCOVERED"
fi

PUSH_COUNT=$(grep -c . "$TO_PUSH" 2>/dev/null || echo 0)
echo ""
echo "==> $PUSH_COUNT image(s) to push, $SKIPPED already published"

while IFS= read -r label || [ -n "$label" ]; do
	[ -n "$label" ] || continue
	echo ""
	echo "==> push $label"
	"$BAZEL" run "$label" "${BAZEL_ARGS[@]}"
done <"$TO_PUSH"

# Charts LAST, and in their own multirun. Ordering is load-bearing now in a way
# it could not be before: push_all ran everything concurrently (jobs = 0), so a
# chart could publish while the images whose digests it pins were still
# uploading. That race is what 819a36cc2 had to design around. Running the
# images to completion first makes the pinned digest present by construction.
echo ""
echo "==> Publishing charts"
"$BAZEL" run //bazel/images:push_charts "${BAZEL_ARGS[@]}"
