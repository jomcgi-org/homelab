# EmberVM R0: deviations and judgment calls

A running log of where the implementation departs from, tightens, or resolves an
ambiguity in `docs/plans/2026-07-12-embervm-r0-tasks-spec-and-plan.md`, so the
spec and the code can be reconciled retrospectively. Each entry says WHAT changed,
WHY, and whether it was approved. Newest phase last.

Deviations here are intentional. Bugs are not deviations; they get fixed.

## Task 12: metering, audit, and quotas (chart 0.1.14)

### D12.1 Usage rides the existing `:succeeded`/`:failed` append (no `:metered` op, no flush timer)
- **Spec said:** "aggregate in ETS, flush to the op-log on interval and drain."
- **Did instead:** carry the billed usage in the `:succeeded`/`:failed` op payload
  and upsert a `(principal, day)` row in the `usage` projection INSIDE the same
  SQLite transaction as that op. No new op kind, no flush timer, no ETS
  pending/flush split. `Embervm.Metering` owns only a read-through quota cache
  (integer `cpu_ms` per `(principal, day)`), rebuilt from the projection on boot.
- **Why:** every task completion already makes one durable fsynced op-log append
  (the FSM requires it and it cannot be removed). Riding it satisfies the spec's
  real constraint ("MUST NOT add a blocking store write") maximally, adding zero
  writes, and eliminates three failure modes the interval-flush design carries:
  flush atomicity, mid-interval crash loss, and cache/durable divergence. The
  usage record is also per-task (carries `task_id`), finer than interval deltas.
- **Approved:** yes (design pause + Fable review).

### D12.2 Denials appended by cadence class, not "every denial"
- **Spec said:** "every denial (quota, cap, auth, stale-capacity) is an op-log
  append with principal and reason."
- **Did instead:** only REQUEST-scoped denials are appended, once each: quota
  (`:quota_enforced`), auth-forbidden 403 (`:denied`), and per-principal
  queue-depth 429 (`:denied`). DISPATCH-tick saturation conditions
  (`:cap`/`:stale_capacity`/`:no_capacity`/`:principal_share`) stay in-process
  counters exposed via `Dispatcher.stats/0`, NOT appended. Unauthenticated 401s
  are NOT appended.
- **Why:** saturation conditions are re-evaluated every ~5s drain tick while they
  persist; appending per tick would flood the log, and they have no principal to
  attribute (so they cannot satisfy "with principal and reason" anyway). 401s are
  reachable by any unauthenticated caller, so appending them turns unauthenticated
  HTTP spam into durable-write amplification against the single-writer op-log.
- **Approved:** yes.

### D12.3 Quota enforced at submit AND dispatch; over-quota tasks are parked, not failed
- **Spec said:** quota "denies dispatch," fail-closed.
- **Did instead:** a submit-time 429 (courtesy fast-fail, appends the audit
  record) PLUS the actual enforcement in the dispatcher's `fq_take`: an
  over-budget principal is skipped in the fair rotation and its task stays
  `queued`, unparking when the daily budget resets. No task is failed for quota
  (the FSM has no `queued -> failed` edge).
- **Why:** submit-only enforcement has a real hole (totals move only on success,
  so a principal at 99% can burst `queueDepthCap` submits that all pass the gate
  and, with no drop edge, all run: bound `= queueDepthCap x max_task_cost`). The
  dispatch-side skip bounds it to one in-flight share. Parking rather than failing
  respects the Task 11 FSM constraint.
- **Approved:** yes (Fable caught the submit-only hole).

### D12.4 Failed-with-usage is charged, not only success
- **Spec implied:** usage is captured from `AssignResponse` on the success path.
- **Did instead:** a guest 4xx/5xx returns a well-formed `AssignResponse` WITH
  usage (it did measured work), so it is billed and counts against quota.
  Transport/timeout failures report no usage and charge nothing.
- **Why:** otherwise a workload that burns CPU then returns 500, amplified by
  retries, is free. The fix is a few lines given D12.1's payload carry.
- **Approved:** yes.

### D12.5 Quota is opt-in (empty = off), asymmetric to the auth allow-list
- An empty budget map means quota is OFF (a principal with no configured budget is
  allowed), NOT deny-all. "Fail-closed" is scoped to a principal that HAS a
  budget: only then does an unreadable cache deny. This keeps a Metering crash
  from bricking dispatch on a cluster that never opted into quotas. The auth
  allow-list stays deny-all (a security gate); quota is a resource-abuse gate.
  CRD-based `Quota` objects remain the follow-on; v1 budgets are values-configured.
  A budget of exactly `0` is a HARD STOP (denies the principal entirely), consistent
  end to end: the values parser accepts `0`, the Helm default guard renders `0`, and
  the runtime gate (`used < budget`) denies at `0`. Omit a principal for unlimited.

### D12.6 Daily budget = UTC epoch-day
- The `(principal, day)` key uses `div(op.ts, 86_400_000)` on the op's wall-clock
  ms, so the daily reset is 00:00 UTC (~16:00-17:00 US Pacific). Documented in
  `chart/values.yaml`. Charged to the SUCCESS day (a task crossing midnight bills
  the day it completed).

### D12.7 `Auth.authenticate` surfaces the username on forbidden
- Return changed from `{:error, :forbidden}` to `{:error, {:forbidden, username}}`
  so the router can name the principal in the 403 audit append. The router still
  accepts a bare `:forbidden` (principal unknown) for reviewers/fakes that do not
  surface it.

