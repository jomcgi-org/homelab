#!/usr/bin/env bash
# Run with hub uploads quiesced and the private originals volume mounted.
set -euo pipefail
umask 077
: "${HUB_PGSERVICE:?}" "${FACTORY_PGSERVICE:?}" "${HUB_ORIGINALS_REMOTE:?}" "${TRIPS_ORIGINALS:?}"
[[ "$HUB_PGSERVICE" != "$FACTORY_PGSERVICE" ]]
[[ -d "$TRIPS_ORIGINALS" ]]
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
root=$(cd "$(dirname "$0")" && pwd)

# The first restore is atomic. Subsequent invocations recheck the committed
# tables and resume the immutable original transfers after interruptions.
if [[ "$(PGSERVICE="$FACTORY_PGSERVICE" psql -XAtqc "SELECT to_regnamespace('trips') IS NULL")" == t ]]; then
	PGSERVICE="$HUB_PGSERVICE" pg_dump -Fc -n trips -f "$scratch/trips.dump"
	PGSERVICE="$FACTORY_PGSERVICE" pg_restore --dbname="service=$FACTORY_PGSERVICE" --single-transaction --no-acl --no-owner "$scratch/trips.dump"
fi
for table in trips points; do
	order=slug
	[[ "$table" == points ]] && order=trip_slug,id
	for service in "$HUB_PGSERVICE" "$FACTORY_PGSERVICE"; do
		PGSERVICE="$service" PGOPTIONS='-c timezone=+00:00' psql -XqAt -v ON_ERROR_STOP=1 \
			-c "COPY (SELECT to_jsonb(t)::text FROM trips.$table t ORDER BY $order) TO STDOUT" | sha256sum >"$scratch/$table.$service.sha"
	done
	cmp "$scratch/$table.$HUB_PGSERVICE.sha" "$scratch/$table.$FACTORY_PGSERVICE.sha"
done
rclone copy "$HUB_ORIGINALS_REMOTE" "$TRIPS_ORIGINALS" --immutable --checksum
rclone check "$HUB_ORIGINALS_REMOTE" "$TRIPS_ORIGINALS" --download
PGSERVICE="$FACTORY_PGSERVICE" psql -Xq -v ON_ERROR_STOP=1 -f "$root/approvals.sql"
