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
cat >"$work/bin/snapshot" <<'STUB'
#!/usr/bin/env bash
mkdir -p "$1/stars" && echo '{"asOf":"x"}' >"$1/stars/latest.json"
echo "snapshot $1" >>"$CALLS"
STUB
cat >"$work/bin/wrangler" <<'STUB'
#!/usr/bin/env bash
echo "wrangler $*" >>"$CALLS"
test -f "$(dirname "$3")/web/dist/stars/latest.json" || test -f "$(dirname "$3")/web/dist/data/stars/latest.json"
STUB
chmod +x "$work/bin/"*

run() {
	CALLS="$work/calls" SITE_SRC="$work/src" SITE_DIR="$work/site" \
		SNAPSHOT_BIN="$work/bin/snapshot" WRANGLER_BIN="$work/bin/wrangler" "$@" bash "$RUN"
}

# 1. Snapshot into the copy, then deploy the copy with the stage's env.
: >"$work/calls"
run env STAGE=prod
expected="snapshot $work/site/web/dist/data
wrangler deploy --config $work/site/wrangler.jsonc --env prod"
[[ "$(cat "$work/calls")" == "$expected" ]] || {
	echo "unexpected calls:"
	cat "$work/calls"
	exit 1
}
[[ -f "$work/site/web/dist/data/stars/latest.json" ]] || { echo "data not in the copy"; exit 1; }
[[ ! -e "$work/src/web/dist/data" ]] || { echo "source was modified"; exit 1; }

# 2. A bad or missing stage stops before any step.
for stage in "" staging; do
	: >"$work/calls"
	if run env STAGE="$stage" 2>/dev/null; then
		echo "STAGE='$stage' should fail"
		exit 1
	fi
	[[ ! -s "$work/calls" ]] || { echo "STAGE='$stage' ran a step"; exit 1; }
done

# 3. A failed snapshot stops before the deploy.
cat >"$work/bin/snapshot" <<'STUB'
#!/usr/bin/env bash
exit 3
STUB
: >"$work/calls"
if run env STAGE=dev; then
	echo "a failed snapshot should fail the run"
	exit 1
fi
! grep -q wrangler "$work/calls" || { echo "deployed after a failed snapshot"; exit 1; }
echo "PASS"
