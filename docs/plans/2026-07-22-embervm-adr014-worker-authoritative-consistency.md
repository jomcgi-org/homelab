# EmberVM ADR 014 Implementation Plan: Worker-Authoritative State and Hot-Path Consistency

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. One comprehensive code review per PR, at the end of that PR (repo policy), not per task. Defer all test execution to CI on the pushed branch.

**Goal:** Implement ADR embervm/014: make node-agent dial-home reports the source of truth for instance runtime state, take durable writes off the boot/wake hot path, make placement reject/retry, pre-provision taps at brick boot, and make destruction node-confirmed.

**Architecture:** Five PRs, ordered so each is independently shippable and gated. PR 1 (node-confirmed destruction) is a pure safety win with no latency dependency. PR 2 (reject/retry placement) and PR 3 (async lifecycle writes) deliver the hot-path latency. PR 4 (tap pre-provisioning) removes netlink work from instance boot. PR 5 is the mandatory TLA+ follow-through (ADR 014 requires every implementation plan to carry this explicitly). Every behavioural change ships behind an off-by-default env gate following the `EMBERVM_WARMTH_RETENTION_SWEEP` pattern, flipped in a later values-only commit once proven.

**Scope note (ADR 015):** ADR 014 decision 6's `isolated_execution` flag is NOT implemented; it was replaced by [ADR 015](../decisions/embervm/015-isolated-high-throughput-lane-data-plane-placement.md), which makes isolation structural to a dedicated Envoy-routed fresh-VM-per-request lane instead of a cross-lane flag. The lane gets its own implementation plan once ADR 015 is reviewed; it builds directly on PRs 1, 2, and 4 here (node-confirmed destroy is its destruction guarantee, node-side cheap rejection is its 503 admission check, tap prealloc is on its per-request boot path).

**Tech Stack:** Elixir control plane (`projects/embervm/control`), Go node agent (`projects/embervm/noded`), protobuf (`projects/embervm/proto/embervm/node/v1/node.proto`), TLA+ specs (`projects/embervm/specs/`), Helm chart + ArgoCD deploy.

---

## Decisions taken by this plan (ADR 014 open questions)

The ADR left five open questions. This plan takes these positions; flag disagreement before execution starts.

