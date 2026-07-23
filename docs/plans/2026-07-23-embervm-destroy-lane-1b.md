# EmberVM Destroy-Lane PR 1b Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Extend the ADR 014 node-confirmed-destroy apparatus (shipped for the `session` class in PR #3818) to the `serving`, `stateful`, and `group` classes, so `EMBERVM_NODE_CONFIRMED_DESTROY` gives consistent worker-authoritative destroy semantics across all four classes and is safe to flip to `1`.

**Architecture:** Mirror `session_manager`'s gated destroy fork into the other three managers, adapted to each class's teardown owner and resources. Three moving parts per class: (1) a `:destroying` adopt-skip so a straggler node report can't re-adopt an instance mid-teardown (TLA-proven load-bearing); (2) gate plumbing (`node_confirmed_destroy` + `destroying_alarm_ms` + `orphan_grace_ms`) into each manager's init and `application.ex`; (3) a gated destroy reorder — durable `:destroying` intent → node-confirmed teardown RPC (returning a real `teardown_confirmed`) → terminal `destroyed` only on confirm — plus a reconcile-side `redrive_destroying` (stuck-alarm + confirm-by-absence) and orphan-VM destroyer. The gate stays **default-OFF**; every gate-off path must be byte-for-byte today's behaviour.

**Tech Stack:** Elixir (embervm control plane), ExUnit, ETS + SQLite/Postgres op-log, gRPC node channels, TLA+/TLC (`bazel/tla`).

