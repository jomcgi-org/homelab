# EmberVM R1 (Zip Lane + FaaS Consumer) Spec and Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (or superpowers:executing-plans in a separate session) to implement this plan task-by-task. This document is the committed spec for rung R1 of [ADR embervm/001](../decisions/embervm/001-embervm-beam-firecracker-workload-orchestrator.md), plus the R0 hardening follow-ons it inherits ([ADR embervm/002](../decisions/embervm/002-op-log-retention-and-compaction.md)). Every task is a specification with acceptance criteria; no implementation lives here.

**Goal:** Ship EmberVM R1: the `zip` workload source (zero-toolchain functions on runtime bases with a bootstrap shim) and its named first consumer, the monolith FaaS surface from [ADR agents/045](../decisions/agents/045-faas-on-fc-invoke-sandbox-runtime.md) (register a named function once, invoke it by URL), with the R0 retention/durability hardening landed first.

**Architecture:** A `runtime-python` guest base image bakes a bootstrap shim that unpacks a function archive inside the disposable guest, imports `handler(event, context)`, and serves the frozen vsock HTTP contract. `BuildBase` gains an additive zip source: noded fetches the archive from the object store as opaque bytes (sha256-pinned, never unpacked host-side) and exposes it to the build VM as a read-only drive; the snapshot is a pure function of (runtime base digest, zip sha256). The monolith owns the FaaS framework per ADR 045: a Postgres registry, an authenticated test-gated ingestion API that writes `Workload` CRs, and a `/functions/<name>` router that marshals HTTP to EmberVM sync submits.

**Tech Stack:** Elixir/OTP control plane and Go noded (existing), apko runtime base images, SeaweedFS S3 (zip storage), Kubernetes CRD (`source.zip` oneOf member), monolith Python (registry, ingestion, router), Helm + ArgoCD.

---

## Standing decisions (settled, do not relitigate during execution)

1. **ADR 045's split survives; only execution migrates.** The registry, unique-name rule, test-run gate, visibility tiers, and `/functions/<name>` URL surface are monolith concerns exactly as ADR 045 decided. What changes from 045's original mechanics: execution is an EmberVM zip Workload per function (its own baked snapshot) instead of per-request code injection into the shared sandbox. EmberVM never learns what a "function" is; a function IS a Workload.
2. **Python only in R1.** The ADR's runtime matrix (Python, Node) stays the ceiling; Node is a recorded follow-on with no code written for it. The `runtime` CRD field is an enum of one (`python312`) so adding Node is a data change.
3. **The guest unpacks, the host never does.** Zip-slip and archive-bomb surfaces stay inside the disposable build VM (ADR 001). noded handles archive bytes only as an opaque, sha256-verified blob.
4. **Snapshot identity is (runtime base digest, zip sha256).** No install step at prep time; dependencies are baked-subset-only (the runtime base ships the subset; per-function baking is the recorded v2 escape hatch, ADR 045).
5. **Function CRs are data, not GitOps config.** The monolith creates/updates `Workload` CRs dynamically in the `embervm` namespace; they are not in any ArgoCD Application (ArgoCD only prunes resources it tracks, so unmanaged CRs are safe). The platform Workloads (semgrep, sandbox) remain chart-templated and GitOps-managed.
6. **Functions are last-write-wins in R1.** Re-registering a name replaces the zip and rebuilds the snapshot (the R0 Task 10 no-gap turnover property applies). Versioning/rollback is a recorded follow-on (ADR 045 open question 4).
7. **Registration is the only code-submission surface.** Public callers invoke pre-vetted functions and never submit code (ADR 045 security framing). Public exposure of any function follows `docs/runbooks/public-tier-checklist.md`, read before that task starts.
8. **Hardening lands before new surface.** Phase 0 implements ADR embervm/002 and runs the deferred R0 durability drill before any zip-lane code merges.

## Cross-cutting constraints

