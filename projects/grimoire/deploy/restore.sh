#!/usr/bin/env bash
set -euo pipefail
umask 077
: "${PRIVATE_DUMP:?}" "${VECTOR_METADATA:?}"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
psql -XAtq -v ON_ERROR_STOP=1 -c "SELECT extversion, nspname FROM pg_extension JOIN pg_namespace ON pg_namespace.oid = extnamespace WHERE extname = 'vector'" >"$tmp/vector"
test -s "$tmp/vector"
cmp "$VECTOR_METADATA" "$tmp/vector"
test "$(psql -XAtq -c 'SELECT current_user')" = grimoire_restore
pg_restore --list "$PRIVATE_DUMP" >"$tmp/archive"
awk '!($1 ~ /^[0-9]+;$/ && (($4 == "EXTENSION" && $5 == "-" && $6 == "vector") || ($4 == "COMMENT" && $5 == "-" && $6 == "EXTENSION" && $7 == "vector")))' "$tmp/archive" >"$tmp/restore"
target=${PGDATABASE:-grimoire}
if [[ -n ${PGSERVICE:-} ]]; then
	target="service=$PGSERVICE"
fi
pg_restore --use-list="$tmp/restore" --exit-on-error --single-transaction \
	--no-owner --no-acl --dbname="$target" "$PRIVATE_DUMP"