**Reference implementation (mirror this):** `projects/embervm/control/lib/embervm/session_manager.ex`
- Gate init: L255-265. Wired in `application.ex` via `node_confirmed_destroy_enabled/0` (L655-660), `destroying_alarm_ms/0`, `orphan_grace_ms/0`.
- Gated destroy fork: `destroy_live/2` L2256 → `destroy_live_legacy` L2265 (record-first) vs `destroy_live_node_confirmed` L2296 (intent → RPC → confirm).
- Reconcile hook: `do_reconcile` L1567-1573 (gate-guarded `redrive_destroying |> destroy_orphan_session_vms`).
- Re-driver: `redrive_destroying` L1583, `maybe_alarm_destroying` L1594, `redrive_one_destroying` L1610, `record_session_destroyed` L1638.
- Orphan destroyer: `destroy_orphan_session_vms` L1679 (NOTE: session's version carries async-write adopt/backfill discriminator logic — that is the *async* lane and is OUT OF SCOPE here; the other three classes get the plain gate-off orphan reap only).
- Adopt-skip: `adopt_one` L1793 `session.state == :destroying -> state`.

**Per-class teardown-owner map (from codebase survey — verify line numbers before editing, main moves):**

| Class | Adopt (skip goes here) | Destroy path (reorder goes here) | Reconcile hook | Reap today |
|---|---|---|---|---|
| serving | `serving_manager.adopt_one/4` ~L1002 | **sweeper-owned** — `serving_sweeper` (manager has no destroy path) | `serving_manager.do_reconcile` ~L968 | `evict_orphan_snapshots` ~L1066; no taps in manager |
| stateful | `stateful_manager.adopt_one/4` ~L1863 | `stateful_manager.force_destroy_live` L1666 (teardown-first) | `stateful_manager.do_reconcile` ~L1784 | `refresh_volume_facts` L2013, `eager_evict_broken_pairs` L1796 |
| group | `group_wake_manager.adopt_one/4` ~L802 | `group_manager.destroy_live_members` L1491 + `group_sweeper.destroy_all_members`/`destroy_live_group` | `group_wake_manager.do_reconcile` ~L746 | `group_sweeper.gc_orphan_networks` L310 |

**Critical nuance:** session's *legacy* path records `destroyed` FIRST; stateful/group already tear down BEFORE recording. So for stateful/group the gate change is "insert durable `:destroying` intent + confirm-by-node before the terminal record", not a literal reorder. **Every implementer MUST quote their manager's exact current ordering in the task before changing it.**

**Testing note (repo-specific):** There is NO local test loop (see CLAUDE.md). "Run the test" steps mean *write* the test and the implementation together; execution is deferred to end-of-plan BuildBuddy CI on the pushed branch. Follow existing test patterns in each `*_manager_test.exs` / `*_sweeper_test.exs`. New `*_test.exs` needs a `py_test`-equivalent — Elixir tests are gazelle-globbed, so run `bazel/tools/format/fast-format.sh` and stage ONLY intended files (local gazelle drifts unrelated BUILD files; revert that drift, commit with `--no-verify`).

---

### Task 1: `:destroying` adopt-skip in all three managers

The uniform, TLA-proven, lowest-risk slice. Independent of the gate (correctness under node-authoritative reconciliation regardless of gate state), so it lands first.

**Files:**
- Modify: `projects/embervm/control/lib/embervm/serving_manager.ex` (`adopt_one/4`, before the `waking` branch)
- Modify: `projects/embervm/control/lib/embervm/stateful_manager.ex` (`adopt_one/4`, after the in-flight-wake branch)
- Modify: `projects/embervm/control/lib/embervm/group_wake_manager.ex` (`adopt_one/4`, before the `waking` branch)
- Test: `test/embervm/serving_manager_test.exs`, `test/embervm/stateful_manager_test.exs`, `test/embervm/group_wake_manager_test.exs`

**Step 1 — quote the current `adopt_one` cond in each manager** (paste into the task notes so the reviewer sees the insertion point).

**Step 2 — add the guard** as the FIRST cond branch that can match a destroying instance, mirroring `session_manager.ex:1793`. Shape (adapt binding name per manager — `serving`/`instance`):

```elixir
# An instance being torn down (destroying, ADR embervm/014 decision 5) must NOT
# be re-adopted even though the node still reports its live VM: the teardown is
# in flight and redrive_destroying owns it. Keying off the CP state, not the node
# report, is the fix for the TLC NoDestroyBeforeConfirm violation (adoption.tla).
instance.state == :destroying ->
  state
```

For stateful, place it so it also guards the live-VM adopt branch (the TLC scenario is a `:destroying` instance whose VM is still node-reported); guard on `instance.state == :destroying` alone is sufficient and simplest — do NOT over-condition on `live_vms`.

**Step 3 — write one test per manager:** seed an instance in `:destroying`, have the node facts still report its live VM, run `reconcile`, assert the instance stays `:destroying` (NOT re-adopted to running/published). Use the existing `poll_mgr`/`wait_until` helpers where the manager spawns workers.

**Step 4 — format, stage only intended files, commit.**

```bash
git add projects/embervm/control/lib/embervm/{serving_manager,stateful_manager,group_wake_manager}.ex \
        projects/embervm/control/test/embervm/{serving_manager_test,stateful_manager_test,group_wake_manager_test}.exs
git commit --no-verify -m "fix(embervm): skip :destroying instances in serving/stateful/group adoption"
```

---

### Task 2: Gate plumbing into the three managers

Add `node_confirmed_destroy` (+ `destroying_alarm_ms`, `orphan_grace_ms`) to each manager's init and wire from `application.ex`. Inert on its own (nothing reads it yet after this task); prerequisite for Tasks 3-5.

**Files:**
- Modify: `serving_manager.ex` init/state (~L151-205), `stateful_manager.ex` init (~L276-357), `group_manager.ex` init (~L196-244) AND `group_wake_manager.ex` init (the adopt/reconcile owner — it needs the gate too)
- Modify: `projects/embervm/control/lib/embervm/application.ex` — `serving_manager_opts/0` (~L804), `stateful_manager_opts/0` (~L928), `group_manager_supervisor_opts/0` (~L1201), `group_wake_manager_opts/0` (~L1216)
- Test: each manager test's start helper already passes opts; add coverage that the field defaults to `false`.

**Step 1 — mirror `session_manager.ex:255-265` into each init:**

```elixir
node_confirmed_destroy: Keyword.get(opts, :node_confirmed_destroy, false),
destroying_alarm_ms: Keyword.get(opts, :destroying_alarm_ms, 300_000),
orphan_grace_ms: Keyword.get(opts, :orphan_grace_ms, 60_000),
```

**Step 2 — wire from `application.ex`** using the existing `node_confirmed_destroy_enabled/0`, `destroying_alarm_ms/0`, `orphan_grace_ms/0` (already defined for session — reuse, do not duplicate). Add the three keys to each `*_opts/0` list.

**Step 3 — test:** assert `:sys.get_state(mgr).node_confirmed_destroy == false` with default start opts (gate off by default is a safety invariant).

**Step 4 — commit** `chore(embervm): plumb node_confirmed_destroy gate into serving/stateful/group managers`.

---

### Task 3: Serving — gated node-confirmed teardown (sweeper) + reconcile hook

Serving teardown is **sweeper-owned**. Read `serving_sweeper.ex` end-to-end first; quote the current force-roll/evict path and its ordering.

**Files:**
- Modify: `projects/embervm/control/lib/embervm/serving_sweeper.ex` (destroy/evict path — add gated `:destroying` intent → node-confirmed tap+VM teardown → `destroyed` on confirm)
- Modify: `serving_manager.ex` `do_reconcile` (~L968): add the gate-guarded `redrive_destroying |> destroy_orphan_serving_vms` block after `evict_orphan_snapshots`, mirroring `session_manager.ex:1567-1573`
- Test: `test/embervm/serving_sweeper_test.exs`, `test/embervm/serving_manager_test.exs`

**Steps:** quote current ordering → implement `redrive_destroying`/`destroy_orphan_serving_vms`/`maybe_alarm_destroying` for serving (mirror session L1583-1734, minus the async discriminator — plain orphan destroy) → make the tap+VM teardown RPC return a real `teardown_confirmed` and record `destroyed` only when true → gate-off path unchanged → tests: (a) gate-on, node confirms → `destroyed`; (b) gate-on, RPC unconfirmed → stays `:destroying`, re-driven next reconcile; (c) gate-on, VM gone from node report → confirm-by-absence → `destroyed`; (d) gate-off → identical to today. Commit.

---

### Task 4: Stateful — gated node-confirmed teardown + volume reap honesty

**Files:**
- Modify: `stateful_manager.ex` — `force_destroy_live` L1666 (add gated intent→confirm variant), `stop_stateful_destroy` L1696 (return real `teardown_confirmed` instead of always-`:ok`), `do_destroy_instance` L1638 aggregate (`%{destroyed: n}` counts node-confirmed teardowns honestly), `do_reconcile` L1784 (gated `redrive_destroying |> destroy_orphan_stateful_vms` after `eager_evict_broken_pairs`)
- Test: `test/embervm/stateful_manager_test.exs`

**Steps:** quote L1666-1694 ordering → split `force_destroy_live` on `state.node_confirmed_destroy`: gate-off keeps teardown-then-record; gate-on writes durable `:begin_destroy`→`:destroying` FIRST, issues `stop_stateful_destroy`, transitions `:stateful_destroyed` only if confirmed, else leaves `:destroying` → `stop_stateful_destroy` returns `{:ok, confirmed_bool}` from the `StopStatefulResponse.teardown_confirmed` field (verify proto has it; PR 1 added it fleet-wide) → `redrive_destroying`/`destroy_orphan_stateful_vms` mirror session, keyed on `stateful_vms` facts, with the stuck-alarm → **volume reap honesty:** do NOT delete volumes on instance destroy (volumes persist; `delete_volume` is the separate destructive verb — leave it untouched) → tests mirror Task 3's (a)-(d) plus one asserting the volume row survives an instance destroy. Commit.

---

### Task 5: Group — gated node-confirmed member teardown + network reap

Group teardown fans out over N members across `group_manager` + `group_sweeper`. Read both; quote both orderings.

**Files:**
- Modify: `group_manager.ex` `destroy_live_members` L1491 / `destroy_one_member` L1500, `group_sweeper.ex` `destroy_all_members` L1033 / `destroy_live_group` L973, `group_wake_manager.ex` `do_reconcile` ~L746 (gated hook)
- Test: `test/embervm/group_manager_test.exs`, `test/embervm/group_sweeper_test.exs`, `test/embervm/group_wake_manager_test.exs`

**Steps:** quote both current orderings → gate-on: durable group `:destroying` intent BEFORE the per-member `StopGroupMember(DESTROY)` fan-out, aggregate the per-member `teardown_confirmed`, record group `destroyed` only when ALL members confirm (else stay `:destroying`, re-drive) → network delete stays after member teardown (already last in sweeper) → `redrive_destroying`/`destroy_orphan_group_vms` in `group_wake_manager`, orphan = node-reported member with no CP group row → gate-off unchanged → tests mirror (a)-(d), plus partial-confirm (some members confirm, one doesn't → group stays `:destroying`). Commit.

---

### Task 6: TLA+ re-check (ADR 014 mandated)

ADR 014's "Formal specification follow-through" MANDATES a TLA+ step in every ADR 014 impl plan. PR 5 already made `adoption.tla` worker-authoritative and added the `:destroying` adopt-skip guard for the *modeled* (session) path. This task extends the model's fidelity to the claim that the skip + node-confirmed destroy hold for all four classes.

**Files:**
- Modify: `projects/embervm/specs/adoption.tla` + cfgs, and/or extend `vocabulary.exs` if any new node.proto verb / op-log kind was introduced (Tasks 3-5 should reuse existing `teardown_confirmed`; if a new enum member appears, `spec_vocabulary_test.exs` will fail CI until classified)
- Reference: `docs/decisions/embervm/014-*.md`, `docs/decisions/embervm/006-*.md`, memory [[reference_embervm_tla_adopt_destroying_guard]]

**Steps:** confirm the AdoptInventory `w \notin destroying /\ w \notin cpDestroyed` guard (commit eea87354b) still covers the generalized model → run TLC locally on the tiny configs (openjdk + `/private/tmp/tla2tools-1.7.4.jar`, `adoption.cfg` safety ~85s, run in background: foreground Bash 2-min cap kills it) → run `pcal.trans` so the CI freshness-diff checksum matches → confirm `NoDestroyBeforeConfirm` / `DestroyIntentPrecedesRecord` still clean → if Tasks 3-5 revealed a genuinely new invariant class not covered by the session model, note it in `DECISIONS.md` and extend the spec. Commit `docs(embervm): TLA re-check for node-confirmed destroy across all four classes` (note: a `docs/plans` + spec change forces monolith+monolith-public chart bumps for the docs-manifest regen — see [[feedback_adr_docs_manifest_forces_monolith_bump]]).

---

### End-of-plan (single PR)

- One comprehensive Opus code review against the full diff (per CLAUDE.md: one review per PR, not per task).
- `bazel/tools/git/bump-chart.sh projects/embervm` (destroy-lane code must deploy → needs the bump in this PR). Expect a monolith+public bump too from Task 6's docs-manifest regen.
- Push, watch CI (`gh pr checks <n> --watch`), iterate on failures via `mcp__buildbuddy__*`.
- The gate stays default-OFF after merge. Flipping `EMBERVM_NODE_CONFIRMED_DESTROY=1` is a SEPARATE step gated behind the Step-3 pre-flip hardening (alarm counter/dedup, confirm-by-absence tests, retry-log fix) — NOT part of this PR.