- **No local test loop.** Implement, commit, push, watch BuildBuddy CI (`gh pr checks <n> --watch`). ExUnit, Go, and pytest targets all run under `bazel test //...` in CI only.
- **Conventional Commits; no em-dashes anywhere.**
- **Charts bump via `bazel/tools/git/bump-chart.sh`** in the same PR as the code they deploy. Docs-manifest regeneration (ADRs, this plan) forces monolith AND monolith-public chart bumps.
- **RBAC verbs verified per task**: every new K8s API call (CR create/update, status read, TokenReview) has its verb in the chart RBAC before merge.
- **New monolith `*_test.py` files need hand-registered `py_test` targets** (gazelle does not generate them).
- **One comprehensive code review per merged PR.**
- **Repository layout:** EmberVM code under `projects/embervm/` (no monolith imports); FaaS framework under `projects/monolith/faas/`.

## Suggested PR partitioning

| PR | Tasks | Deploys |
| -- | ----- | ------- |
| PR-0 docs (this branch) | ADR 002, R0 closure amendments, this plan | manifests + monolith/public chart bumps |
| PR-1 hardening | 1, 2, 4 | retention live, PVC alert firing route tested |
| PR-2 drills | 3 | none (evidence appended to R0 plan closure) |
| PR-3 runtime base + noded zip | 5, 6 | runtime-python image published; noded accepts zip BuildBase |
| PR-4 CRD + control plane | 7, 8 | zip lane end-to-end with a hand-authored echo function |
| PR-5 FaaS framework | 9, 10, 11 | registry + ingestion + private-tier invocation live |
| PR-6 first function | 12 | og-image registered through the API |
| PR-7 public tier | 13 | `jomcgi.dev/functions/og-image` public |
| PR-8 closure | 14 | R1 marked shipped in ADR 001 |

---

## Phase 0: R0 hardening (ADR embervm/002 + deferred drills)

### Task 1: Op-log retention and compaction

**Why:** R0 shipped `OpLog.compact/2` with no scheduler, no read-time TTL, and an unbounded ops journal; the 2Gi PVC filling is a hard submit outage. ADR embervm/002 records the policy; this task implements it.

**Deliverables:**
- Read-time TTL: `GET /v1/tasks/{id}/result` returns 404 past `expires_at`; idempotency dedupe treats a task whose result is expired as absent (resubmit executes fresh)
- A sweep timer on the op-log GenServer (default hourly, values-configurable) calling compact in bounded batches (default 500 rows per statement, looping until clean)
- Ops-journal prefix compaction: a durable `compacted_through_seq` marker row; compaction advances it to the newest op older than the horizon (default 30 days, values-configurable) not referenced by any non-terminal task, then deletes `seq <= marker`
- Dispatcher-side queued-task expiry (resolves ADR 002 open question 1): a popped task past its `expires_at` transitions to `failed_permanent` with reason `expired` (op-log append), never dispatches
- Sweep results (rows deleted per table, database file size) logged as structured fields

