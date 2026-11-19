#!/usr/bin/env bash
# Restores a custom-format Grimoire dump into an empty factory database as the
# owner login, grimoire_restore. Then it checks that every foreign key and
# pgvector index in the dump is valid in the target. It refuses a dump that
# cannot pass these checks before it imports anything, because a check that
# fails after the commit leaves tables that block a retry. libpq reads the
# target from PGSERVICE or PGHOST, PGPORT, PGDATABASE and PGUSER.
set -euo pipefail
umask 077
: "${PRIVATE_DUMP:?}" "${VECTOR_METADATA:?}"
tmp=$(mktemp -d "${TMPDIR:-/tmp}/grimoire-restore.XXXXXXXX")
trap 'rm -rf "$tmp"' EXIT
query() { psql -XAtq -v ON_ERROR_STOP=1 -c "$1"; }

# The operator creates pgvector as a superuser. Its version and schema must
# match the values recorded from the source.
query "SELECT extversion, nspname FROM pg_extension JOIN pg_namespace ON pg_namespace.oid = extnamespace WHERE extname = 'vector'" >"$tmp/vector"
test -s "$tmp/vector"
cmp "$VECTOR_METADATA" "$tmp/vector"
test "$(query 'SELECT current_user')" = grimoire_restore
tables=$(query "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.relkind IN ('r', 'p') AND n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg\_toast%'")
if [[ $tables != 0 ]]; then
	echo "the target has $tables tables; restore only into an empty database" >&2
	exit 1
fi

# The owner cannot replace the operator's extension or its comment.
pg_restore --list "$PRIVATE_DUMP" >"$tmp/archive"
awk '!($1 ~ /^[0-9]+;$/ && (($4 == "EXTENSION" && $5 == "-" && $6 == "vector") || ($4 == "COMMENT" && $5 == "-" && $6 == "EXTENSION" && $7 == "vector")))' "$tmp/archive" >"$tmp/restore"

# Count what the target must have from the dump alone. Each count renders only
# the archive entries of its type, so the foreign key check never reads a
# string in an index predicate. A NOT VALID foreign key stays NOT VALID in the
# target, so it can never match the valid count.
awk '$1 ~ /^[0-9]+;$/ && $4 == "FK" && $5 == "CONSTRAINT"' "$tmp/restore" >"$tmp/keys"
want_keys=$(wc -l <"$tmp/keys")
pg_restore --use-list="$tmp/keys" --file="$tmp/keys.sql" "$PRIVATE_DUMP"
awk '$1 ~ /^[0-9]+;$/ && $4 == "INDEX"' "$tmp/restore" >"$tmp/indexes"
pg_restore --use-list="$tmp/indexes" --file="$tmp/indexes.sql" "$PRIVATE_DUMP"
want_indexes=$(grep -cE '^CREATE (UNIQUE )?INDEX .* USING (hnsw|ivfflat) ' "$tmp/indexes.sql" || true)
not_valid=$(grep -c ' NOT VALID;$' "$tmp/keys.sql" || true)
if [[ $not_valid != 0 ]]; then
	echo "NOT VALID foreign keys in the dump: $not_valid. Validate them in the source, then dump again" >&2
	exit 1
fi

target=${PGDATABASE:-grimoire}
if [[ -n ${PGSERVICE:-} ]]; then
	target="service=$PGSERVICE"
fi
pg_restore --use-list="$tmp/restore" --exit-on-error --single-transaction \
	--no-owner --no-acl --dbname="$target" "$PRIVATE_DUMP"
# Shared catalogs warn that only a superuser can analyze them.
query 'SET client_min_messages = error; ANALYZE'

keys=$(query "SELECT count(*) FROM pg_constraint WHERE contype = 'f' AND convalidated")
indexes=$(query "SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid JOIN pg_am a ON a.oid = c.relam WHERE a.amname IN ('hnsw', 'ivfflat') AND i.indisvalid AND i.indisready")
if [[ $keys != "$want_keys" || $indexes != "$want_indexes" ]]; then
	echo "valid foreign keys $keys of $want_keys, valid pgvector indexes $indexes of $want_indexes" >&2
	exit 1
fi
echo "restored with $keys valid foreign keys and $indexes valid pgvector indexes"
