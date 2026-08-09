#!/usr/bin/env bash
# Unit tests for check-missed-bump.sh.
#
# The script under test compares the ghcr.io/jomcgi image digests pinned in a
# freshly built chart against the published chart of the same version and fails
# (exit 1) only when the version is already published AND the digests differ.
# helm is stubbed so no network or real binaries are needed. The
# stubs key their behaviour off env vars set per case:
#   STUB_CHART_RC     exit code of `helm show chart` (0 = published)
#   STUB_FRESH_TAG    tag `helm show values <tgz>` reports (the PR build)
#   STUB_PUB_TAG      tag `helm show values ... --version` reports (published)
#   STUB_FRESH_DIGEST digest pinned in the fresh chart's values
#   STUB_PUB_DIGEST   digest pinned in the published chart's values
#   STUB_FRESH_NO_DIGEST / STUB_PUB_NO_DIGEST
#                     omit `digest:` from that side's values, as a chart
#                     published before digest pinning would (forces UNRESOLVED)
#   STUB_NO_IMAGES    if set, `helm show values` pins no images
set -o errexit -o nounset -o pipefail

SCRIPT_REL="bazel/helm/check-missed-bump.sh"
SCRIPT=""
for candidate in \
	"${RUNFILES_DIR:-}/_main/${SCRIPT_REL}" \
	"${TEST_SRCDIR:-}/_main/${SCRIPT_REL}" \
	"${BASH_SOURCE[0]%/*}/check-missed-bump.sh"; do
	if [[ -f "$candidate" ]]; then
		SCRIPT="$candidate"
		break
	fi
done
if [[ -z "$SCRIPT" ]]; then
	echo "ERROR: cannot locate check-missed-bump.sh in runfiles" >&2
	exit 1
fi

TMP="${TEST_TMPDIR:-$(mktemp -d)}"
FAILURES=0

# --- stubs -----------------------------------------------------------------
STUB_HELM="$TMP/helm"
cat >"$STUB_HELM" <<'EOF'
#!/usr/bin/env bash
sub="${1:-} ${2:-}"
case "$sub" in
  "show chart")
    # STUB_CHART_PUBLISH_AFTER simulates main's publish landing mid-poll: the
    # first N calls report unpublished, later ones published. Call count is
    # kept in STUB_CHART_COUNT_FILE (unique per test case).
    if [[ -n "${STUB_CHART_PUBLISH_AFTER:-}" ]]; then
      n=$(cat "${STUB_CHART_COUNT_FILE}" 2>/dev/null || echo 0)
      n=$((n + 1))
      echo "$n" > "${STUB_CHART_COUNT_FILE}"
      [[ "$n" -gt "${STUB_CHART_PUBLISH_AFTER}" ]] && exit 0
      exit 1
    fi
    exit "${STUB_CHART_RC:-0}" ;;
  "show values")
    [[ -n "${STUB_NO_IMAGES:-}" ]] && exit 0
    if [[ "$*" == *--version* ]]; then
      tag="${STUB_PUB_TAG}"; digest="${STUB_PUB_DIGEST:-p}"; nodigest="${STUB_PUB_NO_DIGEST:-}"
    else
      tag="${STUB_FRESH_TAG}"; digest="${STUB_FRESH_DIGEST:-f}"; nodigest="${STUB_FRESH_NO_DIGEST:-}"
    fi
    printf 'image:\n  repository: ghcr.io/jomcgi/homelab/projects/foo/backend\n  tag: %s\n' "$tag"
    # A chart published before digest pinning emits repository+tag only.
    [[ -n "$nodigest" ]] || printf '  digest: sha256:%s\n' "$digest" ;;
  *) exit 0 ;;
esac
EOF
chmod +x "$STUB_HELM"

FAKE_TGZ="$TMP/foo.tgz"
: >"$FAKE_TGZ"

run_check() {
	# Echoes exit code; stdout+stderr captured in $TMP/out.
	set +e
	env HELM="$STUB_HELM" \
		REPOSITORY="oci://ghcr.io/jomcgi/homelab/charts" \
		"$@" \
		bash "$SCRIPT" foo 1.2.3 "$FAKE_TGZ" projects/foo >"$TMP/out" 2>&1
	local rc=$?
	set -e
	echo "$rc"
}

expect() {
	# $1 test name, $2 expected exit, $3 exit, $4 substring (optional), out=$TMP/out
	local name="$1" want="$2" got="$3" needle="${4:-}"
	if [[ "$got" != "$want" ]]; then
		echo "FAIL: ${name}: expected exit ${want}, got ${got}"
		echo "  --- output ---"
		sed 's/^/  /' "$TMP/out"
		FAILURES=$((FAILURES + 1))
		return
	fi
	if [[ -n "$needle" ]] && ! grep -qF "$needle" "$TMP/out"; then
		echo "FAIL: ${name}: output missing '${needle}'"
		echo "  --- output ---"
		sed 's/^/  /' "$TMP/out"
		FAILURES=$((FAILURES + 1))
		return
	fi
	echo "PASS: ${name}"
}

# 1. Published + digests differ -> FAIL with the bump command.
rc=$(run_check STUB_CHART_RC=0 STUB_FRESH_TAG=fresh STUB_PUB_TAG=pub \
	STUB_FRESH_DIGEST=aaa STUB_PUB_DIGEST=bbb)