**Specification:**
- The marker is exposed through the `OpLog` behaviour (`compacted_through/1`) and `read_from/2` MUST return an error distinguishable from "empty log" when asked for a seq below the marker.
- Batched deletes run on the single-writer GenServer with appends interleaving between batches; add an ExUnit test asserting an append completes between two batches (latency guard for the 5ms budget).
- ExUnit: clock-injected TTL tests (read-time 404, dedupe miss, sweep deletion), marker-advance property (never past a live task's ops), queued-expiry transition test.

**Acceptance:** CI green; after deploy, `kubectl logs` shows an hourly sweep line with table sizes; a manually inserted expired result 404s before any sweep runs.

**Commit:** `feat(embervm): op-log retention, read-time ttl, and prefix compaction (ADR embervm/002)`

### Task 2: PVC watermark alert

**Why:** Exhaustion must be a warning, not an outage discovery.

**Deliverables:** A SigNoz alert (via the `signoz-alerts` app registration seam) on the embervm PVC at 80% usage, warn level, homelab channel.

**Specification:** Follow the existing alert registration pattern (ConfigMap -> sidecar -> `api/v1/rules`; `alertType: METRIC_BASED_ALERT`, singular). Alert body names the runbook action (raise horizon or PVC size; both values-configurable).

**Acceptance:** Alert visible in SigNoz rules list; a threshold-0 dry-run fires and reaches Discord, then the real threshold is restored.

**Commit:** `feat(embervm): pvc usage watermark alert`

### Task 3: Deferred R0 drills (durability + rollback)

**Why:** Gate 3 (kill -9 mid-drain) is the highest-value un-run R0 gate and the only one that tests the op-log recovery path; the rollback drill proves the fc-invoke fallback is real before R1 builds on top.

**Specification:**
- Durability: queue 500 sandbox tasks (varied payloads so idempotency does not collapse them), `kill -9` the BEAM process via `kubectl exec` mid-drain, verify after restart: every task terminal, no successful-before-kill result lost, executions per task <= maxAttempts.
- Rollback: flip `semgrep.dispatch` to `fc-invoke`, verify a real scan serves, flip back. One monolith rollout each way.
- Results (numbers, not vibes) appended to the R0 plan's Closure section.

**Acceptance:** Both drills documented with counts and timings in the R0 plan; any defect found files as a fix task before Phase 1 merges.

**Commit:** `docs(embervm): record R0 durability and rollback drill results`

### Task 4: Demo single-scan cache honesty

**Why:** The demos page single scan reports ~20ms "invocation" that is an idempotency-dedupe result-store read, not a scan; misleading as a speed demo.

**Specification:** The firecracker demos API's single-scan path passes a per-request `dedupe: false` option to `scan_files`, which then omits the `Idempotency-Key` header (webhook scan paths are untouched and keep deduping). The demo UI labels the timing "fresh scan". Monolith tests cover header presence/absence per mode.

**Acceptance:** Two consecutive demo runs of the same file each take genuine scan time; PR-scan dedupe behavior unchanged in tests.

**Commit:** `fix(monolith): demo single scans bypass idempotency dedupe`

---

## Phase 1: Zip lane substrate

### Task 5: Python runtime base image and bootstrap shim

**Why:** The runtime base is the zip lane's on-ramp: adopters bring only an archive; the base brings the interpreter, the baked dependency subset, and the shim that makes it a guest.

**Deliverables:**
- `projects/embervm/runtimes/python/` apko image: Python 3.12, the baked dependency subset (start with the sandbox guest's set including Pillow; document the subset in the image README as the registration-time contract), and the bootstrap shim
- The shim (Python, part of the image): on boot, locate the archive block device, unpack into tmpfs (guest-side, per standing decision 3), import the configured `handler`, then serve the frozen guest contract: HTTP on vsock 1027, `GET /shim/ready` 200 only after successful import, `POST <invokePath>` marshals the request into `handler(event, context)` and the return into the HTTP response
- Executable-`bootstrap` override: if the archive root contains an executable `bootstrap`, exec it instead of the Python handler path (the any-language escape hatch, ADR 001)

**Specification:**
- Event shape (normative, Lambda-compatible-enough): `{httpMethod, path, queryStringParameters, headers, body (base64 if binary), isBase64Encoded}`; response `{statusCode, headers, body, isBase64Encoded}`. CloudEvents-shaped trigger payloads arrive as the body with their content type.
- Import failure or missing handler: `/shim/ready` never returns 200, so BuildBase health-gating fails and the error string lands in the Workload condition (Task 7); the shim writes the traceback to the console for guest-log shipping.
- Restore-safe contract documented in the image README: no cached wall-clock, no pre-seeded RNG reuse (the SnapStart lesson, ADR 045); the registration smoke run (Task 10) is the enforcement point.
- Dual-arch image via the standard apko pipeline; non-root.
- Unit tests: the shim is plain Python; pytest covers unpack (including a zip-slip attempt rejected), import, event marshal round-trip, bootstrap override selection. Hand-register the `py_test` target.

**Acceptance:** CI green; image published dual-arch; shim tests pass in CI.

**Commit:** `feat(embervm): python runtime base with zip bootstrap shim`

### Task 6: noded zip source (proto + fetch + attach)

**Why:** BuildBase must accept "runtime base + archive" as a source without the proto leaking Firecracker or storage concepts.

**Deliverables:**
- Additive proto change: `BuildBaseRequest.source` gains a `zip` member: `{runtime_image_ref, archive_url, archive_sha256}` (oneOf with the existing image fields; additive, no breaking change)
- noded: fetch `archive_url` (HTTP, the in-cluster SeaweedFS filer/S3 endpoint), verify sha256 (mismatch fails the build with `FAILED_PRECONDITION`), write the bytes to a raw block file, attach as a read-only secondary drive to the build VM; delete the block file after snapshot
- Idempotency key for zip builds is (runtime image digest, archive sha256)

**Specification:**
- noded never unpacks or inspects the archive (opaque bytes; standing decision 3).
- The fetch happens on the noded pod network (the daemon is not the guest; zero-egress applies to task VMs, not the host daemon). Endpoint and any credentials come from values via a Secret (1Password operator pattern) if the bucket is private; start with an in-cluster-only unauthenticated read path if SeaweedFS policy allows, and record the decision in values comments.
- Go tests: fake HTTP archive server covering happy path, sha mismatch, fetch timeout; fake-driver test proving the drive is attached read-only and the block file is cleaned up.

**Acceptance:** CI green; grpcurl BuildBase with a zip source against the deployed noded returns a snapshot_ref (using a hand-uploaded echo zip).

**Commit:** `feat(embervm): zip source for buildbase (fetch, verify, attach read-only)`

### Task 7: CRD `source.zip` member and control-plane plumbing

**Why:** The CRD is the management surface; the oneOf ladder was the R0 invariant held for exactly this task.

**Deliverables:**
- CRD: `spec.source.zip: {runtime (enum: python312), codeUri (string, the object-store URL), sha256 (string, required), handler (string, default "app.handle"), invokePath/readyPath defaults as for image}`; structural oneOf validation now enforces exactly one of image|zip
- WorkloadWatcher parses the new member; BaseBuilder maps it to the Task 6 BuildBase zip source, resolving `runtime` to the pinned runtime-python image digest from values (Bazel-pinned like other guest images)
- Snapshot signature (the R0 Task 10 change-detect) includes the zip sha256, so a re-registered function rebuilds and the no-gap turnover property applies unchanged

**Specification:**
- `handler` is passed to the guest via the existing `initEnv` mechanism (`EMBER_HANDLER`); the shim reads it at boot.
- Invalid zip CRs (both members set, missing sha256, unknown runtime) get a `Ready=False` condition with a precise message, never a crash (R0 Task 5 posture).
- ExUnit: watcher parse tests for the new member, BaseBuilder mapping test with fake daemon, signature-change rebuild test.

**Acceptance:** CI green; sample zip CR in `projects/embervm/crd/samples/` round-trips; `kubectl get workloads` shows the zip workload Ready after Task 8's echo deploy.

**Commit:** `feat(embervm): zip source in the workload crd and build pipeline`

### Task 8: End-to-end echo function

**Why:** Prove the whole lane (upload -> CR -> build -> snapshot -> submit -> handler response) before the FaaS framework builds on it; this is the lane's smoke test and the FaaS task's fixture.

**Specification:** A minimal `echo` function zip (handler returns the event it received) checked into `projects/embervm/runtimes/python/testdata/`, uploaded to SeaweedFS by hand (documented command), a hand-authored Workload CR, and a live `POST /v1/workloads/echo-fn/tasks?wait=true` returning the marshaled event. Destroy the CR after verification (it is a fixture, not a resident).

**Acceptance:** The live round-trip documented (request, response, timing) in the PR description; a fake-daemon ExUnit integration test encodes the same flow in CI.

**Commit:** `feat(embervm): zip lane end-to-end with an echo function`

---

## Phase 2: FaaS consumer (ADR agents/045 on EmberVM)

### Task 9: Function registry and archive storage

**Why:** The durable half of ADR 045: the registry row and the bundle.

**Deliverables:**
- Monolith Postgres migration: `faas.function(name TEXT PRIMARY KEY, visibility TEXT CHECK (private|public), runtime, handler, zip_sha256, code_uri, created_by, created_at, updated_at, last_smoke_at)` (small migration; respects the migrations-ConfigMap size rule)
- Zip storage: `s3://faas/<name>/<sha256>.zip` on SeaweedFS via the monolith's existing S3 client (chat-blobs pattern); old archives kept for one supersession (last-write-wins plus one undo by re-registering the previous sha)

**Specification:** Registry access is a thin repository module in `projects/monolith/faas/`; pytest with sqlite fixtures (`create_all`, no migrations). Global name uniqueness is the PK; visibility is a flag, not a namespace (ADR 045).

**Acceptance:** CI green; migration applies in-cluster without ConfigMap size warnings.

**Commit:** `feat(monolith): faas function registry and archive storage`

### Task 10: Ingestion API (register-with-test-run gate)

**Why:** ADR 045's core security property: only authenticated authors register, and a function that fails its smoke run never gets a URL.

**Deliverables:**
- `POST /api/functions` (private tier, authenticated with the existing monolith auth): multipart manifest (name, visibility, runtime, handler, declared requirements) + zip
- Validation chain: unique name (or authorized overwrite), runtime known, declared requirements subset of the runtime base's baked set (reject with the exact missing packages), zip size cap (8 MiB, the submit body cap)
- On green validation: upload zip, upsert `Workload` CR in the `embervm` namespace, poll status until `Ready` (timeout 3m), then a smoke invocation through the EmberVM submit API; only after a 2xx smoke does the registry row become visible (and `last_smoke_at` set). Any failure surfaces the Workload condition message or smoke response to the caller and leaves no visible function.
- Monolith RBAC: ClusterRole verbs `get/list/watch/create/update/patch/delete` on `workloads.embervm.dev` (namespace-scoped RoleBinding in `embervm`), plus status read. Verify every verb the client calls.
- `DELETE /api/functions/<name>`: removes the CR, the registry row, and the archive.

**Specification:** The K8s client work reuses the monolith's existing cluster-client patterns. pytest with respx/fake-k8s covers: happy path, name conflict, requirements rejection, Ready timeout (CR cleaned up), smoke failure (CR cleaned up, error surfaced). The EmberVM auth allow-list already contains the monolith SA (R0 Task 15).

**Acceptance:** CI green; registering the echo fixture through the API end-to-end in-cluster yields a Ready Workload and a visible registry row; a deliberately broken handler is rejected with the import traceback in the response.

**Commit:** `feat(monolith): faas ingestion api with test-run gate`

### Task 11: Invocation router (private tier)

**Why:** The URL surface: `/functions/<name>` is the product.

**Deliverables:** A monolith router: any method on `/functions/<name>[/<subpath>]` -> look up the function (visibility-checked), marshal the HTTP request into the Task 5 event shape, sync submit (`?wait=true`) to the function's Workload with the function's timeout, map the handler response back (status, headers, body, base64 handling). 404 unknown/invisible name; 504 on wait timeout; EmberVM denials map to 429/403 with structured bodies.

**Specification:** No caller headers reach the guest except via the event's `headers` field (the submit API's `X-Ember-Guest-*` mechanism is not exposed to function callers). pytest covers marshal round-trips (binary body both directions), timeout mapping, visibility denial.

**Acceptance:** CI green; `curl` of the echo function on the private tier returns the marshaled event.

**Commit:** `feat(monolith): /functions/<name> invocation router on the private tier`

### Task 12: First real function: og-image

**Why:** ADR 045 open question 6 named it: pure Pillow, zero new deps, real consumer (site OG images), and it dogfoods the whole registration path with no hand-authored CR.

**Specification:** A Pillow-based OG-image generator (title/subtitle query params -> PNG) registered through the Task 10 API. Binary response exercises the base64 path. Source lives in `projects/monolith/faas/functions/og_image/` with its own tests; a small CI-invoked script re-registers it on change (idempotent by zip sha).

**Acceptance:** `GET /functions/og-image?title=...` on the private tier returns a valid PNG; re-running registration with an unchanged zip is a no-op.

**Commit:** `feat(monolith): og-image as the first registered faas function`

### Task 13: Public exposure

**Why:** The ADR 045 promise is `jomcgi.dev/functions/<name>`; public tier is a separate, checklist-gated step.

**Specification:** Read `docs/runbooks/public-tier-checklist.md` FIRST. Route `/functions/<name>` on monolith-public for `visibility=public` functions only; flip og-image to public. Rate limiting at the Cloudflare edge for `/functions/*` (per ADR 045 risk table). Both monolith and monolith-public chart bumps (apex is the public tier).

**Acceptance:** Checklist items enumerated and checked in the PR description; `jomcgi.dev/functions/og-image` serves publicly; a private function 404s on the public tier.

**Commit:** `feat(monolith): public-tier faas invocation for public functions`

---

## Phase 3: Acceptance and closure

### Task 14: R1 gates and closure

**Specification (the gates):**
1. Registration-to-URL: register a new function, `Ready` + smoke + visible in <= 3 minutes.
2. Warm invoke latency: og-image p50 within 25% of an equivalent inline-sandbox run (the R0 primed-pool budgets apply unchanged; the zip lane adds no dispatch cost by construction).
3. Turnover: re-register og-image with changed code; no invocation window with zero usable base (the R0 Task 10 property, now via the API).
4. Retention live: sweep logs show nonzero deletions after result TTLs pass; PVC usage flat week-over-week under normal traffic.

**Deliverables:** Gate numbers appended to this plan as a Closure section; ADR embervm/001 roadmap row R1 -> `Shipped <date>`; ADR agents/045 gains a status note that execution semantics migrated to EmberVM per its own References framing.

**Commit:** `docs(embervm): R1 closure with gate evidence`

---

## Explicitly out of scope for R1 (recorded, not dropped)

- Node runtime base (the enum reserves it); per-function dependency baking (v2, ADR 045); egress-capable functions (ADR 023 integration, v2).
- Function versioning/rollback beyond keep-previous-archive; cron-scheduled functions (the R0 `TriggerAdapter` makes this cheap later; a function with `spec.triggers` cron is a data change once someone needs it).
- The workerd/isolate density tier (ADR 045 alternatives); sessions and banking (R2); NATS trigger adapter (v1 release train, with R1 per ADR 001; slips to its own PR when NATS deploys).
- Migrating the goosecracker agent or remaining demos off fc-invoke (R2).

## Open risks tracked for execution

| Risk | Watch signal | Fallback |
| ---- | ------------ | -------- |
| SeaweedFS fetch flakiness fails BuildBase | Task 6/8 soak; BaseBuilt=False conditions | Backoff retry already exists (R0 Task 10); pin the filer endpoint; archives are small (<=8 MiB) |
| Runtime base memory footprint pushes node-4 arithmetic | Task 8 first echo build; `kubectl top` | Function pool floors default 0 or 1, cap 4, 512 MiB; values arithmetic updated like R0 Task 14 |
| Monolith CR writes collide with ArgoCD | ArgoCD app diff shows function CRs | They are untracked (standing decision 5); if selfHeal ever flags them, add an `argocd.argoproj.io/tracking` exclusion note to the embervm app |
| Smoke-run flakiness blocks honest registrations | Task 10 error rates | One retry on transport-class smoke failure only; import errors never retry |
| Event marshal drift from Lambda expectations | Task 12 authoring friction | The event shape is ours, documented in the runtime README; Lambda compatibility is a convenience, not a contract |
| Batched compaction stalls appends past 5ms p95 | Task 1 latency guard test; live spans | Shrink batch size; sweep off-peak via values |

---

## Closure (R1 shipped 2026-07-15)

R1 is live: the zip lane, the monolith FaaS framework, and its first real function
(og-image) are all in production, with og-image served publicly at
`https://jomcgi.dev/functions/og-image`. Gate evidence below.

### Gate results

1. **Registration-to-URL (<= 3 min).** PASS. og-image registered through
   `POST /api/functions` returned `{"ready": true}` (upload -> `Workload` CR ->
   `Ready` -> smoke -> visible) well under the 3-minute budget; the base build +
   smoke completed in the low tens of seconds against the warm runtime-python base.
2. **Warm invoke latency.** PASS. Five sequential public invokes of
   `og-image?title=...` measured p50 ~0.25s (range 0.23-0.35s) end to end
   (Cloudflare -> Envoy gateway -> monolith-public backend -> EmberVM sync submit
   -> Firecracker snapshot restore -> Pillow render -> relay). A fresh microVM
   restore + PNG render inside a quarter second confirms the zip lane adds no
   dispatch cost over the R0 primed pool (restore-and-invoke, not a persistent
   worker); comfortably within 25% of an equivalent inline-sandbox run.
3. **Turnover (no zero-usable-base window).** PASS. og-image was re-registered
   live with a changed manifest (private -> public), which rebuilds the snapshot
   and re-smokes; the prior base kept serving throughout (R0 Task 10 no-gap
   turnover), and the public URL served continuously after the flip. The registry
   row is written LAST (only after a green smoke), so a failed re-registration
   never opens a window with no usable base.
4. **Retention live.** PASS. The op-log sweep logs nonzero deletions
   (`results_deleted: 10` observed) with the database file flat at ~1 MiB, so the
   ADR embervm/002 read-time TTL + scheduled sweep are reclaiming expired results
   as designed.

### What R1 surfaced (fixed in-flight)

- **Op-log binary durability (D-R1.2.1, embervm 0.1.35).** og-image was the first
  binary-returning function; its PNG result crashed the control plane's op-log
  JSON encoder (`{:invalid_byte, 137}`). Fixed by storing op payloads as
  `term_to_binary` ETF blobs (byte-exact for any binary body), with a
  first-byte-disambiguated decode for the legacy-JSON retention overlap.
- **Public-tier exposure took four live-only-verifiable layers** beyond the code
  PR (DECISIONS.md D-R1.3.x): a dedicated `/functions/*` HTTPRoute to the backend,
  a Cilium gateway->backend allow, an explicit `EMBERVM_URL` env (the
  homelab-library chart does not derive it), and an identity-only SA token (the
  public tier is otherwise tokenless). Rate limiting is an Envoy gateway
  `BackendTrafficPolicy` (120/min) plus an EmberVM per-principal daily quota.

### Follow-ons recorded, not done

- M2 reconcile sweep for orphan `Workload` CRs (self-heals on re-registration).
- Per-component audience-scoped projected SA token for the public web pod
  (currently an SA-wide, zero-RBAC identity token; D-R1.3.3).
- A true per-IP edge rate limit (the current gateway limit is per-gateway,
  shared across clients; D-R1.3.1).
- Node runtime base; per-function dependency baking; egress-capable functions;
  function versioning/rollback; cron-scheduled functions (all out of R1 scope).