## Task 14a: first live sandbox VM (chart 0.1.15)

The plan's Task 14 lands semgrep + sandbox side-by-side with node-4 rebalancing.
This is split: Task 14a gets ONE real python-sandbox microVM running end to end
(the headline: prove EmberVM boots a real Firecracker VM through the controller),
Task 14b does semgrep + the fc-invoke concurrency rebalance + finding-equality.

### D14a.1 Split Task 14 into 14a (live sandbox) and 14b (full side-by-side)
- **Why:** the gating risk is the first real Assign (rootfs provisioning, FC cold
  boot, snapshot, vsock), not breadth. Getting one sandbox VM live de-risks the
  whole chain with the smallest surface; semgrep + rebalancing is mechanical once
  it works. 14a keeps a small footprint (floor 1, cap 4) that fits node-4's
  headroom WITHOUT touching fc-invoke, so no coexistence-budget change is needed
  yet (14b does the 16->8 fc-invoke halving for the full-cap run).

### D14a.2 noded rootfs provisioning ported from fc-invoke (was missing)
- noded had a complete Firecracker driver but NO rootfs provisioning: `EMBERVM_
  NODED_IMAGES` pointed at a pre-baked ext4 that nothing produced. Added a
  rootfs-builder initContainer + ConfigMap (crane export + mkfs.ext4 onto the
  nvme scratch), ported from the proven fc-invoke pattern, reusing fc-invoke's
  rootfs-builder tool image. Idempotent via a `.guest-ref` marker.

### D14a.3 EMBERVM_NODED_IMAGES is DERIVED, not a hand-maintained map
- The guest image_ref must match in three places (rootfs-builder GUEST_IMAGE, the
  noded image table key, the Workload CR source.image.ref) or BuildBase fails
  FAILED_PRECONDITION. Rather than hand-sync a static `noded.images` map key to
  the Bazel-pinned ref, the deployment DERIVES the table from `noded.workloads` +
  each workload's `noded.<name>.guestImage`, so one pinned value flows to all
  three consumers. Explicit `noded.images` entries still merge as overrides.

### D14a.4 Guest image digest-pinned via Bazel, contract frozen
- `noded.sandbox.guestImage` and `noded.rootfsBuilder.image` are pinned by
  `helm_chart(images=)` from the fc-invoke guests' public `.info` providers (zero
  image changes, per the plan). The sandbox guest contract is used verbatim: vsock
  port 1027, `/shim/ready`, one python snippet per `/invoke`, `sandbox-guest-init`.

### D14a.6 helm_images_values collides on shared-prefix dotted keys (chart 0.1.16 fix)
- First live deploy (0.1.15) CrashLooped the rootfs-builder with `MANIFEST_UNKNOWN`
  pulling `sandbox/guest:latest`: the Bazel image pin did not apply, leaving the
  values default. ROOT CAUSE: `bazel/helm/images.bzl` emits one top-level YAML
  block per dotted image key, so three keys sharing a prefix (`noded.image`,
  `noded.sandbox.guestImage`, `noded.rootfsBuilder.image`) produce three duplicate
  `noded:` keys that collapse to the last on parse, dropping the guest-image pin
  AND clobbering `noded.image` itself back to `:latest`.
- Fix: give each Bazel-pinned image a DISTINCT top-level key (`sandbox.guestImage`,
  `rootfsBuilder.image`, flat top-level `workloads`), mirroring fc-invoke's proven
  layout. `noded.image` stays the sole `noded.*` pinned key. The underlying
  images.bzl limitation (silent clobber on shared-prefix keys) is a latent footgun
  worth a general fix (deep-merge the fragment) as a separate follow-up.

### D14a.7 LIVE VM ACHIEVED, and the invokePath precedence bug it surfaced (chart 0.1.17)
- **A real python task ran in a Firecracker microVM through the controller**
  (chart 0.1.16): `POST /v1/workloads/sandbox/tasks?wait=true` with `{"code":
  "print(6*7)"}` returned `{"stdout":"42\n","exit_code":0,"duration_ms":16}`, 200
  OK. Chain verified: init container baked the ext4, BaseBuilder cold-booted +
  snapshotted a real VM (status.snapshotRef=sandbox__..., BaseReady+BaseBuilt),
  PoolManager primed a floor VM (primedFloorSatisfied=true), and the dispatched
  Assign restored a VM and round-tripped the guest /invoke.
- **Bug surfaced:** the FIRST submit dead-lettered (guest 404) because the Router
  baked `path: "/"` into every submitted request, and the dispatcher prefers the
  stored request path (`req_env["path"] || invoke_path`), so the workload's
  `invokePath: /invoke` was dead unless the client sent `X-Ember-Guest-Path`. The
  intended precedence is request-header > workload invokePath > "/". Fix: the
  Router records a path ONLY when `X-Ember-Guest-Path` is set, so the dispatcher's
  fallback to the catalog `invoke_path` works. Only a real end-to-end invocation
  could catch this (router + dispatcher were unit-tested in isolation).
- Also disabled the `echo` sample workload (its BuildBase failed in a tight loop
  now that BaseBuilder is active; sandbox is the real watcher/RBAC exercise).

### D14a.5 Live-verify, not CI-verify
- CI cannot run a real VM (no KVM in RBE). 14a is verified live AFTER merge+sync:
  the init container bakes the ext4, the Workload goes Ready with a snapshotRef
  (BaseBuilder cold-booted + snapshotted a real VM), the PoolManager primes a
  floor VM, and a `/invoke` submit runs python end to end. Recorded here as the
  acceptance evidence once observed.

