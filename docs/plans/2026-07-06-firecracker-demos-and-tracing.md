# Firecracker Demos Page + fc-invoke OTEL Tracing Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Note the repo has **no local test loop** (see CLAUDE.md): implement, format, commit, and defer all `bazel test` execution to end-of-plan BuildBuddy CI on the pushed branch. Do not run tests from the workstation.

**Goal:** Ship an authenticated `private.jomcgi.dev/demos/firecracker` page with rich per-project modals (python sandbox, semgrep diff scan, goose agent) that run a real firecracker invocation, show live latency, and embed the real SigNoz trace waterfall for that invocation, backed by newly-wired end-to-end OTEL tracing through fc-invoke.

**Architecture:** The monolith private tier gains a SvelteKit route plus backend endpoints that wrap the existing `sandbox`, `semgrep`, and `goosecracker` handlers and return web-shaped payloads including the captured `trace_id`. fc-invoke (Go) gets a global `TracerProvider`+OTLP exporter, extracts the incoming `traceparent` at ingress, and opens a root span plus child spans for the invocation lifecycle, so a single click produces one connected monolith -> fc-invoke trace. A new backend ClickHouse traces helper queries `signoz_traces` by `trace_id` to rebuild the span waterfall, which the frontend polls (~5-10s ingestion lag) and renders.

**Tech Stack:** Go (fc-invoke, opentelemetry-go SDK, otlptracegrpc), Python (monolith FastAPI backend, existing `home/observability/clickhouse.py`), SvelteKit (frontend), Bazel/apko (build), Helm (deploy), SigNoz/ClickHouse (traces).

**Deferred (NOT in this plan):** guest-interior instrumentation (sandbox/semgrep/goose guest runtimes emit no spans; `guest_exec` stays a black-box span), and agent-egress tracing. Both are fast-follows per the design discussion.

---

## Pre-flight verification (do first, before Task 1)

Two facts must be confirmed against the live cluster; both affect later tasks. Record the answers in the PR description.

1. **SigNoz traces table name + columns.** Our SigNoz version's traces table is assumed to be `signoz_traces.distributed_signoz_index_v3` with columns `traceID`/`trace_id`, `spanID`, `parentSpanID`, `name`, `durationNano`, `timestamp`/`startTimeUnixNano`, `serviceName`. Confirm the real names:
   ```bash
   kubectl -n signoz exec -it $(kubectl -n signoz get pod -l app.kubernetes.io/component=clickhouse -o name | head -1) -- \
     clickhouse-client -q "SHOW TABLES FROM signoz_traces"
   kubectl -n signoz exec -it <clickhouse-pod> -- \
     clickhouse-client -q "DESCRIBE signoz_traces.distributed_signoz_index_v3 FORMAT TSVRaw"
   ```
   Use the confirmed table/columns in Task 6.

2. **Private HTTPRoute allows the new API prefix.** `projects/monolith/chart/templates/httproute-private.yaml` currently passes `/private/`, `/api/knowledge`, `/api/scheduler`, `/api/grimoire`. The new backend endpoints live under `/api/demos/`, confirm whether the private HTTPRoute needs a new prefix match (Task 8) or whether `/private/` reverse-proxying already covers the frontend `+page.server.js` fetches. If the frontend calls the backend server-side (via `API_BASE` inside `+page.server.js`), no HTTPRoute change is needed; if it calls from the browser, add the `/api/demos` prefix.

---

## Track 1, fc-invoke OTEL tracing (Go)

Reference pattern to mirror: `projects/operators/oci-model-cache/internal/telemetry/tracing.go` and its `cmd/main.go:138-160` integration. Collector endpoint (gRPC): `signoz-k8s-infra-otel-agent.signoz.svc.cluster.local:4317`.

### Task 1: Telemetry package + exporter wiring

**Files:**
- Create: `projects/firecracker/substrate/invoke/internal/telemetry/tracing.go`
- Modify: `projects/firecracker/substrate/invoke/cmd/main.go` (run() setup + deferred shutdown)
- Modify: `projects/firecracker/substrate/invoke/BUILD.bazel` (new deps via gazelle)
- Modify: `MODULE.bazel` / go.mod as needed for `go.opentelemetry.io/otel/exporters/otlp/otlptracegrpc`, `sdk/trace`, `sdk/resource` (check if already vendored by oci-model-cache; likely present)

