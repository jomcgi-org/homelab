#!/bin/sh
# Refresh the dev database from production.
#
# This lives in a file, mounted from a ConfigMap, rather than inline in the
# CronWorkflow's container `args`. That is not tidiness, it is correctness.
# kubelet runs its own `$(VAR)` expansion over `command`, `args` and `env`
# values at container start, in which `$$` is the escape for a literal `$`. An
# inline PL/pgSQL block therefore reached the shell as `DO $` and failed with a
# syntax error, while the whole wipe was skipped:
#
#   ERROR:  syntax error at or near "$"
#   LINE 1: DO $
#
# `helm template` plus `sh -n` could not catch that, because the rendered
# manifest still contains `$$`; the mangling happens later, inside kubelet.
# Mounted file contents are never expanded, so what is in git is byte for byte
# what the shell executes, and static checks on this file are meaningful. It
# also retires the rest of the class: any future `$(WORD)` in an inline script
# where WORD matches a defined env var would be silently substituted.
#
# Every phase asserts what it did. Four consecutive nightly failures were each a
# different cause with one shape in common: a step reported success without
# having done anything. Exit codes lie here (psql exits 0 on failed statements
# unless ON_ERROR_STOP is set) and so do rendered manifests, so the job trusts
# neither and reads the database back instead.
#
# Deliberately no `set -x`: it would print PGPASSWORD into the Workflow logs.
set -eu

dump_file=/tmp/monolith.dump
list_file=/tmp/monolith.list

case "$DEV_PGHOST" in
*monolith-dev*) ;;
*)
	echo "refusing non-dev restore target: $DEV_PGHOST" >&2
	exit 1
	;;
esac

# --no-psqlrc so no operator's ~/.psqlrc can alter behaviour, ON_ERROR_STOP so a
# failed statement fails the script. Without it psql exits 0 having errored,
# which is exactly how the wipe failed silently and the restore then ran against
# a populated database and produced 664 "already exists" errors.
dev_psql() {
	psql --host "$DEV_PGHOST" --port 5432 --username app --dbname monolith \
		--no-psqlrc --set ON_ERROR_STOP=1 "$@"
}

# The barrier: dev must hold no actionable queue state, or the chat leader
# re-posts production's Discord and WhatsApp messages and DBOS resumes
# production's in-flight agent runs.
#
# Two separate calls on purpose. A single --command 'A; B' without
# ON_ERROR_STOP lets a failed A silently skip B.
barrier() {
	dev_psql --command 'DROP SCHEMA IF EXISTS dbos CASCADE;'
	dev_psql --command 'TRUNCATE TABLE chat.discord_outbox, chat.whatsapp_outbox;'
}

# ---------------------------------------------------------------- dump
export PGPASSWORD=$DUMP_PASSWORD
# The PRIMARY, not the -ro replica. A standby cancels a query whose snapshot
# blocks WAL replay, and the COPY of knowledge.chunks is long enough to hit that
# every single night ("canceling statement due to conflict with recovery").
pg_dump \
	--host "$PROD_PGHOST" --port 5432 \
	--username "$DUMP_ROLE" --dbname monolith \
	--format custom --file "$dump_file"

prod_chunks=$(psql --host "$PROD_PGHOST" --port 5432 \
	--username "$DUMP_ROLE" --dbname monolith \
	--no-psqlrc --set ON_ERROR_STOP=1 \
	-tAc 'SELECT count(*) FROM knowledge.chunks')

# ---------------------------------------------------------------- restore list
# Never restore the dangerous data in the first place, so dev holds no
# actionable queue state at ANY instant rather than only after the barrier runs.
# activeDeadlineSeconds SIGKILLs the pod, and an untrapped fatal signal does not
# run an EXIT trap, so a deadline-killed restore would otherwise leave
# production's outbox rows live in dev with only MONOLITH_LEADER_SINGLETONS
# standing between them and being posted a second time.
#
#   EXTENSION          app owns neither vector nor uuid-ossp, so DROP/COMMENT on
#                      them fail the ownership check and pg_restore exits 1
#   SCHEMA - public    public is app-owned here, so a CREATE SCHEMA public entry
#                      would fail "already exists" once --clean is gone. Not
#                      observed in this archive, kept because it is harmless and
#                      the failure it prevents is another whole round
#   dbos               DBOS recreates its own schema on launch
#   TABLE DATA chat …  the outbox TABLES are still created, just left empty
#
# Verified before adopting this: no foreign key from outside dbos points into
# dbos or into either outbox table, so excluding them cannot break the restore
# of a referencing table.
pg_restore --list "$dump_file" |
	grep -Ev ' EXTENSION | SCHEMA - public | dbos | TABLE DATA chat (discord_outbox|whatsapp_outbox) ' \
		>"$list_file"

# An empty list is the dangerous outcome, not an error one. There is no pipefail
# here, so a failed --list leaves grep succeeding on no input and --use-list
# would then restore NOTHING while exiting 0: dev silently keeps yesterday's
# data and every signal stays green.
if [ ! -s "$list_file" ]; then
	echo "refusing to restore: empty restore list from $dump_file" >&2
	exit 1
fi

# ---------------------------------------------------------------- wipe
export PGPASSWORD=$DEV_PASSWORD
trap barrier EXIT