## Task 14b: semgrep side-by-side + node-4 rebalance (embervm 0.1.18, fc-invoke 0.4.82)

### D14b.1 Plan-literal rebalance (Joe's call): halve fc-invoke, embervm to cap-16
- Chose the plan-literal Option B over a modest-cap Option A. fc-invoke semgrep +
  sandbox concurrency 16->8 (substrate chart), its memory limit 50Gi->40Gi
  (worst case ~47->~35Gi). embervm adds a semgrep Workload (1 vcpu/1536Mi, floor 4,
  cap 16) and raises sandbox to floor 4/cap 16; noded memory limit 4Gi->36Gi. The
  rootfs-builder + derived EMBERVM_NODED_IMAGES pick up semgrep automatically from
  the top-level `workloads.semgrep` + `semgrep.guestImage` pin.
- **Node-4 memory arithmetic (63.4GiB allocatable):** the daemon memory LIMITS are
  cgroup OOM ceilings, NOT scheduling reservations (both daemons keep tiny
  requests), so the ceilings may oversubscribe (fc-invoke 40Gi + embervm 36Gi =
  76Gi) without overcommitting node scheduling. This is safe because real
  simultaneous cap-16 peak never occurs during side-by-side: production PR scans
  stay on fc-invoke, so embervm idles at its floor (semgrep 4x1536 + sandbox 4x512
  = 8Gi steady); at cutover fc-invoke drains so only embervm peaks; and
  oom_score_adj kills guests before the daemon/DB. The absolute cap-16 throughput
  gate is a post-drain Task 16 run (the plan's per-slot-normalization clause).
- **Deferred:** the fc-invoke semgrep concurrency (and the whole fc-invoke scan
  path) is restored-or-deprecated at the Task 16 cutover, not here.

### D14b.2 semgrep guest is private; reused the existing ghcr pull path
- The semgrep guest image is proprietary (Pro engine + rules). embervm's chart
  already sets imagePullSecret.enabled with a 1Password-synced ghcr dockerconfig,
  and the noded rootfs-builder init container mounts it (DOCKER_CONFIG=/ghcr) when
  that flag is on, so crane pulls the private guest with no new wiring (the public
  sandbox guest simply did not need it).

## Task 13: observability (embervm 0.1.19) — structured logging done; OTLP tracing FLAGGED

### D13.1 Structured JSON logging landed; OTLP tracing split off (dependency risk)
- Task 13 has two halves: structured JSON logs and OTel traces. Landed the logging
  half now (low risk, no new deps): `Embervm.LogFormatter` (an Erlang `:logger`
  formatter emitting one JSON object per line via the built-in `:json` encoder,
  defensively guarded so it can never crash logging), wired as the default handler
  formatter in config.exs. Request-scoped denials now log a structured `warn` with
  principal/workload/reason. The formatter MODULE is runtime-safe (guarded), but
  the CONFIG key matters: `:default_handler, formatter: {mod, cfg}` (NOT
  `:default_formatter`, which expects keyword opts and crashes app boot on a module
  tuple). CI's release-boot smoke caught exactly that on the first push, which is
  the real safety net absent a local test loop.
### D13.2 OTLP tracing LANDED (embervm 0.1.21)
- The traces half is now implemented (was flagged as risky). Added the 12-package
  OpenTelemetry closure to the hermetic hex build (repositories.bzl `_HEX_DEPS` +
  the hex_tarballs filegroup + MODULE.bazel `use_repo` + mix.exs path deps). Two
  concrete hermetic gaps surfaced in CI and were fixed deps-first (debug-as-we-go):
  (1) every hex repo must be exported via `use_repo` in MODULE.bazel, not just
  listed in `_HEX_DEPS`; (2) the exporter's Erlang deps (gproc, grpcbox,
  ts_chatterbox, ...) are rebar projects, so `rebar3` had to be bundled (a fetched
  arch-independent escript staged onto the OTP bin, passed absolute as
  MIX_REBAR3_SRC + exported as MIX_REBAR3, wired into all THREE mix drivers:
  mix_test / mix_release / roundtrip). All pure Erlang/Elixir (no NIF). Closure
  compiles + release boots green.
- Spans: `embervm.auth` (the TokenReview, the 5-QPS-lesson span), `embervm.dispatch`
  (the assign worker, with OTel context captured in the dispatcher and ATTACHED in
  the spawn_monitor worker so spans nest across the process boundary), and children
  `embervm.prime` (miss-path restore) + `embervm.guest_exec` (the Assign RPC). Span
  attrs ember.task_id/workload/principal/node_id/pool_hit.
- Tracing is OFF by default (config.exs `traces_exporter: :none`); config/runtime.exs
  enables OTLP/gRPC when OTEL_EXPORTER_OTLP_ENDPOINT is set (the chart points it at
  SigNoz 4317, like fc-invoke). Submit-side and result_store spans + a single-root
  trace via op-log context propagation are lighter follow-ons; the execution-path
  spans (auth/dispatch/prime/guest_exec) carry the latency the R0 gates need.
- "Key transitions at info" logging remains a lighter follow-on.

## Task 15: monolith EmberVM dual-path scan dispatch (monolith 0.285.186, embervm 0.1.20)