**Step 1: Write `tracing.go`** mirroring oci-model-cache: `InitTracing(ctx) (*sdktrace.TracerProvider, error)` that

- reads `OTEL_EXPORTER_OTLP_ENDPOINT`; if empty, returns `sdktrace.NewTracerProvider()` (no-op export) and logs "tracing disabled", graceful degrade so local/CI runs don't require a collector.
- builds an `otlptracegrpc` exporter `WithEndpoint(endpoint)` + `WithInsecure()`.
- builds a `resource` with `service.name` from `OTEL_SERVICE_NAME` (fallback `"fc-invoke"`) and `service.version` from `OTEL_SERVICE_VERSION` (fallback `"dev"`).
- `sdktrace.NewTracerProvider(WithBatcher(exporter), WithResource(res), WithSampler(parentbased_always_on default))`.
- `otel.SetTracerProvider(tp)` AND `otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(propagation.TraceContext{}, propagation.Baggage{}))`.
- `Shutdown(ctx, tp)` helper with a 5s timeout.

**Step 2: Wire into `main.go`**, call `InitTracing` early in `run()`, `defer telemetry.Shutdown(...)` on drain. Do not `os.Exit` if the endpoint is set but export init fails transiently; log and continue with no-op (a demo page should never take the daemon down).

**Step 3: Local verify**
Run: `bazel/tools/format/fast-format.sh` (updates BUILD deps via gazelle; must succeed)
Run: `cd projects/firecracker/substrate/invoke && go build ./...` (compiles clean)
Expected: builds, gazelle adds the otel deps to `BUILD.bazel`.

**Step 4: Commit**
```bash
git add projects/firecracker/substrate/invoke MODULE.bazel
git commit -m "feat(fc-invoke): wire OTEL TracerProvider and OTLP exporter"
```

### Task 2: Root server span + traceparent extraction at ingress

**Files:**
- Modify: `projects/firecracker/substrate/cluster/ingress/server.go` (~line 64 `ServeHTTP`, ~line 103 handleInvoke)

**Step 1:** At the top of the `/invoke/` branch, extract the incoming context and open a root span:
```go
ctx := otel.GetTextMapPropagator().Extract(r.Context(), propagation.HeaderCarrier(r.Header))
ctx, span := tracer.Start(ctx, "fc_invoke",
    trace.WithSpanKind(trace.SpanKindServer),
    trace.WithAttributes(
        attribute.String("fc.workload", workloadName),
        attribute.String("fc.session", session),
    ))
defer span.End()
```
Define `var tracer = otel.Tracer("fc-invoke/ingress")` at package level. Pass the new `ctx` (not `r.Context()`) into `inv.Invoke(ctx, session, r.Body)` so the whole chain nests. On error paths call `span.RecordError(err)` and `span.SetStatus(codes.Error, ...)`.

**Step 2: Local verify**, `go build ./...`, `fast-format.sh`.

**Step 3: Commit**
```bash
git commit -am "feat(fc-invoke): extract traceparent and open root invoke span"
```

### Task 3: Lifecycle child spans (auth, acquire, warm/cold, wait-ready, guest_exec)

**Files:**
- Modify: `projects/firecracker/substrate/cluster/auth/auth.go` (~line 86, around `reviewer.Review`)
- Modify: `projects/firecracker/substrate/invoke/internal/invoker/invoker.go` (acquire ~217, warm/cold branch ~231, wait-ready ~337, RoundTrip ~357)
- Modify: `projects/firecracker/substrate/node/fcvm/driver/driver.go` (~300 warm restore branch, add `snapshot_restore` span; leave existing `provision_rootfs`/`firecracker_boot`)

**Spans to add** (each `tracer.Start(ctx, name)` ... `span.End()`, threading the returned ctx):
- `auth_tokenreview` around `reviewer.Review` (only reached when `FC_INVOKE_ALLOWED_CALLERS` is set).
- `acquire_slot` around the semaphore acquire.
- Set `pool_hit` bool attribute on the **root** span at the warm/cold decision (invoker.go:231). Add a `snapshot_restore` span on the warm path in `driver.go` loadInto (~300).
- `guest_wait_ready` around `transport.WaitReady`.
- `guest_exec` around `transport.RoundTrip` (invoker.go:357), attrs `fc.workload`, `fc.session`. This is the black-box guest time.

