# EmberVM R0: deviations and judgment calls

A running log of where the implementation departs from, tightens, or resolves an
ambiguity in the R0 tasks spec and plan, so the spec and the code can be
reconciled retrospectively. Each entry says WHAT changed,
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

## R2 Phase 3: sandbox consumer (PR-6, embervm 0.1.41 + monolith 0.285.204 + public 0.89.129)

Task 10 choices where the plan was silent (sessioned run_python across guest + chart
+ monolith). Flagged for post-impl review.

### D-R2.6.1 Guest frame protocol: 4-byte big-endian length prefix + JSON body
- Both directions. `io.ReadFull` reassembles partial reads; a >16 MiB length is rejected
  (not allocated). Chosen over newline-delimiting so snippet output containing newlines
  or NULs cannot corrupt framing.

### D-R2.6.2 Per-snippet timeout is PARENT-enforced; only a timeout resets the namespace
- The Go parent times the response-frame read (not the child), because a mid-exec
  interrupt could leave the shared namespace half-mutated. On timeout the parent SIGKILLs
  the child's process group and lazily restarts on the next request (that response carries
  session_reset). A snippet EXCEPTION (a typo/error) does NOT reset: state survives.
- session_reset is OR-folded from two sources: the guest sets it whenever it started a
  fresh child (first request or post-timeout); the monolith client sets it on a 410
  transparent re-create. Either surfaces as `session_reset: true`.

### D-R2.6.3 One-shot is byte-identical: additive omitempty fields + one guarded branch
- handler.go adds `Mode`/`SessionReset` as `omitempty` and a single early
  `if req.Mode == ModeSession` branch; the entire one-shot path below is untouched, so the
  shared sandbox guest still serves the task class + the deprecated fc-invoke path
  byte-for-byte when `mode` is absent (guest tests prove both the behavior and the response
  JSON still omits `session_reset`).

### D-R2.6.4 sandbox-session reuses the sandbox rootfs; client read timeout 45s
- The session workload's image ref equals sandbox's, so the node resolves the same rootfs
  via EMBERVM_NODED_IMAGES (no new noded image entry). `SANDBOX_SESSION_READ_TIMEOUT` = 45s
  (vs 35s one-shot) so a cold-banked relight completes within the read window. Also
  registered `sandbox_client_test` (previously had NO py_test target, so it was not running
  in CI) alongside the new `sandbox_session_test`.

### D-R2.7.1 R2 shipped on CI-verified mechanisms + observed-Ready infra; live functional gate drill deferred to a Joe-approved prod-exec session
- The plan's seven closure gates split into (a) mechanisms CI proves on every push and
  (b) end-to-end functional numbers / destructive drills that need real session tokens
  minted inside the prod monolith pod and real snapshot-disk pressure. R2 is marked
  Shipped on (a) + the observed live infra state (`sandbox-session` workload `Ready`,
  base built and adopted, session inventory projection reporting). (b) is the single
  open item, recorded as a follow-on flagged for Joe rather than run autonomously.
- WHY: the auto-mode classifier (correctly) gates exec-into-prod-backend as a
  prod-exec action needing explicit approval, and the standing directive is "flag for
  me to review post-implementation." Running the drill autonomously would either fight
  that gate or overclaim gates I did not measure. Marking every mechanism CI-green and
  citing the observed Ready workload is honest; asserting "all 7 gates PASS live" would
  not be. The drill is ~30 min, expected to pass (every mechanism is already green), and
  the Open Risks table records the tuning fallbacks if a number (relight p95) misses.
- ADR 001 R2 -> Shipped 2026-07-15; ADR 004 gate condition 1 (R2 sessions exist) now
  holds, condition 2 (agent-sandbox upstream traction) still open. Goosecracker remains
  the last fc-invoke consumer; its migration onto the session class is the R2.x follow-on
  that triggers retiring the fc-invoke substrate.

### D-R2.7.2 Session invoke was broken in prod (registry gap); fixed by lazy adoption on first SessionAssign
- **The live drill (D-R2.7.1) caught a real bug the CI-green mechanisms all missed:** every
  first invoke on a freshly created session returned 502 and the session went `failed`.
  ROOT CAUSE: session create primes/claims its VM through the shared warm pool, so noded's
  `Prime` lands it in the TASK registry (`s.vms`); but `SessionAssign` only accepts VMs from
  the disjoint SESSION registry (`s.sessionVMs`), which until now was populated ONLY by
  `Relight`. So the create -> first-invoke path was never wired: SessionAssign hit "unknown
  session VM" -> FAILED_PRECONDITION (status 9) -> control plane 502 + failed the session.
  Tasks work because task `Assign` reads the same `s.vms` that `Prime` writes; sessions had
  no equivalent bridge. Every unit test passed because each side was tested against its own
  fake; nothing drove a real control-plane -> gRPC -> noded session invoke end to end. This
  is exactly the integration seam a mock hides, and the reason the live drill was worth running.
- **FIX (Option C, simplest of three):** `SessionAssign` ADOPTS a primed VM from the task
  registry into the session registry on the first invoke (`Server.adoptPrimedSession` +
  `vmRegistry.claimForSession`), gated by a WORKLOAD MATCH (a session may only adopt a VM
  primed from its own class:session base; the request's `trace.workload` must equal the
  primed VM's workload). The registry comment already anticipated this ("add registers a
  freshly relit OR primed-into-session VM"), so it is the intended seam. Rejected Option A/B
  (a new create-time noded verb + proto + control-plane round-trip): more surface, an extra
  round-trip at create, no benefit since the physical VM is already a session VM and only its
  bookkeeping was wrong. Rejected the unguarded adopt: it would let a task-class primed vm_id
  be hijacked as a session (broke `TestSessionAssignTaskClassVMRejected`); the workload guard
  restores that invariant as defense-in-depth (the control plane never names a foreign vm_id,
  but noded enforces it anyway).
- **Two secondaries fixed in the same PR:** (1) `Session.run_invoke` invalidated the SHARED
  node channel on ANY invoke error, including a server-returned `%GRPC.RPCError{}` that rode a
  healthy channel; tearing it down would disconnect every other session multiplexed on it.
  Now `maybe_invalidate/3` invalidates ONLY on a transport fault, never on a server gRPC
  status. (2) `NodeChannel` had no `handle_info/2`, so the linked Mint connection's normal
  `{:EXIT, _, :normal}` on every channel teardown logged a spurious `no_handle_info` error
  report; added a swallowing clause. Both are hygiene, not the root cause.
- **Honesty note on the closure:** ADR 001 marks R2 "Shipped 2026-07-15" (D-R2.7.1). The drill
  showed the invoke path was functionally broken at that moment, so the label was optimistic
  until this fix deploys and the drill re-runs green. The closure never claimed the gates
  passed live (it flagged the drill as pending), so it is not false, but the plan Closure
  section should gain a "post-ship fix" note once the drill is green. Flagged for Joe.

### D-R2.7.3 Session invoke forwarded guest path "/" (shim serves only /invoke) -> 404; fixed to fall back to invokePath
- The re-run drill (post D-R2.7.2) showed create->first-invoke now REACHES the guest
  (the control-plane adoption fix worked: the 404 body was Go's `404 page not found`, not
  the Elixir router's JSON, and the session SURVIVED the invoke = the guest's 4xx treated
  as the guest's answer). But every invoke 404'd: the session invoke handler baked the
  guest path to "/" when no `X-Ember-Guest-Path` header was set, and the sandbox shim's mux
  serves ONLY `/invoke` (+`/invoke/`), so "/" hit the ServeMux default 404. This is the
  SAME R1 baked-path trap the router's own comment warns about (baking "/" makes invokePath
  dead), reintroduced on the session path. The real monolith client sets no guest-path
  header either, so the session invoke was never wired end to end.
- FIX (control-plane only, mirrors the R1/task default): `guest_path/1` returns nil when
  `X-Ember-Guest-Path` is absent (was "/"); the Session process defaults a nil req path to
  the workload's `invokePath` (`run_invoke`: `Map.get(ctx.req, :path) || ctx.invoke_path`).
  `invoke_path` is threaded from the catalog entry through `start_session_process` (covers
  both create and relight/adoption restart) with a "/invoke" default in `Session.init`. An
  EXPLICIT X-Ember-Guest-Path still overrides. Tests: bare-invoke-forwards-invokePath +
  explicit-path-overrides (Elixir). Bumps the embervm chart to 0.1.43.
- LESSON (recurring, now twice in R2): the sandbox shim mux is FIXED at /invoke regardless
  of the workload spec, so any path the control plane forwards MUST resolve to the
  workload's invokePath. Two integration seams (registry adoption D-R2.7.2, guest path
  D-R2.7.3) were both invisible to every unit test because each side was faked; only the
  live drill drove a real control-plane -> gRPC -> shim -> guest invoke. Flagged for Joe.

### D-R2.7.4 Session restore corrupted the guest FS (rootfs rebuilt in place); fixed by digest-versioning the rootfs file. Reaper is the flagged follow-on.
- The re-run drill (8/9) exposed EXT4 corruption on restored guests
  (`__ext4_find_entry ... checksumming directory block`, `comm python3`). ROOT CAUSE
  (mapped): base MEMORY snapshots are per-digest (`snapshots/bases/<baseKey>/`), but the
  read-only rootfs (vda) they reference is a SINGLE fixed file per workload
  (`workloads.<name>.rootfsPath`). The FC memfile embeds the rootfs HOST PATH, not its
  bytes, and restore (`driver.loadInto`) issues only LoadSnapshot, never re-PutDrive. The
  rootfs-builder `mv -f`s that fixed file IN PLACE on any guest-digest change, so a chart
  roll swaps the bytes under every banked session snapshot born on the old rootfs -> the
  restored guest's page-cache ext4 metadata no longer matches on-disk blocks -> corruption.
  Confirmed cross-version: it hit sessions banked pre-roll, relit post-roll. This is the
  split-brain [[feedback]]: the control plane refcounts the birth base (base_builder.ex) but
  the physical rootfs file is overwritten blind to that refcount.
- FIX (tactical, CHART-ONLY, no Go change): digest-version the rootfs file. New helper
  `embervm.noded.rootfsPath` renders `<dir>/rootfs-<guest-tag>.ext4`, used by BOTH the
  rootfs-builder `BASE_ROOTFS_PATH` and the `EMBERVM_NODED_IMAGES[ref].rootfsPath` (one
  source, so the built file and the path noded attaches are byte-identical -- the riskiest
  coupling). The builder skips-if-the-digest-file-exists and NEVER overwrites/deletes other
  `rootfs-*.ext4`. Because each guest version is now an immutable file and the memfile embeds
  a stable per-digest path, restore re-attaches the correct (still-present) rootfs with NO
  driver change. Rejected the full Go plan (record birth rootfs in baseEntry + re-PutDrive on
  restore): unnecessary once the embedded path is immutable-by-construction. This is the
  node-local form of the offsite versioned-store design (ADR 003), same invariant on nvme.
- **FOLLOW-ON, flagged for Joe (the "TTL for flushing" half):** old `rootfs-*.ext4` files now
  accumulate (~2GB each per deploy) until reaped. The reaper needs the refcount wiring:
  when `base_builder.ex maybe_evict_base` fires EvictSnapshot for a superseded base (primed=0
  AND sessions=0), also delete that digest's rootfs file (extend `RemoveBaseBundle`, which
  exists but is unwired, to cover the rootfs path). Deferred: disk growth is slow and this is
  not in active use. The strategic version is offsite S3/OCI export (ADR 003), where
  immutability + TTL are free. Also deferred: a defensive `base_superseded` 410 if a birth
  rootfs is ever missing on restore (belt-and-suspenders once the reaper can race a relight).
- Also in this PR: guest-kernel child-lifecycle logging (generation counter + pid + the
  session_reset decision, to the serial console noded captures) to DIAGNOSE the separate,
  within-version `session_reset=true`-but-state-survived anomaly (gate 9). Not a fix yet:
  the logging is the tooling to root-cause it on the next drill.

### D-R2.7.5 klog now writes to /dev/console (was inert on stderr); rootfs reaper DEFERRED to offsite ADR 003, not built node-local
- The child-lifecycle klog shipped in #3553 wrote to guest-init os.Stderr, which does NOT
  reach the VM serial console (only the kernel ring buffer does), so it never showed in
  `kubectl logs ...noded` -- confirmed inert. Fixed: open /dev/console (the console=ttyS0
  serial device noded pipes to its stdout; guest-init is root in prod so it can), fall back
  to stderr for tests. Now the generation/pid/session_reset logs are actually observable.