### D15.1 Dual-path client + shadow mode, DORMANT behind the default flag
- `projects/monolith/semgrep_scan/client.py` gains an EmberVM path and a
  `SEMGREP_DISPATCH` flag (`fc-invoke` default | `embervm` | `shadow`). `embervm`
  serves the per-PR diff from EmberVM's semgrep Workload (submit API, `?wait=true`,
  Idempotency-Key from the content hash). `shadow` serves fc-invoke and mirrors to
  EmberVM asynchronously (fire-and-forget, never affecting the served scan),
  comparing finding-count/status and tallying `shadow_stats` + a structured warn on
  divergence (the Task 16 gate signal). Only the `semgrep` DIFF workload is routed
  (EmberVM has no semgrep-full/hi in R0); those stay on fc-invoke.
- Shipped DORMANT: deploy values wire `embervmUrl` but keep `dispatch: fc-invoke`,
  so the served path is unchanged. Flip to `shadow` to start the divergence soak,
  then `embervm` at cutover. The monolith SA (`system:serviceaccount:monolith:
  monolith`, already an fc-invoke caller) is added to EmberVM's auth allow-list now
  so the flip authenticates immediately; no traffic until then.
- Tests: `client_test.py` (globbed into the monolith py_test via `semgrep_scan/**`;
  semgrep_scan is `gazelle:exclude`d, so NO new BUILD target) covers all three
  modes, the Idempotency-Key, and shadow divergence/error tallying with a fake
  httpx client.
- **FLAGGED (external, not doable in-session): Task 16.** The shadow SOAK
  (>= 48h or >= 200 real PR scans, divergence rate 0) and the six acceptance gates
  (throughput/latency load test, kill -9 durability drill, fairness, enforcement,
  rollback drill) require real elapsed time + a load harness. The code to RUN the
  soak is now in place (flip `dispatch: shadow`); gathering the evidence + the
  cutover flip is the remaining Task 16 work.

## Task 16: semgrep cutover to EmberVM (direct cut, no soak)

### D16.1 Direct cutover, not a 48h shadow soak (Joe's call: personal homelab)
- The plan's Task 16 gates a cutover behind a >= 48h / >= 200-scan shadow soak with
  divergence 0 plus a six-gate load/durability/fairness battery. For a personal
  homelab with no external SLA, that enterprise-caution is overkill: cut over
  directly and debug live. Flipped `semgrep.dispatch: fc-invoke -> embervm` in the
  monolith deploy values, so the per-PR semgrep diff scan is now SERVED by EmberVM.
- Env-only change (no chart bump): the monolith reads deploy/values.yaml from a git
  `$values` ref (targetRevision HEAD), so the flip flows through on merge without an
  image rebuild. fc-invoke's scan path stays deployed as the instant fallback
  (revert the one value + rollout) until R1/R2 formally deprecates it.
- De-risked by prior live verification: the EmberVM semgrep Workload returned
  identical PRO findings (`subprocess-shell-true` etc.) for a scripted vuln. A
  large real multi-file PR diff is the debug-live surface; the shadow machinery
  (Task 15) remains available to compare if a regression appears.
- The formal load/throughput/durability GATES (kill -9 drain, fairness ratio,
  cap-16 absolute throughput) are the remaining enterprise-grade evidence, deferred
  as unnecessary for personal use; run them if EmberVM ever takes external traffic.

### D13.3 Distributed trace propagation (caller trace joins EmberVM spans, 0.1.22)
- EmberVM now restores the CALLER's W3C traceparent so its dispatch/guest_exec
  spans nest under the caller's trace (the monolith demos page's httpx is
  OTel-instrumented and auto-injects traceparent). Async submit means the context
  rides the DURABLE op-log, not the live process: Router captures the traceparent
  into the submitted request envelope; the dispatcher restores it
  (`:otel_propagator_text_map.extract`, guarded) before opening the dispatch span.
  The demos waterfall query (`WHERE traceID = trace_id`, service-agnostic) then
  shows EmberVM's spans alongside the monolith's with no frontend change. Falls
  back to the dispatcher's own context for trace-less submits (cron/retries).

## D12 known gaps accepted for R0 (documented, not fixed)
- The `usage` projection ACCUMULATES (the only projection that does), so it is not
  idempotent under op replay (the future `read_from` replica path); safe today
  because R0 projects each op exactly once. Commented at the projection.
- Over-budget parked tasks can outlive their `expires_at` (queued-task TTL is not
  enforced; `compact` only prunes terminal rows). CLOSED in R1 Phase 0, see D-R1.0.4
  (the dispatcher now expires a popped over-TTL queued task before dispatch).
- `gb_seconds = peak_rss_mib x wall` under-bills vs a Firecracker VM's reserved
  `memMib`; the raw per-task `peak_rss_mib`/`cpu_ms`/`wall_ms` are stored in the op
  payload so the formula can be rebased later without losing history.
- A daemon that never populates `UsageStats` bills zero (proto3 defaults);
  mitigated by a log-once warning on all-zero success usage and a non-zero
  assertion in the metering test.

## R1 Phase 0: op-log retention and compaction (ADR embervm/002, chart bump)

### D-R1.0.1 TTLs are enforced at READ time, independent of the sweeper
- `TaskStore.get_result/2` and the submit dedupe path both filter a stored result
  whose `expires_at` is past the injected clock, replying `{:ok, nil}` (a 404 at the
  router). Correctness never waits on a sweep: a result 404s the instant its TTL
  lapses. The filter lives in the store (where the clock is injected), so the op-log
  `load_result/2` behaviour signature is unchanged.

