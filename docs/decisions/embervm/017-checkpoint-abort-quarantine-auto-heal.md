# ADR 017: Bounded auto-heal of the checkpoint-abort quarantine via a durable checkpoint-dispatch record

**Author:** Joe McGinley
**Status:** Accepted
**Created:** 2026-07-22
**Refines:** [011-distribution-longhorn-fencing-cp-rollouts](011-distribution-longhorn-fencing-cp-rollouts.md), [014-worker-authoritative-state-hot-path-consistency](014-worker-authoritative-state-hot-path-consistency.md), [008-interruptible-bank-stateful-datastores](008-interruptible-bank-stateful-datastores.md)

---

## Context

The abort-generation-blessing fix (`aa3df2cd5`,
`docs/plans/2026-07-22-embervm-abort-generation-blessing.md`) made the control
plane the sole issuer of a volume generation on the interruptible-bank ABORT
lane: the CP blesses `next_blessed_generation` before dispatch and noded
`RecordBlessed`s it, so a normal checkpoint-abort no longer quarantines. That
closed the common cause of the `demo-postgres` outage.

It deliberately left one lane self-bumping: `autoAbortCheckpoint`
(`projects/embervm/noded/server/stateful.go`), noded's resolve-timeout backstop.
When the control plane does not resolve an in-flight checkpoint within
`statefulResolveTimeout`, noded aborts the checkpoint itself to avoid pinning a
paused VM. No control plane is reachable at that instant to issue a generation,
so it calls `recordAbortGeneration(workload, 0)`, which `BumpGeneration`s the
volume (advances `genFile`, leaves the blessed marker behind). The node then
reports `generation_blessed:false` one past the blessed watermark, and
`StatefulStore.update_quarantine/4`
(`not blessed_on_wire and reported_gen > blessed_gen`) quarantines the volume.
Recovery is a manual break-glass re-bless
(`docs/runbooks/embervm-stateful-generation-quarantine.md`, Cause 2).

ADR 011 recorded this as accepted fail-closed behaviour and explicitly deferred
auto-heal. The reason for the deferral is real, not laziness: the
checkpoint-in-flight state (noded's `markCheckpointed` / `claimResolve`; the
CP's outstanding resolve worker) is in-memory. The auto-abort fires precisely
when the control plane was too slow or rolled, which is exactly the moment a
restarted control plane has forgotten it ever dispatched that checkpoint. A
control plane that cannot remember its own outstanding checkpoint cannot tell
its own auto-aborted `+1` from a rogue second writer's `+1`, so it must fail
closed on both.

The residual is now narrow enough that a fully manual runbook for it is a smell:
the surviving trigger has a positive benign signature. `autoAbortCheckpoint`
always resumes the SAME paused VM (never boots a second writer) and always
advances by exactly `+1` (`BumpGeneration`). Genuine split-brain, by contrast,
is a SECOND writer, which is a different `vm_id` (a fresh boot mints a new one)
or a jump past `+1`. The ambiguity that justified fail-closed for this specific
cause can be removed if the control plane can positively prove the `+1` was its
own checkpoint, rather than guessing from the signature alone.

## Decision

The control plane auto-heals the checkpoint-abort quarantine for the provably
self-inflicted subset only, and stays fail-closed for everything else. Two
changes make the benign case self-identifying and durable across a control-plane
restart.

### A durable checkpoint-dispatch record

When noded confirms a VM is paused for an interruptible bank (the
`{:checkpoint_done}` report, where `Embervm.StatefulSweeper.finish_checkpoint`
stamps `checkpoint_generation` G and `vm_id` V into the store), the control
plane appends a durable op-log record `checkpoint_dispatched{workload, vm_id: V,
generation: G}`. It is cleared when the control plane itself drives the resolve
(COMMIT or ABORT, in `apply_resolve`) or when the auto-heal below consumes it.
Op-log replay reconstructs the set of unresolved dispatch records on control-plane
boot, so a recovered control plane recognizes a checkpoint it dispatched before
it crashed or rolled. This is the piece the ADR 011 deferral was missing: the
benign signal is made durable at the sole issuer before the state that produces
it can be lost.

A narrow window remains fail-closed by construction: if the control plane dies
between receiving the checkpoint-paused report and appending the dispatch record,
no record exists, noded's later auto-abort quarantines, and the manual break-glass
applies. This is a genuine gap in the control plane's knowledge (it never durably
witnessed the checkpoint), so refusing to auto-heal it is correct, not a
regression.