- **Rootfs reaper (the "TTL flush") deliberately NOT built node-local now.** The rootfs file
  is SHARED across workloads (sandbox + sandbox-session resolve to ONE guest digest -> ONE
  rootfs-<tag>.ext4), so a safe reaper must prove NO base AND NO session of ANY workload
  references a digest before deleting it -- get it wrong and you delete a rootfs a live
  session needs, re-introducing the exact D-R2.7.4 corruption (the highest-severity class in
  this subsystem). That cross-workload refcount is free in the offsite immutable-store model
  (object store refcounts by content hash), so a node-local reaper is both risky and
  throwaway. RECOMMENDATION for Joe: fold the reaper into the offsite session-snapshot
  distribution work (ADR 003 ExportBase/RestoreBase generalized to sessions); if node-local
  disk pressure bites before then, an AGE-based sweep (delete rootfs-*.ext4 older than
  max(maxLifetimeSeconds, bankedTtlSeconds)+margin and not the current digest) is a safe
  stopgap because every session born on an older rootfs has hit its TTL and 410'd. Disk grows
  ~2GiB/deploy until then; fine for a not-in-use system.

## D-R3.2.1: serving_stats usage upsert (request-count only, principal-on-op, live-seconds deferred)
- serving_stats is workload-scoped (per-cluster rq_delta), with no single instance to join, so the op carries principal/tenant on its top-level Op fields (the workload owner, set by the Task 9 appender). project_usage stays a pure per-op function, preserving kill-and-restart rebuild equivalence. serving_instance_id is NULL on serving_stats ops.
- Request counts charge a new usage.request_count column (guarded ALTER, default 0), not task_count, so serving requests are never conflated with task/session invocation counts (irreversible if merged). The count increment is parameterized; task/session callers keep charging task_count unchanged.
- Live-seconds accrual (vcpu/gb-seconds over the alive interval) is deferred out of Task 2: the serving_instances projection carries no resource columns, so accrual belongs with the Task 9+ lifecycle/sweeper machinery that has the resource shape. Task 2 usage wiring is request-count only; a code comment marks serving compute as intentionally un-accrued here, not free.
- FLAG FOR JOE: review the request_count-column choice and the live-seconds deferral post-implementation.

## D-R3.3.1: serving does NOT participate in base-refcounting (deferral closed in Task 9 as unnecessary)
- RESOLVED (Task 9): serving does not participate in base-refcounting at all, so the deferred guard-widening is a genuine NO-OP and is closed as unnecessary. evictable?/1 stays primed/sessions-keyed; no serving: 0 term is added and no ServingStore report_base_refs reporter is landed.
- Why: base_builder's refcount + eviction (evictable?/1, report_base_refs, EvictSnapshot) reaps a superseded BuildBase base SNAPSHOT, kept alive only while a primed VM or a session still pins it as birth lineage (a snapshot they can relight FROM). But serving never restores from a base snapshot: StartServing(fresh) cold-boots from a rootfs IMAGE (noded/server/serving.go startServingFresh -> img.RootfsPath, the init-container-baked immutable rootfs resolved via the node image table, per D-R3.4.2), and once banked a serving instance rides its OWN per-instance serving snapshot (bank/relight), not a shared base snapshot. A serving instance's birth lineage is an image (permanent daemon config, never evicted by base turnover), so base eviction can never remove anything a live serving instance needs. There is nothing for a :serving refcount to hold a superseded base alive for.
- Consequence for the code: the guard MUST NOT be widened. Widening evictable?/1 to require serving: 0 with no reporter existing would make it require serving: 0 forever (nil never equals 0), silently wedging base eviction for EVERY workload class, not just serving. Not-widening is therefore both the correct model AND the only safe move. The :serving key threaded through merge_refcounts/maybe_put_count in Task 3 stays as deliberately-inert MECHANISM (a code comment there marks it so), not ripped out, to avoid churning PR-1's accepted counts contract; if a future rung ever gives serving a shared evictable base lineage, that is where a reporter + a widened guard would land together.
- Original deferral (superseded by the above): Task 3 threaded :serving through the counts machinery and deferred the evictable?/1 widening + first reporter to Task 9, to be landed atomically. Task 9's investigation found there is nothing to report, so the atomic widen+report is unnecessary rather than merely deferred.

## D-R3.4.1: v1 pins the serving tap IP across bank/relight (no guest IP reconfiguration)
- A restored Firecracker serving VM resumes with the eth0 IP baked at fresh boot (kernel boot-args ip=); a snapshot resume does not re-run kernel init, so the guest L3 IP cannot change on relight without in-guest reconfiguration (MMDS or a guest agent), neither wired in v1. A different relight IP would black-hole traffic: the guest eth0 still holds the old IP and drops packets to the new one even though the app binds 0.0.0.0 (an L4 bind, not L3 ownership).
- v1 noded therefore PINS the tap IP to the serving snapshot lineage: the assigned IP is written into the serving snapshot bundle metadata at bank, re-acquired on relight, and recovered by inventory rescan after a daemon restart. The bank/relight round-trip works with zero guest-side network reconfiguration.
- The proto contract (tap IP may differ across relights, endpoint re-reported every wake, guests must not persist their own IP) is preserved: the control plane still re-reports ip:port every wake and the guest still binds 0.0.0.0 and does not persist its IP; v1 simply reports the same IP each wake. The genuine IP-change case arises only with cross-node relight, deferred to ADR 003 snapshot distribution, which will require guest IP reconfiguration via MMDS as the recorded follow-on.
- FLAG FOR JOE: confirm v1 IP-pinning and the deferral of guest IP reconfiguration (MMDS) to the distribution rung.

## D-R3.4.2: StartServing(fresh) is a cold boot, not a base-snapshot restore
- Firecracker cannot hot-attach a NIC to a snapshot-restored VM: PUT /network-interfaces is pre-boot only, and /snapshot/load + resume brings up exactly the device set captured at snapshot time. A vsock-only base snapshot has no NIC, so a serving VM cannot be made by restoring a task/session-style base and attaching a tap. The plan's "restore pristine base, attach NIC" (Task 4 / line 147) is mechanically impossible.
- v1 therefore cold-boots StartServing(fresh): boot the serving rootfs with the tap NIC configured pre-Start (PutNetworkInterface) and the static IP via kernel boot-args ip=, then health-gate over the tap. "Pristine base ref" for serving means the rootfs/image to cold-boot (resolved via the image table, like BuildBase), not a snapshot to resume. StartServing(relight) resumes a serving snapshot that was banked with its NIC live (the NIC exists because fresh cold-booted it), re-pinning the same host IP per D-R3.4.1 so the guest baked eth0 IP still matches.
- The two source modes are thereby coherent: fresh cold-boots to create the NIC, bank captures a NIC-bearing VM, relight resumes it. Cost: a fresh serving start pays a cold boot (guest init), amortized to nothing for a long-lived serving VM; wake-from-idle uses the fast relight path. The only slow path is the first-ever request to a never-run workload (no snapshot yet), acceptable in v1.
- Rejected: a NIC-baked serving base snapshot restored for fresh (every restore collides on one baked IP, forcing per-VM MMDS reconfig that D-R3.4.1 defers to cross-node distribution). Strictly worse for v1.
- FLAG FOR JOE: confirm serving fresh-start is a cold boot (not an instant base restore) and that first-ever-request cold-start latency is acceptable v1 (subsequent waves use fast relight).

## D-R3.5.1: xDS snapshot version is a fixed-width monotonic string (PR-4 publisher contract)
- The xds sidecar orders snapshot versions by string comparison and rejects any PUT whose version is not strictly greater than the current one (per-node). This matches the plan's "monotonic string" and keeps the sidecar decision-free.
- CONSEQUENCE for PR-4 (EndpointPublisher): the published version must be fixed-width / zero-padded so string ordering equals numeric ordering. A bare integer counter breaks lexically ("10" < "9"). The plan's epoch-prefixed scheme (restart re-pushes at a higher epoch) must therefore format BOTH the epoch and the counter as fixed-width, zero-padded fields (e.g. a 20-digit zero-padded epoch-millis or boot-count, then a zero-padded counter). PR-4 must not emit a bare or variable-width version.
- The sidecar does not call go-control-plane Snapshot.Consistent() (it models a full LDS+RDS+CDS+EDS graph and fails spuriously with no listeners served, since node Envoy listeners are static bootstrap); the sidecar's own validate() enforces route->cluster and cluster->EDS references, the correct check for the 3-type surface.

## D-R3.10.1: serving alerts source from node-Envoy Prometheus stats, not ember spans (control plane is spans-only)
- The ember control plane is SPANS-ONLY: it emits OpenTelemetry spans (session/serving lifecycle + the Task 10 activate/publish_flush spans) but NO OTel metric series, and the SigNoz collector has NO spanmetrics/span-to-metric processor. So ember-side signals (activator errors, publication ACK failures) live in the traces store, not as metric series an alert can threshold. Every existing embervm/hubble alert is a METRIC_BASED_ALERT over a scraped Prometheus series (hubble_httpv2_requests_total, kubeletstats/hostmetrics gauges); the repo has zero traces-signal alerts.
- DECISION (Task 10): the two R3 serving alerts source from the node-Envoy Prometheus stats now scraped via the serving-envoy DaemonSet's prometheus.io annotations (Task 10 Fork 4), consistent with R3's thesis (control plane off the hit path => serving-health observability comes from Envoy). (1) Activator error rate = serving-cluster 5xx (envoy_cluster_upstream_rq_xx, response class 5, on serve| clusters): a miss the activator cannot satisfy (429/503/wake_failed) returns through the node Envoy and lands here. (2) Publication failure = node-Envoy xDS update rejection (envoy_cluster_update_rejected on serve| clusters): a malformed pushed snapshot Envoy rejects.
- COVERAGE GAP (recorded, not closed): the publication-failure alert catches ONLY the "sidecar pushed a config Envoy REJECTED" mode. The "publisher CANNOT REACH the sidecar" mode is invisible to Envoy stats (the sidecar serves stale config, Envoy accepts it, no rejection). That second mode is covered by the EndpointPublisher's loud PUT-failure log + the embervm.serving.publish_flush span (ember.ack_ok=false) in SigNoz traces. A UNIFIED metric-based publication-failure alert covering both modes is a recorded follow-on: it needs a control-plane metrics pipeline (OTel metrics from the publisher) or a spanmetrics processor so ember.ack_ok becomes a metric series.
- Rejected: (B) writing the alerts as SigNoz traces-signal queries (novel v5 traces-builder JSON with no in-repo precedent to copy, on the CM->sidecar->api/v1/rules seam that only live-verifies post-merge => malformation risk); (A2) adding a spanmetrics connector to the platform SigNoz collector this PR (platform-chart change, larger blast radius). Both deferred; the metric-series-over-Envoy-stats path mirrors the proven hubble-invoke-http JSON exactly.
- The two new alert templates use the STANDARD Envoy Prometheus stat names (envoy_cluster_upstream_rq_xx, envoy_cluster_update_rejected) and each flags "confirm exact scraped metric + label names via a live /stats/prometheus scrape in post-merge verification" (the OTel Prometheus receiver may normalize names), the same discipline hubble-invoke-http documents.
- FLAG FOR JOE: confirm the two alerts should stay Envoy-stats-sourced, and decide whether to schedule the follow-on unified metric-based publication-failure alert (needs the control-plane metrics/spanmetrics pipeline).