On every span, record errors + set error status so failed invocations are visibly red in the waterfall.

**Step 1-2:** implement, `go build ./...`, `fast-format.sh`.

**Step 3: Commit**
```bash
git commit -am "feat(fc-invoke): add lifecycle spans (auth, acquire, snapshot_restore, wait_ready, guest_exec)"
```

### Task 4: Deploy env + chart bump for fc-invoke

**Files:**
- Modify: `projects/firecracker/.../deploy/values.yaml` (add env)
- Modify via script: `bazel/tools/git/bump-chart.sh projects/firecracker/<chart-path>`

**Step 1:** Add to fc-invoke pod env:
```yaml
OTEL_EXPORTER_OTLP_ENDPOINT: signoz-k8s-infra-otel-agent.signoz.svc.cluster.local:4317
OTEL_SERVICE_NAME: fc-invoke
```
(Confirm the fc-invoke deploy path; it may be a Helm values file under `projects/firecracker/.../deploy/`.)

**Step 2:** `helm template <release> <chart> -f <values>` renders with the env present.

**Step 3:** Bump chart:
```bash
bazel/tools/git/bump-chart.sh projects/firecracker/<chart-path>
git commit -am "build(fc-invoke): export traces to SigNoz + chart bump"
```

---

## Track 2, SigNoz traces query helper (Python backend)

### Task 5: ClickHouse traces query helper

**Files:**
- Create: `projects/monolith/home/observability/traces.py`
- Test: `projects/monolith/home/observability/traces_test.py` (+ hand-add `py_test` to BUILD, see memory `reference_monolith_gazelle_pytest_targets.md`)

**Step 1: Write the failing test**, a unit test that mocks `ClickHouseClient.query_rows` returning fake span rows and asserts `fetch_trace_spans(trace_id)` returns a normalized list of `{span_id, parent_span_id, name, service, start_ms, duration_ms}` sorted by start time, and that a root-less set still renders (parent not found -> treated as root).

**Step 2:** Implement `fetch_trace_spans` using the existing `ClickHouseClient` (mirror `home/observability/stats.py` usage). Query the confirmed table from pre-flight, e.g.:
```sql
SELECT spanID, parentSpanID, name, serviceName, startTimeUnixNano, durationNano
FROM signoz_traces.distributed_signoz_index_v3
WHERE traceID = %(trace_id)s
ORDER BY startTimeUnixNano
```
Convert nanos to ms relative to the min start so the frontend can lay out bars. Return `[]` on unknown trace (still ingesting).

**Step 3: local verify**, `fast-format.sh`; type-check. Test execution deferred to CI.

**Step 4: Commit**
```bash
git commit -am "feat(observability): query SigNoz trace spans by trace_id"
```

---

## Track 3, Demos backend endpoints (Python)

### Task 6: Demos API router

**Files:**
- Create: `projects/monolith/demos/__init__.py`, `projects/monolith/demos/firecracker_api.py`
- Modify: `projects/monolith/app/main.py` (mount the router)
- Test: `projects/monolith/demos/firecracker_api_test.py` (+ hand-add `py_test`)

**Endpoints (all capture the current trace_id via `trace.get_current_span().get_span_context().trace_id` formatted as 32-hex):**
- `POST /api/demos/firecracker/python`, body `{code, files?}` -> calls `sandbox.client.run_python_in_sandbox`; returns `{stdout, stderr, exit_code, duration_ms, trace_id}`.
- `POST /api/demos/firecracker/semgrep`, body `{files:[{path,content}]}` -> calls `semgrep.mcp` scan path; returns `{findings, errors, duration_ms, trace_id}`.
- `POST /api/demos/firecracker/goose`, body `{task, recipe?, tier?}` -> `goosecracker.submit(...)`; returns `{session, thread_id, trace_id}` (async).
- `GET /api/demos/firecracker/goose/{thread_id}`, polls `get_agent_thread`; returns status + result when done.
- `GET /api/demos/firecracker/trace/{trace_id}` -> `traces.fetch_trace_spans` (Task 5) as waterfall JSON; `{spans:[...], complete:bool}` where `complete` is false while empty (still ingesting).

Wrap each invocation in an explicit server span so `trace_id` is always populated and the backend's own timing is recorded even before SigNoz ingests.