### Auto-heal only on a matching record, quarantine otherwise

`StatefulStore.update_quarantine/4` gains a pre-quarantine branch. When a report
would quarantine (`not blessed_on_wire and reported_gen > blessed_gen`), it first
checks for an unresolved dispatch record for the same workload whose `vm_id`
equals the reporting instance's `vm_id` and whose `generation` is exactly
`reported_gen - 1`. If it matches, the control plane blesses `reported_gen` (the
same `bless_generation/3` write-through path a CP-driven abort uses) instead of
quarantining, and consumes the record. If it does not match (a different
`vm_id`, a jump past `+1`, or no record at all), the volume is quarantined
exactly as today.

The safety argument is that the match conditions are precisely the fingerprint
`autoAbortCheckpoint` leaves and nothing else can forge cheaply: the same
`vm_id` still serving means the same process image resumed (a checkpoint-resume,
not a new writer), the exact `+1` matches `BumpGeneration`, and the durable
record proves the control plane initiated that checkpoint on that `vm_id`. A
second writer breaks at least one condition (new `vm_id`, or an unrecorded
generation), so it stays quarantined. Auto-heal narrows the fence's teeth to the
provably benign; it does not blunt them.

### The runbook keeps the break-glass for the ambiguous remainder

The manual re-bless is not deleted. It remains the recovery for the residual
fail-closed cases the auto-heal deliberately does not cover (the crash-window
above; any future self-bump lane without a durable record). The runbook is
updated to say the common `autoAbortCheckpoint` case now self-heals and the
manual step is only for a quarantine that persists past the next node report.

## Consequences

What becomes possible:

- The last routine manual step in the stateful fence disappears. A
  resolve-timeout auto-abort, previously a `demo-postgres`-style manual recovery,
  now clears itself on the next node status report with an audit trail (the
  dispatch record plus the auto-issued blessing).
- The fence stops looking like a generic outage for its one benign cause, which
  was the actual smell: an operator paged for a `503` that was always going to be
  a rubber-stamp re-bless.

What is given up / must be maintained:

- A new op kind (`checkpoint_dispatched`, plus its clear) enters the op-log.
  It is short-lived (created at checkpoint dispatch, cleared at resolve or heal)
  and must be accounted for by op-log retention and compaction
  ([ADR 002](002-op-log-retention-and-compaction.md)); an unresolved record is
  the only kind that must survive compaction until its resolve.
- The auto-heal is one more place the control plane issues a blessing without a
  human in the loop. It is constrained to `+1` on a recorded `vm_id`, so it can
  never advance the watermark past a generation the control plane did not itself
  set in motion.

What stays true:

- Fail closed on enforcement, fail open on warmth (ADR 011). The default is still
  quarantine; auto-heal is a narrow, positively-proven exception, and every case
  it does not prove stays quarantined.
- Single-writer is still enforced by Longhorn attach exclusivity and generation
  blessing (ADR 011); this ADR changes only how a self-inflicted quarantine is
  cleared, never who may write.
- The hit/miss invariant (ADR 011): dispatch-record and blessing writes are
  lifecycle actions on the sweep / resolve path, never on the request hot path.

## Alternatives considered

- **Signature-only, no durable record.** Auto-bless when `reported_gen ==
  blessed_gen + 1` and the workload has one live instance and that `vm_id` is
  serving. Rejected: after the control-plane restart that caused the auto-abort,
  "same `vm_id` still serving" is the only surviving signal, so a rogue `+1` on
  the live `vm_id` would pass the same gate. It cannot fail closed correctly in
  the exact scenario the fence exists for.
- **Detect and alert, stay manual.** Classify the benign signature and downgrade
  it to an info-level alert carrying the one-command remediation, leaving the
  human as the discriminator. Rejected as the primary design because it keeps the
  manual step this ADR set out to remove; retained in spirit as the runbook
  break-glass for the ambiguous remainder.
- **Make the checkpoint non-abortable without the control plane** (drop noded's
  resolve-timeout auto-abort). Rejected: the auto-abort exists so a dead control
  plane cannot pin a paused VM burning a cap slot; removing it trades a rare
  self-healing quarantine for a hard resource leak under control-plane outage.