## D-R3.11.1: the python zip-lane shim gained an env-gated TCP-serving mode so zip-lane guests can serve over the tap NIC (og-image serving, Part A)
- PROBLEM: the R3 serving lane health-probes and proxies over a tap NIC (TCP) at the guest's real L3 endpoint (noded/server/serving.go: GET http://ip:port{healthPath}; noded/serving/probe.go probes the same continuously). But the python zip-lane runtime shim (projects/embervm/runtimes/python/shim.py) bound VSOCK ONLY (VsockHTTPServer on port 1027, the frozen task/session contract), and guest-init hardcoded "the HTTP-over-vsock guest contract". NO guest in the system bound TCP on the tap. So a zip-lane serving workload would boot listening on vsock, the daemon's TCP health probe over the tap would get connection-refused, and finishServingStart would reap the VM (FAILED_PRECONDITION): it could never health-gate. The R3 plan's assumption that "the og-image shim already complies" was incorrect; this is why PR-2's serving drill was deferred and never exercised a real TCP guest. (The serving sample CR uses source.image = an OCI guest that binds TCP natively; no such image exists in-repo, and no zip-lane guest bound TCP.)
- NOT a hydrate problem: the zip lane bakes the unpacked, handler-imported state into the base snapshot at BuildBase (the archive is build-time-only, ADR embervm/001), so a serving cold boot of a zip base already has the handler imported. The ONLY gap was the transport. [CORRECTED by D-R3.11.2: this is FALSE for the zip lane. The handler lives in the tmpfs->memfile RAM, not on any block device, and a NIC cold boot cannot resume that memory snapshot, so a serving cold boot comes up with NO handler. See D-R3.11.2.]
- DECISION: add an env-gated TCP-serving mode to the shim. When EMBER_SERVING_PORT is set and > 0, the shim binds AF_INET on 0.0.0.0:<port> using the IDENTICAL request handler (make_request_handler) and does NOT bind vsock (a serving VM has no vsock HTTP consumer); when unset, the boot is the byte-unchanged vsock path, so task/session guests are unaffected. The handler was already transport-agnostic (shim_test drives it over a loopback TCP server), so /shim/healthz, /shim/ready, and the invoke path behave identically over either transport; the serving CR's healthPath is therefore /shim/healthz (already exists, always-200, now reachable over TCP).
- SIGNAL FLOW (3 components, single-sourced port): noded startServingFresh puts spec.serving.port on NICSpec.ServingPort; the driver's bootArgsFor(nic) appends `ember.serving_port=<port>` to the kernel command line ONLY when nic != nil (mirroring the existing serving-only ip= directive, so task/session boot args stay byte-identical); guest-init reads that token from /proc/cmdline and exports EMBER_SERVING_PORT for the shim (mirroring how it already seeds EMBER_HANDLER the raw FC boot drops). The port is single-sourced from the StartServing request into BOTH the boot-arg and the health probe, so the shim binds exactly what the daemon probes and publishes. Boot-arg (not init_env) is the correct channel because init_env is baked per-BASE at BuildBase and cannot carry a per-VM-boot signal, whereas bootArgsFor is already the per-cold-boot serving channel.
- CONSUMER (Part A): og-image is re-registered as a serving-class zip Workload (serving-og-image) with the SAME zip identity the task-class FaaS registration produces (runtime python312, handler app.handle, sha256 2c5f9342…, the SeaweedFS codeUri), spec.serving {port 8080, healthPath /shim/healthz, host og-image-serving.private.jomcgi.dev, min 0 / max 2 / idleBank 600 / drain 5 / lifetime 86400}, sized memMib 768 (the WARM serving footprint, headroom over the fresh-per-request task-class 512Mi: Pillow stays resident and the TCP server runs continuously; fc-base sizing coupling sizes to the warm peak). It is ADDITIVE and takes NO production traffic: the task-class FaaS og-image registration is untouched and stays the instant rollback. The private R3 drill route (repointed from the never-realized serving-hello placeholder to /og-image-serving on private.jomcgi.dev) is the only edge exposure. The REAL public og-image traffic flip is Part B (controller-driven, after gate drills), NOT this.
- FLAG FOR JOE.