| ADR open question | Position in this plan | Rationale |
| ----------------- | --------------------- | --------- |
| Q1: worker authority for serving/xDS relay state | **Answered by ADR 015:** worker authority extends all the way to per-request placement in the isolated lane; existing serving relay reconciliation is untouched by this plan. | The lane's brick-local pool pop is the logical end state of worker authority; the shared serving lane keeps its current posture. |
| Q2: async write queue in-process vs oplog seam | **In-process (spawned writer after Assign success), repaired by adoption reconcile.** | Matches the ADR risk table verbatim: a lost write is resurfaced by the next dial-home report. The oplog group-commit batching (ADR 007) is complementary throughput work, not a prerequisite, and the Postgres adapter is still unwired. |
| Q3: tap pool in node agent vs brick boot scripting | **Node agent** (`noded/serving/net.go`). | All netlink code, the IP allocator, and the del-before-add repair already live there; brick boot scripting has none of it. |
| Q4: `isolated_execution` default | **Moot: flag dropped per ADR 015.** Isolation is chosen by targeting the isolated lane, not by a flag. | An opt-in flag policed reuse transitions in lanes an isolated workload never occupies. |
| Q5: does isolation forbid booting FROM shared warm snapshots | **Capture-only** (carried into ADR 015's security section). Booting from a pre-workload base snapshot is allowed; capturing state from an isolated instance is structurally impossible in the lane. | Base snapshots contain nothing tenant-owned. A future `distrust_shared_base` refinement can come later if a scanning lane demands it. |

## What is already ADR-014-shaped (do not rebuild)

- **Adoption reconcile** already treats node reports as truth for sessions, serving, stateful, and group state (`session_manager.ex do_reconcile/1`, sweepers, `stateful_manager.ex reconcile/1`). PR 1 extends this posture to destruction; it does not introduce it.
- **NodeChannel keying** is already instance-keyed (`node_id`, `pod_uid`) after the alias-misroute fix. No keying work in this plan.
- **Metering** is already synchronous and fail-closed (O(1) ETS reads on submit/dispatch, charge inside the terminal op transaction). Untouched by this plan except a guard test.
- **Node-side rejection plumbing** exists: `Prime`/`StartServing`/`StartStateful`/`StartGroupMember` already return `ResourceExhausted` on the max-live-VMs cap. PR 2 widens the predicate and teaches the dispatcher to retry.

---

## PR 1: Node-confirmed destruction (ADR decision 5)

Branch: `feat/embervm-node-confirmed-destroy`. Gate: `EMBERVM_NODE_CONFIRMED_DESTROY` (default off; off = today's behaviour).

Today every store appends `:*_destroyed` durably FIRST, then tears the VM down asynchronously with no wait. Decision 5 inverts this for destruction only: RPC -> node-side teardown -> node confirmation -> then the durable destroyed record.

### Task 1.1: Proto + node agent: Destroy confirms teardown

**Files:**
- Modify: `projects/embervm/proto/embervm/node/v1/node.proto` (DestroyResponse and the Stop* responses)
- Modify: `projects/embervm/noded/server/server.go` (`Destroy` at ~line 1075, `reap`)
- Test: `projects/embervm/noded/server/server_test.go`

**Steps:**
1. Add `bool teardown_confirmed = N;` to `DestroyResponse` (and the StopServing/StopStateful/StopGroupMember responses used for destroy-class stops). Proto field additions are wire-compatible; old CPs ignore the field, old nodes leave it false, which reads as unconfirmed and keeps the CP in `destroying` until the absence-based fallback (Task 1.3) fires. Regenerate via the existing `node_go_src` genrule (no checked-in stubs).
2. In `Destroy`, only return `teardown_confirmed: true` after `reap()` has completed: microVM process gone, scratch/snapshot bundle for the instance removed, tap released (for tap-bearing classes). Destroy stays idempotent: destroying an unknown vm_id returns confirmed (nothing held).
3. Go tests: destroy of live VM returns confirmed and the VM no longer appears in NodeStatus; destroy of unknown id returns confirmed; a reap failure returns an error, not a false confirm.
4. Commit: `feat(embervm): node-confirmed teardown in Destroy/Stop responses`

### Task 1.2: CP `destroying` state + synchronous destroyed record

**Files:**
- Modify: `projects/embervm/control/lib/embervm/session_store.ex`, `serving_store.ex`, `stateful_store.ex`, `group_store.ex` (FSM: add `destroying`)
- Modify: `projects/embervm/control/lib/embervm/session_manager.ex` (`do_destroy/2`), `serving_manager.ex`, `stateful_manager.ex`, `group_manager.ex`
- Test: corresponding store/manager test files

**Steps:**
1. New op kind `:*_destroying` per store (durable intent record, appended before the RPC so a CP crash mid-destroy resumes the destroy rather than forgetting it). `:*_destroyed` append moves to AFTER the RPC returns `teardown_confirmed`.
2. Behind the gate: `EMBERVM_NODE_CONFIRMED_DESTROY` unset/0 keeps the current order (destroyed-first, async teardown); set: intent op -> RPC in the spawned worker -> on confirmed, append `:*_destroyed` -> ETS terminal. On RPC failure the instance stays `destroying`, retried by the reconcile loop; alarm (log at error + SigNoz-visible counter) if `destroying` persists beyond a threshold (`EMBERVM_DESTROYING_ALARM_MS`, default 5 minutes), per the ADR risk table.
3. Rebuild path: `OpLog` load functions and projection rebuild must understand the new intent ops (a `destroying` row rebuilds as destroying and re-drives the destroy on boot).
4. ExUnit tests: destroy with confirming stub -> both ops in order; destroy with failing stub -> stays destroying, no destroyed op; rebuild from `[created, destroying]` re-issues destroy.
5. Commit: `feat(embervm): destroying state, destroyed recorded only on node confirmation (gated)`

### Task 1.3: Fail-closed reconciliation in both directions

**Files:**
- Modify: `projects/embervm/control/lib/embervm/session_manager.ex`, sweepers, `group_manager.ex` (adoption reconcile)
- Modify: `projects/embervm/control/lib/embervm/node_registry.ex` (owner-resolved dial rule surface)
- Test: manager/sweeper tests

**Steps:**
1. Direction 1 (CP knows, node silent): an instance the CP holds that its owner node's fresh report does not list is terminalized only on an owner-resolved dial (the node's own report, not an age-out), after a grace window (`EMBERVM_ORPHAN_GRACE_MS`, default 60s). This is the existing PR-B0b posture; add the grace window and an oplog record of the triggering report identity on every terminalization.
2. Direction 2 (node reports, CP unaware): an instance a node reports that no CP record matches is an orphan to destroy, not adopt. Route through the node-confirmed destroy path from Task 1.2. Exclusions: primed-pool VMs (Dispatcher inventory, not store-backed) and instances younger than the grace window (async-write race, see PR 3, where the reconciler must adopt-and-backfill instead; the adopt-vs-destroy discriminator is "does a pending async write or task/session record reference this vm_id").
3. Tests: unknown reported instance -> Destroy issued after grace; known instance missing from owner report -> terminalized only when the report is owner-resolved; primed VMs never destroyed by this rule.
4. Commit: `feat(embervm): fail-closed orphan reconciliation toward destruction (gated)`

### Task 1.4: Vocabulary + spec + rollout

**Steps:**
1. Classify the new `:*_destroying` op kinds and `teardown_confirmed` in `specs/vocabulary.exs` (the guard will fail CI otherwise; that is the designed forcing function).
2. Chart bump, format, push, PR, watch CI.
3. Rollout: merge with gate off; flip `EMBERVM_NODE_CONFIRMED_DESTROY=1` in a follow-up values-only commit (with its own chart bump) after one clean day; watch the destroying-persistent alarm.

---

## PR 2: Reject/retry placement (ADR decision 3)

Branch: `feat/embervm-reject-retry-placement`. Gate: `EMBERVM_PLACEMENT_RETRY` (CP side; node-side pressure rejection is safe to ship ungated since ResourceExhausted is already a handled response class).

### Task 2.1: Node-side cheap rejection under real pressure

**Files:**
- Modify: `projects/embervm/noded/server/server.go` (`Prime` ~line 906, and the Start* handlers)
- Test: `projects/embervm/noded/server/server_test.go`, `budget_test.go`

**Steps:**
1. Before claiming resources in `Prime`/`StartServing`/`StartStateful`/`StartGroupMember`, extend the existing cap check with a pressure predicate: `memHeadroom()` below the workload's need plus a floor (`--mem-reject-floor-mib`, default one smallest-workload footprint), or tap/IP allocator exhausted (tap-bearing classes only). Return `codes.ResourceExhausted` with a machine-readable reason in the status message (`pressure:mem`, `pressure:taps`), same shape as the existing max-live-VMs rejection.
2. Rejection must be cheap: predicates read already-maintained counters (cgroup budget, allocator freelist); no disk or netlink work before rejecting. This same predicate later backs the ADR 015 lane's 503 admission check.
3. Go tests: Prime under exhausted headroom rejects without creating VM state; reason string round-trips.
4. Commit: `feat(embervm): reject boot RPCs cheaply under memory/tap pressure`

### Task 2.2: CP dispatcher retry-next-brick

**Files:**
- Modify: `projects/embervm/control/lib/embervm/dispatcher.ex` (MISS-tier worker), `pool_manager.ex` (Prime workers), `placement.ex`
- Test: `dispatcher_test.exs`, `pool_manager_test.exs`, `placement_test.exs`

**Steps:**
1. On `ResourceExhausted` from a boot-class RPC, behind the gate: mark that brick ineligible in the in-memory candidate list for this attempt, decrement its cached headroom view immediately (advisory refresh without waiting for the next report), and retry the next candidate from `BrickLedger.candidates/3`. Bounded: `EMBERVM_PLACEMENT_RETRY_MAX` (default 3, ADR: "a wrong guess costs one extra RPC"). Exhausted retries -> fast explicit failure to the caller (existing `:no_capacity` shape), never a queue wedge.
2. Session/serving/stateful/group create paths route through the same retry helper (single module, e.g. `Embervm.Placement.Retry`), so the policy is one piece of code.
3. ExUnit tests: first brick rejects -> second brick receives the RPC; all reject -> `:no_capacity` after exactly max attempts; gate off -> today's single-attempt behaviour.
4. Commit: `feat(embervm): dispatcher reject/retry placement across candidate bricks (gated)`

### Task 2.3: Vocabulary, bump, rollout

**Steps:**
1. Vocabulary guard: no new op kinds; classify nothing or the reason strings if the guard sees them.
2. Chart bump, push, PR, CI. Flip `EMBERVM_PLACEMENT_RETRY=1` in a values follow-up after soak.

---

## PR 3: Async durable writes off the boot/wake hot path (ADR decision 2)

Branch: `feat/embervm-async-lifecycle-writes`. Gate: `EMBERVM_ASYNC_LIFECYCLE_WRITES` (default off). Depends on PR 1 (the adopt-and-backfill reconciler is the repair path) and PR 2 is recommended first (retry makes the write-lost window smaller in practice).

Scope: the `:assigned`/`:started` appends on the task dispatch path and `:session_created`/`:session_relit` on the session boot/wake path move behind the instance becoming interactive. Explicitly NOT async: `:submitted` (quota audit trail), all metering ops, all destruction ops (PR 1), all bank ops (bank is not a hot path and its snapshot identity must be durable before eviction).

### Task 3.1: Async append worker with ordered per-instance queue

**Files:**
- Create: `projects/embervm/control/lib/embervm/async_writer.ex`
- Test: `projects/embervm/control/test/embervm/async_writer_test.exs`

**Steps:**
1. Failing tests first: ops for one instance apply in submission order; a crash between enqueue and append loses the op (documented, repaired by reconcile); writer drains on graceful shutdown (`terminate/2` flush) so a normal CP roll loses nothing.
2. Implement a small GenServer (or per-scheduler pool if contention shows in CI perf tests, YAGNI until then) that accepts `{op, store_callback}` and appends via the configured `op_log_mod`, then runs the ETS mutation callback. It reuses `OpLog.append/2`; no new oplog behaviour callbacks.
3. Commit: `feat(embervm): AsyncWriter for off-hot-path oplog appends`

### Task 3.2: Rewire dispatch and session-boot ordering behind the gate

**Files:**
- Modify: `projects/embervm/control/lib/embervm/dispatcher.ex`, `task_store.ex`, `session_manager.ex`, `session_store.ex`
- Test: `dispatcher_test.exs`, `task_store_test.exs`, `session_manager_test.exs`

**Steps:**
1. Gate on: warm-tier claim issues Assign immediately; `:assigned`/`:started` ops enqueue to AsyncWriter after the RPC succeeds. Session create/wake: placement + Prime/Relight RPC first, `:session_created`/`:session_relit` enqueued once the node reports the VM up (RPC success). Result/terminal ops stay synchronous (they gate metering charge).
2. Gate off: exact current write-through ordering (assert this with a test that the gate genuinely bypasses AsyncWriter).
3. Adopt-and-backfill: extend PR 1's direction-2 discriminator so an instance surfacing in a node report with no row but a matching in-flight dispatch/create is adopted and its missing ops backfilled (the ADR risk table's "resurfaces and adopts" repair). Test: kill the writer between RPC success and append (stub), run reconcile with a node report carrying the vm, assert backfilled row.
4. ExUnit tests per path; include a metering guard test: quota charge never precedes the terminal op append (fail-closed preserved).
5. Commit: `feat(embervm): boot/wake lifecycle appends off the hot path (gated)`

