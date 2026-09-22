#!/usr/bin/env bash
# Hermetic contract tests for the final chart versus image result assertion.

set -o errexit -o nounset -o pipefail

SCRIPT_REL="bazel/helm/verify-published-images.sh"
SCRIPT=""
for candidate in \
	"${RUNFILES_DIR:-}/_main/${SCRIPT_REL}" \
	"${TEST_SRCDIR:-}/_main/${SCRIPT_REL}" \
	"${BASH_SOURCE[0]%/*}/verify-published-images.sh"; do
	if [[ -f "$candidate" ]]; then
		SCRIPT="$candidate"
		break
	fi
done
if [[ -z "$SCRIPT" ]]; then
	echo "ERROR: cannot locate verify-published-images.sh in runfiles" >&2
	exit 1
fi
SCRIPT="$(cd "$(dirname "$SCRIPT")" && pwd)/$(basename "$SCRIPT")"

TMP="${TEST_TMPDIR:-$(mktemp -d)}"
FAILURES=0
RUN="source-commit-123"
REPO="ghcr.io/jomcgi/homelab/projects/demo/image"
INDEX_DIGEST="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
STALE_DIGEST="sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

pass() { echo "  PASS: $1"; }
fail() {
	echo "  FAIL: $1" >&2
	echo "$2" | sed 's/^/        /' >&2
	FAILURES=$((FAILURES + 1))
}

setup() {
	CASE="$1"
	CASE_DIR="$TMP/$CASE"
	mkdir -p "$CASE_DIR/projects/demo/chart" \
		"$CASE_DIR/bazel-bin/projects/demo/chart" "$CASE_DIR/records"
	printf 'name: demo\nversion: 1.2.3\n' >"$CASE_DIR/projects/demo/chart/Chart.yaml"
	printf '%s\n' '//projects/demo/chart:chart.push' >"$CASE_DIR/chart-targets"
	printf '%s\n' 'projects/demo/chart 1.2.3' >"$CASE_DIR/records/demo"
	: >"$CASE_DIR/results"
	: >"$CASE_DIR/final.values"
	: >"$CASE_DIR/bazel-bin/projects/demo/chart/chart.package.tgz"

	cat >"$CASE_DIR/helm" <<'STUB'
#!/usr/bin/env bash
if [[ "${1:-}" != "show" || "${2:-}" != "values" ]]; then
  exit 2
fi
case "${3:-}" in
  */chart.package.tgz) cat "$3" ;;
  oci://*)
    [[ -n "${FINAL_VALUES:-}" && -f "$FINAL_VALUES" ]] || exit 1
    cat "$FINAL_VALUES"
    ;;
  *) exit 2 ;;
esac
STUB
	chmod +x "$CASE_DIR/helm"
}

candidate_values() {
	cat <<EOF
image:
  repository: ${REPO}
  tag: desired-input
  digest: ${1}
thirdParty:
  repository: docker.io/library/busybox
  digest: sha256:cccc
tagOnly:
  repository: ghcr.io/jomcgi/homelab/tools/tag-only
  tag: main
EOF
}

final_values() {
	cat <<EOF
image:
  repository: ${REPO}
  tag: published
  digest: ${1}
thirdParty:
  repository: docker.io/library/busybox
  digest: sha256:dddd
tagOnly:
  repository: ghcr.io/jomcgi/homelab/tools/tag-only
  tag: main
EOF
}

run_case() {
	(
		cd "$CASE_DIR"
		HELM="$CASE_DIR/helm" FINAL_VALUES="${FINAL_VALUES_OVERRIDE:-$CASE_DIR/final.values}" \
			bash "$SCRIPT" "$RUN" "$CASE_DIR/results" "$CASE_DIR/records" \
			"$CASE_DIR/chart-targets" "$CASE_DIR/bazel-bin"
	) 2>&1
}

echo "verify-published-images.sh"

# The candidate archive defines membership only. Its desired digest deliberately
# differs from both the pushed tag result and final artifact, proving the test
# compares independent publication evidence rather than desired input twice.
setup matching-changed
candidate_values "$STALE_DIGEST" >"$CASE_DIR/bazel-bin/projects/demo/chart/chart.package.tgz"
final_values "$INDEX_DIGEST" >"$CASE_DIR/final.values"
printf '%s\tpushed\t%s\t%s\t%s\n' "$RUN" '//projects/demo:image.push' "$REPO" "$INDEX_DIGEST" >"$CASE_DIR/results"
if out=$(run_case); then
	pass "matching changed multi-platform index digest passes from final artifact bytes"
else
	fail "matching changed image" "$out"
fi

