#!/usr/bin/env bash
# Writes the files that the factory Grimoire restore reads
# (projects/grimoire/deploy/README.md) to WORK:
#   grimoire.dump     a custom-format pg_dump of the schemas in GRIMOIRE_SCHEMAS
#   vector.metadata   the pgvector version and schema, which restore.sh compares
#   references.csv    REFERENCES_SQL: the object locations that rows reference
#   source.inventory  INVENTORY_SQL over the same schemas
# One psql session exports a snapshot and holds it open while pg_dump, the
# references and the inventory read it, so all three hold the same rows. libpq
# reads the source from DATABASE_URL.
set -euo pipefail
# The uploader container reads these files through the pod's fsGroup.
umask 027
: "${DATABASE_URL:?}" "${GRIMOIRE_SCHEMAS:?}" "${WORK:?}" "${INVENTORY_SQL:?}" "${REFERENCES_SQL:?}"

if [[ ${1:-} == in-snapshot ]]; then
	: "${GRIMOIRE_SNAPSHOT:?}"
	IFS=, read -ra names <<<"$GRIMOIRE_SCHEMAS"
	schemas=()
	for name in "${names[@]}"; do
		schemas+=("--schema=$name")
	done
	# --strict-names: each schema must exist.
	pg_dump --format=custom --snapshot="$GRIMOIRE_SNAPSHOT" --strict-names \
		"${schemas[@]}" --file="$WORK/grimoire.dump" --dbname="$DATABASE_URL"
	psql -Xq -v ON_ERROR_STOP=1 -v snapshot="$GRIMOIRE_SNAPSHOT" \
		-f "$REFERENCES_SQL" -d "$DATABASE_URL" >"$WORK/references.partial"
	mv "$WORK/references.partial" "$WORK/references.csv"
	psql -XqAt -v ON_ERROR_STOP=1 -v snapshot="$GRIMOIRE_SNAPSHOT" \
		-v schemas="$GRIMOIRE_SCHEMAS" -f "$INVENTORY_SQL" \
		-d "$DATABASE_URL" >"$WORK/inventory.partial"
	# psql ignores the exit status of \!, so this file, the last one, is the
	# success signal.
	mv "$WORK/inventory.partial" "$WORK/source.inventory"
	exit
fi

rm -f "$WORK/grimoire.dump" "$WORK/vector.metadata" "$WORK/references.csv" "$WORK/source.inventory"
psql -XAtq -v ON_ERROR_STOP=1 -d "$DATABASE_URL" -c "SELECT extversion, nspname FROM pg_extension JOIN pg_namespace ON pg_namespace.oid = extnamespace WHERE extname = 'vector'" >"$WORK/vector.metadata"
test -s "$WORK/vector.metadata"
GRIMOIRE_DUMP_SCRIPT=$(realpath "${BASH_SOURCE[0]}")
export GRIMOIRE_DUMP_SCRIPT
psql -XAtq -v ON_ERROR_STOP=1 -d "$DATABASE_URL" <<'SQL'
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
SELECT pg_export_snapshot() AS snapshot \gset
\setenv GRIMOIRE_SNAPSHOT :snapshot
\! bash "$GRIMOIRE_DUMP_SCRIPT" in-snapshot </dev/null
COMMIT;
SQL
if [[ ! -s $WORK/source.inventory ]]; then
	echo "the dump or the inventory failed; see the messages above" >&2
	exit 1
fi
echo "dumped schemas $GRIMOIRE_SCHEMAS: $(wc -c <"$WORK/grimoire.dump") bytes"