**Step 1-4:** failing test (mock the sandbox/semgrep/goose clients, assert shapes + trace_id hex), implement, format, commit.
```bash
git commit -am "feat(demos): firecracker invocation + trace API endpoints"
```

---

## Track 4, Frontend (SvelteKit, rich modals)

Load `impeccable:frontend-design` guidance for the modal polish; match the existing private-tier look (see `/private/notes`, `/private/chat`).

### Task 7: Demos page, modals, waterfall

**Files:**
- Create: `projects/monolith/frontend/src/routes/private/demos/firecracker/+page.svelte`
- Create: `projects/monolith/frontend/src/routes/private/demos/firecracker/+page.server.js` (if server-side fetch chosen, preferred, avoids HTTPRoute change)
- Create: `projects/monolith/frontend/src/lib/components/demos/RunPanel.svelte`, `TraceWaterfall.svelte`, `ProjectModal.svelte`
- Note: SSR deps must be in `ssr.noExternal` (memory `feedback_vite_ssr_noexternal.md`).

**Components:**
- Tabbed launcher (Python / Semgrep / Goose), each opening a rich modal.
- `RunPanel`: input area (Python: code editor textarea; Semgrep: editable textarea pre-populated with a realistic sample diff; Goose: task prompt + recipe/tier selectors), a Run button, a **live latency counter** (ticks while awaiting), a result pane.
- On completion, take `trace_id`, then poll `GET /api/demos/firecracker/trace/{trace_id}` every ~1.5s up to ~10s until `complete` (spans present), rendering `TraceWaterfall` (span bars positioned by start_ms/duration_ms, colored by service, red on error) plus a deep-link to the SigNoz UI trace view (`private.jomcgi.dev/app/signoz/trace/{trace_id}`).
- Goose modal uses the async lifecycle: submit -> show `thread_id` -> poll the goose status endpoint -> render final artifact/result, then hydrate its waterfall.

**Step 1-4:** build the page, `fast-format.sh`, `pnpm`/vite build check if available locally, commit.
```bash
git commit -am "feat(demos): firecracker demos page with modals and trace waterfall"
```

---

## Track 5, Wiring, deploy, CI

### Task 8: Ingress prefix (if needed) + monolith chart bump

**Files:**
- Modify (conditional on pre-flight #2): `projects/monolith/chart/templates/httproute-private.yaml` (add `/api/demos` prefix)
- Bump: `bazel/tools/git/bump-chart.sh projects/monolith`

**Step 1:** If frontend fetches server-side via `+page.server.js`, skip the HTTPRoute change. Otherwise add the `/api/demos` prefix match to the private HTTPRoute.
**Step 2:** Bump monolith chart (Chart.yaml + application.yaml together via the script).
**Step 3: Commit**
```bash
git commit -am "build(monolith): route demos API + chart bump"
```

### Task 9: Push, watch CI, iterate

- Push branch, open PR, `gh pr checks <n> --watch`.
- On red CI: fetch the actual log via `mcp__buildbuddy__get_invocation` (commitSha selector) -> `get_target` -> `get_log`; quote the assertion before hypothesizing (CLAUDE.md).
- Common gotchas to pre-empt: new `*_test.py` need hand-added `py_test` targets (gazelle won't); atlas/semgrep unrelated; SSR deps in `ssr.noExternal`.

### Task 10: End-of-PR review + live verification

- One comprehensive Opus code review of the full diff (per CLAUDE.md cadence: one review per PR, not per task).
- After merge + rollout: run each demo once in the live page, confirm the waterfall renders the fc-invoke child spans (`snapshot_restore`/`provision_rootfs`, `firecracker_boot`, `guest_exec`) and `pool_hit` attribute, and that latency numbers are sane. Verify a warm run shows `snapshot_restore` and a cold run shows the provision->boot pair.

---

## Verification summary (repo-specific)

- **Local per-task:** `bazel/tools/format/fast-format.sh`, `go build ./...` (Go tasks), `helm template` (chart tasks). No local `bazel test`.
- **End-of-plan:** push branch -> BuildBuddy CI runs `bazel test //...` -> read failures via buildbuddy MCP -> fix -> repush.
- **Live:** exercise the page; inspect the trace in SigNoz UI to confirm one connected trace.
