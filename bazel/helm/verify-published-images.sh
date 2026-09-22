#!/usr/bin/env bash
# Verify that final OCI chart artifacts pin the image manifests established by
# this publish run. The fresh package defines which digest-pinned first-party
# images belong to each chart. It is never the digest authority: pushed images
# use the stamped tag resolved from the registry, and skipped images use the
# registry's digest-addressed existence proof.

set -o errexit -o nounset -o pipefail

RUN_ID="${1:?run identity required}"
IMAGE_RESULTS="${2:?image result file required}"
CHART_RECORD_DIR="${3:?chart record directory required}"
CHART_TARGETS="${4:?chart target file required}"
BAZEL_BIN="${5:?bazel-bin path required}"

HELM="${HELM:?HELM env required}"
CHART_REPOSITORY="${CHART_REPOSITORY:-oci://ghcr.io/jomcgi/homelab/charts}"

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
FAILURES=0

_fail() {
	local chart="$1" image="$2" expected="$3" observed="$4" reason="$5"
	printf 'ERROR: publish verification run=%s chart=%s image=%s expected=%s observed=%s (%s)\n' \
		"$RUN_ID" "$chart" "$image" "$expected" "$observed" "$reason" >&2
	FAILURES=$((FAILURES + 1))
}

# Print sorted repository<TAB>digest rows from chart values. A tag-only image
# is outside this assertion because there is no chart manifest identity to
# compare. Third-party repositories are outside the publisher's first-party
# image set.
_image_pins() {
	"$HELM" show values "$@" | awk '
    function flush() {
      gsub(/"/, "", repo); gsub(/"/, "", digest)
      if (repo ~ /^ghcr\.io\/jomcgi\/homelab\// && digest != "") {
        print repo "\t" digest
      }
      repo = ""; digest = ""
    }
    /^[[:space:]]*repository:[[:space:]]*/ { flush(); repo=$2 }
    /^[[:space:]]*digest:[[:space:]]*/ && repo!="" { digest=$2 }
    END { flush() }' | LC_ALL=C sort -u
}

if [[ ! -s "$IMAGE_RESULTS" ]]; then
	_fail "-" "-" "same-run image results" "missing" "no image publication evidence"
fi
if [[ ! -s "$CHART_TARGETS" ]]; then
	_fail "-" "-" "published chart targets" "missing" "no expected chart evidence"
fi

chart_index=0
while IFS= read -r chart_target || [[ -n "$chart_target" ]]; do
	[[ -n "$chart_target" ]] || continue
	chart_index=$((chart_index + 1))
	chart_dir="${chart_target#//}"
	chart_dir="${chart_dir%%:*}"
	chart_yaml="${chart_dir}/Chart.yaml"
	chart_name=""
	if [[ -f "$chart_yaml" ]]; then
		chart_name=$(awk '$1 == "name:" { gsub(/"/, "", $2); print $2; exit }' "$chart_yaml")
	fi
	if [[ -z "$chart_name" ]]; then
		_fail "$chart_target" "-" "chart name" "missing" "invalid chart target evidence"
		continue
	fi

	record="${CHART_RECORD_DIR}/${chart_name}"
	if [[ ! -s "$record" ]]; then
		_fail "$chart_name" "-" "same-run chart record" "missing" "chart publication was not recorded"
		continue
	fi
	if ! read -r recorded_dir version extra <"$record" || [[ -n "${extra:-}" ]] ||
		[[ "$recorded_dir" != "$chart_dir" ]] ||
		[[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
		_fail "$chart_name" "-" "${chart_dir} semver" "invalid" "malformed chart publication record"
		continue
	fi

	candidate="${BAZEL_BIN}/${chart_dir}/chart.package.tgz"
	candidate_pins="${TMP}/candidate-${chart_index}"
	final_pins="${TMP}/final-${chart_index}"
	if [[ ! -s "$candidate" ]] || ! _image_pins "$candidate" >"$candidate_pins"; then
		_fail "$chart_name" "-" "fresh packaged chart" "missing" "expected pin evidence unavailable"
		continue
	fi
	if [[ ! -s "$candidate_pins" ]]; then
		_fail "$chart_name" "-" "digest-pinned first-party images" "missing" "expected image set is empty"
		continue
	fi
	if ! _image_pins "${CHART_REPOSITORY}/${chart_name}" --version "$version" >"$final_pins"; then
		_fail "$chart_name" "-" "published artifact ${version}" "missing" "final artifact bytes unavailable"
		continue
	fi

	while IFS=$'\t' read -r repository _candidate_digest || [[ -n "${repository:-}" ]]; do
		[[ -n "${repository:-}" ]] || continue
		matches="${TMP}/matches-${chart_index}"
		awk -F '\t' -v repository="$repository" '$4 == repository { print }' \
			"$IMAGE_RESULTS" >"$matches"
		match_count=$(awk 'END { print NR + 0 }' "$matches")
		if [[ "$match_count" -ne 1 ]]; then
			_fail "$chart_name" "$repository" "one same-run push result" "$match_count results" "missing or ambiguous image evidence"
			continue
		fi

		IFS=$'\t' read -r result_run provenance label _result_repository expected_digest result_extra <"$matches"
		if [[ -n "${result_extra:-}" ]] ||
			[[ "$provenance" != "pushed" && "$provenance" != "skipped" ]] ||
			[[ -z "$label" || ! "$expected_digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then
			_fail "$chart_name" "$repository" "valid image result" "invalid" "malformed image evidence"
			continue
		fi
		if [[ "$result_run" != "$RUN_ID" ]]; then
			_fail "$chart_name" "$repository" "run=${RUN_ID}" "run=${result_run}" "wrong-run image evidence"
			continue
		fi

		observed_digest=$(awk -F '\t' -v repository="$repository" '$1 == repository { print $2 }' "$final_pins")
		if [[ -z "$observed_digest" ]]; then
			_fail "$chart_name" "$repository" "$expected_digest" "missing" "final artifact pin unavailable"
		elif [[ "$observed_digest" != "$expected_digest" ]]; then
			_fail "$chart_name" "$repository" "$expected_digest" "$observed_digest" "published pin does not match ${provenance} manifest"
		fi
	done <"$candidate_pins"

	# A final first-party digest pin absent from the fresh package is stale or
	# otherwise unrelated to the chart this run built. Do not let it broaden the
	# expected set silently.
	while IFS=$'\t' read -r repository observed_digest || [[ -n "${repository:-}" ]]; do
		[[ -n "${repository:-}" ]] || continue
		if ! awk -F '\t' -v repository="$repository" '$1 == repository { found=1 } END { exit !found }' "$candidate_pins"; then
			_fail "$chart_name" "$repository" "absent" "$observed_digest" "unexpected first-party pin in final artifact"
		fi
	done <"$final_pins"
done <"$CHART_TARGETS"

if [[ "$FAILURES" -ne 0 ]]; then
	echo "${FAILURES} published image verification failure(s)" >&2
	exit 1
fi

echo "Published chart image verification passed for run=${RUN_ID}."
