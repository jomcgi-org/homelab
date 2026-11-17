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
# (one per line, consumed in order; 0 when empty); otherwise it writes data,
# after sleeping $SNAPSHOT_SLEEP seconds.
cat >"$work/bin/snapshot" <<'STUB'
#!/usr/bin/env bash
if [[ "$1" == fence-check ]]; then
	echo "fence-check $WORKER $RELEASE" >>"$CALLS"
	sleep "${FENCE_SLEEP:-0}"
	code="$(head -n1 "$FENCE_CODES" 2>/dev/null || true)"
	sed -i '1d' "$FENCE_CODES" 2>/dev/null || true
	exit "${code:-0}"
fi
if [[ -n "${SNAPSHOT_IGNORE_TERM:-}" ]]; then trap '' TERM; fi
sleep "${SNAPSHOT_SLEEP:-0}"
mkdir -p "$1/stars" && echo '{"asOf":"x"}' >"$1/stars/latest.json"
echo "snapshot $1" >>"$CALLS"
STUB
cat >"$work/bin/wrangler" <<'STUB'
#!/usr/bin/env bash
set -o errexit -o nounset
echo "wrangler $*" >>"$CALLS"
# The snapshot's data must be in the copy being deployed.
test -f "$(dirname "$3")/web/dist/data/stars/latest.json" || {
	echo "no snapshot data in $(dirname "$3")" >&2
	exit 8
}
# wrangler writes .wrangler/tmp under its working directory: it must be the
# writable site copy, not the read-only image directory.
[[ "$PWD" == "$(dirname "$3")" ]] || {
	echo "wrangler ran from $PWD" >&2
	exit 9
}
STUB
chmod +x "$work/bin/"*

run() {
	CALLS="$work/calls" FENCE_CODES="$work/codes" SITE_SRC="$work/src" SITE_DIR="$work/site" \
		SNAPSHOT_BIN="$work/bin/snapshot" WRANGLER_BIN="$work/bin/wrangler" \
		REFRESH_BUDGET=2 KILL_GRACE=1 SYNC_WINDOW=4 FENCE_INTERVAL=1 FENCE_TIMEOUT=2 RELEASE="chart 0.9.0 image abc" "$@" bash "$RUN"
}
expect() {
	if [[ "$(cat "$work/calls")" != "$1" ]]; then
		printf 'case: %s\nexpected:\n%s\ngot:\n' "$2" "$1"
		cat "$work/calls"
		exit 1
	fi
}
reset() {
	: >"$work/calls"
	printf '%s' "${1:-}" >"$work/codes"
}
D="wrangler deploy --config $work/site/wrangler.jsonc"
M="--message jomcgi-site chart 0.9.0 image abc"
F_PROD="fence-check jomcgi-site chart 0.9.0 image abc"
F_DEV="fence-check jomcgi-site-dev chart 0.9.0 image abc"
S="snapshot $work/site/web/dist/data"

# Lines of the calls log after the first two (snapshot, first deploy).
after_deploy() { tail -n +3 "$work/calls"; }