# A stale final pin fails even when it equals the candidate's desired input.
setup stale-pin
candidate_values "$STALE_DIGEST" >"$CASE_DIR/bazel-bin/projects/demo/chart/chart.package.tgz"
final_values "$STALE_DIGEST" >"$CASE_DIR/final.values"
printf '%s\tpushed\t%s\t%s\t%s\n' "$RUN" '//projects/demo:image.push' "$REPO" "$INDEX_DIGEST" >"$CASE_DIR/results"
if out=$(run_case); then
	fail "stale pin mismatch" "expected non-zero"
elif [[ "$out" == *"run=${RUN}"* && "$out" == *"chart=demo"* &&
	"$out" == *"image=${REPO}"* && "$out" == *"expected=${INDEX_DIGEST}"* &&
	"$out" == *"observed=${STALE_DIGEST}"* ]]; then
	pass "stale pin fails with concise run, chart, image, expected and observed diagnostics"
else
	fail "stale pin diagnostics" "$out"
fi

setup missing-push-result
candidate_values "$INDEX_DIGEST" >"$CASE_DIR/bazel-bin/projects/demo/chart/chart.package.tgz"
final_values "$INDEX_DIGEST" >"$CASE_DIR/final.values"
if out=$(run_case); then
	fail "missing push result" "expected non-zero"
elif [[ "$out" == *"one same-run push result"* && "$out" == *"missing or ambiguous image evidence"* ]]; then
	pass "missing expected push result fails closed"
else
	fail "missing push result diagnostics" "$out"
fi

setup missing-final-artifact
candidate_values "$INDEX_DIGEST" >"$CASE_DIR/bazel-bin/projects/demo/chart/chart.package.tgz"
printf '%s\tpushed\t%s\t%s\t%s\n' "$RUN" '//projects/demo:image.push' "$REPO" "$INDEX_DIGEST" >"$CASE_DIR/results"
FINAL_VALUES_OVERRIDE="$CASE_DIR/does-not-exist"
if out=$(run_case); then
	fail "missing final artifact" "expected non-zero"
elif [[ "$out" == *"final artifact bytes unavailable"* && "$out" == *"observed=missing"* ]]; then
	pass "missing final artifact bytes fail closed"
else
	fail "missing final artifact diagnostics" "$out"
fi
unset FINAL_VALUES_OVERRIDE

setup missing-final-pin
candidate_values "$INDEX_DIGEST" >"$CASE_DIR/bazel-bin/projects/demo/chart/chart.package.tgz"
printf 'other:\n  repository: docker.io/library/busybox\n  digest: sha256:dddd\n' >"$CASE_DIR/final.values"
printf '%s\tpushed\t%s\t%s\t%s\n' "$RUN" '//projects/demo:image.push' "$REPO" "$INDEX_DIGEST" >"$CASE_DIR/results"
if out=$(run_case); then
	fail "missing final pin" "expected non-zero"
elif [[ "$out" == *"final artifact pin unavailable"* && "$out" == *"observed=missing"* ]]; then
	pass "missing expected pin in final artifact fails closed"
else
	fail "missing final pin diagnostics" "$out"
fi

setup valid-skipped
candidate_values "$INDEX_DIGEST" >"$CASE_DIR/bazel-bin/projects/demo/chart/chart.package.tgz"
final_values "$INDEX_DIGEST" >"$CASE_DIR/final.values"
printf '%s\tskipped\t%s\t%s\t%s\n' "$RUN" '//projects/demo:image.push' "$REPO" "$INDEX_DIGEST" >"$CASE_DIR/results"
if out=$(run_case); then
	pass "legitimately unchanged image passes with registry provenance"
else
	fail "valid unchanged image" "$out"
fi

setup stale-invocation
candidate_values "$INDEX_DIGEST" >"$CASE_DIR/bazel-bin/projects/demo/chart/chart.package.tgz"
final_values "$INDEX_DIGEST" >"$CASE_DIR/final.values"
printf '%s\tpushed\t%s\t%s\t%s\n' 'older-commit' '//projects/demo:image.push' "$REPO" "$INDEX_DIGEST" >"$CASE_DIR/results"
if out=$(run_case); then
	fail "stale invocation evidence" "expected non-zero"
elif [[ "$out" == *"expected=run=${RUN}"* && "$out" == *"observed=run=older-commit"* ]]; then
	pass "wrong-run image evidence fails closed"
else
	fail "stale invocation diagnostics" "$out"
fi

echo ""
if [[ "$FAILURES" -ne 0 ]]; then
	echo "$FAILURES test(s) failed"
	exit 1
fi
echo "All published image verification tests passed"
