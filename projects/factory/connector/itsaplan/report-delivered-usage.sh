#!/usr/bin/env bash
# Read both databases; import connector totals only into a temporary table.
set -euo pipefail

: "${FACTORY_CONNECTOR_DATABASE_URL:?Set the connector database connection}"
: "${ITSAPLAN_DATABASE_URL:?Set the itsaplan database connection}"
report_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
psql_bin="${PSQL_BIN:-psql}"
usage_csv="$(mktemp)"
trap 'rm -f -- "$usage_csv"' EXIT

# Finish the export first. A failed export must never yield a partial report.
"$psql_bin" --dbname="$FACTORY_CONNECTOR_DATABASE_URL" -X -q -t --csv \
	--set=ON_ERROR_STOP=1 --file "$report_dir/../store/export_run_usage.sql" >"$usage_csv"

{
	cat "$report_dir/import_run_usage.sql"
	printf '\nCOPY pg_temp.connector_run_usage FROM STDIN WITH (FORMAT csv);\n'
	cat "$usage_csv"
	printf '\\.\n'
	cat "$report_dir/delivered_issue_usage.sql"
} | "$psql_bin" --dbname="$ITSAPLAN_DATABASE_URL" -X -q --csv --set=ON_ERROR_STOP=1
