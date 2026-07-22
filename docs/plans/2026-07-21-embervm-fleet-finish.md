# EmberVM Fleet Finish Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task in the current session. Per repo policy (.claude/CLAUDE.md), do one comprehensive code review per PR at the end of that PR, not per sub-task, and defer ALL test execution to BuildBuddy CI on the pushed branch.

**Goal:** Close out the EmberVM fleet program: fix the noded stateful/group local-eviction misroute (#38), then safely flip the warmth-GC gate to reclaim ~120G (#36), give node-1/2/3 a guaranteed brick each (#35), land the Postgres op-log adapter without flipping it (#18/#27), and resolve the two open investigations (#28, #25).

**Architecture:** Four PRs plus two investigation tasks. PR-1 (#38) is the safety-critical fix and HARD-BLOCKS PR-2 (#36): the warmth reaper deletes the S3 recovery copy of each orphan, and today the local eviction it pairs with silently no-ops, so flipping the gate before PR-1 is deployed would delete the ONLY real copy while the disk copy leaks forever. PR-3 (#35) and PR-4 (#18/#27) are independent and can run in parallel with PR-1.

**Tech Stack:** Go (noded), Elixir (control plane), Helm chart + ArgoCD GitOps, Bazel/apko images, BuildBuddy CI.

---

## The #38 -> #36 blocking relationship (read this first)

`EMBERVM_WARMTH_RETENTION_SWEEP` (the #36 flip) arms `Embervm.WarmthReaper.evict_entry/2`, which for each orphaned stateful bundle or group set fires BOTH a local eviction and a REMOTE (S3) eviction, best-effort and ungated (`warmth_reaper.ex:298-337`, `run_evict/3` at 342).

The local half is broken today:

- `EvictSnapshot` (`noded/server/server.go:1380-1407`) dispatches serving refs via the `servingSnap` inventory, then falls through to `sessionDriver.RemoveSessionBundle(ref)`. There is NO stateful arm and NO group-member arm.
- `evictArtifactLocal` (`noded/server/store.go:507-531`) routes `ARTIFACT_KIND_STATEFUL` into `EvictSnapshot`, so a stateful local evict does `RemoveAll(sessions/<ref>)`: a nonexistent path, success returned, `stateful/<ref>` left on disk. This is the exact misroute class the BASE arm fixed in base-durability PR-3 (see the history in the `evictBaseLocal` docstring, `store.go:533-540`); the stateful and group arms were never added.
- Group member refs are `group/<set_id>/<member_name>` (`noded/fcvm/driver/driver.go:2004`), so the GroupSweeper/reaper per-member `EvictSnapshot` no-ops into `sessions/group/<set>/<member>` the same way.
- The reaper docstring (`warmth_reaper.ex:289-291`) wrongly claims noded routes STATEFUL to `RemoveAll(stateful/<ref>)`. It does not, yet.

Consequence of flipping #36 first: local evict "succeeds" without deleting anything, then the ungated remote evict deletes the S3 copy. Result: the ~120G of orphan warmth never leaves disk AND its recovery copy is destroyed. **PR-2 must not merge until PR-1 is merged AND confirmed rolled out fleet-wide (control plane and every brick's noded at the new image).**

---

## PR sequence overview

| PR | Task | Subject | Depends on | Chart bump |
|----|------|---------|-----------|------------|
| PR-1 | #38 | noded stateful + group-member eviction arms, reaper remote-guard, real-RPC test | none | YES (noded + CP images deploy) |
| PR-2 | #36 | wire + flip `EMBERVM_WARMTH_RETENTION_SWEEP` | PR-1 merged AND deployed | YES (env change deploys) |
| PR-3 | #35 | per-node brick floors for node-1/2/3 | none (host pre-flight gates rollout) | YES |
| PR-4 | #18/#27 | `Embervm.OpLog.Postgres` adapter + DSN selection (not flipped) | none | YES (CP image deploys) |
| Task 5 | #28 | investigate monolith ArgoCD Synced/Degraded | none | only if a fix surfaces |
| Task 6 | #25 | verify evict_orphan_snapshots log spam resolved, close | PR-1 deployed (+ ideally PR-2) | none |

Parallelism: dispatch PR-1, PR-3, and PR-4 implementers concurrently (separate worktrees, separate chart bumps taken race-free by `bazel/tools/git/bump-chart.sh`, which numbers from the origin/main tip; re-bump whichever PR loses a rebase race). PR-2 is strictly serial after PR-1's rollout. Task 5 can run any time; Task 6 runs last.

Every PR follows the repo workflow: worktree off origin/main, Conventional Commits, push, `gh pr checks <n> --watch`, diagnose failures via `mcp__buildbuddy__get_invocation` -> `get_target` -> `get_log` (quote the actual error before hypothesizing), one comprehensive Opus code review per PR, rebase-merge. Never run tests locally.

---

## PR-1 (#38): noded stateful/group local eviction arms + reaper remote-guard

**Model routing:** Opus implements (eviction correctness is subtle and only CI + prod behavior verify it). Escalate to Fable if any eviction-semantics fork appears (e.g. guard ordering, idempotency vs refusal trade-offs). Opus reviews the final diff.

**Files:**
- Modify: `projects/embervm/noded/server/server.go` (EvictSnapshot, ~1380-1407; add two helpers near evictServingSnapshot)
- Modify: `projects/embervm/noded/server/store.go` (evictArtifactLocal ~507-531; optional guard in EvictArtifact ~485)
- Modify: `projects/embervm/noded/server/stateful_registry.go` and `group_registry.go` (add a `get(ref)` lookup if absent; both registries are keyed by snapshotRef already)
- Modify: `projects/embervm/control/lib/embervm/warmth_reaper.ex` (~284-347: gate remote on local success; fix the docstring at 289-291)
- Test: `projects/embervm/noded/server/store_test.go` (or a new `evict_local_test.go` beside it)
- Test: `projects/embervm/control/test/embervm/warmth_reaper_test.exs` (extend existing)
- Chart bump: `bazel/tools/git/bump-chart.sh projects/embervm` in the SAME PR

### Step 1: Write the failing Go integration test (real-RPC, real disk)

The existing faked-channel tests mask the misroute because the fake drivers' Remove ops mutate in-memory maps, not disk. Use the repo's two existing patterns together:

- `newTestServer` (`server_test.go:320-347`) wires the Server behind a REAL in-process gRPC dial (bufconn). Route all calls through the gRPC client, not direct method calls.
- The disk-backed driver stub pattern (`store_test.go:174-200`, `diskScanStatefulDriver`) makes driver ops act on a real temp SnapshotRoot. Extend the stateful and group-member fakes so `RemoveStatefulBundle` / `RemoveGroupMemberBundle` do a real `os.RemoveAll` under the temp root (mirroring `driver.go:1736` and `driver.go:2037`), and so bundle creation writes real `stateful/<ref>/snapfile` and `group/<set>/<member>/snapfile` dirs.

Test cases (each asserts against the REAL temp dir with `os.Stat`):

1. `TestEvictArtifactLocalStatefulRemovesDisk`: seed `stateful/<ref>` on disk, register it (via the ReconcileStatefulFromDisk path or `statefulBundles.add`), call `EvictArtifact{remote: false, artifact: {kind: STATEFUL, workload, ref}}` over bufconn, assert `stateful/<ref>` is GONE from disk and gone from NodeStatus. This test FAILS today: the current code returns success but the dir survives.
2. `TestEvictSnapshotGroupMemberRemovesDisk`: seed a two-member set `group/<set>/<a>` and `group/<set>/<b>`, register both in `groupBundles`, call `EvictSnapshot{snapshot_ref: "group/<set>/a"}` then `.../b` over bufconn, assert both member dirs (and hence the set) leave disk. FAILS today.
3. `TestEvictStatefulInUseRefused`: with a live stateful VM whose `statefulEntry.snapshotRef == ref` registered in `statefulVMs`, the same evict returns `FAILED_PRECONDITION` and the dir SURVIVES.
4. `TestEvictGroupMemberInUseRefused`: same shape against a live `groupMembers` entry relit from the ref.
5. Idempotency: a second evict of an already-gone ref returns success.

### Step 2: Implement the noded arms

In `server.go`, extract the eviction bodies into helpers mirroring `evictServingSnapshot` (1415-1431):

- `evictStatefulSnapshot(ref)`: `statefulDriver == nil` -> `Unimplemented`; scan `s.statefulVMs.snapshot()` for a live VM with `snapshotRef == ref` -> `FailedPrecondition` (the registry comment at `stateful_registry.go:96-102` explicitly notes R4 v1 deferred this guard; this adds it); else `statefulDriver.RemoveStatefulBundle(ref)`, `s.statefulBundles.remove(ref)`, `s.signalChange()`.
- `evictGroupMemberSnapshot(ref)`: look up `s.groupBundles` by ref to recover `(setID, memberName)` (the registry map is keyed by snapshotRef, `group_registry.go:304`); `groupDriver == nil` -> `Unimplemented`; scan `s.groupMembers.snapshot()` for a live member with `snapshotRef == ref` -> `FailedPrecondition`; else `groupDriver.RemoveGroupMemberBundle(setID, memberName)`, `s.groupBundles.remove(ref)`, `s.signalChange()`.

Dispatch order inside `EvictSnapshot` (inventory-first, mirroring the existing serving dispatch comment at 1385-1388): serving -> stateful -> group-member -> session fallback. A ref in no inventory keeps today's idempotent session-path semantics.

In `store.go` `evictArtifactLocal`, split `ARTIFACT_KIND_STATEFUL` out of the SESSION/SERVING case (line 511) into a direct call to `evictStatefulSnapshot(ref.GetRef())` so a typed stateful evict removes the on-disk dir even if the inventory entry is missing (RemoveAll is idempotent). Leave `GROUP_SET` local eviction per-member (`Unimplemented`, the R5 contract at 519-526, unchanged): the per-member refs now actually resolve via the new EvictSnapshot arm.

Optional but recommended (defense-in-depth): in `EvictArtifact` remote path (`store.go:485`), before `DeleteArtifact`, refuse `FailedPrecondition` when a live VM was relit from the named STATEFUL ref (same registry scan). With the reaper gating below this is a backstop against a mistargeted CP request, mirroring the volume pairing guard's intent at 477-484.

Update the misleading comment block above the STATEFUL case if any remains, and re-read the `evictBaseLocal` docstring (533+) to keep the two histories consistent.

### Step 3: Gate the reaper's remote evictions on local success

In `warmth_reaper.ex`:

- Restructure `run_evict/3` (342-347) so each entry's requests are ordered locals-then-remote and the remote fires ONLY if every local returned `{:ok, _}`. A local failure (including `FAILED_PRECONDITION` from the new in-use guards) logs and SKIPS the remote: never delete the S3 copy of something the node refused to delete locally. Keep per-request best-effort logging via the existing `safe_call/2`.
- Stateful entries (298-314): local `EvictArtifact{remote: false}` first, remote `{remote: true}` only on local success.
- Group entries (316-337): ALL per-member `EvictSnapshot`s must succeed before the single remote `GROUP_SET` evict fires.
- Fix the docstring at 289-291 to describe the NEW noded routing (stateful arm -> `RemoveStatefulBundle`, group member arm -> `RemoveGroupMemberBundle`) instead of the false pre-fix claim.
- Extend `warmth_reaper_test.exs` using the existing `evict_artifact_fun` / `evict_snapshot_fun` / `channel_fun` seams: assert (a) remote NOT called when local errors, (b) remote called after local ok, (c) group remote NOT called when one member's local evict fails.

### Step 4: Self-review, format, commit, push, CI

- `bazel/tools/format/fast-format.sh`
- Commit: `fix(embervm): add noded stateful/group local eviction arms + gate reaper remote evict on local success`
- Chart bump: `bazel/tools/git/bump-chart.sh projects/embervm` (noded AND control-plane images both deploy through this chart; the fix is dead code in prod without the bump). Commit the bump in the same PR.
- Push, open PR, `gh pr checks <n> --watch`. CI verifies: the new Go tests (which fail against the old code), the Elixir reaper tests, plus the full `bazel test //...` sweep. No local test runs.

### Step 5: Review + merge + rollout verification

- One comprehensive Opus code review of the full PR diff (eviction ordering, guard coverage, idempotency, no behavior change for session/serving/base arms).
- Rebase-merge on green. Then verify rollout before PR-2 is allowed to start: `kubectl get applications -n argocd` shows embervm Synced/Healthy at the new targetRevision; confirm the CP pod AND every brick pod restarted onto the new images (`kubectl get pods -n embervm -o wide`, image tags match the bumped chart's pins).

---

## PR-2 (#36): flip the warmth-GC gate (DESTRUCTIVE: deletes S3 recovery copies)

**HARD PREREQ: PR-1 merged and confirmed deployed fleet-wide (Step 5 above). Do not open this PR before that.**

**Model routing:** Sonnet does the mechanical chart edit (three-file values/template change, locally renderable with `helm template`). Fable (or the main loop) owns the pre-flight judgment call and the post-flip supervision: this is the one genuinely destructive flip in the plan. Opus reviews.

**What exists today:** `EMBERVM_WARMTH_RETENTION_SWEEP` is read in `application.ex` (`warmth_retention_sweep_enabled/0`, ~495-500) but has NO chart wiring: nothing renders the env var. The pattern to mirror exactly is `baseRetention.sweepEnabled` (chart `values.yaml:712-713`, template `deployment.yaml:317-323`, deploy `values.yaml:201-202`).

**Files:**
- Modify: `projects/embervm/chart/values.yaml` (add `warmthRetention: { sweepEnabled: "" }` beside `baseRetention`, default off)
- Modify: `projects/embervm/chart/templates/deployment.yaml` (add the env block beside `EMBERVM_BASE_RETENTION_SWEEP`, ~317-323, gated `{{- if .Values.warmthRetention.sweepEnabled }}`, with a comment naming the gate's meaning and rollback)
- Modify: `projects/embervm/deploy/values.yaml` (add `warmthRetention: { sweepEnabled: "1" }` beside `baseRetention` at ~201, with a comment recording the flip date, the ~120G target, and the rollback lever)
- Chart bump: `bazel/tools/git/bump-chart.sh projects/embervm`

### Step 1: Pre-flight (before pushing the deploy-values flip)

1. Confirm PR-1 rollout (PR-1 Step 5). Non-negotiable.
2. Read the dry-run output: `kubectl logs -n embervm <cp-pod> | grep "warmth-retention sweep"`. Expect per-kind `(DRY RUN, gate off) WOULD evict N orphaned stateful|group warmth artifact(s) (~B bytes)` lines (`warmth_reaper.ex:276-278`). Sanity-check the counts against the known leak (~143 stateful orphans + ~5 group sets, ~120G): a wildly larger count means the orphan classification is wrong, STOP and investigate before flipping.
3. Confirm the orphan classification is what we think it is: spot-check a few WOULD-evict refs against `kubectl` / CP state to confirm they belong to instances the CP genuinely no longer tracks (terminal or absent), not to anything live. Remember the semantics: an orphan is evicted ENTIRELY (local disk AND S3); after the flip there is no recovery copy, by design.
4. Render check: `helm template embervm projects/embervm/chart/ -f projects/embervm/deploy/values.yaml` and confirm the env var appears with value "1" (and does NOT appear when the value is "").

### Step 2: Implement, commit, push, merge

- Make the three-file change + chart bump, commit `feat(embervm): flip warmth-retention sweep gate (reclaim orphaned stateful/group warmth)`.
- CI verifies: chart lint/render in the Bazel tree; there is no code change, so CI here is mostly the guardrails (missed-bump check, template render). Merge on green.

### Step 3: Supervised rollout (do not walk away)

- Watch the ArgoCD sync, then follow the reaper logs live: expect the dry-run lines to become `warmth-retention sweep evicting N ... (~B bytes)` and per-eviction failures (if any) as `eviction ... failed:` warnings. A storm of local `FAILED_PRECONDITION` refusals means live VMs are being targeted: rollback and investigate.
- Verify reclaim: node scratch usage drops on the affected nodes (`kubectl exec` into a noded/brick pod for `df /var/lib/embervm/scratch`, or node metrics), converging toward the ~120G figure over a few sweep intervals.
- Verify S3 side: the orphan prefixes disappear from the warmth bucket; non-orphan (live/banked) warmth prefixes remain.
- Confirm #25's `evict_orphan_snapshots` spam is gone (feeds Task 6).

**Rollback lever:** set `warmthRetention.sweepEnabled: ""` in `projects/embervm/deploy/values.yaml`, chart bump, merge. That stops further eviction immediately (the reaper returns to dry-run). Already-evicted orphans are gone and unrecoverable; the lever bounds damage, it does not undo it.

---

## PR-3 (#35): brick min-concurrency floors on node-1/2/3

**Model routing:** Opus implements (placement/fleet-full accounting interactions are subtle and CI cannot exercise the scheduler). Opus reviews. The design choice below was pre-made; if implementation surfaces a conflict with BrickController accounting, escalate to Fable rather than improvising.

**Design (decided): values-declared per-node floor Deployments, NOT topology spread, NOT autoscale minReplicas.**

- `topologySpreadConstraints` is explicitly out: bricks bin-pack deliberately with NO anti-affinity (`chart/templates/brick-deployment.yaml:9-11`); spreading would reverse that placement decision fleet-wide.
- `bricks.autoscale.minReplicas` (already implemented in `brick_controller.ex`, per-class floors) cannot pin nodes: three min replicas of one class can all bin-pack onto node-4, which is exactly the observed failure ("fills node4 then spills under load").
- So: a new `bricks.nodeFloors` values list, e.g. `[{node: "node-1", class: "2gi"}, {node: "node-2", class: "2gi"}, {node: "node-3", class: "2gi"}]`, rendering one additional Deployment per entry named `<fullname>-brick-<class>-<node>`, identical to the class brick pod (same `_noded-pod.tpl` include, same labels + size-class label, same hard memory reservation and `maxSurge: 0` strategy) plus `nodeSelector: {kubernetes.io/hostname: <node>}` and fixed `replicas: 1`.

**Interaction analysis the implementer must verify in code (not assume):**

1. **BrickController scaling:** the controller PATCHes `/scale` on the CLASS deployment by its exact name (`<fullname>-brick-<class>`, `brick_controller.ex`). Floor deployments have distinct names, so the controller never touches them. Confirm no code path enumerates deployments by label for scaling.
2. **Fleet-full + autoscale accounting:** `fleet_full?/2` compares a class's desired count against dial-home REGISTERED bricks of that class, and the idle-based scale-down counts registered instances. Floor bricks register as normal bricks of their class (dial-home is name-agnostic), so registered-per-class will exceed the class deployment's desired count by the number of floors. Read `brick_controller.ex` and confirm the comparisons are `registered < desired` shaped (floors then only make fleet-full LESS likely and scale-down MORE likely, both acceptable) rather than equality-shaped. If any equality or "extra registered = anomaly" logic exists, fix it in this PR with a test.
3. **Registration/addressing:** bricks are instance-keyed, not node-name-keyed (the alias-misroute cleanup, PR-B0c). Confirm nothing keys on the deployment name.

**Files:**
- Modify: `projects/embervm/chart/templates/brick-deployment.yaml` (second range over `.Values.bricks.nodeFloors` rendering the pinned floor Deployments; reuse the class template body via the existing includes)
- Modify: `projects/embervm/chart/values.yaml` (`bricks.nodeFloors: []` default + comment)
- Modify: `projects/embervm/deploy/values.yaml` (initially ONE floor entry: `node-1` / `2gi`; the full set lands in a follow-up values-only flip after the canary)
- Possibly modify: `projects/embervm/control/lib/embervm/brick_controller.ex` + test (only if check 2 finds equality-shaped accounting)
- Chart bump: `bazel/tools/git/bump-chart.sh projects/embervm`

### Step 0: Host pre-flight per node (GATES the rollout, do not skip)

Nothing auto-provisions node scratch anymore (the scratch-prep DaemonSet was dropped, PR #3798); a floor brick scheduled onto a node without a real scratch mount will wedge or silently misbehave (the node-4 mountpoint guard masks a missing loop file). Note: the runbook path previously cited for this (`docs/runbooks/embervm-node-scratch-setup.md`) does not exist in the repo; use these checks directly (and consider writing that runbook as a docs commit in this PR):

For EACH of node-1/2/3 BEFORE adding its floor entry:

1. `/var/lib/embervm/scratch` is a real mountpoint: `mountpoint /var/lib/embervm/scratch` on the host (via Joe or a read-only debug pod; kubectl on managed workloads stays forbidden, host inspection is fine).
2. It is the ~35Gi ext4 loop file, not the root disk: `df -h /var/lib/embervm/scratch` shows ~35G capacity; `mount | grep embervm/scratch` shows a loop device.
3. Free space is sane (>25G free; these are etcd masters, protect the OS disk).
4. Known gotcha: the Wolfi image has NO losetup; provisioning is host-level `mount -o loop` of a formatted file. If a node fails the check, the fix is a HOST action for Joe, not a chart change; park that node's floor entry until done.

### Steps 1-4: Implement, render, canary, expand

1. Implement the chart change; verify with `helm template embervm projects/embervm/chart/ -f projects/embervm/deploy/values.yaml`: exactly one floor Deployment rendered, correct nodeSelector, correct class labels, replicas 1, and zero diff to the existing class Deployments.
2. Run the BrickController accounting checks (above); add the Elixir test if code changed.
3. `bazel/tools/format/fast-format.sh`; commit `feat(embervm): per-node brick floor deployments (node-1 canary)`; chart bump; push; watch CI (Go/Elixir tests + template render + guardrails).
4. Opus review, merge, then supervise the canary: floor brick pod Running on node-1, registers in the CP (brick instance visible, class 2gi, correct node), a canary invoke lands VMs on it, no fleet-full flag flips, BaseBuilder hydrates bases from S3 on first use. Only then add node-2/node-3 entries in a follow-up values-only PR (own chart bump, trivially reviewable).

---

## PR-4 (#18/#27): Embervm.OpLog.Postgres adapter + DSN selection (adapter only, NO flip)

**Model routing:** Sonnet implements (well-specified mirroring of an existing module, locally type-checkable). Opus reviews with attention to callback semantics parity (ordering, seq monotonicity, compaction horizon). No deploy-values DSN wiring in this PR, so the shipped behavior is unchanged.

**Files:**
- Create: `projects/embervm/control/lib/embervm/op_log/postgres.ex`
- Modify: `projects/embervm/control/mix.exs` (add `{:postgrex, path: "deps/postgrex"}` alongside the exqlite path dep at :58)
- Modify: `bazel/erlang/repositories.bzl` (`_HEX_DEPS` at :55: add `postgrex` + its `decimal` dep with pinned versions + sha256; `db_connection` and `telemetry` are already vendored). Grep for every place `exqlite` threads through the hermetic mechanism and mirror ALL of them; per the otel-tracing precedent the dep list appears in 3 places, and missing one fails only in CI.
- Modify: `projects/embervm/control/lib/embervm/application.ex`:
  - `op_log_mod/0` (:379) branches on `EMBERVM_OPLOG_DSN`: set and nonempty -> `Embervm.OpLog.Postgres`, else `Embervm.OpLog.SQLite`. The docstring above it (:373-378) already promises exactly this change.
  - Child spec (:78) starts the SELECTED backend with its own opts (Postgres takes the DSN; SQLite keeps `path:` + `journal_horizon_ms:`).
  - Compactor spec (:337-338) currently hardcodes `op_log: Embervm.OpLog.SQLite`; thread the selected module/server there too.
  - **PR-1-review acceptance item:** add `op_log_mod: op_log_mod()` (and matching `op_log:`) to the `Embervm.BaseBuilder` child spec (~:96-99). `base_builder.ex:316-317` already accepts both and silently defaults to SQLite; without this the `:artifact_exported` audit would not follow the flip.
- Modify: `projects/embervm/control/lib/embervm/op_log/compactor.ex` (:113-118): handle `{:error, :not_supported}` from `db_size/1` gracefully (omit the size from the log/metric rather than crash or warn each tick).
- Test: `projects/embervm/control/test/embervm/op_log/postgres_test.exs` + an `application` selection test.
- Chart bump: `bazel/tools/git/bump-chart.sh projects/embervm` (the CP image ships the new module even though it is dormant).

**Spec details:**
- Implement ALL 16 `@behaviour Embervm.OpLog` callbacks (`op_log.ex:219-332`): `append`, `read_from`, `load_tasks`, `load_sessions`, `load_serving_instances`, `load_stateful_instances`, `load_volumes`, `load_volume_blessing`, `load_group_instances`, `load_group_members`, `load_result`, `load_request`, `list_usage`, `compact`, `compacted_through`, `evict_task`. Mirror `sqlite.ex`'s schema, projection semantics, and single-writer GenServer shape, translating SQL dialect only (postgrex parameters, `ON CONFLICT`, types). Note repo gotcha: nullable psycopg-style params needing casts applies to Python; in postgrex prefer explicit `::` casts where a parameter can be NULL in a WHERE clause.
- `db_size/1` is NOT a behaviour callback; it is a public module function the Compactor dispatches on `op_log_mod` (`compactor.ex:114`). The Postgres module exposes `db_size/1` returning `{:error, :not_supported}`.
- **Testing honestly:** CI has no Postgres service for the control plane today. Do not fake one. Test (a) the module compiles under the behaviour (dialyzer/`@behaviour` warnings are CI-visible), (b) pure SQL-fragment/row-mapping helpers as plain functions, (c) `op_log_mod/0` selection via the env seam. Full round-trip conformance against a real Postgres lands with the future DSN-flip PR (which must add a CI-side Postgres and re-run the SQLite test suite's scenarios against it; note this explicitly as that PR's acceptance bar).
- Commit: `feat(embervm): Embervm.OpLog.Postgres adapter behind EMBERVM_OPLOG_DSN (not wired)`.

---

## Task 5 (#28): investigate monolith ArgoCD Synced/Degraded

**Model routing:** main loop (Opus). Investigation first; code only if a real fix surfaces (then its own small PR, with a chart bump only if a chart changes).

Steps:
1. `kubectl get application monolith -n argocd -o yaml`: read `.status.health`, and list every entry in `.status.resources[]` with `health.status` not Healthy/empty. The report is "no concrete unhealthy resource", so expect the aggregate Degraded with no member Degraded.
2. Prime suspect (per prior sightings): the CNPG `Cluster` resource aggregating as health-Unknown. Check `kubectl get cluster -n monolith -o yaml` (`.status.phase`, conditions) and whether ArgoCD has a health Lua for `postgresql.cnpg.io/Cluster` (`kubectl get cm argocd-cm -n argocd -o yaml`, `resource.customizations`).
3. If it is the aggregation artifact: fix is a `resource.customizations.health.postgresql.cnpg.io_Cluster` Lua (or an `ignoreHealthCheck`/`ignoreDifferences` entry) in the platform ArgoCD values (`projects/platform/...`), landed via GitOps. Check upstream CNPG docs for their recommended ArgoCD health check before writing one (anti-pattern: hand-rolling what upstream provides).
4. If instead a real resource is unhealthy, follow it concretely (describe + events + logs) before proposing anything.
5. Record the outcome on task #28 either way (fix PR link, or "artifact, fixed via health customization", or "real issue X").

## Task 6 (#25): verify evict_orphan_snapshots spam resolved, close

After PR-1 is deployed (and ideally after the PR-2 flip has swept the backlog):
1. `kubectl logs -n embervm <cp-pod> --since=24h | grep -c evict_orphan_snapshots` (and the corresponding noded logs on the bricks): expect zero or near-zero, versus the prior spam.
2. The BASE-ref root cause was already folded into base-durability PR-3; #38 fixes the sibling stateful/group arms, so post-deploy the sweepers' evictions actually converge instead of retrying forever. If spam persists, quote the exact recurring log line and treat it as a NEW investigation (do not force-close).
3. Close #25 with the evidence (log counts before/after).

---

## Deferred (explicitly out of scope)

- **#17 (temp sudo pw rotation + node-3 tune2fs):** host actions owned by Joe; he is rotating the password himself (2026-07-22).
- **#23 (ADR 014 worker-authoritative consistency, PR #3767):** large multi-PR rework, self-described as deferred pending Joe's review of the draft ADR first.
- **#32 (offsite Pg backups / DR target):** marked FUTURE; needs a real offsite object-store decision before any implementation is plannable.
