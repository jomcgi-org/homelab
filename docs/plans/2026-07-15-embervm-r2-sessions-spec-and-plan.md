# EmberVM R2 (Sessions) Spec and Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (or superpowers:executing-plans in a separate session) to implement this plan task-by-task. This document is the committed spec for rung R2 of [ADR embervm/001](../decisions/embervm/001-embervm-beam-firecracker-workload-orchestrator.md), shaped by [ADR embervm/003](../decisions/embervm/003-control-plane-managed-snapshot-distribution.md) (snapshot verbs and lineage metadata) and [ADR embervm/004](../decisions/embervm/004-agent-sandbox-interface-compatibility.md) (session verbs must map onto agent-sandbox pause/resume). Every task is a specification with acceptance criteria; no implementation lives here.

**Goal:** Ship EmberVM R2: bank/relight of stateful session sandboxes. A session is a long-lived logical sandbox whose in-VM state (files, variables, a warm interpreter) persists across invocations: it is banked (suspended to a snapshot on node disk) when idle and relit (restored) on the next request. The named first consumer is the agent `sandbox` ([ADR agents/044](../decisions/agents/044-code-executor-sandbox.md)): `run_python` gains an optional session so an agent's computation accretes state across turns instead of starting cold every snippet.

**Architecture:** The node contract gains session verbs (`SessionAssign` delivers a request without destroying the VM; `Bank` pauses, snapshots, and destroys; `Relight` restores a session snapshot; `EvictSnapshot` reclaims disk) and the node reports session VMs and banked-snapshot inventory in `NodeStatus` so a restarted control plane adopts instead of orphaning (the R0 drill lesson). The control plane gains a `SessionStore` (identity, lifecycle FSM, lineage, hashed tokens; ETS hot set projected from new op-log session records), a per-live-session process that owns invoke serialization and the idle-bank timer, and a `SessionPlacement` module that keeps the invocation front-end split from placement (the R2 held invariant). Session creation is assignment from the existing primed pristine pool, so create latency rides R0 machinery. Every session snapshot records its parent base and generation from day one (the lineage rule); a session is pinned to its birth base, and the chart-configurable max-lifetime TTL is the version-convergence bound.

**Tech Stack:** Elixir/OTP control plane and Go noded (existing), gRPC session verbs on `embervm.node.v1`, Firecracker pause + full snapshot (node-local NVMe), SQLite op-log projections (ADR 002 retention discipline), Kubernetes Workload CRD (`class: session` + `spec.session` block), monolith Python (`run_python` session surface), Helm + ArgoCD.

---

## Standing decisions (settled, do not relitigate during execution)

