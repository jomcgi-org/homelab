#!/usr/bin/env bash
# Deploys the site with fresh data (HOME-209). The image holds the built pages
# at /app/site; the root filesystem is read-only, so assemble a copy in /tmp,
# add each section's data from R2 under web/dist/data, then deploy that copy.
#
# MODE=sync (the Sync-hook Job) deploys this release, then re-checks for a
# while and deploys again if an older refresh that was already running
# replaced it. MODE=refresh (the CronJob) deploys only while the live
# deployment is this release, so a refresh never replaces another release.
# Each deploy carries its chart version as the deployment message.
#
# Env: STAGE (dev|prod), MODE (sync|refresh), CHART_VERSION, R2_ACCESS_KEY_ID,
# R2_SECRET_ACCESS_KEY, CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID.
# The *_BIN, SITE_SRC/SITE_DIR and FENCE_* overrides exist for run_test.sh.
set -o errexit -o nounset -o pipefail

SITE_SRC="${SITE_SRC:-/app/site}"
SITE_DIR="${SITE_DIR:-/tmp/site}"
SNAPSHOT_BIN="${SNAPSHOT_BIN:-/app/projects/jomcgi-site/data/snapshot}"
WRANGLER_BIN="${WRANGLER_BIN:-/app/projects/jomcgi-site/wrangler/wrangler}"
FENCE_CHECKS="${FENCE_CHECKS:-6}"
FENCE_INTERVAL="${FENCE_INTERVAL:-15}"

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
: "${CHART_VERSION:?CHART_VERSION must be set}"

# 0: this release may deploy; 10: a different release is live; else an error.
fence() {
	local status=0
	"$SNAPSHOT_BIN" fence-check || status=$?
	return "$status"
}

deploy() {
	"$WRANGLER_BIN" deploy --config "$SITE_DIR/wrangler.jsonc" --env "$STAGE" \
		--message "jomcgi-site chart $CHART_VERSION"
}

if [[ "$MODE" == refresh ]]; then
	status=0
	fence || status=$?
	if [[ "$status" -ne 0 ]]; then
		echo "refresh skipped: the live deployment is not chart $CHART_VERSION (fence $status)"
		exit 0
	fi
fi

rm -rf "$SITE_DIR"
cp -R "$SITE_SRC" "$SITE_DIR"
"$SNAPSHOT_BIN" "$SITE_DIR/web/dist/data"
deploy

if [[ "$MODE" == sync ]]; then
	# A refresh from the old CronJob spec may still be deploying. It cannot
	# start again after this sync, so a few checks are enough.
	for _ in $(seq 1 "$FENCE_CHECKS"); do
		sleep "$FENCE_INTERVAL"
		status=0
		fence || status=$?
		if [[ "$status" -eq 10 ]]; then
			echo "another deploy replaced chart $CHART_VERSION; deploying it again"
			deploy
		elif [[ "$status" -ne 0 ]]; then
			echo "fence check failed ($status); will check again" >&2
		fi
	done
fi
