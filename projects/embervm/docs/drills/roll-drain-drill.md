# Roll-drain drill (R6 Continuity)

How to roll `embervm-noded` with live workloads and confirm the R6 bounded-preemption
drain evacuates state instead of losing it. This is the manual runbook behind closure
gates 2 to 5 (see the R6 spec+plan). Run it in a stable no-deploy window: a mid-drill
ArgoCD sync that restarts the pod again invalidates the observation.

## What the drain does

On SIGTERM `noded` publishes `drain_deadline_unix_ms` on its NodeStatus and holds the
gRPC surface up (serving lifecycle rpcs) until every managed session/serving/stateful/
group VM has left its registry or the 110s deadline passes. The control plane's
`DrainCoordinator`, watching the drain rising edge on the WatchNode stream, force-banks
each class: stateful with COMMIT-despite-parked, groups as whole bundle sets, sessions
and serving via their bank verbs. A `node_drain_started` / `node_drain_finished` op
pair records the edge and the per-class counts.

The invariant under test: a routine noded roll never cold-boots a stateful workload and
never destroys a banked group. Connections are NOT preserved (spot semantics): a parked
caller re-wakes against the new noded.

## Preconditions

- `kubectl` context on the cluster, access to the `embervm` namespace.
- At least one live stateful workload (scratch-postgres). Confirm it is `:serving`
  / `:running`, not banked:
  `kubectl get workloads -n embervm` and the control-plane op-log.

## Gate 2: stateful roll, zero data loss

1. Write a sentinel row so loss is detectable. Port-forward to scratch-postgres and:
   `psql -c "create table if not exists drill(id int); insert into drill values (42);"`
2. Note the current volume generation from the control plane's stateful projection.
3. Roll noded: `kubectl rollout restart deploy/embervm-noded -n embervm`.
4. Watch the op-log for `node_drain_started` then `stateful_banked` for scratch-postgres
   BEFORE the pod terminates, then `node_drain_finished` with `stateful: 1`.
5. After the new noded is Ready, trigger a wake (any invoke) and confirm the workload
   RELIT (op-log `stateful_relit` at the matched generation, not `stateful_cold_booted`).
6. Confirm the sentinel survives: `psql -c "select * from drill;"` returns `42`.

Record: the drain span, the op-log excerpt, and the psql output.

## Gate 3: full-node drain wall time under 120s

1. Have every live class live at once (stateful + session + serving).
2. Roll noded and measure the `ember.node_drain` span duration (SigNoz), or the wall
   time between `node_drain_started` and the last per-class bank op.
3. Confirm it is under the 110s deadline with margin.

## Gate 5: parked wake during drain

1. Open a long-lived connection to scratch-postgres (a parked caller).
2. Roll noded. The drain force-COMMITs despite the parked connection.
3. Confirm the caller sees only a retryable error (connection reset), and its next
   attempt relights the banked bundle against the new noded with no data loss.

## If a bank does not complete in the window

That is the spot contract, not a failure: the workload's durable state is its volume (or
prior banked bundle), and the daemon reaps the straggler at the deadline. Confirm the
next wake cold-boots from the volume (no data loss) rather than relighting. A workload
that repeatedly cannot bank in 120s is a sizing signal (bump the deadline or the bank
concurrency), tracked separately.
