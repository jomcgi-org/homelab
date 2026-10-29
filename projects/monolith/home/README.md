# Retire snapshot alerts under a maintenance window

#7030 stages removal of `home.cluster_snapshot.alerts`. The dashboard consumes
health and freshness; the independent agent projection uses
`agent_view.cluster_snapshot`. Vessel and campsite data are outside the drop.

The original migration defines `alerts JSONB NOT NULL` without a default.
Old code selects and inserts `alerts`; new code omits it. New writes fail on
the original schema, and old readers and writers fail after the drop. A rolling
mixed-version deployment is unsafe.

The `AtlasMigration` reads the migrations ConfigMap directly. The chart has no
sync-wave or readiness barrier ordering migration, backend and jobs. Argo
`home-cluster-snapshot-refresh` runs independently of the backend. An unmerged PR
stages repository work only. Once published, the migration is eligible for
Atlas execution; neither this document nor its SQL comments disable it.

## Prerequisites for later authorized application

Keep #7030 open until operational acceptance is recorded. Production work
requires separate authorization and an operator-coordinated maintenance window:

1. Inspect the live schema, alerts values and database dependencies. Agree on
   recovery expectations for the column's contents. `RESTRICT` refuses external
   dependent objects; it does not preserve the dropped column's data or detect
   old application consumers.
2. Before publishing/applying this migration, fence GitOps reconciliation as
   needed through the established operator process. Stop old dashboard readers,
   backend instances and snapshot refresh jobs, including in-flight/manual jobs.
   Confirm no old consumer can restart during the transition. Merely suspending
   the schedule does not stop a running job.
3. With old consumers quiesced, separately authorize and apply the exact
   column-only migration through GitOps/Atlas. Verify health, snapshot_at and
   the singleton row are unchanged, along with `agent_view.cluster_snapshot`
   and all vessel/campsite data. Do not start new writers before the migration.
4. Separately authorize the new backend/job rollout and resume reconciliation
   and refresh scheduling only with the new image. Obtain a verified deployment
   verdict. Observe successful refreshes, dashboard health freshness and
   missing/stale fallback, and the independent agent summary. Check for
   missing-column and not-null errors.

The drop is forward-only and loses alerts values. Code-only rollback is unsafe:
old code requires the column. Restoring compatibility requires a separately
reviewed forward migration recreating its type and constraints before old
consumers resume, plus an authorized backup restore if original values matter.
No live inspection, migration, rollout or recovery rehearsal is claimed here.
