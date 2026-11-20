#!/usr/bin/env bash
# Recover one legacy creation payload from the original tuple in a physical
# point-in-time restore. Obtain the INSERT transaction ID independently from
# archived WAL. Never use the current tuple's xmin as the source of that ID.
# Configure libpq service entries for the restore and the live itsaplan DB.
set -euo pipefail
if [[ $# != 4 ]]; then
	echo "usage: $0 RESTORED_SERVICE LIVE_SERVICE ISSUE_ID WAL_INSERT_XID" >&2
	exit 2
fi
# The two queries exchange identifiers and digests only. Missing evidence or
# a non-unique creation event aborts before updating the live database.
proof=$(
	PGSERVICE="$1" psql -XqAt -F '|' -v ON_ERROR_STOP=1 -v id="$3" -v xid="$4" <<'SQL'
SELECT system_identifier, current_database(), project_id, sequence_number,
       extract(epoch FROM created_at), encode(sha256(convert_to(title, 'UTF8')), 'hex'),
       encode(sha256(convert_to(description, 'UTF8')), 'hex')
FROM issue CROSS JOIN pg_control_system()
WHERE id = :'id'::integer AND issue.xmin = :'xid'::xid;
SQL
)
if [[ -z $proof ]]; then
	echo "The restore does not contain the specified insert version." >&2
	exit 1
fi
IFS='|' read -r cluster database project sequence created title description <<<"$proof"
PGSERVICE="$2" psql -Xq -v ON_ERROR_STOP=1 -v id="$3" -v cluster="$cluster" -v database="$database" \
	-v project="$project" -v seq="$sequence" -v created="$created" -v title="$title" -v description="$description" <<'SQL'
BEGIN;
SELECT a.id AS event FROM issue_activity a JOIN issue i ON i.id = a.issue_id
CROSS JOIN pg_control_system() c
WHERE c.system_identifier = :'cluster'::bigint AND current_database() = :'database'
  AND i.id = :'id'::integer AND i.project_id = :'project'::integer
  AND i.sequence_number = :'seq'::integer AND extract(epoch FROM i.created_at) = :'created'::numeric
  AND a.kind = 'activity' AND a.action = 'created'
FOR UPDATE OF a
\gset
SELECT jsonb_build_object('subject', jsonb_build_object('value','factory-creation-sha256-v1'),
                          'from', jsonb_build_object('value', :'title'::text),
                          'to', jsonb_build_object('value', :'description'::text)) AS snapshot
\gset
UPDATE issue_activity SET payload = :'snapshot'::jsonb
WHERE id = :'event'::integer AND payload = '{}'::jsonb;
SELECT id AS verified FROM issue_activity WHERE id = :'event'::integer AND payload = :'snapshot'::jsonb
\gset
COMMIT;
SQL