### Task 3.3: Vocabulary, bump, rollout

**Steps:**
1. No new op kinds expected; classify anything the guard flags.
2. Chart bump, push, PR, CI. Flip `EMBERVM_ASYNC_LIFECYCLE_WRITES=1` only after PR 1's gate has soaked clean, and watch the adoption-backfill counter for a week (a nonzero steady rate is expected and fine; a growing rate means the writer is losing races it should not).

---

## PR 4: Tap pre-provisioning at brick boot (ADR decision 4)

Branch: `feat/embervm-tap-preprovision`. Node-agent-only; gate via noded flag `--tap-prealloc` (default 0 = off).

### Task 4.1: Tap pool in the node agent

**Files:**
- Modify: `projects/embervm/noded/serving/net.go` (`EnsureNetwork` ~line 292, `AllocateTap` ~line 385, `ReleaseTap` ~line 442)
- Test: `projects/embervm/noded/server/serving_test.go` (or a new `net_test.go` beside the code; add the Bazel `go_test` via gazelle)

**Steps:**
1. At `EnsureNetwork`, when `--tap-prealloc N` > 0, pre-create N taps using the existing deterministic `TapNameForIP` naming over the allocator's IP range (del-before-add retained per tap, exactly the #3745 repair), attach to the bridge, leave down. N defaults to the brick's slot ceiling (`slotCeiling()`), which ADR 013 already fixes per size class.
2. `AllocateTap` draws a pre-created tap (bring link up, wire DNAT) instead of creating one; falls back to on-demand creation when the pool is empty (pool exhaustion also feeds PR 2's `pressure:taps` rejection). `ReleaseTap` returns the tap to the pool (link down, DNAT removed) instead of deleting, when prealloc is on.
3. Group bridges/taps stay on-demand (per-group_instance_id identity cannot be pre-provisioned; out of scope, matches ADR "instance creation attaches to pre-built network state" for the fixed-bridge classes).
4. Go tests with netlink faked/skipped as the existing tests do: pool drained then refilled on release; fallback path when exhausted; del-before-add still exercised on repair.
5. Commit: `feat(embervm): pre-provision serving taps at brick boot (flagged)`

### Task 4.2: Wire the flag, bump, rollout

**Steps:**
1. Thread `--tap-prealloc` through noded config + chart values (`values.yaml` noded block) + DS/brick templates; default 0.
2. Chart bump, push, PR, CI. Enable on one brick class first (node-1 canary pattern), watch tap counts (`ip link` via node debug) and the serving-boot latency in SigNoz, then fleet.

---

## PR 5: TLA+ follow-through (mandatory per ADR 014)

Branch: `docs/embervm-adr014-tla-followthrough`. Runs AFTER PR 1 and PR 3 land (ADR 006 rule: specs are not updated while the protocol churns).

### Task 5.1: Re-check `adoption.tla` against worker authority

**Files:**
- Modify: `projects/embervm/specs/adoption.tla`, `projects/embervm/specs/adoption*.cfg`, `projects/embervm/specs/README.md`

**Steps:**
1. Human-pass review (the ADR names this explicitly: the vocabulary guard cannot catch the semantic shift): today the spec models the CP's primed-pool inventory as authoritative with reconcile-on-restart; under ADR 014 the CP view is a reconciled cache at all times. Update the model so node state is the truth variable and CP inventory is derived, and re-run the four TLC configs (`bazel` CI targets `tlc_adoption`, `tlc_adoption_liveness`, `tlc_adoption_wedge`, `tlc_adoption_resurrection`); the two negative configs must still fail for their original reasons.
2. Add the destroying-state transition from PR 1 with the new invariant: **no instance is recorded destroyed before its owning node confirms teardown.**
3. Commit: `docs(embervm): adoption.tla reflects worker-authoritative semantics + destroy invariant`

### Task 5.2: Bank/relight + generation-pairing spec (ADR 006 protocol 2)

**Files:**
- Create: `projects/embervm/specs/bank_relight.tla` + `.cfg` files + BUILD targets (mirror the adoption genrule pattern in `projects/embervm/specs/BUILD`)
- Modify: `projects/embervm/specs/vocabulary.exs` (protocol-2 verbs move from excluded to modeled)

**Steps:**
1. Model against post-014 semantics with the invariants ADR 014 names checkable: destroyed-only-after-node-confirm, a single-use instance (ADR 015 isolated lane) never reaches pool return, relight, or snapshot, no wake resumes a stale snapshot, stored volume generations never regress (fold in the pair-broken monotonic-floor fix from PR #3770).
2. TLC configs: one positive safety, one negative proving the pre-#3770 generation regression is caught.
3. Commit: `docs(embervm): bank/relight generation-pairing TLA spec (protocol 2)`

---

## Sequencing, gates, and verification summary

| PR | Gate | Depends on | Flip criteria |
| -- | ---- | ---------- | ------------- |
| 1 node-confirmed destroy | `EMBERVM_NODE_CONFIRMED_DESTROY` | none | 1 day soak, destroying-alarm silent |
| 2 reject/retry | `EMBERVM_PLACEMENT_RETRY` (CP half) | none (better after 1) | rejection reasons visible in logs, no retry storms |
| 3 async writes | `EMBERVM_ASYNC_LIFECYCLE_WRITES` | 1 (repair path), 2 recommended | adoption-backfill counter flat over a week |
| 4 tap prealloc | `--tap-prealloc` noded flag | none | canary brick class, then fleet |
| 5 TLA follow-through | n/a (docs/specs) | 1 and 3 merged | TLC green in CI, negatives still fail |

After PR 5, the ADR 015 isolated-lane plan picks up: it consumes PR 1's destruction guarantee, PR 2's node-side admission predicate, and PR 4's tap pool, and adds the brick request listener, xDS lane clusters (least-request, retry, outlier), and quota leases.

Per-PR mechanics (every PR): worktree from origin/main; conventional commits; `bazel/tools/format/fast-format.sh` before each commit; `bazel/tools/git/bump-chart.sh projects/embervm` in any PR whose code must deploy; push and watch `gh pr checks --watch`; diagnose via `mcp__buildbuddy__get_invocation` -> `get_target` -> `get_log`, quoting errors verbatim; rebase-merge only; if `BEHIND`, `gh pr update-branch --rebase`. Implementer subagents self-review per task; one comprehensive Opus review per PR at the end. Grep the test tree before changing any default/env value that tests assert on.

Cluster verification after each gate flip: `kubectl get pods -n embervm`, CP logs via `kubectl logs`, SigNoz for boot/wake latency percentiles and the new counters (destroying-persistent, placement-retry, adoption-backfill, tap-pool-depth).
