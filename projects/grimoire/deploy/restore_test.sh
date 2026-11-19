#!/usr/bin/env bash
set -euo pipefail

restore=$1
fixture=$(mktemp -d "${TMPDIR:-/tmp}/grimoire-restore.XXXXXXXX")
trap 'rm -rf "$fixture"' EXIT
export VECTOR_METADATA="$fixture/vector.metadata"
export PRIVATE_DUMP="$fixture/rehearsal.dump"
export RESTORE_LIST="$fixture/selected.list"
export PATH="$fixture:$PATH"

# Stand in for the two PostgreSQL clients. Capture the list consumed by the
# restore invocation while the script's temporary directory still exists.
cat >"$fixture/psql" <<'PSQL'
#!/usr/bin/env bash
case "${!#}" in
  *extversion*) echo '0.8.2|public' ;;
  'SELECT current_user') echo grimoire_restore ;;
  *) exit 1 ;;
esac
PSQL
cat >"$fixture/pg_restore" <<'RESTORE'
#!/usr/bin/env bash
set -eu
case "$1" in
  --list) cat "$2" ;;
  --use-list=*) cp "${1#--use-list=}" "$RESTORE_LIST" ;;
  *) exit 1 ;;
esac
RESTORE
chmod +x "$fixture/psql" "$fixture/pg_restore"
echo '0.8.2|public' >"$VECTOR_METADATA"
cat >"$PRIVATE_DUMP" <<'TOC'
; Selected TOC Entries:
2; 3079 16385 EXTENSION - vector
3390; 0 0 COMMENT - EXTENSION vector
3; 3079 16400 EXTENSION - vector_tools
3391; 0 0 COMMENT - EXTENSION vector_tools
215; 1259 16403 TABLE public vector grimoire_restore
3389; 0 0 COMMENT public TABLE vector grimoire_restore
3250; 1259 16500 INDEX public chunks_embedding_idx grimoire_restore
TOC
cat >"$fixture/expected" <<'TOC'
; Selected TOC Entries:
3; 3079 16400 EXTENSION - vector_tools
3391; 0 0 COMMENT - EXTENSION vector_tools
215; 1259 16403 TABLE public vector grimoire_restore
3389; 0 0 COMMENT public TABLE vector grimoire_restore
3250; 1259 16500 INDEX public chunks_embedding_idx grimoire_restore
TOC
bash "$restore"
diff -u "$fixture/expected" "$RESTORE_LIST"

rm "$RESTORE_LIST"
echo '0.7.0|public' >"$VECTOR_METADATA"
if bash "$restore"; then
	echo 'restore accepted mismatched extension metadata' >&2
	exit 1
fi
test ! -e "$RESTORE_LIST"
