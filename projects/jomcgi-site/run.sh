#!/usr/bin/env bash
# Deploys the site with fresh data (HOME-209). The image holds the built pages
# at /app/site; the root filesystem is read-only, so assemble a copy in /tmp,
# add each section's data from R2 under web/dist/data, then deploy that copy.
# The Sync-hook deploy Job and the refresh CronJob both run this.
# Env: STAGE (dev|prod), R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, CLOUDFLARE_*.
# The *_BIN and SITE_SRC/SITE_DIR overrides exist only for run_test.sh.
set -o errexit -o nounset -o pipefail

SITE_SRC="${SITE_SRC:-/app/site}"
SITE_DIR="${SITE_DIR:-/tmp/site}"
SNAPSHOT_BIN="${SNAPSHOT_BIN:-/app/projects/jomcgi-site/data/snapshot}"
WRANGLER_BIN="${WRANGLER_BIN:-/app/projects/jomcgi-site/wrangler/wrangler}"

case "${STAGE:-}" in
dev | prod) ;;
*)
	echo "STAGE must be dev or prod, not '${STAGE:-}'" >&2
	exit 2
	;;
esac

rm -rf "$SITE_DIR"
cp -R "$SITE_SRC" "$SITE_DIR"
"$SNAPSHOT_BIN" "$SITE_DIR/web/dist/data"
exec "$WRANGLER_BIN" deploy --config "$SITE_DIR/wrangler.jsonc" --env "$STAGE"