# 1. sync: snapshot into the copy, deploy with the release label, then only
#    re-check during the window (3 s at 1 s intervals; the count depends on
#    timing, so check the shape).
reset
run env STAGE=prod MODE=sync
[[ "$(head -n2 "$work/calls")" == "$S
$D --env prod $M" ]] || {
	echo "sync: wrong first steps"
	cat "$work/calls"
	exit 1
}
[[ "$(after_deploy | sort -u)" == "$F_PROD" ]] || {
	echo "sync: only fence checks expected after the deploy"
	cat "$work/calls"
	exit 1
}
[[ ! -e "$work/src/web/dist/data" ]] || {
	echo "source was modified"
	exit 1
}

# 2. sync: a refresh replaced the release during the window: deploy again,
#    once, and keep checking.
reset $'0\n10\n'
run env STAGE=prod MODE=sync
[[ "$(grep -c '^wrangler deploy' "$work/calls")" == 2 ]] || {
	echo "sync: expected one redeploy"
	cat "$work/calls"
	exit 1
}
[[ "$(after_deploy | head -n3)" == "$F_PROD
$F_PROD
$D --env prod $M" ]] || {
	echo "sync: redeploy should follow the replaced check"
	cat "$work/calls"
	exit 1
}

# 3. refresh: its own release is live at both checks, so it deploys.
reset
run env STAGE=dev MODE=refresh
expect "$F_DEV
$S
$F_DEV
$D --env dev $M" "refresh deploys over its own release"

# 4. refresh: another release (10) or no deployment (11) at the start, or
#    another release before the deploy: skip with exit 0 and no deploy.
for code in 10 11; do
	reset "$code"
	run env STAGE=dev MODE=refresh
	expect "$F_DEV" "refresh skips at the start (fence $code)"
done
reset $'0\n10\n'
run env STAGE=dev MODE=refresh
expect "$F_DEV
$S
$F_DEV" "refresh skips before the deploy"

# 5. refresh: a failed check fails the Job (non-zero), with no deploy.
for codes in "1" $'0\n1\n'; do
	reset "$codes"
	if run env STAGE=dev MODE=refresh 2>/dev/null; then
		echo "a failed fence check should fail the refresh"
		exit 1
	fi
	! grep -q wrangler "$work/calls" || {
		echo "deployed after a failed check"
		exit 1
	}
done

# 6. refresh: a snapshot slower than the budget is stopped; no deploy.
reset
if run env STAGE=dev MODE=refresh SNAPSHOT_SLEEP=4 2>/dev/null; then
	echo "a refresh over its budget should fail"
	exit 1
fi
! grep -q wrangler "$work/calls" || {
	echo "deployed after the budget"
	exit 1
}

# 6a. refresh: a snapshot that ignores SIGTERM is killed after the grace, so
#     the run still ends near REFRESH_BUDGET + KILL_GRACE (2 + 1 s).
reset
started=$SECONDS
if run env STAGE=dev MODE=refresh SNAPSHOT_SLEEP=30 SNAPSHOT_IGNORE_TERM=1 2>/dev/null; then
	echo "a refresh whose snapshot ignores SIGTERM should fail"
	exit 1
fi
((SECONDS - started < 10)) || {
	echo "the refresh outlived its hard bound"
	exit 1
}
! grep -q wrangler "$work/calls" || {
	echo "deployed after the bound"
	exit 1
}

# 6b. refresh: a fence check that hangs is stopped by the budget; no deploy.
reset
if run env STAGE=dev MODE=refresh FENCE_SLEEP=5 2>/dev/null; then
	echo "a hung fence check should fail the refresh"
	exit 1
fi
! grep -q wrangler "$work/calls" || {
	echo "deployed after a hung check"
	exit 1
}

# 6c. sync: if the live release cannot be confirmed at the end (every check
#     errors), the sync fails so Argo CD retries.
reset $'1\n1\n1\n1\n1\n1\n1\n1\n'
if run env STAGE=prod MODE=sync 2>/dev/null; then
	echo "a sync that cannot confirm its release should fail"
	exit 1
fi

# 6c2. sync: every check reports no deployment (11). Each one deploys again,
#      and the sync still fails, because only its own label confirms it.
reset $'11\n11\n11\n11\n11\n11\n11\n11\n'
if run env STAGE=prod MODE=sync 2>/dev/null; then
	echo "a sync that never sees its own label should fail"
	exit 1
fi
(($(grep -c '^wrangler deploy' "$work/calls") >= 2)) || {
	echo "no deployment should trigger a redeploy"
	cat "$work/calls"
	exit 1
}

# 6d. sync: errors during the window but a good final check: success.
reset $'1\n1\n'
run env STAGE=prod MODE=sync >/dev/null
tail -n1 "$work/calls" | grep -q "^$F_PROD$" || {
	echo "sync should end with a confirming check"
	cat "$work/calls"
	exit 1
}

# 7. A bad stage, mode or window stops before any step.
for args in "STAGE= MODE=sync" "STAGE=staging MODE=sync" "STAGE=dev MODE=" "STAGE=dev MODE=other" "STAGE=dev MODE=sync SYNC_WINDOW=3"; do
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

# 7b. A snapshot that exits 0 but writes no data: the deploy step fails (the
#     stub checks the copy holds the data), so the run fails.
cat >"$work/bin/snapshot.nodata" <<'STUB'
#!/usr/bin/env bash
[[ "$1" == fence-check ]] && exit 0
exit 0
STUB
chmod +x "$work/bin/snapshot.nodata"
reset
if CALLS="$work/calls" FENCE_CODES="$work/codes" SITE_SRC="$work/src" SITE_DIR="$work/site" \
	SNAPSHOT_BIN="$work/bin/snapshot.nodata" WRANGLER_BIN="$work/bin/wrangler" \
	REFRESH_BUDGET=2 KILL_GRACE=1 SYNC_WINDOW=4 FENCE_INTERVAL=1 FENCE_TIMEOUT=2 \
	RELEASE="chart 0.9.0 image abc" STAGE=dev MODE=sync bash "$RUN" 2>/dev/null; then
	echo "a deploy without snapshot data should fail"
	exit 1
fi

# 8. A failed snapshot stops before the deploy.
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