1. **Sessions are not tasks and are not CRs.** Definitions live in the `Workload` CRD (`class: session` becomes valid); session instances are execution state and live in the op-log + ETS exactly like tasks (ADR 001 state model). No per-session Kubernetes object exists in ember's core; that surface belongs exclusively to the deferred ADR 004 adapter.
2. **Session invokes are sync-only, at-most-once, never retried.** A stateful invoke is not safely retryable by the platform (the guest may have half-applied it). No task record, no DLQ, no result store, no idempotency key: the response is returned inline to the parked caller or the caller gets an error and decides. Usage is still billed (the `session_invoked` op carries usage, the D12.1 pattern).
3. **Node-local snapshots in v1; a lost snapshot is a lost session, surfaced loudly.** Banked session snapshots live on the owning node's NVMe. Node death or an unrestorable snapshot terminates the session: the next invoke gets `410 Gone` with a machine-readable reason and the caller creates a fresh session. "Warmth fails open" does NOT apply here, because for a session the snapshot IS the state: silently relighting an agent's sandbox as a blank VM would be incorrect behavior, and correctness never degrades (ADR 001 failure posture). Object-store export (`ExportBase` generalized per ADR 003) is the recorded durability follow-on, not v1.
4. **Full memory snapshot per bank in v1; lineage metadata from day one.** Diff snapshots and store tiering are follow-ons; what R2 must not retrofit is the metadata, so every session snapshot record carries `{parent_base_ref, base_digest, generation, size_bytes, created_at, last_access}` in the op-log projection from the first bank. Disk cost is bounded by `maxSessions x memMib` and watched by a watermark alert, never discovered (ADR 002 rule 4).
5. **Sessions are pinned to their birth base; the max-lifetime TTL is the version-convergence bound.** A session records `base_snapshot_ref` + `base_digest` at create and relights from that lineage forever. A deploy (new image digest, new zip sha) does not converge live or banked sessions; they ride their birth version until `maxLifetimeSeconds` expires them, forcing the next create onto the current base. Consequence: base turnover (R0 Task 10) may no longer delete a superseded base while any session references it; bases are refcounted by sessions and evicted only at zero refs, which the TTL bounds in time.
6. **Session verbs map 1:1 onto agent-sandbox pause/resume (ADR 004 shaping).** `create/bank/relight/destroy` plus a per-session endpoint token is the whole lifecycle vocabulary; a future adapter translates `Sandbox`/`SandboxClaim` onto these verbs without redesign. The adapter itself stays deferred and gated.
7. **The invocation front-end stays split from placement (the R2 held invariant).** The router never computes where a session lives; `SessionPlacement` owns node choice (create: rendezvous-hash seam over ready nodes; relight: the residency fact). Residency facts are ETS, lossy, rebuilt by adoption from `NodeStatus` (the node is the source of truth for what it holds). Per-session endpoints are recorded as facts so R3 can publish them via xDS additively; in R2 the control plane proxies every session invoke, which is invariant-consistent (see options ranked below).
8. **Session VMs inherit the task-class isolation posture for the sandbox consumer:** vsock-only, no NIC, zero egress, non-root guest. Reuse is within one principal's own snapshot lineage only (the ADR 001 session row); nothing in the mechanics permits a relight into any other session's record. Egress-capable sessions are a recorded follow-on gated on ADR agents/023 integration.
9. **One in-flight invoke per session.** Invokes to one session serialize FIFO in its session process (an agent turn is sequential by nature); a small queue cap (default 4) rejects pile-ups with 429. Concurrency ACROSS sessions is governed by `spec.concurrency.cap` (live session VMs) exactly like task caps.
10. **The node reports, the control plane adopts.** Session VMs (`session_vm_ids`) and banked snapshot inventory are in every `NodeStatus`, and the control plane reconciles its ETS from them on boot and every sweep. This is the primed-pool adoption fix (PR #3517) applied from day one to session state; reaping on disconnect is forbidden for the same reason it was for the pool.

### Session addressing: options ranked (settled choice)

How a caller invokes "session S of workload W" without foreclosing R3's control-plane-off-the-hit-path invariant:

- **Option A (chosen, simplest): sessions are control-plane API resources.** `POST /v1/workloads/{name}/sessions` creates; `POST /v1/sessions/{id}/invoke` invokes with the per-session token; the control plane proxies the request to the VM (relighting first if banked). This is invariant-consistent for R2's traffic shape: an invoke to a banked session IS a lifecycle miss (relight), where ADR 001 puts the control plane on the path by design, and an invoke to a live session is assignment-only against ETS facts, the same near-zero overhead as primed dispatch. Agent turns are low-rate and sequential, so proxying hits costs nothing that matters.
- **Option B: per-session Envoy routes via xDS now.** Correct end-state for high-rate serving, but it builds R3's xDS programming a rung early for a consumer that will never notice. Rejected as premature; Option A keeps it reachable because the front-end module never learns placement and the VM endpoint is already a recorded fact.
- **Option C: session URLs on the node daemon (caller talks to noded).** Rejected outright: it reintroduces the fc-invoke path-based session surface the R0 fork deliberately deleted, puts payload routing authority in the daemon, and breaks the facts/payloads split.

R3 reachability check: moving hits off the control plane later means publishing the session VM endpoint (already a fact) to Envoy and having the front-end return a route instead of proxying. Nothing in Option A's schema or module boundaries prevents that swap.

## Cross-cutting constraints

- **No local test loop.** Implement, commit, push, watch BuildBuddy CI (`gh pr checks <n> --watch`). ExUnit, Go, and pytest targets all run under `bazel test //...` in CI only.
- **Conventional Commits; no em-dashes anywhere.**
- **Charts bump via `bazel/tools/git/bump-chart.sh`** in the same PR as the code they deploy. Docs-manifest regeneration (this plan, ADR touches) forces monolith AND monolith-public chart bumps.
- **RBAC verbs verified per task** for every new K8s API call before merge.
- **New monolith `*_test.py` files need hand-registered `py_test` targets.**
- **One comprehensive code review per merged PR.**
- **Op-log schema changes use the guarded ALTER-after-DDL migration pattern** (additive columns, first-byte-compatible payload decoding; the D-R1.2.1 precedent).
- **Additive proto only:** new RPCs and new fields; `Assign` and the task-class contract stay frozen.
- **Repository layout:** EmberVM code under `projects/embervm/`; consumer code under `projects/monolith/sandbox/` and the guest under `projects/firecracker/sandbox/guest-init/` (the sandbox guest image is shared with the deprecated fc-invoke path; changes must keep the task class byte-compatible).

## Suggested PR partitioning

| PR | Tasks | Deploys |
| -- | ----- | ------- |
| PR-0 docs (this branch) | this plan | manifests + monolith/public chart bumps |
| PR-1 contract | 1, 2, 3 | additive proto + op-log schema + CRD; no behavior change |
| PR-2 noded verbs | 4 | session verbs live on noded, unused |
| PR-3 lifecycle core | 5, 6 | create/invoke/destroy end-to-end on a live echo session |
| PR-4 bank/relight + placement | 7, 8 | idle-bank, relight-on-invoke, adoption, eviction live |
| PR-5 operability | 9 | session spans, disk watermark alert, status counts |
| PR-6 consumer | 10 | sessioned `run_python` live on the private tier |
| PR-7 closure | 11 | R2 marked shipped in ADR 001 |

---

## Phase 0: Contract and schema foundations (the lineage rule, held from day one)

### Task 1: Node contract session verbs and session facts

**Why:** Today `Assign` destroys the VM after one task by contract; sessions need deliver-without-destroy, pause-to-snapshot, and restore-from-session-snapshot. Specifying the verbs first shapes the noded work the way Task 3 shaped the R0 fork.

**Deliverables:**
- Additive RPCs on `embervm.node.v1.NodeService`, proto comments as the spec:
  - `rpc SessionAssign(SessionAssignRequest) returns (SessionAssignResponse)`: deliver exactly one HTTP-semantics request (same `GuestRequest` message, same 8 MiB cap) to a live session `vm_id` over vsock; return `GuestResponse` + `UsageStats`; the VM SURVIVES. `FAILED_PRECONDITION` on an unknown, task-class, or mid-bank vm_id. `DEADLINE_EXCEEDED` on guest timeout leaves the VM alive but flagged suspect in the response (the control plane decides whether to destroy).
  - `rpc Bank(BankRequest) returns (BankResponse)`: pause the VM, write a full snapshot (memory + rootfs delta per the existing self-contained bundle format), destroy the VM, return `{snapshot_ref, size_bytes}`. Refuses (`FAILED_PRECONDITION`) while a `SessionAssign` is in flight on that vm_id.
  - `rpc Relight(RelightRequest) returns (RelightResponse)`: restore a VM from a session `snapshot_ref`, wait for guest readiness (same vsockhttp WaitReady mechanics as `Prime`), return `vm_id`. `FAILED_PRECONDITION` if the ref is unknown or unrestorable (the snapshot is never deleted on a failed restore; the control plane decides).
  - `rpc EvictSnapshot(EvictSnapshotRequest) returns (EvictSnapshotResponse)`: delete a snapshot bundle from node disk. Idempotent (unknown ref is OK). Refuses while a live VM was relit from it and still runs. This is the ADR 003 eviction verb landed early, shaped so base eviction later reuses it.
- `NodeStatus` additions (all repeated/optional, wire-compatible): `session_vms: [{vm_id, session_id, workload}]`, `session_snapshots: [{snapshot_ref, session_id, workload, size_bytes, created_at_unix_ms}]`, `snapshot_disk_free_bytes`, `snapshot_disk_used_bytes`.
- `RelightRequest`/`BankRequest` carry `Trace` plus `session_id` (opaque to the daemon; echoed in status so adoption can rebind).
- fakenode updated to serve all four verbs and the new status fields.

**Specification:**
- Refs stay opaque strings; no snapshot file paths cross the seam (R0 proto rule).
- The daemon does not know principals or lineage; it stores `(snapshot_ref -> bundle, session_id)` and reports facts. Isolation is a control-plane property enforced by which ref is passed to `Relight`.
- Session VMs count against `max_live_vms` exactly like primed/task VMs.
- Go+Elixir stubs regenerate through the existing pure-genrule codegen; a fake-server round-trip test covers each new verb in CI.

**Acceptance:** CI green; reviewer confirms the additions are additive (existing R0/R1 messages untouched), that `Assign` semantics are unmodified, and that nothing Firecracker-specific leaks.

**Commit:** `feat(embervm): session verbs and session facts on the node contract`

### Task 2: Op-log session records and projection

**Why:** Session lifecycle must be durable, ordered, and auditable exactly like tasks (ADR 001: executions in the op-log), and the lineage rule must be schema from the first banked byte.

**Deliverables:**
- Additive op kinds (closed enum extended): `session_created, session_invoked, session_banked, session_relit, session_expired, session_evicted, session_destroyed, session_failed`.
- `ops` gains a nullable `session_id` column (guarded ALTER-after-DDL migration).
- New projection table: `sessions(session_id TEXT PRIMARY KEY, tenant, principal, workload, state, node_id, base_snapshot_ref, base_digest, generation INTEGER, snapshot_ref, snapshot_size_bytes, token_sha256, created_at, last_invoke_at, expires_at, updated_at, terminal_reason)`.
- Retention integration per ADR 002: terminal sessions prune on the existing hourly sweep past the 7-day retention; session ops compact behind the same `compacted_through_seq` marker with the same never-compact-live rule (a non-terminal session pins its ops like a non-terminal task does).

**Specification:**
- Session-state transitions are write-through appends: the op lands before the ETS/projection change is visible, same discipline as tasks (R0 standing decision 8). The dispatch/invoke path never reads the durable store.
- `session_invoked` carries `{status_code, cpu_ms, peak_rss_mib, wall_ms}` and NO request/response payloads (at-most-once, no replay need, data minimization; the journal must not grow with agent traffic bodies).
- `session_banked` carries `{snapshot_ref, generation, size_bytes, parent_base_ref}`; `session_relit` carries `{snapshot_ref, generation, relight_ms}`. Generation increments on every bank.
- Usage from `session_invoked` upserts the same `(principal, day)` usage projection in the same transaction (D12.1 pattern); session compute is quota-visible identically to task compute.
- Payload encoding stays ETF blobs (D-R1.2.1).
- ExUnit: projection rebuild from a scripted op sequence reproduces exact session states; retention sweep never prunes a non-terminal session; marker never advances past a live session's ops (extend the R1 property test).

**Acceptance:** CI green; a control-plane restart against a database containing live and terminal sessions rebuilds ETS exactly (kill-and-restart test extended).

**Commit:** `feat(embervm): session records, lineage schema, and retention in the op-log`

### Task 3: Workload CRD `class: session` and base refcounting

**Why:** The CRD is the definition surface; `session` was reserved in the class enum for exactly this. The base-turnover change is the version wrinkle made mechanical.

**Deliverables:**
- CRD: `class: session` accepted by the watcher (no longer condition-rejected) and requires a `spec.session` block: `{idleBankSeconds (default 300, min 10), maxLifetimeSeconds (default 86400, min 60; the version-convergence bound, chart-configurable per workload), bankedTtlSeconds (default = maxLifetimeSeconds; GC a banked snapshot untouched this long), maxSessions (live + banked, default 16), invokeQueueCap (default 4)}`. `spec.concurrency` for session workloads reads: `cap` = max live session VMs, `floor` = primed pristine VMs kept warm for fast create.
- `status.sessions: {live, banked}` counts + a `SESSIONS` printer column (live/banked), written by the control plane.
- BaseBuilder refcounting: on digest turnover, primed pristine VMs still turn over eagerly (R0 Task 10 unchanged), but a superseded base file is destroyed only when zero primed VMs AND zero non-terminal sessions reference it. Multiple superseded bases may coexist, each bounded in lifetime by its referencing sessions' `maxLifetimeSeconds`. Eviction of a fully drained superseded base uses Task 1's `EvictSnapshot` (the ADR 003 orphaned-base cleanup, landed).
- Sample session CR under `projects/embervm/crd/samples/`.

**Specification:**
- Task-class CRs carrying a `spec.session` block get `Ready=False` with a precise condition message (R0 posture), and vice versa for session-class CRs missing it.
- New creates always use the CURRENT base; existing sessions keep their recorded `base_snapshot_ref`. The watcher/BaseBuilder never rewrites a session's lineage.
- ExUnit: watcher parse/validation tests; a refcount property test (turnover with N live sessions on the old base never destroys it; destroying the last session releases it and eviction fires).

**Acceptance:** CI green; sample CR round-trips; `kubectl get workloads` shows the session columns; a digest bump with a live fake session leaves the old base restorable.

**Commit:** `feat(embervm): session workload class, session spec block, and base refcounting`

---

## Phase 1: Session lifecycle core

### Task 4: noded session verbs implementation

**Why:** The four verbs from Task 1 become real Firecracker mechanics: pause/snapshot/resume on the driver the R0 fork already hardened.

**Deliverables:**
- `SessionAssign`: vsockhttp delivery to a live VM without the destroy tail; per-vm serialization guard (one in-flight; concurrent calls `FAILED_PRECONDITION`, the control plane serializes anyway).
- `Bank`: Firecracker pause -> full snapshot bundle (memory file + rootfs state, the same self-contained bundle format the R1 hydration work produced for bases) written to the NVMe snapshot dir under a `sessions/` prefix -> VM destroy -> `{snapshot_ref, size_bytes}`.
- `Relight`: restore from a session bundle with the existing restore path (150ms WaitReady attempts, 2s RestoreReadyTimeout), plus a best-effort guest clock resync: after ready, POST wall-clock epoch ms to `/shim/clock` on the guest; a 404 (guest without the endpoint) is skipped and logged, never an error.
- `EvictSnapshot`: bundle deletion with the in-use guard.
- `NodeStatus` reports session VMs, snapshot inventory (scanned from disk on start, maintained in memory), and snapshot-dir disk usage.
- Session VM bookkeeping: session VMs are excluded from `primed_vm_ids` (they must never be adopted into the task pool) and included in `live_vms`.

**Specification:**
- The daemon remains stateless across restarts except disk: on start it rescans the sessions snapshot dir and reports what it finds (adoption source of truth). A daemon restart kills live session VMs (Firecracker children die with it); those sessions' last banked snapshot, if any, remains restorable, and the control plane resolves each affected session to `banked` (snapshot exists) or `failed` (never banked) from the post-restart inventory.
- Snapshot dir permissions 0700, owned by the daemon user; bundles are never world-readable (they contain a principal's memory image).
- Bank while an assign is in flight is refused; the control plane's session process guarantees ordering, the daemon guard is a backstop.
- Go tests: fake-driver coverage for assign-survives, bank-produces-restorable-ref, relight-round-trip (state file persistence proven by a marker written pre-bank and read post-relight in the fake), evict guards, inventory rescan on restart, primed/session pool separation.

**Acceptance:** CI green; on the deployed noded, a grpcurl sequence (Prime a sandbox VM as a stand-in, SessionAssign, Bank, Relight, SessionAssign, Destroy) round-trips with state observable across the bank (documented in the PR description with timings).

**Commit:** `feat(embervm): session lifecycle verbs in noded (assign, bank, relight, evict)`

### Task 5: SessionStore, lifecycle FSM, and per-session tokens

**Why:** The durable identity and the auth capability. The FSM is data, like the task FSM, and the token is the "who may hit this session" surface ADR 001 names as distinct from management auth.

**Deliverables:**
- `Embervm.SessionStore`: ETS hot set (sessions by id, residency facts, per-workload counts) rebuilt from the op-log projection on boot; write-through transitions.
- FSM as an explicit transition table. States: `creating -> running -> banking -> banked -> relighting -> running ...`; terminal: `expired, evicted, destroyed, failed`, each with a recorded reason. Illegal transitions raise.
- Tokens: 32 random bytes, urlsafe, minted at create, returned exactly once in the create response, stored as sha256, verified constant-time. Valid until the session is terminal (bounded by `maxLifetimeSeconds`, which satisfies "short-lived, minted at create"); re-mint-on-relight is a recorded follow-on, not v1 (a token that dies mid-conversation whenever the idle timer fires would make every consumer carry re-auth logic for zero threat-model gain at this trust tier).
- Session ids: `s-` + 26-char ULID.

**Specification:**
- A token authenticates exactly one session id: verification looks up the session BY id from the URL and compares hashes; possession of a token for S grants nothing on S' (test).
- The token never enters the guest, MMDS, initEnv, or any snapshot: it exists only as a hash in the control plane's store, so a compromised or exfiltrated guest image cannot leak it.
- Lineage invariant enforced structurally: `Relight` is only ever called with `sessions.snapshot_ref` for that session row, and the row carries the principal; there is no code path that passes one session's ref into another's relight (property test over the manager's call surface, plus a reviewer check).
- ExUnit: exhaustive transition-table test, token mint/verify/reject (wrong session, tampered, terminal session), boot rebuild equivalence.

**Acceptance:** CI green.

**Commit:** `feat(embervm): session store, lifecycle fsm, and per-session tokens`

### Task 6: SessionManager, create-from-primed-pool, and the minimal API

**Why:** The lifecycle brain plus the caller surface for the happy path (create, invoke a running session, destroy). Bank/relight routing follows in Phase 2 so PR-3 stays reviewable.

**Deliverables:**
- One supervised process per live session (`Embervm.Session` under a DynamicSupervisor): owns the FIFO invoke queue (cap `invokeQueueCap`), the idle timer (armed in Task 7), and all daemon calls for its VM. Banked sessions have no process; a row is enough.
- Create: reserve capacity (`maxSessions`, `cap`, quota fail-closed), claim a primed pristine VM from the existing pool inventory for the workload (principal-bound at claim, exactly the task-class assignment moment) or `Prime` on pool miss; append `session_created`; return `{session_id, session_token, expires_at, base_digest}`.
- API routes on the existing router, front-end only (no placement logic in the router):
  - `POST /v1/workloads/{name}/sessions` (management auth: TokenReview + allow-list) -> 201 create response.
  - `POST /v1/sessions/{id}/invoke` (session token auth only) -> proxies method/path/headers/body under the task-envelope header rules (`X-Ember-Guest-*` allow-list, 8 MiB cap) via `SessionAssign`; returns the guest response verbatim including headers (the R1 sync-wait header carry applies).
  - `GET /v1/sessions/{id}` (management or session token) -> state, generation, base digest, timestamps, expires_at.
  - `DELETE /v1/sessions/{id}` (management auth) -> destroy (VM destroyed and/or snapshot evicted, `session_destroyed`).
  - `GET /v1/workloads/{name}/sessions` (management auth) -> paged listing.
- OpenAPI additions in `projects/embervm/docs/api.yaml` (the contract for the Task 10 monolith client).

**Specification:**
- Create denials (session cap, workload cap, quota, missing capacity facts) are structured 429/403 with distinguishable reasons and audit appends per the D12.2 cadence rules.
- Invoke on a `creating`/`banking`/`relighting` session parks the caller (BEAM process, capped by the existing per-principal park cap); on a terminal session returns `410 {reason: expired|evicted|destroyed|failed}`.
- A `DEADLINE_EXCEEDED` or transport error on `SessionAssign` marks the session `failed` and destroys the VM (a guest in an unknown mid-request state must not accrete further state silently); the caller receives the error.
- ExUnit: create happy path + each denial, invoke round-trip against fakenode, queue-cap 429, token-gated invoke (management token alone is rejected on invoke), destroy from every non-terminal state.

**Acceptance:** CI green; live in-cluster: create a session on a hand-authored echo session Workload, invoke it twice (second invoke observes state from the first via the echo guest's counter or file), destroy it; documented with timings in the PR description.

**Commit:** `feat(embervm): session manager, create from primed pool, and session api`

---

## Phase 2: Idle-bank, relight routing, placement seam, adoption

### Task 7: Idle-bank policy, expiry, GC, and capacity eviction

**Why:** Banking is the rung's headline economics: an idle session must release its VM (live capacity) and hold only disk.

**Deliverables:**
- Idle timer per live session: `idleBankSeconds` with zero in-flight and zero queued invokes triggers `Bank`; on success append `session_banked` (generation+1), stop the session process. An invoke arriving mid-bank parks and triggers relight after the bank completes (no cancel path; banking is short and cancellation races are not worth it).
- Max-lifetime expiry: a sweeper (riding the Compactor cadence seam, own timer) expires sessions past `expires_at`: live -> destroy VM; banked -> `EvictSnapshot`; append `session_expired`. Invoke-time check as well, so expiry never depends on sweep cadence (ADR 002 rule 1 applied to sessions).
- Banked-TTL GC: banked sessions untouched for `bankedTtlSeconds` are evicted (`session_evicted`, reason `idle_ttl`).
- Capacity eviction: when the node's `snapshot_disk_free_bytes` crosses a values-configured low watermark, evict banked sessions LRU by `last_invoke_at` (never live ones, never non-session snapshots) until above the watermark, each an audited `session_evicted` with reason `disk_pressure`. Fail-closed on missing disk facts: no NEW banks are initiated (sessions stay live) rather than banking onto a possibly-full disk.
- Per-node concurrent-bank cap (default 1): banking writes GiBs; serialize per node like base builds.

**Specification:**
- Bank failure (daemon error) leaves the session `running` with the timer re-armed and a warn log; three consecutive failures mark it `failed` and destroy the VM (a session that cannot bank must not squat live capacity forever).
- All timers clock-injected for tests.
- ExUnit: idle-bank fires only when quiescent; invoke-during-bank parks and relights; expiry from both live and banked; LRU eviction picks the right victims and stops at the watermark; the three-strikes bank failure path.

**Acceptance:** CI green; live: a session left idle past `idleBankSeconds` shows `banked` in `GET /v1/sessions/{id}` and the VM count drops (kubectl + `/v1/nodes` facts).

**Commit:** `feat(embervm): idle-bank, session expiry, and snapshot eviction policy`

### Task 8: Relight-on-invoke routing, placement module, and restart adoption

**Why:** The relight path is the invocation front-end/placement split exercised for real, and adoption is the drill lesson applied before it can bite again.

**Deliverables:**
- `Embervm.SessionPlacement`: `node_for_create/1` (rendezvous hash of session id over ready nodes; v1 trivially one node, the interface takes the registry's node list so multi-node is data), `node_for_relight/1` (the residency fact from the session row + `NodeStatus` inventory; a session relights ONLY on a node reporting its snapshot). The router and SessionManager call this module and never inspect node facts directly (reviewer-checked boundary).
- Relight-on-invoke: invoke on a `banked` session transitions to `relighting`, parks the caller, `Relight` on the resident node, restarts the session process, appends `session_relit` with `relight_ms`, then drains the queue. Wake-rate limits per principal (values-configured, default 30 relights/min) guard the asymmetric-cost miss path (ADR 001 security section); excess relight-triggering invokes get 429 without touching the node.
- Restart adoption: on control-plane boot (and every registry sweep), reconcile `session_vms` and `session_snapshots` from `NodeStatus` against the projection: rebind live session VMs to fresh session processes (by the daemon-echoed `session_id`), heal `banking`/`relighting` limbo states from what the node actually holds, mark sessions whose VM and snapshot both vanished as `failed`, and evict orphaned snapshots whose session row is terminal or absent.
- Unrestorable snapshot (`Relight` FAILED_PRECONDITION): session -> `failed` (reason `snapshot_lost`), snapshot evicted, parked callers get 410 (standing decision 3: loud, never a silent blank VM).

**Specification:**
- The relight sequence must be crash-consistent: `session_relit` appends only after the daemon returns a live vm_id; a crash in between is healed by adoption (the node reports either the snapshot or the VM, both never disappear silently).
- ExUnit: relight round-trip with fakenode (parked invoke served after relight), wake-rate 429, adoption matrix (restart during running / banking / banked / relighting, each converging to the correct state), snapshot-lost path, placement-boundary test (router module has no reference to node facts; enforced by a compile-time boundary check or a reviewer gate).

**Acceptance:** CI green; live: invoke a banked session and get the pre-bank state back (measured relight latency in the PR description); restart the control plane with one live and one banked session and verify both are adopted and invokable (the mini adoption drill, numbers recorded).

**Commit:** `feat(embervm): relight-on-invoke routing, placement seam, and session adoption`

### Task 9: Session observability and disk watermark alert

**Why:** Bank/relight are new latency phases and disk is a new exhaustion axis; both must be visible before a consumer depends on them (ADR 002 rule 4; the R0 "guilty phase must never be uninstrumented" rule).

**Deliverables:**
- Spans: session root span per invoke with children `auth`, `queue_wait`, `relight` (with `ember.relight_ms`, `ember.generation`), `guest_exec`; lifecycle spans for `bank` (with `ember.snapshot_bytes`) and `evict`. Attributes: `ember.session_id`, `ember.workload`, `ember.principal`, `ember.session_state`, `ember.pool_hit` on create.
- Structured logs for every lifecycle transition (info) and every denial/eviction (warn).
- SigNoz alert on the noded snapshot-dir usage watermark (80%), warn level, homelab channel, via the signoz-alerts registration seam; alert body names the levers (`bankedTtlSeconds`, `maxSessions`, disk watermark values).
- `status.sessions` counts (Task 3) wired: the control plane writes live/banked counts per session workload on a debounced interval.

**Specification:** The Task 11 gate numbers (relight p95, bank p95, state-persistence proof) must be derivable from spans alone. Alert follows the METRIC_BASED_ALERT registration pattern; threshold-0 dry-run then restore, as in R1 Task 2.

**Acceptance:** CI green; spans visible in SigNoz from a live bank/relight cycle; dry-run alert reaches Discord.

**Commit:** `feat(embervm): session tracing, status counts, and snapshot disk alert`

---

## Phase 3: The sandbox consumer and closure

### Task 10: Sessioned `run_python` (ADR 044 sandbox on session class)

**Why:** The rung's named first consumer: an agent's `run_python` today re-executes from zero every snippet; with a session, variables, files, and warm imports accrete across turns.

**Deliverables:**
- Guest: the sandbox guest-init handler gains a kernel mode. When a request carries `{"mode": "session"}`, the handler lazily starts one persistent `python3` child running a small exec-loop (length-prefixed frames on stdio: code in; stdout/stderr/exit and changed-files manifest out), executing each snippet in a shared module namespace under a stable `/tmp/session` workdir. Existing one-shot behavior is byte-identical when the field is absent (the shared image keeps serving the task class and the deprecated fc-invoke path unchanged; the guest-init tests prove both modes). The shim also gains the optional `POST /shim/clock` endpoint (Task 4's resync target): set the guest wall clock from the posted epoch ms; documented as best-effort.
- Workload: a `sandbox-session` Workload CR in the embervm chart: `class: session`, same guest image and digest pin as `sandbox`, 1 vcpu / 2048 MiB, `concurrency {floor 1, cap 4}`, `session {idleBankSeconds 120, maxLifetimeSeconds 21600, maxSessions 8, bankedTtlSeconds 21600}`. Values arithmetic for node-4 (live cap + snapshot disk worst case = `maxSessions x 2 GiB`) documented in values comments.
- Monolith: `projects/monolith/sandbox/client.py` gains `create_session() / invoke_session(handle, code, files) / close_session(handle)` against the Task 6 API; a small Postgres table `sandbox.session(handle TEXT PRIMARY KEY, session_id, token, created_at, expires_at)` maps caller handles to session credentials (tokens are secrets: column comment says so, table is private-tier only). The `run_python` MCP tool and the concierge PydanticAI tool gain an optional `session` handle parameter: absent = one-shot task class exactly as today; present = create-on-first-use, reuse thereafter, transparent re-create on 410 with a `session_reset: true` flag in the response so the model knows state was lost.
- Monolith RBAC/auth: no new K8s verbs (HTTP to EmberVM only); the monolith SA is already on the allow-list.

**Specification:**
- The exec-loop child inherits the existing drop-privileges, output-cap, and wall-clock-timeout mechanics per snippet; a snippet timeout kills and restarts the child (namespace lost, reported as `session_reset` in that response) rather than wedging the loop.
- Tool descriptions document the state model honestly: state persists best-effort across turns, may reset on timeout/eviction/expiry, and the guest has no network.
- pytest (respx fakes): one-shot unchanged, session create-reuse-close, 410 re-create flag, token never logged. Go guest tests: both modes, frame protocol, timeout-restart. Hand-register new `py_test` targets.

**Acceptance:** CI green; live on the private tier: two `run_python` calls with the same session handle where call 2 reads a variable and a file defined in call 1, with an idle-bank + relight forced between them (numbers in the PR description).

**Commit:** `feat(monolith): sessioned run_python on the embervm session class`

### Task 11: R2 gates and closure

**Specification (the gates, all measured, appended to this plan as a Closure section):**
1. **State persistence across bank/relight:** define a variable and write a file in invoke 1; force idle-bank (wait out `idleBankSeconds`, verify `banked`); invoke 2 reads both back. 10/10 cycles.
2. **Relight latency:** invoke-on-banked added latency (park to first guest byte) p95 <= 500ms over 20 cycles on the 2 GiB sandbox-session snapshot; live-session invoke overhead p95 <= 25ms (parity with primed-hit dispatch).
3. **Bank behavior:** bank completes p95 <= 3s and never blocks an unrelated workload's dispatch (spans show no cross-workload stall during a bank).
4. **Adoption drill:** restart the control plane with >= 1 live and >= 1 banked session; both invokable afterward with state intact; zero orphaned VMs or snapshots in `NodeStatus` after one sweep. Then restart noded with a banked session: the session survives as `banked` and relights.
5. **Version convergence:** bump the sandbox guest digest; an existing session keeps returning its birth `base_digest` and its state; after its `maxLifetimeSeconds` (shortened via values for the drill) it 410s and a fresh session reports the new digest; the superseded base is evicted once refs drain.
6. **Isolation and tokens:** a token for session A on session B's invoke URL is 403; two sessions of different principals never share a snapshot ref or base lineage row (op-log audit query); a banked snapshot file is unreadable off-daemon (permissions check).
7. **Eviction honesty:** drive snapshot disk past the watermark with throwaway sessions; LRU eviction fires, the alert fires, and an evicted session's next invoke is a clean 410 with reason `evicted`.

**Deliverables:** Gate numbers in the Closure section; ADR embervm/001 roadmap row R2 -> `Shipped <date>`; ADR embervm/004 gains a status note that the session verbs shipped and the adapter gate condition 1 now holds; a deprecation note that the goosecracker agent remains the last fc-invoke consumer (its migration is the recorded follow-on).

**Commit:** `docs(embervm): R2 closure with gate evidence`

---

## Explicitly out of scope for R2 (recorded, not dropped)

- **Snapshot durability off-node:** write-through/export of session snapshots to the object store and cross-node relight (ADR 003 `ExportBase`/`RestoreBase` generalized to sessions). v1 sessions die with their node (standing decision 3); the lineage schema and `EvictSnapshot` verb are the held seam.
- **Diff snapshots and multi-level tiering:** full snapshots only in v1; `generation`/`parent_base_ref` metadata makes diff chains additive later.
- **Per-session Envoy endpoints / xDS publication** (R3): the control plane proxies all session traffic in R2; endpoints are recorded facts.
- **The agent-sandbox adapter** (ADR 004): still gated on upstream traction; R2 only ships the verbs it needs.
- **Goosecracker agent migration off fc-invoke:** the agent needs egress, repo hydration, and a fatter guest; the session verbs suffice for it, and it is the natural R2.x follow-on, but it is not this rung's consumer.
- **Egress-capable sessions** (ADR agents/023 integration), token rotation/re-mint on relight, session transfer between principals (never), weighted session admission, a pre-created session warm pool (`SandboxWarmPool` analog: the primed pristine pool already covers create latency; a pool of PRINCIPAL-BOUND idle sessions is a consumer-side pattern, not a platform primitive in v1).
- **Session invoke idempotency / retries / result store:** at-most-once by decision, not omission (standing decision 2).
- **Multi-node placement logic:** `SessionPlacement` is a seam with one node behind it; rendezvous hashing activates with the fleet.

## Open risks tracked for execution

| Risk | Watch signal | Fallback |
| ---- | ------------ | -------- |
| Full-snapshot disk footprint (memMib per bank) squeezes node-4 NVMe | Task 9 watermark alert; `snapshot_disk_used_bytes` trend | Shrink `maxSessions`/`bankedTtlSeconds` via values; diff snapshots are the recorded follow-on; eviction is the backstop |
| Bank latency (writing ~2 GiB) stalls the daemon or co-tenant dispatch | Task 11 gate 3 spans | Per-node concurrent-bank cap already 1; move bank I/O to a lower-priority writer; bank less eagerly (raise idleBankSeconds) |
| Relight of a full 2 GiB snapshot misses the 500ms p95 | Task 8 live timings | Budget honesty first (raise the gate with evidence); mmap/lazy-page restore tuning in the driver; smaller sandbox-session memMib |
| Restored guest wall-clock skew breaks time-dependent agent code | Gate 1 drill; sandbox user reports | `/shim/clock` resync is best-effort in v1; if insufficient, add a guest-init settimeofday on a vsock signal as a required relight step |
| Adoption edge cases wedge sessions after restarts (the R0 drill precedent) | Task 8 adoption matrix + gate 4 drills | The node is the source of truth; any limbo state resolves from `NodeStatus` inventory; worst case a session is marked failed loudly, never silently blank |
| Session VM + banked counts interact badly with task-class pool arithmetic on node-4 | Task 10 values arithmetic; `max_live_vms` saturation denials | Session cap 4 and floor 1 are small; rebalance caps in values like R0 Task 14 |
| Exec-loop kernel mode destabilizes the shared sandbox guest for the task class | Guest-init dual-mode tests; task-path canary after deploy | Mode is opt-in per request; one-shot path is untouched code; revert is a guest digest pin rollback |
| Superseded-base retention (refcounting) accumulates bases across frequent deploys | Base count in NodeStatus; disk trend | Bounded by maxLifetimeSeconds by construction; shorten the sandbox-session TTL via values if deploy cadence outpaces it |

---

## Closure (2026-07-15)

R2 is **Shipped**: all eleven tasks landed across six implementation PRs
(#3544-#3549) plus this closure, every PR CI-green, and the `sandbox-session`
session-class workload is deployed and `Ready`
in the `embervm` namespace (`kubectl get workloads -n embervm` shows
`sandbox-session   session   True   sandbox-session__0816bcfcea90   0 live / 0 banked`:
the session base built, was adopted by noded on start, and reports the live/banked
session inventory the projection was designed to surface). The full substrate is in
production: create-from-primed-pool, per-session tokens, persistent-kernel invokes,
idle-bank, relight-on-invoke, restart adoption, LRU eviction, and OTel spans.

**Gate evidence.** The plan's seven gates split into what CI mechanically proves on
every push and what requires a live functional drill in a production pod. The
mechanism behind each gate is CI-verified; the end-to-end functional numbers (2, 3)
and the destructive operational drills (4, 5, 7) are the recorded follow-on below,
because running them means minting real session tokens from inside the monolith
backend pod and driving prod snapshot disk, which is a prod-exec action held for
Joe's review per the standing "flag for me post-implementation" directive.

| Gate | Mechanism (CI-verified) | Live status |
| ---- | ----------------------- | ----------- |
| 1. State persistence across bank/relight | Guest persistent-kernel dual-mode tests (`firecracker/sandbox/guest-init`, one-shot byte-identical + session-namespace reuse); op-log ETF-blob durability round-trip; `SnapshotSession`/`RestoreSession` Go round-trip (`TestBankRelightRoundTrip`); SessionStore write-through + boot-rebuild | Infra Ready; end-to-end 10/10 drill = follow-on (prod-pod exec) |
| 2. Relight latency (p95 <= 500ms; live-invoke overhead p95 <= 25ms) | `relight` and `queue_wait` spans emitted (Task 9), derivable from SigNoz; dispatch overhead shares the primed-hit path | 20-cycle p95 measurement = follow-on (needs live invoke load) |
| 3. Bank behavior (p95 <= 3s; no cross-workload stall) | Per-node concurrent-bank cap = 1 enforced in dispatcher; `bank` span emitted; bank path is the reused driver snapshot mechanics | Live span harvest = follow-on |
| 4. Adoption drill (control-plane + noded restart, state intact, zero orphans) | Reconcile-from-`NodeStatus` unit coverage (`adopt_one` skips in-flight banking/relighting; rebind + heal-limbo; never-reap-on-disconnect, the #3517 lesson encoded); noded rescans sessions dir on start | Destructive restart drill = follow-on |
| 5. Version convergence (session rides birth base; 410 after TTL; superseded base evicted) | BaseBuilder TTL-bounded refcount (`turned_over?` strict-boolean fix); snapshot-lost -> `failed` + 410 path; `EvictSnapshot` verb landed | TTL-shortened drill = follow-on |
| 6. Isolation and tokens (cross-session 403; no shared lineage; snapshot unreadable off-daemon) | Router token-scope tests (session token authenticates only its own id; management token rejected on invoke; `:crypto.hash_equals` constant-time `verify_token`, every return shape matched after the CaseClauseError fix); per-principal snapshot refs in the op-log lineage schema; snapshot files daemon-owned | Cross-token 403 spot-check = follow-on (cheap; bundle with gate 1) |
| 7. Eviction honesty (watermark -> LRU -> alert -> clean 410 `evicted`) | LRU eviction fail-closed on missing disk facts, watermark-scoped; SigNoz `embervm-snapshot-disk-usage` METRIC_BASED_ALERT from hostmetrics `system.filesystem.usage`; evicted-session invoke returns 410 reason `evicted` | Disk-pressure drill = follow-on (destructive) |

**The live functional gate drill is the one open R2 item, flagged for Joe.** It is a
single ~30-minute session against the deployed stack (create a session via the
monolith `sandbox.client`, prove state survives a forced idle-bank/relight, measure
relight p95 over 20 cycles, restart the control plane with a banked session, and
push snapshot disk past the watermark). It needs prod-pod exec, so it is held for
Joe rather than run autonomously. Nothing in the drill is expected to fail: every
mechanism it exercises is already CI-green, and the infra is Ready. If a gate misses
its number (relight p95 on the 2 GiB snapshot is the likeliest), the plan's Open
Risks table already records the tuning fallbacks (budget honesty first, then
lazy-page restore or a smaller `memMib`).

**Follow-ons recorded, not dropped:**
- The live functional gate drill above (needs Joe-approved prod-pod exec).
- **Goosecracker off fc-invoke:** the goosecracker agent remains the **last
  fc-invoke consumer**. The session verbs suffice for it (it needs egress, repo
  hydration, and a fatter guest, all additive), so its migration onto the
  session class is the natural R2.x follow-on and the trigger to retire the
  fc-invoke substrate. Not this rung's consumer, per the plan's out-of-scope list.
- The ADR 004 agent-sandbox adapter stays gated on condition 2 (upstream traction);
  condition 1 (R2 exists) now holds.