expect "published+drift fails" 1 "$rc" "will NOT deploy"
[[ "$rc" == 1 ]] && grep -qF "bazel/tools/git/bump-chart.sh projects/foo" "$TMP/out" &&
	echo "PASS: fail message names the bump command" ||
	{
		echo "FAIL: fail message missing bump command"
		FAILURES=$((FAILURES + 1))
	}

# 2. Published + identical digests (different build-timestamp tags) -> OK.
rc=$(run_check STUB_CHART_RC=0 STUB_FRESH_TAG=fresh STUB_PUB_TAG=pub \
	STUB_FRESH_DIGEST=same STUB_PUB_DIGEST=same)
expect "published+match passes" 0 "$rc" "digests match"

# 3. Version not yet published (this PR bumped it) -> OK, nothing to assert.
rc=$(run_check STUB_CHART_RC=1 STUB_FRESH_TAG=fresh STUB_PUB_TAG=pub \
	STUB_FRESH_DIGEST=aaa STUB_PUB_DIGEST=bbb)
expect "unpublished passes" 0 "$rc" "not published yet"

# 4. Published chart predates digest pinning (repository+tag, no digest) ->
# FAIL OPEN. Never block a PR on a chart we cannot compare content-wise.
rc=$(run_check STUB_CHART_RC=0 STUB_FRESH_TAG=fresh STUB_PUB_TAG=pub \
	STUB_PUB_NO_DIGEST=1 STUB_FRESH_DIGEST=aaa STUB_PUB_DIGEST=bbb)
expect "unpinned published chart fails open" 0 "$rc" "fail open"

# 4b. Same on the fresh side.
rc=$(run_check STUB_CHART_RC=0 STUB_FRESH_TAG=fresh STUB_PUB_TAG=pub \
	STUB_FRESH_NO_DIGEST=1 STUB_FRESH_DIGEST=aaa STUB_PUB_DIGEST=bbb)
expect "unpinned fresh chart fails open" 0 "$rc" "fail open"

# 4c. Identical digests behind DIFFERENT tags still pass: the tag is not the
# comparison, and after the PR-push removal the fresh tag is not in ghcr at all.
rc=$(run_check STUB_CHART_RC=0 STUB_FRESH_TAG=never-pushed STUB_PUB_TAG=pub \
	STUB_FRESH_DIGEST=same STUB_PUB_DIGEST=same)
expect "unpushed fresh tag still compares" 0 "$rc" "digests match"

# 5. No ghcr.io/jomcgi images pinned -> OK, nothing to compare.
rc=$(run_check STUB_CHART_RC=0 STUB_NO_IMAGES=1)
expect "no images passes" 0 "$rc" "nothing to check"

# 6. Unpublished but origin/main claims a DIFFERENT version -> this PR carries
# the bump; still OK.
rc=$(run_check STUB_CHART_RC=1 MAIN_CHART_VERSION=1.2.2 \
	STUB_FRESH_TAG=fresh STUB_PUB_TAG=pub STUB_FRESH_DIGEST=aaa STUB_PUB_DIGEST=bbb)
expect "unpublished with own bump passes" 0 "$rc" "carries the bump"

# 7. Unpublished AND origin/main claims the SAME version, publish never lands
# -> FAIL CLOSED (collision signature / broken main publish).
rc=$(run_check STUB_CHART_RC=1 MAIN_CHART_VERSION=1.2.3 \
	PUBLISH_WAIT_TRIES=2 PUBLISH_WAIT_SECS=0 \
	STUB_FRESH_TAG=fresh STUB_PUB_TAG=pub STUB_FRESH_DIGEST=aaa STUB_PUB_DIGEST=bbb)
expect "unpublished main version fails closed" 1 "$rc" "not in the registry"

# 8. Same, but main's publish lands mid-poll and digests match -> OK.
rc=$(run_check MAIN_CHART_VERSION=1.2.3 \
	STUB_CHART_PUBLISH_AFTER=2 STUB_CHART_COUNT_FILE="$TMP/count8" \
	PUBLISH_WAIT_TRIES=5 PUBLISH_WAIT_SECS=0 \
	STUB_FRESH_TAG=fresh STUB_PUB_TAG=pub STUB_FRESH_DIGEST=same STUB_PUB_DIGEST=same)
expect "publish lands mid-poll + match passes" 0 "$rc" "digests match"

# 9. Same, publish lands mid-poll but digests DIFFER -> FAIL with the bump
# command (the dropped-bump case caught after the forced branch update).
rc=$(run_check MAIN_CHART_VERSION=1.2.3 \
	STUB_CHART_PUBLISH_AFTER=2 STUB_CHART_COUNT_FILE="$TMP/count9" \
	PUBLISH_WAIT_TRIES=5 PUBLISH_WAIT_SECS=0 \
	STUB_FRESH_TAG=fresh STUB_PUB_TAG=pub STUB_FRESH_DIGEST=aaa STUB_PUB_DIGEST=bbb)
expect "publish lands mid-poll + drift fails" 1 "$rc" "will NOT deploy"

if [[ "$FAILURES" -gt 0 ]]; then
	echo "${FAILURES} test(s) failed"
	exit 1
fi
echo "All check-missed-bump tests passed"