# The dev app holds locks on these tables. Without this the wipe blocks until
# the hour deadline SIGKILLs the pod, which reads as a hang rather than a
# conflict. usename filter because terminating another role's backend raises an
# error without pg_signal_backend.
dev_psql --command "SELECT pg_terminate_backend(pid) FROM pg_stat_activity \
	WHERE datname = current_database() AND pid <> pg_backend_pid() AND usename = 'app';"

# \gexec rather than PL/pgSQL: no dollar quoting anywhere for anything to
# mangle, one connection, and ON_ERROR_STOP applies to every generated
# statement. lock_timeout so a re-grabbed lock is a loud failure in a minute
# rather than an hour-long hang.
#
# public is excluded from the schema sweep and its objects dropped individually.
# It is owned by app too, so a plain DROP OWNED BY app CASCADE would take the
# schema with it and cascade into vector and uuid-ossp, which live there and are
# owned by postgres. app cannot recreate either (vector is not a trusted
# extension, and cnpg-cluster.yaml installs them only at cluster bootstrap), so
# that would break the refresh permanently and need a cluster rebuild. The
# deptype = 'e' exclusions are what keep both extensions alive.
dev_psql <<'SQL'
SET lock_timeout = '60s';

SELECT format('DROP SCHEMA %I CASCADE', nspname)
FROM pg_namespace
WHERE nspname NOT LIKE 'pg\_%'
  AND nspname NOT IN ('information_schema', 'public')
\gexec

SELECT format('DROP %s IF EXISTS public.%I CASCADE',
       CASE c.relkind WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW'
                      WHEN 'S' THEN 'SEQUENCE' ELSE 'TABLE' END, c.relname)
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r','p','v','m','S')
  AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = c.oid AND d.deptype = 'e')
\gexec

SELECT format('DROP ROUTINE IF EXISTS %s CASCADE', p.oid::regprocedure)
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'public'
  AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e')
\gexec
SQL

# ASSERT EMPTY. The load-bearing check: it makes an incomplete wipe loud, and
# loud BEFORE the restore rather than as hundreds of "already exists" errors
# after it. Round four failed precisely because nothing stood here.
leftover=$(
	dev_psql -tA <<'SQL'
SELECT 'schema ' || nspname FROM pg_namespace
WHERE nspname NOT LIKE 'pg\_%' AND nspname NOT IN ('information_schema', 'public')
UNION ALL
SELECT 'relation public.' || c.relname
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r','p','v','m','S')
  AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = c.oid AND d.deptype = 'e')
SQL
)
if [ -n "$leftover" ]; then
	echo "wipe incomplete, refusing to restore onto a populated database:" >&2
	echo "$leftover" >&2
	exit 1
fi

# ---------------------------------------------------------------- restore
# With an asserted-empty target and no --clean, zero errors are expected, so a
# non-zero exit is now trustworthy rather than routine noise.
pg_restore \
	--host "$DEV_PGHOST" --port 5432 \
	--username app --dbname monolith \
	--use-list "$list_file" \
	--no-owner --no-acl "$dump_file"

# ---------------------------------------------------------------- verify
# Explicitly, in the main flow, not only from the trap. The trap fires at exit,
# which is AFTER the verification below, so a trap-only barrier would be
# verified before it had run. The trap stays as the backstop for an early exit
# and simply repeats this idempotently.
barrier

dbos_present=$(dev_psql -tAc "SELECT count(*) FROM information_schema.schemata WHERE schema_name = 'dbos'")
discord_rows=$(dev_psql -tAc 'SELECT count(*) FROM chat.discord_outbox')
whatsapp_rows=$(dev_psql -tAc 'SELECT count(*) FROM chat.whatsapp_outbox')
if [ "$dbos_present" != "0" ] || [ "$discord_rows" != "0" ] || [ "$whatsapp_rows" != "0" ]; then
	echo "safety barrier did not hold: dbos=$dbos_present discord=$discord_rows whatsapp=$whatsapp_rows" >&2
	exit 1
fi

# Compare the schemas the archive carries against the schemas dev now has,
# rather than asserting a hardcoded count that would rot as prod grows. dbos is
# absent from both sides, since it is filtered out of the list above.
# Keyed on the "SCHEMA -" marker rather than a field offset from the end: the
# owner is the last field today, so $(NF-1) would work, but it silently picks
# the wrong token on any entry that omits it.
grep ' SCHEMA - ' "$list_file" |
	awk '{ for (i = 1; i <= NF; i++) if ($i == "SCHEMA" && $(i + 1) == "-") print $(i + 2) }' |
	sort >/tmp/expected_schemas
dev_psql -tAc "SELECT nspname FROM pg_namespace \
	WHERE nspname NOT LIKE 'pg\_%' AND nspname NOT IN ('information_schema', 'public') \
	ORDER BY nspname" >/tmp/actual_schemas
diff -u /tmp/expected_schemas /tmp/actual_schemas

dev_chunks=$(dev_psql -tAc 'SELECT count(*) FROM knowledge.chunks')
if [ "$dev_chunks" -lt $((prod_chunks * 9 / 10)) ]; then
	echo "restore looks short: dev knowledge.chunks=$dev_chunks vs prod=$prod_chunks" >&2
	exit 1
fi

echo "refresh OK: chunks dev=$dev_chunks prod=$prod_chunks, schemas match archive, barrier verified"