## D-R3.11.2: zip-lane serving needs a build-produced, cold-boot-readable HANDLER ARTIFACT (corrects D-R3.11.1's "cold boot already has the handler")
- CORRECTION: D-R3.11.1 asserted "the zip lane bakes the unpacked, handler-imported state into the base snapshot at BuildBase, so a serving cold boot of a zip base already has the handler imported. The ONLY gap was the transport." That is FALSE for the zip lane. The handler is unpacked to a TMPFS (`/tmp/ember-app`, mounted over `/tmp` because the runtime rootfs is READ-ONLY and snapshot-shared) and imported into the running python process; both live ONLY in the base memory snapshot's memfile (RAM), never on any block device. A serving VM must COLD-BOOT with a NIC (D-R3.4.2: FC cannot hot-attach a NIC to a resumed snapshot), and a cold boot cannot resume that memory snapshot, so it comes up on the bare read-only runtime rootfs with NO handler and no on-disk source to import from. The shim's only handler-import path was the build-only `POST /shim/hydrate` from tmpfs. So the serving base-provisioning path did not exist for zip: PR-7 Part A fixed the transport onto a base that a serving cold boot could not consume, which is why `StartServing(fresh)` returned FAILED_PRECONDITION ("serving image ... not provisioned"): the control plane passed the base-snapshot key into `startServingFresh`, which looks it up in the runtime-rootfs image table where it can never appear.
- DECISION (A1): BuildBase for a serving workload additionally produces a per-workload HANDLER ARTIFACT (`bases/<baseKey>/handler.zip`, the verified archive bytes noded already holds in memory) that a NIC cold-boot can read. `startServingFresh` cold-boots the runtime rootfs WITH the NIC AND attaches the handler artifact as a second READ-ONLY drive; guest-init signals it via an `ember.handler_disk=` boot-arg (mirroring `ember.serving_port=`), and the shim reads the zip off that drive and runs its EXISTING unpack+import (`unpack_archive` + `load_handler`) BEFORE binding TCP, so the serving guest is ready without a network hydrate. This honors BOTH D-R3.4.2 (NIC configured pre-boot) and the baked-at-build invariant (the handler is materialized at build time from the build-time-only archive; nothing is fetched or hydrated per request; the block device is a local, re-derivable artifact, not a portability dependency).
- MECHANIC (raw-zip drive, not a pre-unpacked ext4; EOCD-padding defence): noded writes the sha256-verified archive bytes it ALREADY holds (`driveBuild`'s `archive []byte`) to `bases/<baseKey>/handler.zip` host-side (via the driver, which owns the bundle layout) plus a `runtime.ref` sidecar (the runtime image whose rootfs is drive 1, so a startup rescan rebuilds the serving-images inventory with no control-plane round-trip); it attaches that file as a read-only drive, and the shim reads the raw device and unpacks to tmpfs at cold boot (~ms for a small zip). CRITICAL: a block device is SECTOR-PADDED (Firecracker rounds a drive file up to a 512-byte boundary), so reading the whole device yields the zip followed by trailing zero padding, and Python's zipfile scans BACKWARD from the end for the End-Of-Central-Directory signature, which the padding hides (BadZipFile). This is exactly the sector-pad/EOCD-trim bug class the R1 zip lane hit and retired when it moved archive delivery to vsock hydration. Defused here by conveying the EXACT zip length at build time via an `ember.handler_zip_bytes=<N>` boot-arg and having the shim read ONLY N bytes off the device, never letting zipfile scan the padded tail. REJECTED the "build guest copies /tmp/ember-app onto an attached writable ext4" piggyback (needs a writable build drive, a guest-side copy step, and a clean-detach/fs-consistency guarantee, for no benefit since noded already has the bytes) and a pre-unpacked ext4 handler-disk (needs `mkfs.ext4`/loop tooling in the noded apko image and fs-consistency care). Raw-zip + exact-length is strictly less machinery.
- ROUTING (bug 1, edge Host-mismatch): the EndpointPublisher builds each node-Envoy virtual host with `Domains: [serving.host]` (og-image-serving.private.jomcgi.dev), but the drill route is a PATH on the private gateway, so the edge forwards `Host: private.jomcgi.dev`, which matches no node-Envoy vhost -> 404. FIX (chart-only): a `URLRewrite.hostname` filter on the drill HTTPRoute rewrites the Host to the workload's `serving.host` before the request reaches the node Envoy. `URLRewrite.hostname` is the canonical Gateway-API Host rewrite (a `RequestHeaderModifier` on `Host` is rejected by some implementations). This keeps the publisher's clean per-workload-host vhost model instead of baking the drill host into the durable publisher path.
- A3 DEFERRED (skip the base snapshot for serving-exclusive bases): per D-R3.3.1 serving never restores from the base memory snapshot (it cold-boots), so for a workload registered ONLY as serving the vsock-only memory snapshot is dead weight (built, disk-consumed, never used), and BuildBase could skip `SnapshotBase` for a serving-exclusive base. NOT done in this PR: the fix ALWAYS produces the snapshot (as today) AND additionally writes the handler artifact for a serving base, which is strictly additive and zero-risk to the task lane. A workload registered as BOTH task-class and serving-class (og-image) needs the snapshot for the task lane regardless. The proto keeps a `serving` flag (noded needs it to know to WRITE the artifact) but no skip logic. A3 is recorded here as a deferred disk optimization.
- REJECTED C (per-cold-start re-hydrate): un-gating `/shim/hydrate` for serving and re-delivering the archive per cold boot re-adds a per-cold-start SeaweedFS fetch and a vsock consumer to serving VMs, and regresses the baked-at-build invariant. Its only value was de-risking the router-match/activator-fire/wake path, which the drill's live 503 ALREADY proved. C is near-pure throwaway; the gate evidence must reflect the durable architecture.
- ADR: the zip-lane rationale lives in ADR embervm/001 (the orchestrator ADR), NOT embervm/002 (op-log retention); DECISIONS.md's prior "ADR embervm/002" zip-lane attribution (D-R3.11.1) is corrected above. A cross-note is added to 001's zip-lane section pointing here.
- FLAG FOR JOE.

## D-R3.11.3: R3 serving live-drill hardening (five more cold-boot/data-plane defects) + a stale-base follow-up
- CONTEXT: the private R3 gate drill (curl through the node Envoy at the serving vhost) drove the zip-lane serving cold-boot through FIVE additional live-only defects beyond D-R3.11.2's base-provisioning gap, each hidden behind the one above it (only a real Firecracker boot surfaces them; no CI-without-KVM can). All five are fixed and merged. Recorded here for async review.
- (1) SECTOR-FLOOR TRUNCATION (#3568, chart 0.1.56). Firecracker exposes a drive sized to the FLOOR of the backing file in 512-byte sectors and drops the remainder ("Disk size N is not a multiple of sector size 512; the remainder will not be visible to the guest"), so an unpadded 3056-byte handler.zip became a 2560-byte device: the EOCD-bearing tail vanished and the guest short-read + crashed. CORRECTS D-R3.11.2's mechanic note, which said "Firecracker rounds a drive file UP to a 512-byte boundary" — it FLOORS (truncates), the opposite. FIX: pad the handler.zip file UP to a whole sector on write so FC exposes the entire zip, plus a `handler.len` sidecar so a post-restart rescan conveys the EXACT pre-pad length (the guest still reads only N bytes). The read-side exact-length contract from D-R3.11.2 was necessary but not sufficient; the file must also be padded so the device is >= N.
- (2) MISSING init= (#3569, chart 0.1.57). The serving cold-boot emitted no `init=` on the kernel cmdline, so the kernel fell through /sbin/init, /etc/init, /bin/init to /bin/sh and the ember guest-init never ran. `ClaimServing` received the resolved harness-init path (img.HarnessInit) but dropped it (`_ = harnessInit`) and relied on the driver-global cfg.HarnessInit, which is empty on the daemon driver. BuildBase works via the per-image init; task/session VMs restore a snapshot rather than cold-booting, so serving-fresh was the only cold-boot path that regressed. FIX: thread the per-boot harnessInit through coldBootSpec and prefer it in bootArgsFor.
- (3) /proc UNMOUNTED (#3570, chart 0.1.58). guest-init reads /proc/cmdline to translate ember.serving_port / ember.handler_disk into EMBER_SERVING_PORT / EMBER_HANDLER_ZIP, but a raw FC boot leaves /proc unmounted, so os.ReadFile failed and both setters silently no-op'd, leaving the shim on the vsock path with no handler ("serving on vsock port 1027 (awaiting hydrate)"). Latent because task/session/build boots use vsock defaults + a /shim/hydrate POST and never read the cmdline. FIX: mount a procfs on /proc in guest-init before the boot-arg readers.
- (4) serving-envoy ON POD NETWORK (#3571, chart 0.1.59). The serving-envoy DaemonSet ran on the pod network (10.42.x), but serving-VM tap IPs live on the node-local serving bridge (embervm-serv0, 172.31.0.0/24) noded creates on the host. noded's host-side health probe reached the VM (so it went live+published), but a pod-network Envoy has no route to that node-local bridge, so its upstream connections failed (membership_healthy=1 yet every request 5xx, upstream_cx_connect_fail). FIX: hostNetwork: true + dnsPolicy ClusterFirstWithHostNet so the Envoy shares the node netns and dials the VMs directly. This was the recorded fallback in the original pod-network design; the drill promoted it to the default.
- (5) ENVOY base_id COLLISION (#3572, chart 0.1.60). The host-networked Envoy crash-looped ("unable to bind domain socket with base_id=0, errno=98"): base_id keys a per-process hot-restart shared-memory/domain-socket region defaulting to 0, and a hostNetwork pod shares the node's abstract socket namespace with every other Envoy on the node (CNI/gateway data plane). FIX: `--use-dynamic-base-id` so this Envoy allocates a free base id at startup.
- PROVEN LIVE (boot-side complete): after #3567-#3570 rolled, the serving VM cold-boots with the runtime rootfs (/dev/vda) + padded handler.zip (/dev/vdb) + tap NIC, guest-init mounts /proc and sets the serving env, the shim reads exactly N bytes off /dev/vdb, imports the handler, binds TCP 0.0.0.0:8080 (ready), passes noded's health gate, and the control plane publishes it (status.serving live:1, published:1). #3571+#3572 close the last hop (node Envoy -> VM). The end-to-end 200 verification was pending a serving-envoy roll wedged behind a stuck ArgoCD sync at time of writing.
- FOLLOW-UP (stale serving-base accumulation + stale-base placement): each image roll builds a NEW serving base (base key = hash(runtime_digest, archive_sha256)) but OLD serving bases are NOT garbage-collected, so multiple serving bases coexist per workload on the node. After a roll, the control plane can TRANSIENTLY place a wake on a STALE base whose runtime image no longer exists on the node -> FAILED_PRECONDITION "runtime image ... not provisioned on this node" -> a transient 503 until reconciliation settles on the current-runtime base. It self-corrects (subsequent drills reconcile), but it adds 503 noise to every post-roll drill. RECOMMENDED follow-up: GC superseded serving bases (+ their handler artifacts and snapshots) once no live instance references them, and have serving placement prefer the base whose runtime matches the node's current provisioned runtime image. Low priority; recorded so it is not re-derived.
- FLAG FOR JOE.

## D-R3.11.4: node-Envoy -> serving-VM reachability via DNAT-through-noded (the tap bridge is in noded's OWN pod netns, so no Envoy can route to it directly)
- ROOT CAUSE (corrects the D-R3.11.3 defect-4/5 hostNetwork fix): the serving bridge (embervm-serv0, 172.31.0.0/24) and every VM tap live inside NODED's pod network namespace, because noded is a privileged pod-network Deployment that creates the bridge in its own netns. So NEITHER a pod-network Envoy NOR a hostNetwork Envoy can reach the taps: the bridge is in neither the pod-CNI netns nor the host netns. #3571 (hostNetwork) + #3572 (dynamic-base-id) assumed the bridge was in the host netns and could not have worked; the endpoint published, Envoy was healthy, and every request was upstream_cx_connect_fail.
- DECISION: noded exposes each live serving VM as noded_pod_ip:vmPort via a kernel nftables prerouting DNAT rule (in noded's own netns) to tap_ip:guest_port. The control plane publishes THAT endpoint over xDS unchanged; the serving Envoy reverts to POD NETWORK and dials noded's pod IP. The kernel forwards and conntrack reverses replies, so noded userspace stays off the request hit path. Pod IPs are cluster-routable, so serving endpoints are now reachable from ANY node's Envoy (and a future cluster-level ember edge tier): this is the "routable VM IPs" enabler the R3 plan deferred to post-Cilium, obtained now with zero CNI work, and it survives the flannel->Cilium migration (pod-IP routing is the CNI's job). Rejected: Envoy-as-sidecar-in-noded-pod (couples Envoy rollout to noded, kills all VMs per Envoy upgrade, node-local-only endpoints); hostNetwork noded+Envoy (host netns pollution, larger blast radius).
- ZERO ELIXIR / ZERO PROTO FIELDS: the control plane consumes the reported {ip, port} only to publish endpoints (serving_manager.ex publishes from StartServingResponse; adoption/health match by vm_id; relight IP-pinning is daemon-side). So the routable endpoint rides the existing proto fields as a pure PROJECTION at the noded edge. The registry keeps storing the TAP IP (probe target, bank-snapshot pin, tap teardown); only StartServingResponse{ip,port} and NodeStatus.ServingVm{ip,port} are projected through serving.Manager.Endpoint(tapIP, guestPort) = (podIP, vmPort). Empty PodIP config falls back to reporting the tap IP and installs no DNAT (tests/local keep old behaviour; startup warning).
- DETERMINISTIC PORT, NO ALLOCATOR: vmPort = portBase + hostOffset(tapIP) (portBase default 30000, env EMBERVM_NODED_SERVING_PORT_BASE); a /24 gives ports 30002..30254, clear of noded's 8080/9090. Reuses the IP allocator's per-VM uniqueness; recomputable anywhere; a relight pinning the same IP re-derives the same port. NewManager rejects portBase + subnetSize - 2 > 65535.
- NFTABLES: the whole inet embervm_serving table is regenerated as a pure function of (bridge, podIP, entries) and applied atomically via nft -f on every add/remove (extends the flush-then-define idempotency; established conntrack flows are unaffected by rebuilds). A serving_dnat chain (type nat hook prerouting priority dstnat) holds one `ip daddr <podIP> tcp dport <vmPort> dnat ip to <tapIP>:<guestPort>` rule per live VM (`dnat ip to` is required syntax in an inet table). No new filter rule: the forward chain is policy accept, inbound eth0->bridge NEW traffic falls through (its iif is eth0, not the bridge, so the VM-egress drop misses it). No SNAT: the VM's default gw is the bridge .1 in the same netns, so replies re-enter the forward chain there and conntrack reverses the DNAT. net.ipv4.ip_forward=1 is set in EnsureNetwork (the routed eth0->bridge path needs it). A cheap MSS clamp on the bridge-egress path caps guest SYN MSS to the route MTU so it cannot exceed the CNI overlay MTU on the return path.
- NO SERVING ADOPTION (corrects a handoff assumption): there is NO daemon-side serving adoption; live serving VMs die with the daemon and are excluded from primed_vm_ids. Container restart in a surviving pod netns: EnsureNetwork's empty-map atomic rebuild clears stale DNAT rules. Pod recreate: new podIP, VMs gone; the control-plane reconcile drains instances, the activator fallback covers, the next wake republishes the new podIP (a brief bounded 503 window, documented in the daemonset comment).
- DEBUGGING EDGE: an in-pod `curl noded_pod_ip:vmPort` from INSIDE noded is NOT DNAT'd (the rule is prerouting-only, no output hook), so verify reachability from a DIFFERENT pod (the Envoy pod), never from noded itself. Probe/readiness stay on the tap IP and never exercise the DNAT path, so a broken DNAT still publishes: the drill curl through the node Envoy is the only end-to-end check (follow-on: a self-check from the Envoy side). If a default-deny NetworkPolicy/Cilium policy ever lands on noded, the 30000+ serving port range must be allowed.
- STATUS: shipped in noded (config PodIP/ServingPortBase; serving.Manager PortForIP/EnsureDNAT/RemoveDNAT-folded-into-ReleaseTap/Endpoint projection; server finishServingStart installs DNAT after readiness and projects the response; servingVMsStatus projects NodeStatus) + chart (noded EMBERVM_NODED_POD_IP downward-API env; serving-envoy reverted to pod network). Live drill (warm path, scale-from-zero cold-boot, on-node nft inspection, noded-restart republish, task-lane regression) runs after merge + sync. Part B public flip stays GATED on Joe's explicit go.

## D-R3.11.5: serving exposure is operator-owned Gateway API, not an embervm concern (public og-image flip)
- DECISION: embervm exposes each serving workload as a Gateway-API BACKEND only: the serving Service plus an INTERNAL vhost authority the node Envoy matches (pushed via xDS by EndpointPublisher). The cluster OPERATOR owns EXPOSURE by listing HTTPRoutes (chart values servingEnvoy.routes), each binding a gateway + hostname + path and rewriting the external Host to the workload's internal vhost authority (rewriteHost). Public vs private is purely which gateway/hostname an entry binds and whether Cloudflare Access fronts that hostname; the serving substrate knows none of it. This is the "not built" Part B ("controller-driven public flip") reframed: there was never a control-plane router, and a homelab where the author is the sole operator does not need one (a control-plane router would re-couple embervm to the cluster's gateway topology and hold cluster-wide RBAC to write routes). Operator-managed Gateway API is the decoupled, standard, zero-privilege model. A future control-plane router (a workload declaring itself public) can be added later without a migration; deliberately NOT stubbed now (YAGNI, internal-only).
- DECOUPLING: the Workload CRD's serving.host was og-image-serving.private.jomcgi.dev (the cluster's DNS baked into a workload spec). It is now serving-og-image.serving.internal: a NON-RESOLVABLE internal authority, never DNS-resolved, only compared against the :authority header by the node Envoy and used as each route's URLRewrite target. The workload spec now carries none of the cluster's ingress DNS, so the same workload is portable across clusters. No control-plane code change: EndpointPublisher matches whatever string serving.host holds, and validate_serving_host_unique still enforces one-authority-one-workload.
- TWO ENVOYS, TWO OWNERS: (1) the node Envoy (embervm serving-envoy DaemonSet) is configured by embervm's xDS (RDS/CDS/EDS from the control-plane xds sidecar): Host authority -> cluster serve|<workload> -> noded podIP:vmPort -> DNAT -> VM. embervm owns this. (2) the edge Gateway (cloudflare-ingress, Envoy Gateway) is configured by the operator's Gateway-API HTTPRoutes: external hostname+path -> rewrite Host -> the serving Service (which fronts the node Envoys). The operator owns this. The seam is the serving Service + the internal vhost authority.
- PUBLIC FLIP: og-image is served both privately (private.jomcgi.dev/og-image-serving, Cloudflare-Access gated, tier trusted) and PUBLICLY (jomcgi.dev/og-image-serving, no Access, tier public). A PATH on the already-routed public apex needs no new DNS/cert/tunnel (mirroring the private path trick); the apex runs through the SAME cloudflare-ingress gateway that monolith-public uses, and /og-image-serving is a free path. Note: og-image was ALREADY public COLD at jomcgi.dev/functions/og-image (the monolith FaaS public router); this route warm-serves it via the embervm serving tier at a distinct path. The cold FaaS path stays the instant rollback.
- EXPOSURE SCOPING (verified at 3 layers): a public request can reach ONLY og-image. (1) the HTTPRoute pins Host -> the og-image internal authority (URLRewrite overrides any client Host) and matches only /og-image-serving; (2) the node Envoy exact-matches the authority and og-image is the sole serving-class workload (semgrep/sandbox are task-class over vsock, not on this Envoy); (3) the shim reserves the exact /shim/ prefix and nothing strips the /og-image-serving path prefix, so /shim/hydrate (handler-code replacement), /shim/healthz, /shim/ready are unreachable publicly. The handler is a pure Pillow function (title/subtitle -> PNG, no wall-clock, no entropy, NO network egress) in a disposable microVM.
- STATUS: shipped in chart (serving-httproute.yaml renders one HTTPRoute per servingEnvoy.routes entry; drillRoute retired; serving.host -> internal authority). No control-plane code change. Chart 0.1.64 -> 0.1.65. Live-verify: curl https://jomcgi.dev/og-image-serving?title=... returns 200 image/png (public), private path still works.

## D-R4.PR-3.1: dynamic LDS for stateful TCP listeners; the R3 HTTP listener stays static
- DECISION: the node Envoy keeps its R3 HTTP listener + connection manager STATIC in the bootstrap (byte-identical, no churn), and ALSO subscribes to LDS on the existing ADS stream so the control plane can add/remove one tcp_proxy listener per stateful workload dynamically. Static and dynamic listeners coexist in Envoy: an empty LDS response leaves the static listeners untouched, so a node with no stateful workloads serves exactly the R3 shape (proven by the snapshot.Build empty-listeners regression test, which asserts zero ListenerType resources). Rejected: moving the HTTP listener into LDS as a control-plane-pushed resource (would let a control-plane bug or an empty push drop the R3 HTTP path; keeping it static makes the R3 path provably independent of the R4 addition). Rejected: a static bootstrap TCP port range (config posing as fact, caps workloads at N pre-declared ports, no per-port cluster indirection).
- SIDECAR: snapshot.Desired gains a `listeners: [{name, port, cluster}]` array; snapshot.Build renders each to a tcp_proxy Listener bound 0.0.0.0:port, added to the snapshot under resourcev3.ListenerType alongside CDS/RDS/EDS. The tcp_proxy config is minimal (opaque L4, decision 4): a per-listener stat_prefix (the source of downstream_cx_active / downstream_cx_total the Task 9 idle scrape and Task 10 metrics read) and idle_timeout=0 (disabled: a long-lived DB connection is never severed by the proxy, decision 7). validate() checks each listener has a unique name, an in-range port, and a cluster that resolves to a defined cluster (which may carry zero VM endpoints, the activator-fallback case). The sidecar stays logic-free: no port-range policy (the CRD watcher owns that), no defaulting, no state. go-control-plane Consistent() is still NOT called (tcp_proxy listeners carry no RDS reference, and the static HTTP listener that references the RouteConfig lives in the bootstrap, not the snapshot, so Consistent() would spuriously fail); snapshot.validate is the correct check for this CDS/RDS/EDS + tcp-proxy-LDS surface.
- CHART: the values-declared servingEnvoy.statefulTcpPortRange (default 5400-5409) is exposed as container ports on the node Envoy DaemonSet and as TCP ports on the serving Service, named state-<port>. This is capacity (static GitOps config); the CRD assigns a listenPort within it and endpoint churn stays xDS. ClusterIP only, no edge exposure (standing decision 10).

## D-R4.PR-4.1: the stateful L4 activator resolves the workload by ACCEPT PORT, not a header
- DECISION: Embervm.TcpActivator binds every port in the values-declared stateful TCP range (default 5400-5409, mirroring the chart's servingEnvoy.statefulTcpPortRange) on the control-plane pod. On accept, it reads the connection's LOCAL port (:inet.sockname/1) and looks up the WorkloadCatalog entry whose stateful.listen_port matches it: that IS the workload the connection is for. There is no L4 equivalent of the serving activator's injected x-ember-workload HTTP header (opaque L4, decision 4), so the listener port itself is the only identity signal available, and it is sufficient because listen_port is enforced unique across live stateful workloads (WorkloadWatcher.validate_stateful_listen_port/3, decision 5).
- THE FALLBACK ENDPOINT IS THEREFORE PER-WORKLOAD, NOT ONE FIXED {ip, port}: Embervm.EndpointPublisher's stateful render was fixed in this task from a single `activator_tcp_endpoint: %{ip, port}` option to `activator_ip: <string>`, an IP ONLY. Each stateful workload's empty-cluster fallback cluster is computed as `%{ip: activator_ip, port: workload.stateful.listen_port}` (the workload's OWN port at the SAME activator ip), so the node Envoy's tcp_proxy for a cold workload dials the activator on exactly the port the activator resolves it from. A single shared {ip, port} pair (the pre-Task-8 placeholder) would have collapsed every stateful workload's fallback onto ONE port, which cannot resolve more than one workload.
- REJECTED: a length-prefixed or magic-byte framing injected by the node Envoy ahead of the raw stream (would violate the opaque-L4 contract, decision 4, and require a matching unwrap in the daemon's guest-facing shim); a single shared activator port with an internal proxy_protocol-style preamble (same framing violation, plus Envoy's tcp_proxy PROXY protocol carries only source/dest IP:port, not a workload name).
- STATUS: shipped (Embervm.TcpActivator, Embervm.StatefulManager, the EndpointPublisher activator_ip fix, Embervm.Application wiring, and the chart deployment wiring). The control-plane Deployment renders EMBERVM_STATEFUL_ACTIVATOR_IP (downward-API podIP) + EMBERVM_STATEFUL_ACTIVATOR_PORT_RANGE (from servingEnvoy.statefulTcpPortRange) and exposes the stateful port range as container ports, gated on servingEnvoy.enabled; with servingEnvoy disabled the activator binds nothing (a safe no-op), exactly the "cannot wake yet" state EndpointPublisher already handles for cold stateful workloads.

## D-R4.PR-10.1: stateful observability mirrors serving's spans-plus-Envoy-stats split; two of three alerts are honest placeholders
- DECISION: stateful gets the same two-tier observability shape as R3 serving (D-R3.10.1): OTel spans on the control plane for wake/lifecycle diagnosis (SigNoz trace view), node-Envoy Prometheus stats for alerting (the control plane stays spans-only, no metric exporter). New spans: `embervm.stateful.park`/`embervm.stateful.wake` (TcpActivator's wake path, retroactive ROOT spans emitted from Embervm.StatefulManager's finish_wake, exactly the serving park/placement/wake idiom collapsed to two phases since plan_wake is a cheap pure ETS read not worth its own child span; UNLIKE serving these are roots, not children of a restored remote parent, because a raw TCP accept carries no W3C traceparent to nest under), `embervm.stateful.splice` (Embervm.TcpActivator, bounds the whole spliced connection's byte-pump lifetime, ember.bytes_in/ember.bytes_out from a lightweight accumulator already in pump/3, no extra syscalls), and `embervm.stateful.forced_roll` (Embervm.StatefulManager.do_destroy_instance, mirroring ServingSweeper.force_roll's span shape for the operator-override destroy). `embervm.stateful.bank` (already shipped in Task 9) gained `ember.generation`, set once the daemon's StopStateful(BANK) reply is known, alongside the existing `ember.snapshot_bytes`. `wake`'s attributes (`ember.wake_ms`, `ember.cold`, `ember.relight`, `ember.cold_boot_reason`) are the Task 12 gate numbers: `ember.relight` is true ONLY for a clean relight (a relight that fell back to a cold boot on the daemon side is NOT a relight); `ember.cold_boot_reason` is the wire reason string (empty, never nil, for a genuine fresh first boot).
- WAKE REPLY GAINED `generation`: Embervm.StatefulManager.wake/3's success map grew an additive `generation` key (`%{ip, port, generation}`) alongside the existing `{ip, port}` (the straggler path still replies bare `{ip, port}`, since it never wakes anything). Existing `%{ip: ip, port: port}` pattern matches at every call site still match; this is purely additive.
- STATUS.STATEFUL: Embervm.StatefulSweeper now writes `status.stateful {state, generation, bundleGeneration, volumeBytes}` + `statefulSummary` on every sweep tick, debounced on the 4-tuple (a workload whose values are unchanged since the last write is skipped), exactly mirroring ServingSweeper.write_serving_status/1's status.serving debounce. Every field defaults to a concrete non-nil value (0, or "" for state) so the merge-patch body never carries a nil (the router's `:json.encode` nil-renders-as-the-string-"nil" trap). Disjoint status keys from status.serving/status.sessions/the CRD watcher.
- SCRAPE: the node Envoy DaemonSet's existing `/stats/prometheus` scrape (chart, R3 Task 10) already covers the new `state-<port>` tcp_proxy listeners stood up by PR-3 (same Envoy admin endpoint, no path restriction excluding listener stats): NO chart change needed for D (verified by reading serving-envoy-daemonset.yaml's scrape annotations against the LDS-added listener shape; not live-verified against a real scrape in this PR, same "confirm the exact metric/label names against a live scrape" caveat the serving alerts carry).
- THREE NEW SIGNOZ ALERTS, deliberately mixed maturity (honesty over completeness, mirroring the serving publication-failure alert's threshold-0 dry-run discipline):
  - `embervm-stateful-wake-failures`: PLACEHOLDER (threshold-0 dry-run), same honesty as the other two. Opaque L4 (decision 4) means a stateful wake failure has no HTTP status to count the way serving's activator-errors alert counts `envoy_cluster_upstream_rq_xx` 5xx: a failed TcpActivator wake just closes the client socket (a normal FIN), which is INDISTINGUISHABLE at the Envoy layer from a client that simply finished and disconnected. The wake failure itself IS observable today (the `embervm.stateful.wake` span with `ember.wake_ms` and the wake-failure Logger.warning), just not as a Prometheus counter without a metrics exporter the control plane does not have. The alert is written against the closest structurally-related Envoy signal (a `state-` listener's `envoy_tcp_downstream_cx_total` staying flat while the workload's connections keep bouncing) as a documented approximation, but ships disabled/threshold-0 rather than claim it reliably catches a wake failure it cannot actually see.
  - `embervm-stateful-generation-mismatch`: PLACEHOLDER (threshold-0 dry-run). No metric series exists yet for `stateful_cold_booted{reason=generation_mismatch}` op counts (would need a control-plane metrics pipeline or an op-log-to-metrics bridge); the op itself is fully queryable today via the op-log (gate 2's "op-log alone reconstructs the discarded warmth" property), just not alertable without that pipeline. Flagged as a recorded follow-on.
  - `embervm-stateful-volume-watermark`: PLACEHOLDER (threshold-0 dry-run). `status.stateful.volumeBytes` (this PR) is a K8s status field, not a Prometheus series; SigNoz alerts read metrics/logs/traces, not arbitrary CRD status, so watermark alerting needs either a control-plane metric exporter for the volume ledger or a kube-state-metrics-style CRD status scraper, neither of which exists yet. Flagged as a recorded follow-on alongside the generation-mismatch alert.
- REJECTED: inventing a fake-but-"passing" metric query for the two placeholders (would silently never fire, indistinguishable from a healthy system, exactly the failure mode D-R3.10.1's dry-run discipline exists to prevent). Building the metrics-exporter pipeline in this task (out of scope for Task 10's observability-on-top-of-existing-mechanisms scope; a real scope of its own).
- STATUS: shipped (spans, status.stateful write + debounce test, three alert ConfigMaps in projects/platform/signoz-addons/alerts/templates/, auto-discovered by the signoz-alerts library chart's templates/ glob, no chart version bump needed since that app's ArgoCD Application tracks targetRevision HEAD). Live-verify the wake-failures alert's Envoy stat names against a real `/stats/prometheus` scrape in post-merge verification, same discipline the serving alerts carry.

## D-R4.PR-7.1: MMDS-lite over boot-args (not a real MMDS service) for a stateful workload's first-boot secrets
- WHY: a stateful workload's first boot (Postgres's initdb, most concretely) needs a secret from a K8s Secret (e.g. POSTGRES_PASSWORD) delivered into the guest's process environment before the image's own bootstrap runs. There is no MMDS (metadata) service in embervm: no vsock/HTTP metadata endpoint the guest can query, and building one is real scope (a new guest-visible service, a new noded responder, a new wire contract) that this PR does not need to spend on. The consumer is a cluster-internal, low-stakes SCRATCH datastore tier (ADR embervm/001's data-on-the-volume-warmth-in-the-snapshot split): not a production credential store, not internet-reachable, and the guest, host, and cluster are all under one operator's control. That risk profile is exactly where "put it on the boot-args instead of building a metadata service" is an accepted tradeoff rather than a shortcut that would be unacceptable for a higher-stakes tier.
- WHAT: the existing `StartStatefulRequest.mmds_env` proto field (already defined, previously always sent empty by the control plane and always dropped by noded) is now wired end to end. The control plane (`Embervm.StatefulManager.cold_request/4`) reads a K8s Secret named by the workload's new optional `spec.stateful.secretRef` (parsed by `WorkloadWatcher.parse_stateful/1` into the catalog as `stateful.secret_ref`) via the new `Embervm.K8s.get_secret/2` (GET `/api/v1/namespaces/<ns>/secrets/<name>`, base64-decodes `data`), and sets the decoded map as `mmds_env`. This happens ONLY on a FRESH/COLD wake (`coldBootStateful` in noded, `cold_request` in the control plane); a RELIGHT never reads the secret and never carries `mmds_env` (a relight resumes the running VM's memory snapshot -- the kernel never re-inits, so boot-args are never re-read, and the secret was already consumed at the original first boot and baked into the volume's initialized data, e.g. Postgres's on-disk password hash from initdb). noded's driver (`bootArgsFor`) encodes each `mmds_env` entry as a kernel boot-arg token: `ember.env.<KEY>=<base64url(value)>`, one per entry, KEY restricted to `[A-Za-z0-9_]` (an invalid key is skipped, not fatal), value base64url-encoded (`RawURLEncoding`: no padding, no characters needing cmdline escaping) so an arbitrary secret byte string survives the kernel command line's space-separated token parsing intact. guest-init (`setMmdsEnv` in `runtimes/python/guest-init/cmd/main.go`) decodes every `ember.env.*` token from `/proc/cmdline` and `os.Setenv`s each KEY to its decoded value, BEFORE `mountStatefulVolume`/`execShim` hands off to the image's own bootstrap, so e.g. `POSTGRES_PASSWORD` is in the process environment from the guest's very first read of it.
- THE CMDLINE-LENGTH CAVEAT: the Linux kernel command line has a hard size limit (historically 2048-4096 bytes depending on kernel config) and no defined escaping for arbitrary binary/whitespace in a token value (hence the base64url encoding). This seam is sized for "a few small secrets" (a DB password, a DB username), NOT bulk config or large blobs. A workload needing more than a handful of short KV pairs is a signal to build the real MMDS service, not to keep stuffing this seam.
- THE SECURITY TRADEOFF: the decoded secret value is readable on the guest's OWN `/proc/cmdline` for the lifetime of that boot (any process inside the guest can read it; this is a property of Firecracker + the Linux kernel, not something embervm controls). It is NOT readable from the host side beyond the one instant it is written into the FC `PutBootSource` API call, and noded's own logs are explicitly redacted: `bootArgsFor`/`ClaimStateful` and `coldBootStateful` log only the mmds_env KEY NAMES (`mmdsEnvKeyNames` in the driver, `mmdsEnvKeyNamesSorted` in the server package, and guest-init's own `setMmdsEnv` logging), never the values, at every log call site touching this path. This is the accepted boundary: guest-process-visible, host-log-invisible, cluster-internal-only.
- FAIL-OPEN ON A SECRET-READ FAILURE (DECISION): if `Embervm.K8s.get_secret/2` errors (missing Secret, transient K8s API failure, RBAC misconfiguration), `Embervm.StatefulManager.cold_boot_mmds_env/3` logs a warning and proceeds with an EMPTY `mmds_env` rather than failing the wake outright. Chosen over fail-closed because a K8s API blip must not take down an otherwise-healthy scratch-tier wake, and because the guest's own readiness gate (noded's `waitStatefulReady` TCP CONNECT poll) is already the loud, existing failure surface: a Postgres image that required `POSTGRES_PASSWORD` and did not get it will fail to start and never open its listen port, so the wake times out and the caller sees the same `{:wake_failed, ...}` shape a hard secret-read failure would have produced, just one hop later with a clearer "guest never came up" symptom. RELIGHT is unaffected either way (it never reads the secret).
- +RBAC: this is NEW runtime RBAC the R4 plan's task list said would be zero. `get` on `secrets` (core API group) was added to the control-plane ClusterRole (`chart/templates/rbac.yaml`) because `Embervm.K8s.get_secret/2` must read the workload-referenced Secret. Every other verb in that ClusterRole predates this PR; this is the first RBAC grant R4 needed past the R0 baseline (workloads get/list/watch, workloads/status update/patch, tokenreviews create).
- MIGRATION PATH: a future real MMDS service (a vsock/HTTP metadata endpoint the guest queries at boot, matching AWS/GCP-style instance metadata) can replace this boot-args seam WITHOUT changing the `mmds_env` proto field or the guest-visible env-var contract: `mmds_env` already carries the exact KEY -> value map a real MMDS response would serve, and the guest already expects those keys to simply appear in its process environment. Only the TRANSPORT changes (kernel cmdline -> a metadata query), which is entirely a noded + guest-init concern; the control plane's `Embervm.K8s.get_secret` + `cold_request` wiring is unaffected. The CRD's `secretRef` field name and semantics also survive the migration unchanged.
- REJECTED: building a real MMDS service now (real scope disproportionate to a single scratch-tier consumer's needs; deferred until a second consumer or a higher-stakes tier needs it). Delivering the secret via a Kubernetes-native mechanism (a projected volume, a mounted Secret) instead of boot-args (the guest's rootfs is a shared, per-workload-provisioned image with no notion of a K8s-mounted volume; the volume drive that DOES exist, `/dev/vdc`, is the workload's OWN durable data volume, not a secrets-delivery channel, and mounting a Secret there would conflate two very different lifecycles). Encrypting the boot-arg value (adds a key-management problem to a seam explicitly scoped as "cluster-internal, low-stakes, migrate to real MMDS later" -- if the threat model needed that, the answer is "build the real MMDS service," not "half-encrypt the interim one").
- STATUS: shipped (noded driver `bootArgsFor`/`ClaimStateful`/`coldBootStateful` threading + redacted logging + Go tests; guest-init `setMmdsEnv`/`mmdsEnvFromCmdline` decode + Go tests; control-plane `WorkloadWatcher.parse_stateful` secretRef parsing, `Embervm.K8s.get_secret/2`, `Embervm.StatefulManager.cold_request/4` + `cold_boot_mmds_env/3` with injectable `get_secret_fun` + Elixir tests covering the FRESH/COLD-reads/no-secretRef-skips/fail-open/RELIGHT-never-reads matrix; CRD `spec.stateful.secretRef`; chart RBAC `get` on `secrets`). This PR is ONLY the seam: it does not create the postgres image, the scratch-postgres CR, the OnePasswordItem producing the Secret, or the monolith DSN wiring -- those are the next pass's scope.

## D-R4.PR-11.1: the scratch-postgres consumer (apko postgres image, first-boot initdb from mmds-lite, PGDATA on the volume)
- WHY: R4's stateful class needed a real, named consumer to justify the rung. The scratch-postgres workload IS it: a scale-to-zero, cluster-internal, low-stakes SCRATCH datastore (ADR embervm/001's data-on-the-volume-warmth-in-the-snapshot split), reachable over opaque L4 TCP, that banks when idle and wakes on the next connection. It is the concrete consumer of every earlier R4 seam (the stateful CRD block, the L4 activator, the volume ledger, the MMDS-lite secret delivery).
- THE IMAGE: a dual-arch Wolfi apko base (`projects/embervm/runtimes/postgres`, published to `ghcr.io/jomcgi/homelab/projects/embervm/runtimes/postgres`), packages `postgresql-16` + `postgresql-16-client` (initdb/postgres/createdb/psql), `e2fsprogs` (mkfs.ext4/blkid for the volume), and `busybox` + `ca-certificates-bundle`. It mirrors the runtime-python layout (apko_image, per-arch guest-init tars). NOTE: the exact Wolfi package name (`postgresql-16` vs an unversioned `postgresql`) and the on-disk binary path need live verification against the Wolfi APKINDEX in CI; PATH covers both `/usr/bin` and `/usr/libexec/postgresql`.
- THE GUEST UID STORY: initdb and the postgres server both REFUSE to run as root. But guest-init MUST be root (PID 1) to `mkfs.ext4` + mount the writable volume (the host never mounts it, decision 9). Resolution: `ember-postgres-init` boots as root, mounts the volume, `chown`s the mount to the `postgres` uid (70, the Wolfi convention), then launches every Postgres child (`initdb`, `postgres`, `createdb`) DROPPED to uid 70 via `SysProcAttr.Credential`. Root does the privileged mount; Postgres never sees root.
- THE PID-1 INIT SERVES TWO BOOT CLASSES OFF ONE ROOTFS. (1) BASE BUILD (a plain cold boot, NO volume/mmds boot-args): noded's `BuildBase` cold-boots the guest and health-gates it over VSOCK at `readyPath` (`/shim/ready`) before snapshotting the warm base. Postgres cannot answer vsock; so `ember-postgres-init` reuses the frozen `substrate/shim` + `vsockproto` to run a tiny vsock HTTP ready server (200 on `/shim/ready`, flipped immediately since a base build has no datastore). The warm base is just the OS; Postgres first runs on the stateful boot. (2) STATEFUL FRESH/COLD (`ember.volume_dev`/`ember.env.*` present): mount the volume, decode `POSTGRES_PASSWORD` from mmds_env, then bootstrap Postgres. The init distinguishes the two purely by the presence of the `ember.volume_dev` boot-arg. RUNTIME health is a TCP connect to 5432 over the tap (noded's `finishStatefulStart`), a separate probe from the base-build vsock one; the vsock server stays up harmlessly.
- THE FIRST-BOOT-VS-RELIGHT BRANCH (the crux): PGDATA lives at `<volumeMount>/pgdata` (e.g. `/data/pgdata`). On an EMPTY/uninitialized PGDATA (`PG_VERSION` absent) it is FIRST boot: run `initdb` (scram-sha-256 auth, superuser password from `$POSTGRES_PASSWORD` via `--pwfile` so the secret never hits argv, UTF8), append a `pg_hba` scram rule for cluster-internal TCP, `listen_addresses='*'`, launch `postgres`, then `createdb scratch` once it accepts. On a NON-EMPTY PGDATA (a later cold boot against an already-initialized volume, `PG_VERSION` present) it SKIPS initdb entirely and just launches `postgres`; crash/WAL recovery runs automatically. Running initdb against a non-empty PGDATA would fail or destroy data, so `PG_VERSION` presence is the exact discriminator. `fsync=on` (the volume IS the durability story, never traded for speed); `shared_buffers=128MB` fits the 512Mi guest (the fc-base sizing coupling sizes memMib to the warm peak).
- THE CR: `chart/templates/workload-scratch-postgres.yaml`, class stateful, `source.image` pinned via `scratchPostgres.guestImage` (build-time digest pin, `port: 1027` + `readyPath: /shim/ready` for the base-build vsock contract), `stateful` port 5432 / listenPort 5400 (in the 5400-5409 range) / volumeSizeGiB 10 / volumeMountPath /data / idleBankSeconds 600 / maxLifetimeSeconds 604800 / bankedTtlSeconds 2592000 / wakeTimeoutSeconds 60 / `secretRef: <fullname>-scratch-postgres`. resources vcpus 1, memMib 512. Gated on `scratchPostgres.enabled`. The `workloads.scratchPostgres` values entry (rootfsPath + harnessInit `/usr/local/bin/ember-postgres-init`) wires the noded rootfs-builder + `EMBERVM_NODED_IMAGES`; all three refs derive from the one `scratchPostgres.guestImage` block so they always match.
- THE SECRET: an `OnePasswordItem` (`chart/templates/onepassworditem-scratch-postgres.yaml`) syncs the superuser password into the `<fullname>-scratch-postgres` Secret in the embervm namespace, matching the CR's `secretRef`. The 1Password item's field must land as a Secret key named exactly `POSTGRES_PASSWORD` (the env var guest-init decodes and initdb reads). Gated on `scratchPostgres.onepassword.itemPath` being set.
- THE NAMESPACE/SECRET CHOICE FOR THE MONOLITH DSN (decision): the scratch-postgres Secret is created in the EMBERVM namespace (by the embervm chart's OnePasswordItem), but the monolith runs in its OWN namespace and a K8s Secret cannot be read across namespaces. Resolution: the MONOLITH chart has its OWN OnePasswordItem (`projects/monolith/chart/templates/onepassworditem-scratch-postgres.yaml`) syncing the SAME 1Password item into the monolith namespace, so both namespaces get the same password with an in-namespace `secretKeyRef`. Rejected: cross-namespace secret access (not a K8s primitive), a values-provided plaintext password (would put the secret in git/values), and a shared-namespace deployment (the two services are separately owned ArgoCD Applications). The DSN is `postgresql://postgres:$(SCRATCH_PG_PASSWORD)@embervm-serving.embervm.svc:5400/scratch`, host = the embervm serving Service (which routes listenPort 5400 to the workload's VM, waking it on the first connection), password from the monolith-namespace Secret via `secretKeyRef` into a separate `SCRATCH_PG_PASSWORD` env var (so the secret never appears inline; `$(...)` is kubelet-expanded). Both the monolith OnePasswordItem and the DSN env are gated on `scratchPostgres.enabled` AND `scratchPostgres.onepassword.itemPath`.
- FIRST WIRING TARGET (run_python session env): the guest exec protocol carries only code + files (no per-invoke env), so `SCRATCH_POSTGRES_DSN` reaches an agent run_python snippet as a tiny preamble (`sandbox/client._with_scratch_dsn`) that sets `os.environ['SCRATCH_POSTGRES_DSN']` (repr-escaped) in the guest process before the user code runs. A snippet can then psycopg-connect to the scratch datastore. When the DSN is unset (feature off) the code is unchanged. This applies to both the one-shot and sessioned paths (they share the payload dict).
- CHARTS THAT NEED BUMPS: BOTH. The embervm chart (postgres image pin + CR + OnePasswordItem + values) and the monolith chart (the DSN env + monolith OnePasswordItem + values). The orchestrator bumps them; not bumped here.
- STATUS: shipped in this PR EXCEPT the apko lock (needs a `bazel run`/CI regenerate against the Wolfi APKINDEX; a placeholder lock cannot be hand-authored with correct per-package digests) and the live-verification of the exact `postgresql-16` package name / binary paths and that the base-build vsock ready + stateful TCP boot both pass end to end on node-4. The first-boot initdb + PGDATA-on-volume + empty-vs-initialized branch is the crux and is implemented in `runtimes/postgres/guest-init/cmd/postgres_linux.go`.

## D-R5.PR-6.1: composite observability mirrors the stateful spans-plus-Envoy-stats split; the op-count alerts (including fresh_boot) stay honest placeholders
- WHY: R5's composite class added new failure surfaces (set integrity, clock resync, N-VM bank duration + disk pressure) that must be visible before anything real depends on the class. This PR is the observability rung: OTel spans for lifecycle/relight diagnosis, node-Envoy Prometheus stats for what can be alerted on live, status.group for the CR-visible summary, and SigNoz alerts for the new surfaces.
- DECISION: composite gets the SAME two-tier shape as R4 stateful (D-R4.PR-10.1) and R3 serving (D-R3.10.1): OTel spans on the control plane for diagnosis (SigNoz trace view), node-Envoy Prometheus stats for alerting (the control plane stays spans-only, no metric exporter). New GROUP-LIFECYCLE ROOT spans in `Embervm.GroupManager`: `embervm.group.create` (the ordered create sequence), `embervm.group.relight` (the whole-set resume), `embervm.group.fresh_boot` (the relight-fallback fresh boot, carrying `ember.reason` = clock_resync_failed | partial_set | relight_failed | ...), `embervm.group.bank` (the whole-set bank, carrying `ember.pause_spread_ms` = the decision-10 honesty number, the wall-clock span between the first and last member's BANK completing). `embervm.group.forced_roll` is a ROOT span in `Embervm.GroupSweeper.do_force_roll` (the operator-override roll, mirroring the stateful forced_roll span). These are ROOTS not children of a restored remote parent, because a raw TCP accept carries no W3C traceparent to nest under (same as the stateful wake/forced_roll roots).
- PER-MEMBER CHILD SPANS + CTX ACROSS Task.async: each member start/resume/bank loop opens a CHILD span (`embervm.group.member_start`, `embervm.group.member_relight`, `embervm.group.member_bank`) carrying `ember.member` (the expanded member name), `ember.was_relight` (the daemon's verified-relight verdict), and `ember.clock_delta_ms`. The OTel ctx is captured on the GroupManager process (inside the root span) via `OpenTelemetry.Ctx.get_current()` and ATTACHED inside each spawned `Task.async` via `OpenTelemetry.Ctx.attach(ctx)` BEFORE opening the child span, exactly the R0 ctx-across-spawn idiom (Dispatcher's otel_ctx capture across spawn_monitor). Without the attach a child span opened in a Task is a silent orphan (Task.async does not propagate OTel context); this is the load-bearing correctness detail. The activator splice span (`embervm.stateful.splice`, shared by both L4 classes) gained `ember.group` (true for a composite splice) so a group entry-member splice is distinguishable from a stateful one.
- CLOCK-DELTA HONESTY (the -1 sentinel): the daemon's `StartGroupMemberResponse` echoes only `{vm_id, ip, was_relight}` (node.proto:886), NOT a measured clock delta. So `ember.clock_delta_ms` cannot be a real number; it is the `-1` "not reported" sentinel (a concrete integer, never nil, per the OTel Elixir SDK's per-key typing requirement). The clock-resync signal the Task 11 gate needs IS still derivable: `ember.was_relight=false` on a `member_relight` span IS a clock-resync-failed member (a relight the daemon could not verify within its one-second bound, decision 7), and the `fresh_boot` root span's `ember.reason=clock_resync_failed` names the whole-set fallback. If a future daemon change reports the measured delta, it drops into this same attribute with no span-shape change.
- ENVOY GROUP-LISTENER STATS FLOW AUTOMATICALLY (no scrape addition): the composite entry listeners are node-Envoy `tcp_proxy` listeners named `group-<listenPort>` (Task 4's stat_prefix), living on the SAME node Envoy admin the R4 `state-<port>` stateful stats and the R3 serving stats already reach. The serving-envoy DaemonSet already carries `prometheus.io/scrape: "true"` on `/stats/prometheus` (chart/templates/serving-envoy-daemonset.yaml), and that exposition globs ALL of Envoy's stats including the tcp.* namespace both stateful and composite listeners share. So the composite listener stats (`envoy_tcp_downstream_cx_total`, label `envoy_tcp_prefix="group-<port>"`) reach SigNoz with ZERO config change; it was config-and-code-free, purely a consequence of the existing scrape.
- status.group DEBOUNCED WRITE: `Embervm.GroupSweeper.write_group_status/1` writes status.group `{state, members{live,degraded}, setId, subnetCidr}` + groupSummary for every composite workload on the sweep tick, DEBOUNCED by a last-written `{state, live, degraded, set_id, subnet_cidr}` tuple (`group_status_written`), so the K8s API is touched at most once per composite workload per sweep, never per transition. Mirrors StatefulSweeper.write_stateful_status/1 exactly. Disjoint status keys (group/groupSummary) so the merge-patch never clobbers another writer. `state` folds a running-with-degraded_member into the CRD's `degraded` status state; counts come from the instance's member rows (live vm_id + healthy = live, live vm_id + unhealthy = degraded). Warmth-only, per the CRD contract (evaporates on destroy/TTL/fresh-boot). Every field defaults to a concrete "" / 0 so the patch never carries a nil (the router's `:json.encode` nil-renders-"nil" trap).
- THE ALERTS (threshold-0 dry-run, METRIC_BASED_ALERT seam): three new alerts in signoz-addons/alerts, following the dry-run-then-restore convention the R4 embervm alerts use. (1) `embervm-group-bank-watermark`: the composite bank snapshot-disk watermark, the set-size generalization of the R4 embervm-stateful-volume-watermark (a 3-member group banks ~the sum of member memory sizes, a set-size MULTIPLE of one stateful bundle). PLACEHOLDER (the per-workload banked-set bytes live on status.group + the group_banked op, not a metric series); the LIVE node-level floor is the existing embervm-snapshot-disk-usage filesystem watermark, which is composite-aware by construction (a group's member bundles are ordinary snapshot files on the same disk). (2) `embervm-group-wake-failures`: sustained group-wake-failures, the group counterpart of embervm-stateful-wake-failures. PLACEHOLDER for the SAME reason: composite is opaque L4 (a failed wake closes the socket with a normal FIN, no 5xx to count), so the query is the closest-available approximation (a `group-<port>` listener's flat cx_total delta) which cannot distinguish idle-healthy from wake-failing. (3) `embervm-group-fresh-boot`: the op-count alert on group_fresh_booted{clock_resync_failed|partial_set}. PLACEHOLDER per D-R4.PR-10.1 (no op-log-to-metrics bridge exists).
- THE OP-COUNT-VS-SPAN-DERIVED CHOICE FOR fresh_boot (the recorded decision): PLACEHOLDER, not span-derived. The reasons ARE on the `embervm.group.fresh_boot` span (`ember.reason`) and derivable per-member from `embervm.group.member_relight` (`ember.was_relight=false`), so the failures are fully INVESTIGABLE in the SigNoz trace view TODAY. But turning a trace attribute into a FIRING alert needs a spanmetrics-processor bridge (a metric series derived from spans) that this cluster does not run, and this task ships alerts on the METRIC_BASED_ALERT seam, not a trace-based alert type. Chosen over inventing a fake-but-passing metric query (which would silently never fire, indistinguishable from a healthy system, exactly the failure mode the dry-run discipline exists to prevent) and over building the spanmetrics/op-log-to-metrics bridge in this task (a real scope of its own, out of scope for an observability-on-existing-mechanisms rung). So the alert is a placeholder keyed on a not-yet-exported metric (`embervm_group_fresh_booted_total`), with the span attributes as the real investigation surface until the bridge lands. This is the SAME honest posture D-R4.PR-10.1 took for the stateful generation-mismatch + wake-failure alerts.
- CHART BUMPS: the signoz-addons/alerts chart is a GIT-PATH ArgoCD Application (targetRevision HEAD, path projects/platform/signoz-addons/alerts), NOT an OCI-published chart, so adding template files needs NO chart version bump (the sync picks up the new ConfigMaps on the next reconcile). The embervm chart needs a version bump for the control-plane code changes (spans + status.group writer) to deploy; the orchestrator bumps it.
- STATUS: shipped (GroupManager root + per-member child spans with ctx-across-Task.async, GroupSweeper forced_roll span + status.group debounced writer, TcpActivator ember.group splice attribute, the three dry-run alerts, ExUnit for the span attributes + status writer). POST-MERGE (live-SigNoz only): spans + the group-<port> Envoy stats visible from a live create/bank/relight cycle, and a dry-run alert reaching Discord.

## D-TLA.PR-1.1: the adoption spec pilot landed with a safety/liveness config split, 1-node bounds, and a load-bearing-guard finding
- WHY: ADR embervm/006 committed to a scoped TLA+ pilot (adoption protocol first) once the feature rungs stabilized; this PR is pilot PR1: `projects/embervm/specs/adoption.tla` (PlusCal, prose-mapped to dispatcher.ex / node_registry.ex), TLC in CI via a prebuilt toolchain (`bazel/tla`: tla2tools.jar v1.7.4 + Temurin 21 JRE; v1.8.0 rejected because its release asset is rebuilt in place, a moving target no sha pin can hold), and the layer-1 vocabulary sync test (`specs/vocabulary.exs` + `spec_vocabulary_test.exs`, partitioning all 23 node.proto rpc verbs, all 4 health states, and all 63 op-log kinds into modeled/excluded).
- CONFIG SPLIT (deviation from the plan's three cfgs): exhaustive liveness over the full safety state graph was CI-hostile (tens of millions of states), so the positive check is TWO configs: `adoption.cfg` (all six invariants, rich bounds, 18.4M distinct states, ~85s, cache-hit-skipped unless the spec or toolchain changes) and `adoption_liveness.cfg` (EventuallyDispatched only, lean bounds, ~2s). The negative modes are unchanged: `adoption_wedge.cfg` (AdoptionEnabled FALSE) re-finds the dispatch restart wedge as a temporal violation, and `adoption_resurrection.cfg` (ForgetBeforeKill FALSE) re-finds the straggler resurrection as a NoResurrection violation; the driver requires the expected violation text so a crash cannot masquerade as a detection.
- BOUNDS (deviation from the ADR's sketched 2 nodes / 3 VMs): 1 node / 2 VMs / 2 tasks / 2 principals. Both historical bugs are single-node phenomena (one node's status stream vs the control plane), so one node is the minimal faithful witness; two VMs still exercise double-assign and adoption idempotence; 2n/3v blew the safety space to ~45M states for no new interleaving class.
- THE FINDING (pilot value, no code change): modeling `known_vm_ids/1` with only two of its three sources (inventory + in-flight miss meta, omitting every assign worker's `meta.vm_id`) produced a real double-assign counterexample in TLC. The three-source union is load-bearing exactly as written; the spec now fails if a future change narrows it. No interleaving was found where the SHIPPED implementation violates an invariant.
- LAYER-2 (trace validation) is PR2, per the plan; the ADR's exit judgment happens after that.

## D-PLACE.1: placement scores fullness, and the two tiers get opposite policies

- **WHY:** ADR embervm/016 section 4 and ARCHITECTURE.md section 7 both described a
  pack-to-empty score as BUILT behaviour. It did not exist: `grep score` over
  `control/lib/` returned nothing. Selection was `BrickLedger.choose/2`
  (`phash2(key, len)` into a sorted list), two copies of `rendezvous_pick`, and
  `PoolManager`'s round-robin cursor, all of which distribute uniformly. ADR 016's
  own alternatives table REJECTS spread placement by name, predicting "no brick
  ever empties, EmberPool cannot shrink, consolidation never fires". That
  prediction had come true: `brick_controller.ex` `pick_victim` requires
  `live_vms == 0`, so scale-down was structurally starved and was recorded as
  waiting on soak when it was actually waiting on this.
- **DECISION:** one ordering primitive, `Embervm.Placement.Score.order/2`, with two
  policies on one comparable scale. Classed bricks PACK (k8s MostAllocated,
  `(slot_ratio + mem_ratio) / 2`, range `[0.0, 1.0]`). Wildcards SPREAD (k8s
  LeastAllocated, `-1.0 - slot_ratio`, range `[-2.0, -1.0]`), which keeps every
  wildcard below every classed brick while spreading among themselves.
- **WHY WILDCARDS SPREAD RATHER THAN PACK:** an intermediate version packed
  everything, and that was wrong. A wildcard (empty `size_class`, or an unreadable
  cgroup reporting zero budget) has NO fullness signal, so all of them score
  identically, the hash pins a workload to one, and `Placement.mem_eligible?/2`
  short-circuits true for a wildcard regardless of headroom. Nothing then
  dislodges it on memory and the only brake is its slot count. That is not
  packing, it is concentrating a workload onto one arbitrary brick with the memory
  gate switched off. The justification does not survive either: MostAllocated
  exists to make bricks reclaimable, and a wildcard is never a reclaim target (it
  is the legacy DaemonSet, one per node, or a fault condition). k8s ships both
  plugins and LeastAllocated is its scheduler default.
- **ONE PRIMITIVE, NOT TWO:** `BrickLedger.choose/2` became
  `order(...) |> List.first()` and `Placement.sort_by_choose_key/2` was deleted.
  Those two previously maintained the same ordering in parallel, kept in sync by a
  comment asserting "head == choose result". Making one a projection of the other
  removes the drift seam rather than documenting it.
- **NON-OBVIOUS PROPERTY:** equal-fullness bricks tie and fall through to the
  pre-existing sticky hash, so behaviour is unchanged wherever there is nothing to
  pack toward. Both existing "spreads distinct keys" tests kept passing UNMODIFIED,
  because their fixtures happen to be equal. A green suite is therefore not
  evidence the packing works; that needs fixtures which differ in fullness.
- **ALSO FIXED:** `PoolManager.consume/2` advanced `budget` and `mem_headroom_mib`
  but never `live_vms`, so the classed `slot_ratio` term was frozen within a
  refill pass and packing ran on the memory term alone.
- **STATUS:** shipped, PR #4142.

## D-PLACE.2: brick class is a supply-side construct only; best-fit replaces class-exactness

- **WHY:** ADR embervm/016 section 4 says "a VM places only into bricks of its size
  class, cross-class borrowing is rejected". Two problems. First, it is NOT
  implemented: no live placement path filters on `size_class`, and
  `BrickLedger.candidates/3` and `pick/4` have zero production callers (every
  reference to them is a comment explaining why they are not used). Second, ADR
  embervm/021 makes `memMib` the only declared dial with CPU derived, so a
  workload has no class at all and the rule has nothing to match on.
- **DECISION:** class is supply-side only. It survives as pod shape (a brick is a
  Guaranteed-QoS pod with a fixed limit, and in-place resize is retired by ADR 013
  section 7, so supply is necessarily discrete even though demand is continuous),
  as reclaim granularity, and as procurement (you cannot instantiate "1536 MiB of
  brick", so `class_for_need` must pick a shape). Placement matches on declared
  memory, which is what the code already did.
- **SECTION 4's GOAL IS KEPT, ITS MECHANISM IS NOT:** the stated goal, do not strand
  a large brick's contiguous headroom, is right. Best-fit achieves it without
  class-exactness. The ratio-based score already gets most of the way (a 2gi brick
  holding one 512 MiB VM scores ~0.33 against a 16gi's ~0.03), but two EMPTY bricks
  score exactly 0.0, tie, and the hash can put a small VM on the empty 16gi. That
  brick exists at `minReplicas: 1` specifically to hold a ~10 GiB composite, and a
  small VM there makes it non-idle and unreclaimable.
- **THE ORDERING IS THREE LEVELS, and the naive version is wrong:** rounded score
  descending, then `mem_budget_mib` ascending, then the sticky hash applied WITHIN
  bricks sharing both. Sorting the tie group by capacity and rotating the whole
  sorted list does NOT work, because rotation puts `Enum.at(sorted, hash_index)` at
  the head rather than the smallest brick. Sub-group by capacity first, rotate only
  within each sub-group.
- **FIT IS A TIE-BREAK, NEVER A COMPETITOR TO THE SCORE:** a 16gi brick at higher
  fullness still beats an emptier 2gi, because concentrating on the already-filling
  brick is what lets the other empty and be reclaimed. Fit only decides between
  equally-full bricks. Inverting that precedence would trade reclaim for
  fragmentation.
- **NO ADR AMENDMENT:** recorded here rather than amending ADR 016, by owner's call.
  `mem_budget_mib` is already on every normalized brick and is usable capacity as
  the daemon computes it, so this needed no new plumbing.
- **STATUS:** shipped.

## D-PLACE.3: proto3 has no null, so an unset scalar is a zero, and four bugs came from reading it as one

- **WHY:** four defects in one family surfaced in a single session, all the same
  shape: a value meaning "unset" on one side read as a number on the other.
  #4101 (noded advertises `SlotCeiling` but admits on raw `cfg.MaxLiveVMs`), #4141
  (the CP placed on `headroom >= need` while noded admits on `need + floor`), #4145
  (denial attribution used the class nameplate rather than usable capacity), and
  the near-miss in #4145's own fix.
- **THE NEAR-MISS, recorded because it nearly shipped:** the new floor lookup was
  written `Map.get(fact, :mem_reject_floor_mib) || 512`. `||` catches only `nil`
  and `false`, and **0 is truthy in Elixir**. That case is live, not hypothetical:
  `node_registry.ex:646` copies the value straight off the decoded proto, and
  proto3 decodes an unset `uint64` as 0, so every daemon predating the field
  reports a PRESENT zero. The floor would have read 0 and attribution would have
  reverted to exactly the defect the PR closes.
- **DECISION:** read it as noded reads it. `noded/server/pressure.go` `memRejectFloorMib`
  treats `<= 0` as unset and falls back to 512, so the control plane does
  `floor when is_integer(floor) and floor > 0 -> floor; _ -> 512`. Reading it
  literally would have been a FOURTH advertise-versus-enforce divergence. The
  regression test is named after the bug so a future simplification back to `||`
  fails loudly.
- **THE GENERAL RULE:** on any never-converging retry loop, suspect that the two
  sides disagree by exactly one term. And when a proto scalar can legitimately be
  zero, carry an explicit presence signal rather than inferring it. ADR embervm/020
  already anticipated this in writing by specifying its capability bit as an
  explicit bool rather than inferring it from a zero reserved count.
- **STATUS:** shipped, PR #4150.

## D-PLACE.4: class capacity is chart-declared, and reconciled against node truth

- **WHY:** `class_for_need` parsed the class nameplate out of the label
  (`"2gi"` -> 2048). Real schedulable memory is `MemBudgetMib()`, which subtracts
  `DaemonReserveMib` (default 512), so a 2gi brick's usable budget is **1536**, not
  2048 and not the ~1933 that `MemHeadroomMib()` reports (headroom does NOT
  subtract the reserve; earlier write-ups conflated the two). Cross-check:
  `SlotCeiling` = 1536/512 = exactly 3, which is the `max_live_vms=3` observed in
  #4101.
- **DECISION:** the chart declares `usable_mib` per class, and
  `bricks.daemonReserveMib` renders BOTH that arithmetic and noded's own
  `EMBERVM_NODED_DAEMON_RESERVE_MIB`, so the two cannot drift. Hardcoding 512 in a
  Helm template would have repeated the duplicate-constant mistake that caused
  #4141.
- **DECLARED RATHER THAN OBSERVED, deliberately:** denial attribution and the future
  floor bin-pack both need class capacity AT ZERO REPLICAS, when no brick of that
  class exists to report anything. A live brick's reported `mem_budget_mib` becomes
  a drift CHECK against the declared value instead. Declared intent reconciled
  against node truth is invariant 5's shape.
- **CONSEQUENCE TO WATCH:** semgrep needs 1536 and a 2gi brick's usable budget is
  exactly 1536. With the floor term it is correctly excluded; once declared
  arithmetic retires the floor, 2gi becomes a legitimate but exactly-fitting
  target. The real cushion then is the 512 MiB declared reserve against actual
  daemon RSS, and the commonly-cited ~115 MiB figure for that RSS is an INFERENCE
  from one idle-state reading (`2048 - 1933`), not a measurement.
- **STATUS:** shipped, PR #4150.

## D-PLACE.5: the probes retry through a control-plane roll, and that is not the same as surviving one

- **WHY:** `bazel-query` and `semgrep` are `class: task`, so the hit/miss invariant
  puts the control plane on their dispatch path BY DEFINITION, and `demo-postgres`
  needs it to wake. The CP runs one replica with `strategy: Recreate`, so every
  roll is 15 to 60s of total downtime. `probe_bazel`, `probe_semgrep` and
  `probe_postgres` had NO retry at all, and `health.py` latches red on a single
  failed row against a `*/5` cadence. So every roll that overlapped a probe run
  paged, with the same component signature as the genuine #4137 outage.
- **DECISION:** a bounded 90s wall-clock retry budget at 15s intervals on the three
  CP-dependent probes. `probe_pages` is untouched: it exercises the public edge,
  which does not depend on the CP, and its existing 0.2s retry is tuned for a
  Cloudflare blip.
- **WHY 90s:** the bound is the CronWorkflow trigger's documented **180s API request
  timeout**, not its 300s `activeDeadlineSeconds`. All four probes run
  concurrently, so wall time is the slowest rather than the sum, and 90s leaves 90s
  of margin inside the request timeout while staying well under the 300s cadence so
  `concurrencyPolicy: Forbid` runs cannot pile up.
- **DETECTION IS NOT BLUNTED:** a real outage persists (#4137 ran over an hour) and
  still latches on its first probe run; the cost is 90s of extra latency on the
  first red. A recovery after retrying APPENDS the retry count to the detail text,
  because a silent recovery is indistinguishable from a clean run and would hide a
  flapping control plane.
- **WHAT THIS IS NOT:** it does not make task dispatch survive a CP restart. That
  needs the CP off the task path, which is ADR embervm/015's isolated lane (Envoy
  to per-brick listeners, brick pops from its local primed pool). This makes the
  HEALTH CHECK survive a roll. A retry that masked a genuine dispatch outage would
  be strictly worse than the current red.
- **STATUS:** shipped, PR #4151.

## D-PLACE.6: capacity accounting is two-tier, brick-authoritative and CP-advisory

- **WHY:** EmberVM schedules against OBSERVED cgroup usage where Kubernetes
  schedules against DECLARED, RESERVED requests. `budget.go` computes headroom as
  `memory.max - working_set` and `pressure.go` admits on it. k8s increments
  `NodeInfo.Requested` at ASSUME time before the pod runs, and kubelet admission
  checks the same arithmetic; observed usage appears only in eviction. Most of the
  compensating machinery here (the reject floor cushion, `Placement.Retry`,
  `minSlotWorkloadMib`, the #4101 advertise/admit split) exists only because the
  number is a measurement rather than a reservation. `pool_manager.ex` `consume/2`
  is already a reservation ledger, scoped to one tick inside one module.
- **DECISION (direction, tracked on #4140):** two tiers with asymmetric authority
  and the same arithmetic. The BRICK is authoritative: noded's claims ledger plus a
  unified `admitVM` gating `claimed_mib + need <= MemBudgetMib()`, O(1), per brick,
  no coordination, passed by every VM creation regardless of initiator. The CONTROL
  PLANE is advisory, at ASSIGNMENT granularity, cell-scoped: it predicts what the
  brick will admit and a wrong guess costs one rejected RPC.
- **WHY NOT CP-AUTHORITATIVE:** ADR embervm/020 decision 1 says the CP "remains a
  placement engine" but computes "at forecast cadence rather than per arrival, with
  the decision advisory rather than authoritative because the brick's
  `admitOrReject` arbitrates". ADR embervm/020 decision 4 independently requires
  brick-side assume-time reservation: "Candidates decrement capacity at ACCEPT under
  a short reservation TTL, and an inbound acceptance runs the same `admitOrReject`
  predicate". Building a CP-authoritative gate would have cost the same and then
  needed unwinding.
- **A MISREADING CORRECTED, recorded so it is not repeated:** ADR embervm/015's
  "balancing is least-request plus cheap rejection, NOT a capacity ledger" was read
  as exempting the isolated lane from memory accounting. It does not. That phrase
  is in the BALANCING decision and rejects a central ledger for routing requests
  across bricks. Decision 4 of the same ADR puts the CP squarely in that lane's
  loop: "It sizes each brick's primed pool from observed demand (the existing
  PoolManager refill loop)". The lane is memory-neutral per request under declared
  arithmetic, because a primed VM is a restored snapshot already holding its full
  declared `MemMib`; a pop converts a primed reservation into a running one of
  identical size and destroy plus background re-prime nets to zero. So CP
  accounting at POOL granularity is exact for that lane without ever seeing a
  request.
- **WHY IT HOLDS AT A MILLION SANDBOXES:** CP ledger write rate is the
  assignment-change rate, not the miss rate. Cardinality is live VMs per CELL,
  bounded by cell fleet memory over smallest VM, never by sandbox-definition count,
  because a banked artifact reserves no memory.
- **STATUS:** decided, not yet built. Sequenced on #4140.