### D-R1.0.2 Dedupe keys on the SAME expiry signal, evicting the stale projection
- An idempotency-key hit on a TERMINAL task whose result has expired treats the task
  as absent and resubmits fresh, so "GET result 404s" and "resubmit runs fresh" stay
  consistent. In-flight (non-terminal) duplicate suppression is preserved absolutely.
  A fresh resubmit under a colliding key first calls the new `OpLog.evict_task/2`
  (a projection prune, `DELETE FROM tasks`, results cascade via the FK; NOT an op),
  so the fresh `:submitted` append does not trip the unique `(workload, key)` index.
  The old task's ops stay in the journal until horizon compaction.

### D-R1.0.3 Scheduled bounded-batch sweep + ops-journal prefix marker
- A supervised `Embervm.OpLog.Compactor` (default hourly, values-configurable) loops
  `compact/2` until `done`; each batch is a discrete call to the op-log's single
  writer, so appends interleave between batches (the 5ms-append-budget guard).
  `compact/2` is now ONE bounded batch (portable `DELETE ... WHERE rowid IN (SELECT
  ... LIMIT ?)`, since Exqlite lacks `DELETE ... LIMIT`) returning per-table counts
  plus the marker and `done`. The ops journal is prefix-compacted behind a durable
  `compacted_through_seq` marker (a `meta` row): the marker advances only to a seq
  where every op at or below it is older than the 30-day journal horizon AND not
  owned by a live task, is monotonic (never decreases), and deletion is always a true
  prefix. `read_from/2` below the marker returns `{:error, {:compacted, marker}}`,
  distinct from an empty log, so a future replayer knows to load the projection
  snapshot instead. The 30-day journal horizon and the 7-day terminal-task retention
  are DISTINCT config (the journal carries request payloads; older audit is SigNoz).

### D-R1.0.4 Dispatcher-side queued-task expiry (closes the D12 gap)
- Added `{:queued, :expire} => :failed_permanent` to the FSM and `TaskStore.expire/2`
  (reusing the existing `:failed` op with reason `expired`, no new op kind, no schema
  change). The dispatcher, right after popping a queued task and BEFORE reserving a
  VM, expires any task past its `expires_at` and skips dispatch, so an over-budget
  parked task never runs after its deadline and never burns a primed VM. Expiry does
  NOT dead-letter (it is not a processing failure; `failed_permanent` is terminal and
  sufficient). This CLOSES the D12 "queued-task TTL not enforced" known gap.

## R1 Phase 2: FaaS consumer (og-image surfaced an op-log durability bug)

