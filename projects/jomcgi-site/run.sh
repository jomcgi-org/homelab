#!/usr/bin/env bash
# Deploys the site with fresh data (HOME-209). The image holds the built pages
# at /app/site; the root filesystem is read-only, so assemble a copy in /tmp,
# add each section's data from R2 under web/dist/data, then deploy that copy.
# Each deploy carries its release (chart version and image digest) as the
# deployment message.
#
# Release fence. Two paths deploy: MODE=sync (the Sync-hook Job, a release)
# and MODE=refresh (the CronJob, new data for the same release).
# - A refresh deploys only while the live deployment carries its own label.
#   It checks at the start and again just before deploying, and every step
#   runs under `timeout -k`, so the whole run ends within REFRESH_BUDGET +
#   KILL_GRACE seconds even if a step ignores SIGTERM.
# - The hook deploys, then re-checks for SYNC_WINDOW seconds, longer than
#   REFRESH_BUDGET + KILL_GRACE, and deploys again if a refresh replaced it. A refresh that
#   was running before the sync ends inside that window and cannot start again
#   (the sync replaced the CronJob's spec), so the hook always deploys last.
#   It then confirms its release is live, or fails so Argo CD retries.
#
# Env: STAGE (dev|prod), MODE (sync|refresh), RELEASE, R2_ACCESS_KEY_ID,
# R2_SECRET_ACCESS_KEY, CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID.
# The *_BIN, SITE_SRC/SITE_DIR, REFRESH_BUDGET, SYNC_WINDOW, FENCE_INTERVAL,
# FENCE_TIMEOUT and KILL_GRACE overrides exist for run_test.sh.
set -o errexit -o nounset -o pipefail

SITE_SRC="${SITE_SRC:-/app/site}"
SITE_DIR="${SITE_DIR:-/tmp/site}"
SNAPSHOT_BIN="${SNAPSHOT_BIN:-/app/projects/jomcgi-site/data/snapshot}"
WRANGLER_BIN="${WRANGLER_BIN:-/app/projects/jomcgi-site/wrangler/wrangler}"
REFRESH_BUDGET="${REFRESH_BUDGET:-240}"
SYNC_WINDOW="${SYNC_WINDOW:-300}"
FENCE_INTERVAL="${FENCE_INTERVAL:-15}"
FENCE_TIMEOUT="${FENCE_TIMEOUT:-30}"
# Seconds between a step's SIGTERM and its SIGKILL (`timeout -k`), so a step
# that ignores SIGTERM still ends; the hard bound of a refresh is
# REFRESH_BUDGET + KILL_GRACE.
KILL_GRACE="${KILL_GRACE:-5}"

case "${STAGE:-}" in
dev) export WORKER=jomcgi-site-dev ;;
prod) export WORKER=jomcgi-site ;;
*)
	echo "STAGE must be dev or prod, not '${STAGE:-}'" >&2
	exit 2
	;;
esac
case "${MODE:-}" in
sync | refresh) ;;
*)
	echo "MODE must be sync or refresh, not '${MODE:-}'" >&2
	exit 2
	;;
esac
: "${RELEASE:?RELEASE must be set}"
if ((SYNC_WINDOW <= REFRESH_BUDGET + KILL_GRACE)); then
	echo "SYNC_WINDOW ($SYNC_WINDOW) must exceed REFRESH_BUDGET + KILL_GRACE ($REFRESH_BUDGET + $KILL_GRACE)" >&2
	exit 2
fi

# Prints the fence result: 0 the live deployment is this release, 10 it is
# another release, 11 the Worker has no deployment, anything else an error (a
# timeout included). In refresh mode the
# check runs inside the run's deadline; in sync mode each check gets
# FENCE_TIMEOUT seconds.
fence() {
	local status=0 limit="$FENCE_TIMEOUT"
	if [[ "$MODE" == refresh ]]; then
		limit=$((deadline - SECONDS))
		if ((limit <= 0)); then
			echo 124
			return
		fi
	fi
	timeout -k "$KILL_GRACE" "$limit" "$SNAPSHOT_BIN" fence-check >&2 || status=$?
	echo "$status"
}

# In refresh mode: return on 0, exit 0 (skip) on 10 or 11 (the hook makes the
# first deploy), and fail on an error, so a broken check shows as a failed Job
# instead of a quiet skip.
refresh_gate() {
	local status
	status="$(fence)"
	case "$status" in
	0) ;;
	10 | 11)
		echo "refresh skipped: the live deployment is not this release (fence $status, $1)"
		exit 0
		;;
	*)
		echo "refresh failed: the release fence check failed with $status ($1)" >&2
		exit 1
		;;
	esac
}

# Runs a step; in refresh mode it must end before the run's deadline.
step() {
	if [[ "$MODE" == refresh ]]; then
		local left=$((deadline - SECONDS))
		if ((left <= 0)); then
			echo "refresh failed: out of time before: $*" >&2
			exit 1
		fi
		timeout -k "$KILL_GRACE" "$left" "$@"
	else
		"$@"
	fi
}

# wrangler writes .wrangler/tmp under its working directory, not beside its
# config, and the image's working directory (/app/site) is read-only. So it
# runs from the site copy.
deploy() {
	cd "$SITE_DIR"
	step "$WRANGLER_BIN" deploy --config "$SITE_DIR/wrangler.jsonc" \
		--env "$STAGE" --message "jomcgi-site $RELEASE"
}

deadline=$((SECONDS + REFRESH_BUDGET))
if [[ "$MODE" == refresh ]]; then
	refresh_gate "before the snapshot"
fi

step rm -rf "$SITE_DIR"
step cp -R "$SITE_SRC" "$SITE_DIR"
step "$SNAPSHOT_BIN" "$SITE_DIR/web/dist/data"

if [[ "$MODE" == refresh ]]; then
	refresh_gate "before the deploy"
fi
deploy

if [[ "$MODE" == sync ]]; then
	end=$((SECONDS + SYNC_WINDOW))
	while ((SECONDS < end)); do
		sleep "$FENCE_INTERVAL"
		status="$(fence)"
		if [[ "$status" == 10 || "$status" == 11 ]]; then
			echo "$RELEASE is not live (fence $status); deploying it again"
			deploy
		elif [[ "$status" != 0 ]]; then
			echo "fence check failed ($status); will check again" >&2
		fi
	done
	# The window is over, so no older refresh can still deploy. Confirm this
	# release is live; if it cannot be confirmed, fail the sync so Argo CD
	# retries it.
	status="$(fence)"
	if [[ "$status" == 10 || "$status" == 11 ]]; then
		echo "$RELEASE is not live (fence $status); deploying it again"
		deploy
		status="$(fence)"
	fi
	# Only the release's own label confirms it; "no deployment" does not.
	if [[ "$status" != 0 ]]; then
		echo "sync failed: could not confirm $RELEASE is live (fence $status)" >&2
		exit 1
	fi
	echo "confirmed: $RELEASE is live"
fi
