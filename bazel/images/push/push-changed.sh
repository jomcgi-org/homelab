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
# every chart here deploys `repository@digest`.
#
# THAT LAST SENTENCE IS A LOAD-BEARING INVARIANT, NOT AN OBSERVATION, and it was
# false for eight months. A chart rendering `repository:tag` pins the very tag
# this script decided not to create, which is an ImagePullBackOff on the next
# content-identical commit. monolith-public deployed that way and wedged for
# ~11h on 2026-08-11 while this script printed "0 image(s) to push" and the
# action stayed green. Fixed in PR #4680 (homelab-library) and #4681
# (oci-model-cache and a dashboard sidecar). Before relying on the skip again,
# check that no chart template renders a bare tag over an image we push:
#
#   grep -rn 'image.repository }}:{{' projects/*/chart/templates projects/*/*/chart/templates
#
# Legitimate hits are upstream images this never pushes (embervm servingEnvoy,
# context-forge toolRefresh, cloudflare-gateway, renovate). embervm tokenBroker
# renders `repo:tag@digest`, where the digest still decides.
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
RUN_ID="${PUBLISH_RUN_ID:-${BUILDBUDDY_INVOCATION_ID:-$(git rev-parse --verify HEAD^{commit})}}"
VERIFY_PUBLISHED_IMAGES="${VERIFY_PUBLISHED_IMAGES:-$WORKSPACE/bazel/helm/verify-published-images.sh}"

# Records are same-run evidence. A runner snapshot can retain them after a
# failed publish, so begin from an empty, exact workspace-local path. Every
# successful new or reused chart recreates its record below.
rm -rf "$WORKSPACE/.chart-version-records"

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

