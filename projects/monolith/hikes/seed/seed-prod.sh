#!/usr/bin/env bash
# One-time bootstrap of the hikes.walks corpus in production.
#
# The WalkHighlands snapshot (1,620 walks) used to ship as an Atlas migration,
# but at ~676 KiB it pushed the monolith-migrations ConfigMap past the 256 KiB
# client-side-apply annotation limit and broke ArgoCD sync. The data now lives
# here as plain SQL and is loaded out-of-band by this script instead.
#
# Safe to re-run: every INSERT ends with ON CONFLICT (uuid) DO NOTHING, so a
# second run is a no-op. Run it once after the hikes schema migration has been
# applied (i.e. the hikes.walks table exists); the weekly scrape job keeps the
# corpus fresh thereafter.
#
# Usage:
#   ./seed-prod.sh                 # uses the defaults below
#   NS=monolith PG_POD=monolith-pg-1 ./seed-prod.sh
set -euo pipefail

NS="${NS:-monolith}"
PG_POD="${PG_POD:-monolith-pg-1}"
DB="${DB:-monolith}"
SQL_FILE="$(dirname "$0")/walks_seed.sql"

if [[ ! -f "$SQL_FILE" ]]; then
	echo "seed SQL not found at $SQL_FILE" >&2
	exit 1
fi

echo "Seeding hikes.walks in $NS/$PG_POD (db=$DB) from $(basename "$SQL_FILE")..."
# Pipe the SQL straight into psql on the Postgres primary over a local peer-auth
# superuser connection. ON_ERROR_STOP makes a genuine failure non-silent.
kubectl exec -i -n "$NS" "$PG_POD" -c postgres -- \
	psql -U postgres -d "$DB" -v ON_ERROR_STOP=1 <"$SQL_FILE"

echo "Done. Row count:"
kubectl exec -n "$NS" "$PG_POD" -c postgres -- \
	psql -U postgres -d "$DB" -tAc "SELECT count(*) FROM hikes.walks;"