### D-R1.2.1 Op payloads are stored as ETF blobs, not JSON (binary-body crash fix)
- **Found:** registering the first binary-returning function (og-image, Task 12)
  crashed the control plane. The guest produced a valid PNG, but
  `Embervm.OpLog.SQLite.encode_payload/1` JSON-encoded the `:succeeded` op payload
  (which embeds the result `body`), and `:json.encode/1` rejects a non-UTF-8 binary
  with `{:invalid_byte, 137}` (137 = 0x89, PNG's first byte). That crashed the
  `OpLog.SQLite` GenServer, cascaded to `TaskStore`, and briefly restarted the
  control-plane router (self-healed by the supervisor in ~15s; semgrep/sandbox
  unaffected; the monolith's smoke gate saw a transport error and rolled the
  registration back, so no poison-pill CR persisted). The `:submitted` request-body
  path had the same latent exposure (a binary request body would crash identically).
- **Did instead (Joe's call among 3 options):** store the op payload as an Erlang
  External Term Format binary (`:erlang.term_to_binary/1`), bound as an Exqlite
  `{:blob, _}`, replacing `:json.encode/1`. ETF round-trips ANY term, so a binary
  body persists byte-exact. `decode_payload/1` disambiguates by first byte (ETF
  version tag 131 vs JSON `{` = 123), so rows written before the upgrade still
  decode via the JSON branch during the 30-day journal retention overlap. A
  `stringify/1` normalizer coerces the `binary_to_term` result (atom keys) into the
  exact string-keyed shape `:json.decode/1` yielded, so every reader (the
  dispatcher's request envelope via `load_request`, replay via `read_from`) is
  unchanged. Also wrapped the `results.body` bind as `{:blob, _}` (was a plain
  binary bound as TEXT) so the projected result row stores a PNG byte-exact too.
- **Cost accepted:** the `ops.payload_json` column no longer holds JSON, so SQL
  JSON-function queryability of op payloads is lost. Nothing queries it that way
  (verified: only plain `SELECT payload_json`), and the payloads are this node's own
  trusted data, so `binary_to_term/1` on read is safe.
- **Tests:** ExUnit covers a binary result body round-trip (append -> `read_from` +
  `load_result`, survives reopen), a binary request body via `load_request`, and a
  legacy JSON row still decoding to string keys (upgrade compatibility).
- **Approved:** yes (Joe picked the ETF-blob option; flagged here for post-impl review).

## R1 Phase 2: public tier (Task 13)

### D-R1.3.1 Public FaaS rate-limit = Envoy gateway rate-limit + EmberVM quota (defense in depth)
- **Spec said (Task 13):** "Rate limiting at the Cloudflare edge for `/functions/*`
  (per ADR 045 risk table)."
- **Clarification found mid-implementation:** the repo's `cloudflare-gateway` is Envoy
  Gateway + a cloudflared tunnel, NOT the Cloudflare WAF product, so a CF-*WAF* per-IP
  rule is a dashboard action (out of repo). BUT the Envoy gateway IS the public
  origin's edge and DOES support in-repo rate limiting via the `cf-ingress.rate-limit`
  BackendTrafficPolicy helper (already used for the `-public` frontend route at
  100/min).
- **Did:** TWO layers. (1) A dedicated `/functions/` HTTPRoute (mirroring `/img`)
  carries its own `cf-ingress.rate-limit` BackendTrafficPolicy at 120/min
  (`cfIngress.public.functionsRateLimit`), an Envoy Local limit at the gateway edge.
  (2) An EmberVM per-principal DAILY vCPU-second quota (`3600`/day, ~72k og-image
  invokes) on the single `monolith-public` SA that all public traffic submits as,
  plus the function pool cap (4 concurrent VMs). The gateway limit is the coarse burst
  cap; the quota is the daily abuse cap; the pool cap bounds concurrency.
- **Caveat:** the Envoy Local limit is per-gateway and shared across clients (not
  per-IP), matching the existing frontend limit; a true per-IP rule would need a CF
  dashboard rule or a client-selector on the policy. Acceptable for a homelab
  low-volume OG-image surface. **FLAG FOR JOE:** say if you want per-IP granularity.

### D-R1.3.2 Public tier calls EmberVM directly (not a proxy to the private tier)
- monolith-public mounts `register_public` -> `invoke_router_public`, which resolves
  `get_public_function` (smoke-passed AND `visibility=public`) and calls EmberVM
  directly, exactly like the private router but with the stricter lookup. This needs:
  (a) the `monolith-public` SA added to EmberVM's `allowedServiceAccounts`; (b)
  `EMBERVM_URL` on the public pods (via the chart's `semgrep.embervmUrl` knob); (c) a
  `public_reader` GRANT on `faas.function` (the row-level `visibility=public` filter
  in the query is the security boundary, not the grant); (d) faas added to the public
  image glob with `router.py`/`storage.py`/`workload.py`/`functions/**` pruned so the
  ingestion/write path stays out of the public binary (`main_public_imports_test`
  enforces it). Chosen over a public->private proxy because a proxy would still need
  the visibility filter AND the grant on the public side (a private function must not
  leak), plus an extra hop; direct is fewer moving parts and matches the private tier.
- **Approved:** proceeding under the "/loop, move fast, not in active use" latitude;
  flagged here for post-impl review.

### D-R1.3.3 The public pod gets an identity-only SA token (was tokenless) for EmberVM auth
- **Found live:** after routing + EMBERVM_URL were fixed, the public URL returned 401
  `{"error":"missing bearer token"}`. The public tier deliberately runs tokenless
  (`serviceAccount.automount: false`, a hardening choice: a public page needs no K8s
  API), so `auth_headers()` had no token to send and EmberVM's TokenReview rejected an
  anonymous submit.
- **Did:** flipped `serviceAccount.automount: true` so the web pod mounts its SA
  token. This is the FIRST public path that must authenticate to an in-cluster service
  (EmberVM), and the token is IDENTITY-ONLY: the `monolith-public` SA has ZERO RBAC
  (verified: no Role/ClusterRoleBindings), so the token grants no K8s API power. It
  only proves "I am monolith-public" to EmberVM's allow-list, which is the exact
  capability the `/functions` surface already exposes (quota-capped). So no new
  attack surface: a leaked token can only do what the public URL already does.
- **Caveat / follow-up:** `automount` is SA-scoped, so the frontend/imgproxy pods
  (same SA, also no RBAC) now also mount the token though they do not use it. A
  per-component audience-scoped **projected** serviceAccountToken volume on the web
  pod only would be tighter. **FLAG FOR JOE:** say if you want the scoped-projected
  hardening now; deferred as a follow-up given the no-RBAC blast radius.
- **Approved:** proceeding under the "/loop, move fast" latitude; flagged for review.

## R2 Phase 0: contract layer (PR-1, embervm 0.1.36)

Implementation choices where the R2 plan was silent (contract PR; no behavior
change, nothing calls the new session verbs yet). Flagged for post-impl review.

### D-R2.0.1 `session_id` is a first-class `Op` struct field, not a payload key
- The op-log `Op` struct gains a nullable `session_id` alongside `task_id`, mapping
  1:1 to the new `ops.session_id` column; session ops carry `task_id: nil` and
  vice versa. Cleaner than burying the id in the ETF payload, and it lets the
  compaction blocker query pin a live session's ops by column (indexed) exactly
  like a live task's.

### D-R2.0.2 `session_created` projects state directly to `running`
- The plan's FSM lists `creating -> running`, but a create is an assignment from an
  already-primed pristine VM, so the durable projection records `running` on
  `session_created`. The transient `creating` is a control-plane-process concern
  (the parked create caller), not a durable state; a future PR can persist it via
  the op payload's optional `:state` override if needed.

### D-R2.0.3 The `SESSIONS` printer column is backed by a `status.sessionsSummary` string
- A Kubernetes additionalPrinterColumns entry cannot format an object, so the
  control plane writes both the structured `status.sessions {live,banked}` (for
  machine reads) and a `status.sessionsSummary` string (e.g. "3 live / 2 banked")
  that the `SESSIONS` column renders. Structured object stays authoritative.

### D-R2.0.4 Base eviction is a fed-refcount seam, not a poll; counts fail safe
- BaseBuilder holds `base_refs` per superseded ref and exposes `report_base_refs/3`
  (PoolManager will report `:primed` counts, SessionStore `:sessions` counts, both
  in PR-4). `evictable?` withholds eviction until EVERY owner has reported a known
  (non-nil) zero: an unknown/nil count NEVER evicts. In this contract PR no base is
  actually evicted (no reporters wired yet); the mechanism + property test land now
  so PR-4 only adds the two reporters. This is the ADR 003 orphaned-base cleanup,
  fail-safe by construction (a live session's base cannot be dropped).

## R2 Phase 1: noded session verbs (PR-2, embervm 0.1.37)

Task 4 daemon-side implementation choices where the plan was silent (nothing
calls the verbs yet; daemon mechanics only). Flagged for post-impl review.

### D-R2.1.1 `suspect` and `DEADLINE_EXCEEDED` are mutually exclusive on the wire
- gRPC cannot carry both a `DEADLINE_EXCEEDED` status code AND a response body with
  `suspect=true`. On a guest TIMEOUT, `SessionAssign` returns the `DEADLINE_EXCEEDED`
  error and leaves the VM alive (the code itself signals suspect-and-alive per the
  proto contract). The `suspect=true` RESPONSE body is reserved for NON-deadline
  transport faults (a 502-bodied response, VM left alive). Either way the VM is
  never destroyed on a transient guest error: the control plane decides. Smallest
  choice consistent with both the proto `suspect` field and gRPC semantics.

### D-R2.1.2 Session snapshot refs are opaque `sess`-prefixed random hex
- `newID("sess")`, a distinct namespace from base keys; no file paths cross the
  seam (R0 proto rule). Bundles live under `SnapshotRoot/sessions/<ref>` (sibling
  of `bases/`), self-contained (memfile + snapfile, no archive backing) so they are
  portable, matching the R1 hydration invariant.

### D-R2.1.3 Disk-usage facts are best-effort; rescanned bundles carry mtime + empty identity
- `unix.Statfs` errors or a missing dir report `(0,0)` (the control plane's
  fail-closed policy reads that as "no facts"). A bundle found by the restart rescan
  takes `created_at` from the snapfile mtime (no better disk-only source) and leaves
  `session_id`/`workload` empty; the control plane rebinds those by adoption from its
  own projection (the node reports what it HOLDS, not what it MEANS).

### D-R2.1.4 Session VMs share the fcvm driver's LiveCount with primed/task VMs
- Both `Prime` and `RestoreSession` claim through the SAME fcvm driver, so
  `driver.LiveCount()` is the single authority for `live_vms`/`max_live_vms`; session
  VMs are tracked in a SEPARATE `sessionRegistry` (never the task `vmRegistry`) and
  reported only in `NodeStatus.session_vms`, never in any `primed_vm_ids`. This is
  the isolation invariant: a session VM can never be adopted into the task pool.

## R2 Phase 1: lifecycle core (PR-3, embervm 0.1.38)

Tasks 5-6 (SessionStore/FSM/tokens + SessionManager/create/API) choices where the
plan was silent. Flagged for post-impl review.

### D-R2.2.1 Primed-VM claim goes through a new Dispatcher.claim/3 (single-writer inventory)
- Create claims a primed pristine VM via a new `Dispatcher.claim/3` that atomically
  pops a vm_id from the `{node, workload}` inventory (reusing `reserve_vm`),
  serialized through the dispatcher, so a session claim and a task dispatch can never
  pop the same single-use VM. On a claim miss SessionManager Primes inline (create
  latency is not the per-invoke hot path). Chosen over reaching into the dispatcher's
  private inventory (which would break the single-writer invariant).

### D-R2.2.2 Placement is inline (first-ready-node-with-budget) until PR-4
- SessionManager picks a node inline with a minimal first-ready-with-budget choice.
  `SessionPlacement` (rendezvous hash) is PR-4/Task 8 with the same
  `workload -> {node, snapshot_ref}` interface, so the swap will not touch create.

### D-R2.2.3 session_invoked is a non-FSM write (record_invoke), not an FSM edge
- `SessionStore.record_invoke/3` is running->running (rejects non-running) and charges
  the usage projection (D12.1) like tasks, rather than forcing an FSM edge for every
  invoke. An invoke is not a lifecycle transition; the FSM stays about bank/relight.

### D-R2.2.4 Invoke on a running-but-processless session is 409 not_ready (PR-4 heals it)
- A `running` session whose GenServer is absent (a control-plane-restart limbo that
  PR-4 adoption will heal) returns `{:not_ready, :running}` -> 409, since no adoption
  path exists yet in PR-3 to rebind the process. `base_digest` at create is the image
  ref placeholder; PR-4 threads the BaseBuilder-resolved digest when relight lineage
  needs it.

## R2 Phase 2: bank/relight, placement, adoption (PR-4)

Tasks 7-8 (idle-bank/expiry/GC/eviction + relight-on-invoke/placement/adoption)
choices where the plan was silent. Flagged for post-impl review.

### D-R2.4.1 `banking`/`relighting` are ETS-only transient states, not durable ops
- The op-log has no `session_banking`/`session_relighting` kind by design: it records
  only COMPLETED lifecycle transitions (`session_banked`/`session_relit`). Entering
  `banking`/`relighting` is an ETS-only move via a new `SessionStore.mark/3` (FSM edge,
  no append); a crash mid-op heals from node inventory (the node reports either the
  live VM or the completed snapshot, never both gone silently), not from the durable
  log. Recovery edges `bank_abort` (banking->running) and `relight_abort`
  (relighting->banked) are the ETS-only rollbacks on a transient RPC failure. Chosen
  over adding two more op kinds, which would durably persist a state the FSM already
  guarantees is transient and crash-healed.

### D-R2.4.2 The bank runs async OFF the manager process (gate 3)
- `SessionManager.bank/2` ADMITS synchronously (per-node concurrent-bank cap + disk
  fail-closed gate + `mark(:bank)`), then spawns a worker for the Bank RPC and
  completes the durable `session_banked` append on a `{:bank_done}` message. This keeps
  the multi-second bank off the manager's message loop, so a bank never head-of-line-
  blocks another session's routing (gate 3). The consequence: the per-session idle
  process STOPS on admission and the three-consecutive-bank-failures counter lives in
  the MANAGER (`bank_failures` map), not the (now-stopped) session process; three
  strikes fails + destroys the VM, a strike under the limit restarts a session process.

### D-R2.4.3 Adoption forces ETS state via a total `adopt_state/3`, never the FSM
- Reconcile derives ETS purely from node truth and must be total over FSM-unbridgeable
  limbo (e.g. a `banking` session whose node reports a live VM has no `banking->running`
  FSM edge). `SessionStore.adopt_state/3` sets the ETS state directly (running/banked,
  no op, never resurrects a terminal row) and `adopt_residency/4` rebinds the VM fact.
  Reaping (mark failed) happens ONLY when the recorded node IS reporting and covers the
  session as neither a VM nor a snapshot (authoritative vanish); a node absent from the
  capacity table (a disconnect) leaves the session untouched (the #3517 reap-free rule).

### D-R2.4.4 Mid-bank invokes park then relight; disk fail-closed is watermark-scoped
- An invoke arriving while a session is `banking` parks in the relight ledger and is
  relit once the bank completes (no cancel path; banking is short), matching the plan.
  The disk fail-closed gate only bites when a snapshot-disk low watermark IS configured:
  with none configured (eviction disabled) banking proceeds, since there is no disk
  policy to fail closed against. The wake-rate limit is a per-principal sliding-window
  counter (default 30/min); the FIRST banked-invoke consumes a token and relights,
  excess ones 429 without a node hit. Mid-bank parked invokes are not re-wake-charged
  (they arrived before the bank finished; the narrow window is not a burst lever).

### D-R2.4.5 Review fixes + one accepted follow-on (PR-4)
- **Fixed (critical):** the bank/relight workers acquired the node channel with a bare
  `channel_fun.(node_id)` in the `with` head; that is a `GenServer.call` which EXITS
  (not returns) on a NodeChannel restart/dial-timeout, killing the worker before it
  sent `{:bank_done}`/`{:relight_done}` and hanging every parked caller forever +
  leaking the per-node bank slot. Wrapped in a `safe_channel/2` that traps the exit
  into `{:error, {:channel_raised, _}}` so the worker always reports an outcome.
- **Fixed (medium):** a periodic reconcile that landed mid-bank forced the session to
  `running` (the node still reports the live VM during a bank), which then made
  finish_bank's `session_banked` transition illegal and silently dropped. `adopt_one`
  now skips a session the manager has in-flight in `state.banking`/`state.relighting`
  (boot reconcile is unaffected: those maps are empty on a fresh process).
- **Accepted (low, follow-on):** `wake_events` accumulates one key per distinct
  relighting principal and is never pruned. Bounded by principal cardinality (not
  request volume), negligible for a homelab; a periodic prune of empty-window keys is
  a recorded follow-on, not done. **FLAG FOR JOE** only if principal cardinality ever
  grows unbounded.

## R2 Phase 3: operability (PR-5, embervm 0.1.40)

Task 9 choices where the plan was silent. Flagged for post-impl review.

### D-R2.5.1 Snapshot-disk alert reuses existing hostmetrics, no new Elixir metric export
- The noded exports no snapshot-disk metric and the snapshot scratch is a hostPath (not
  a PVC), so the op-log alert's kubeletstats `k8s.volume` metric cannot see it. Rather
  than add an OTel METRICS SDK to the Elixir control plane (a fragile multi-place
  hermetic-hex + MODULE.bazel change, disproportionate for an alert), the alert is
  sourced from the already-scraped k8s-infra hostmetrics `system.filesystem.usage`
  gauge, filtered to node-4's NVMe mountpoint that holds `embervm-noded/snapshots`.
  Zero new export; whole-scratch usage (bases + snapshots share it) is the right
  exhaustion axis. Alert notifies via `incidentio` (the only channel registered in
  signoz-alerts; the plan's "homelab channel" maps to it, same as every other embervm
  alert). No chart bump: the alerts chart is git-HEAD-synced, not OCI-pinned.

### D-R2.5.2 status.sessions counts written on the sweep tick, change-detected
- The control plane patches `status.sessions {live,banked}` + `sessionsSummary` on the
  existing 30s sweep tick, debounced per workload against a written-counts map (the
  PoolManager `primedFloorSatisfied` pattern), never per-transition. Disjoint status
  keys so it does not collide with pool/base status writes.

### D-R2.5.3 Session spans nest via a threaded traceparent, not OTel process context
- OTel process context does not cross `GenServer.call`/`spawn`, so the invoke root span
  is opened at the router and its W3C traceparent threaded through the plain `req` map
  (`session_trace.ex`, reusing the dispatcher's `:otel_tracer.from_remote_span` idiom,
  not a new tracing layer). Timer/API-driven bank/create/evict have no caller trace, so
  they are root spans (consistent with the dispatcher's prime/auth spans). The Task 11
  gates (relight p95, bank p95, state-persistence) are derivable from spans alone.
