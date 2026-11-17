#!/usr/bin/env bash
# Tests run.sh with stub snapshot and wrangler binaries.
set -o errexit -o nounset -o pipefail
RUN="$(cd "$(dirname "$0")" && pwd)/run.sh"
[[ -f "$RUN" ]] || RUN="${TEST_SRCDIR}/_main/projects/jomcgi-site/run.sh"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

mkdir -p "$work/src/web/dist" "$work/bin"
echo '{}' >"$work/src/wrangler.jsonc"
echo '<html>' >"$work/src/web/dist/index.html"
# The stub snapshot: `fence-check` exits with the next code in $FENCE_CODES
# (one per line, consumed in order; 0 when empty); otherwise it writes data.
cat >"$work/bin/snapshot" <<'STUB'
#!/usr/bin/env bash
if [[ "$1" == fence-check ]]; then
	echo "fence-check $WORKER $CHART_VERSION" >>"$CALLS"
	code="$(head -n1 "$FENCE_CODES" 2>/dev/null || true)"
	sed -i '1d' "$FENCE_CODES" 2>/dev/null || true
	exit "${code:-0}"
fi
mkdir -p "$1/stars" && echo '{"asOf":"x"}' >"$1/stars/latest.json"
echo "snapshot $1" >>"$CALLS"
STUB
cat >"$work/bin/wrangler" <<'STUB'
#!/usr/bin/env bash
echo "wrangler $*" >>"$CALLS"
test -f "$(dirname "$3")/web/dist/data/stars/latest.json"
STUB
chmod +x "$work/bin/"*

run() {
	CALLS="$work/calls" FENCE_CODES="$work/codes" SITE_SRC="$work/src" SITE_DIR="$work/site" \
		SNAPSHOT_BIN="$work/bin/snapshot" WRANGLER_BIN="$work/bin/wrangler" \
		FENCE_CHECKS=2 FENCE_INTERVAL=0 CHART_VERSION=0.9.0 "$@" bash "$RUN"
}
expect() {
	if [[ "$(cat "$work/calls")" != "$1" ]]; then
		echo "case: $2"
		echo "expected:"
		echo "$1"
		echo "got:"
		cat "$work/calls"
		exit 1
	fi
}
reset() {
	: >"$work/calls"
	printf '%s' "${1:-}" >"$work/codes"
}
DEPLOY_PROD="wrangler deploy --config $work/site/wrangler.jsonc --env prod --message jomcgi-site chart 0.9.0"
DEPLOY_DEV="wrangler deploy --config $work/site/wrangler.jsonc --env dev --message jomcgi-site chart 0.9.0"

# 1. sync: snapshot into the copy, deploy with the chart label, then re-check.
reset
run env STAGE=prod MODE=sync
expect "snapshot $work/site/web/dist/data
$DEPLOY_PROD
fence-check jomcgi-site 0.9.0
fence-check jomcgi-site 0.9.0" "sync deploys once when nothing replaces it"
[[ ! -e "$work/src/web/dist/data" ]] || {
	echo "source was modified"
	exit 1
}

# 2. sync: an older refresh replaced the release, so it deploys again.
reset $'10\n0\n'
run env STAGE=prod MODE=sync
expect "snapshot $work/site/web/dist/data
$DEPLOY_PROD
fence-check jomcgi-site 0.9.0
$DEPLOY_PROD
fence-check jomcgi-site 0.9.0" "sync redeploys after being replaced"

# 3. refresh: the live deployment is this release, so it deploys (no re-check).
reset
run env STAGE=dev MODE=refresh
expect "fence-check jomcgi-site-dev 0.9.0
snapshot $work/site/web/dist/data
$DEPLOY_DEV" "refresh deploys over its own release"

# 4. refresh: a different release is live, or the check fails: no deploy.
for code in 10 1; do
	reset "$code"
	run env STAGE=dev MODE=refresh
	expect "fence-check jomcgi-site-dev 0.9.0" "refresh skips (fence $code)"
done

# 5. A bad stage or mode stops before any step.
for args in "STAGE= MODE=sync" "STAGE=staging MODE=sync" "STAGE=dev MODE=" "STAGE=dev MODE=other"; do
	reset
	# shellcheck disable=SC2086
	if run env $args 2>/dev/null; then
		echo "'$args' should fail"
		exit 1
	fi
	[[ ! -s "$work/calls" ]] || {
		echo "'$args' ran a step"
		exit 1
	}
done

# 6. A failed snapshot stops before the deploy.
cat >"$work/bin/snapshot" <<'STUB'
#!/usr/bin/env bash
[[ "$1" == fence-check ]] && exit 0
exit 3
STUB
reset
if run env STAGE=dev MODE=sync; then
	echo "a failed snapshot should fail the run"
	exit 1
fi
! grep -q wrangler "$work/calls" || {
	echo "deployed after a failed snapshot"
	exit 1
}
echo "PASS"