# Split exactly four tab-separated fields without Bash collapsing adjacent tab
# characters. Tabs are IFS whitespace, so `read -r label repo digest tag` moves
# a tag into `digest` when the digest field is blank, defeating the fail-closed
# path this script promises. The manifest fields cannot themselves contain a
# tab. Results are returned in the named globals used by both loops below.
_parse_image_row() {
	local row="$1" rest
	label="${row%%$'\t'*}"
	rest="${row#*$'\t'}"
	repository="${rest%%$'\t'*}"
	rest="${rest#*$'\t'}"
	digest="${rest%%$'\t'*}"
	if [[ "$rest" == *$'\t'* ]]; then
		tag="${rest#*$'\t'}"
	else
		tag=""
	fi
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

# "${BAZEL_ARGS[@]}", not a bare --config=ci, even though crane is a prebuilt
# binary that --stamp cannot affect. Bazel discards the whole analysis cache
# whenever --stamp flips, so building this one target unstamped between two
# stamped ones cost TWO re-analyses per deploy: observed 2026-08-11 at 05:26:02
# and again at 05:26:16, the second re-configuring 19,282 targets in 21s. Keep
# every bazel command in this script on the same value.
echo "==> Building crane"
"$BAZEL" build @multitool//tools/crane "${BAZEL_ARGS[@]}"
# `|| true` because pipefail turns a find that cannot read the tree into an
# abort with no explanation, instead of the message below.
CRANE="${CRANE:-$(find -L "$BAZEL_BIN/external" -name "crane" -type f -perm /111 2>/dev/null | head -1 || true)}"
if [ -z "$CRANE" ]; then
	echo "ERROR: crane not found under $BAZEL_BIN/external" >&2
	exit 1
fi

# The chart multirun carries Helm in each target's runfiles, but the final
# artifact assertion executes after that multirun and therefore resolves the
# tool explicitly. This also makes missing runtime tooling fail before any
# publication begins.
echo "==> Building helm"
"$BAZEL" build @multitool//tools/helm "${BAZEL_ARGS[@]}"
HELM="${HELM:-$(find -L "$BAZEL_BIN/external" -name "helm" -type f -perm /111 2>/dev/null | head -1 || true)}"
if [ -z "$HELM" ]; then
	echo "ERROR: helm not found under $BAZEL_BIN/external" >&2
	exit 1
fi
if [ ! -f "$VERIFY_PUBLISHED_IMAGES" ]; then
	echo "ERROR: published image verifier not found at $VERIFY_PUBLISHED_IMAGES" >&2
	exit 1
fi

echo ""
echo "==> Deciding which images need a push"

COVERED=$(mktemp)
TO_PUSH=$(mktemp)
IMAGE_RESULTS=$(mktemp)
CHART_TARGETS=$(mktemp)
trap 'rm -f "$COVERED" "$TO_PUSH" "$IMAGE_RESULTS" "$CHART_TARGETS"' EXIT

_multirun_labels push_charts >"$CHART_TARGETS"

SKIPPED=0
# `|| [ -n "$label" ]` so a manifest whose last line has no trailing newline
# still gets its last image compared. Without it `read` returns non-zero at EOF
# and the final image drops out of the comparison silently. The coverage check
# below would still push it, so this was never a correctness hole, but it made
# the last image in the manifest permanently un-skippable.
while IFS= read -r image_row || [ -n "${image_row:-}" ]; do
	_parse_image_row "$image_row"
	[ -n "${label:-}" ] || continue
	echo "$label" >>"$COVERED"

	# `crane digest` returns the registry's top-level manifest identity. For a
	# multi-platform image this is the index digest, matching both OciImageInfo
	# and the chart pin. Keep the observed value as provenance for skipped
	# images instead of copying the desired manifest row into the result.
	observed=""
	if [ -n "${repository:-}" ] && [ -n "${digest:-}" ]; then
		observed=$("$CRANE" digest "${repository}@${digest}" 2>/dev/null || true)
	fi
	if [ "$observed" = "$digest" ]; then
		echo "  skip  $label  ($digest already published)"
		printf '%s\t%s\t%s\t%s\t%s\n' \
			"$RUN_ID" "skipped" "$label" "$repository" "$observed" >>"$IMAGE_RESULTS"
		SKIPPED=$((SKIPPED + 1))
		continue
	fi

	echo "  push  $label"
	printf '%s\t%s\t%s\t%s\n' "$label" "${repository:-}" "${digest:-}" "${tag:-}" >>"$TO_PUSH"
done <"$MANIFEST"

# Coverage cross-check. The manifest and push_all are generated from the same
# list, so they can only disagree if one was hand-edited or the generator did
# not re-run. main's format stage would catch that, but it runs AFTER this
# script (deliberately: a formatter hiccup must not block a deploy), so an
# uncovered image is pushed here rather than silently never published.
#
# Set subtraction with `grep -Fxv -f`, NOT `comm`. comm requires its inputs
# sorted in comm's OWN collation, and these are sorted LC_ALL=C while the runner
# locale is not: under en_GB.UTF-8 comm silently mis-merged and reported a chart
# target present in BOTH lists as an uncovered image. Fixed-string matching has no
# ordering requirement and no locale sensitivity.
UNCOVERED=$(_multirun_labels push_all |
	grep -Fxv -f <(_multirun_labels push_charts) |
	grep -Fxv -f "$COVERED" || true)
if [ -n "$UNCOVERED" ]; then
	echo "  WARNING: these images are in push_all but not in the digest manifest;" >&2
	echo "           pushing them unconditionally. Re-run 'format' to regenerate." >&2
	while IFS= read -r label; do
		[ -n "$label" ] || continue
		echo "  push  $label  (not in manifest)"
		printf '%s\t\t\t\n' "$label" >>"$TO_PUSH"
	done <<<"$UNCOVERED"
fi

PUSH_COUNT=$(grep -c . "$TO_PUSH" || true)
echo ""
echo "==> $PUSH_COUNT image(s) to push, $SKIPPED already published"

# --build_runfile_links, and NOT a download-mode override. preset.bazelrc sets
# `common --nobuild_runfile_links` repo-wide, and its own comment says a binary
# that is going to be EXECUTED has to ask for the tree back. apko_push.sh.tpl
# resolves everything through rlocation, and with no tree rlocation falls back
# to RUNFILES_MANIFEST_FILE, which is where this breaks.
#
# 57c2dd16c reached for --remote_download_outputs=all instead and changed
# nothing: BuildBuddy recorded the flag on the argv and bazel exited SUCCESS
# while the push still died on the same jq line. Building the tree is what
# materialises the layout, because bazel needs real files to symlink; measured
# on a runner, --build_runfile_links ALONE is sufficient and the download
# override is pure BuildBuddy egress for no benefit. Do not add it back.
#
# WHY CHARTS PUBLISHED FINE THROUGHOUT. push.sh.tpl has the identical runfiles
# preamble and the identical rlocation calls, and chart pushes never broke. The
# difference is what each resolves: CHART_TGZ is a regular FILE and IMAGE_DIR is
# a DIRECTORY. A runfiles manifest maps individual files, so a .tgz lookup
# succeeds and a tree artifact, which has no entry of its own, does not. That
# asymmetry is why the failure reads as an image-only problem and why the
# deploy could keep writing chart versions back while publishing no images.
#
# Verified on a real BuildBuddy Linux runner before landing, via
# `bazel run ... --run_under` to list the tree without invoking crane:
#
#   PROBE_LAYOUT_NODL=blobs  index.json  oci-layout
#
# The flag stays on this line rather than in BAZEL_ARGS: this is the only
# command in the deploy that executes a bazel-built binary, and the skip
# decision above bounds it to the images whose content actually changed,
# normally one or two rather than all 24.
#
# The branch had never executed until 2026-08-11 (#4685). Every deploy for weeks
# found all 24 images content-identical and took the skip path, so the first
# commit that really needed a push turned main's deploy red.
while IFS= read -r image_row || [ -n "${image_row:-}" ]; do
	_parse_image_row "$image_row"
	[ -n "$label" ] || continue
	echo ""
	echo "==> push $label"
	"$BAZEL" run "$label" "${BAZEL_ARGS[@]}" --build_runfile_links

	# Resolve the stamped tag written by this successful push. Unlike the
	# manifest row, this is an independent registry result attributable to this
	# source commit. Missing tag or registry evidence is left absent so the
	# chart-scoped verifier can fail precisely when the image is expected.
	if [ -n "${repository:-}" ] && [ -n "${tag:-}" ]; then
		observed=$("$CRANE" digest "${repository}:${tag}" 2>/dev/null || true)
		if [ -n "$observed" ]; then
			printf '%s\t%s\t%s\t%s\t%s\n' \
				"$RUN_ID" "pushed" "$label" "$repository" "$observed" >>"$IMAGE_RESULTS"
		fi
	fi
done <"$TO_PUSH"

# Charts LAST, and in their own multirun. Ordering is load-bearing now in a way
# it could not be before: push_all ran everything concurrently (jobs = 0), so a
# chart could publish while the images whose digests it pins were still
# uploading. That race is what 819a36cc2 had to design around. Running the
# images to completion first makes the pinned digest present by construction.
echo ""
echo "==> Publishing charts"
"$BAZEL" run //bazel/images:push_charts "${BAZEL_ARGS[@]}"

# Pull every final chart artifact named by this run, including an
# already-published version reused by push.sh.tpl, and join its pins to the
# independent image results above. This is artifact consistency only; rollout
# and live cluster state remain outside this workflow.
echo ""
echo "==> Verifying published chart image manifests"
HELM="$HELM" bash "$VERIFY_PUBLISHED_IMAGES" \
	"$RUN_ID" "$IMAGE_RESULTS" "$WORKSPACE/.chart-version-records" \
	"$CHART_TARGETS" "$BAZEL_BIN"
