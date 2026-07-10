# Semgrep Pro Load-Test Demo Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task (repo default; do not offer the parallel-session choice). One comprehensive Opus code review per PR at the end, not per task.

**Goal:** Add a "Load Test" sub-tab to the private Firecracker Semgrep demo that drains a corpus of Pro-triggering example files through the fc-invoke semgrep workload as fast as the daemon allows for a fixed 2-minute run, with live throughput counters, a per-scan drill-down, and a resource-efficiency summary (per-scan + aggregate + daemon/node footprint) that extrapolates to a multi-node fleet.

**Architecture:** The load test is a CPU-bound queue-drain. Per-scan cost is ~0.85s of rule matching on 1 vcpu; VM warm-restore is single-digit ms, so aggregate throughput tracks concurrent-VM count until node-4's cores saturate (~15). Three layers change: (1) the fc-invoke daemon is tuned to `concurrency: 15` and surfaces per-scan CPU-ms / peak-RSS as response headers; (2) the monolith runs a detached asyncio drain over-subscribed against the daemon's semaphore, buffers per-scan rows to Postgres, and samples the daemon/node footprint; (3) the SvelteKit demo gains a Single Scan / Load Test sub-tab with live counters and a clickable per-scan table.

**Tech Stack:** Go (fc-invoke substrate daemon, `golang.org/x/sync/semaphore`, OTel), Python (FastAPI monolith backend, httpx, asyncio, psycopg3), Postgres + Atlas migrations, SvelteKit 5 (runes), apko/Helm/ArgoCD deploy, Semgrep Pro engine in Firecracker microVMs.

**Key constraints (read before starting):**
- **No local test loop.** Never run `bazel test`, `pytest`, `go test`, `pnpm test` on the workstation. Implement, commit, push the branch, watch CI (`gh pr checks <n> --watch`). Locally verify only: `helm template`, `GOOS=linux go build`, `pnpm build` (frontend), type-check, and `bazel/tools/format/fast-format.sh`.
- **GitOps.** No `kubectl apply/patch/edit`. Changes land in git; ArgoCD syncs.
- **No em-dashes** in any code, comment, doc, or commit message.
- **Chart bumps are manual and in-PR:** any PR whose chart code must deploy bumps `chart/Chart.yaml` + `deploy/application.yaml` together via `bazel/tools/git/bump-chart.sh projects/firecracker/substrate` (and the monolith chart for backend/frontend/migration changes).
- **Private tier only.** Everything here mounts on `app/main.py`, never `main_public.py`; the `demos/**` glob must never reach the public backend.

**Decisions already made (do not relitigate):**
- Daemon tuned to **max**: `concurrency: 15`, `vcpus: 1`, container mem limit `14Gi -> 26Gi`. node-4 is 16 cores / ~62Gi (currently ~35Gi free). This assumes the sandbox/agent workloads are idle during a load test (explicit operator assumption).
- Corpus = **5 code packs only** (python, golang, javascript/typescript, kubernetes, rust). No SCA manifests.
- Fixed **2-minute** run duration.
- Per-scan resources come from **invoke response headers** (exact, cheap), not per-scan ClickHouse queries. Daemon/node footprint sampled via the k8s metrics API, best-effort, falling back to derived-from-per-scan aggregation.

---

## Reference: current-state facts (verified during exploration)

**fc-invoke semgrep workload** (`projects/firecracker/substrate/chart/values.yaml`, workloads.semgrep):
```yaml
semgrep:
  image: semgrep-guest
  rootfsPath: /disks/nvme-02/fc-invoke/semgrep/rootfs.ext4
  harnessInit: /usr/local/bin/semgrep-guest-init
  vcpus: 1
  memMib: 1536
  concurrency: 2          # -> 15
  egressEnabled: false
  warmBase: true
  readyPath: /shim/ready
  sessioned: false
  requestTimeout: 90s
```
Daemon container resources in the same values.yaml: `requests.memory: 2Gi`, `limits.memory: 14Gi` (-> 26Gi). Chart version currently `0.4.52` at `projects/firecracker/substrate/chart/Chart.yaml`; `deploy/application.yaml` `targetRevision: 0.4.52`.

**Concurrency enforcement** (`projects/firecracker/substrate/node/invoker/invoker.go`): a `semaphore.Weighted(concurrency)`; `Invoke` opens an `acquire_slot` span and blocks on `sem.Acquire(ctx, 1)`; on ctx-cancel returns `*GuestUnavailableError` -> HTTP 503. The slot is released only after `driver.Release()` (memory reclaimed) in `ownResponse`. So the daemon **blocks** callers rather than 429ing; a saturating client keeps VMs busy.

**Invoke HTTP contract** (`projects/firecracker/substrate/vsockproto/proto.go`):
- Request: `{"files":[{"path": str, "content": str}]}`
- Response: `{"findings":[{"path","line","col","rule_id","severity","message"}], "errors":[str]}`
- Ingress (`projects/firecracker/substrate/cluster/ingress/server.go`): `POST /invoke/{workload}`; 8 MiB body cap; 200 on scan (partial errors in `errors`), 503 GuestUnavailable, 502 guest HTTP failure.
- The guest picks language by the **`path` extension**, not the on-disk filename, so corpus files can be stored under inert names and sent with a virtual path.

**Per-scan telemetry today** (`reference_fc_base_build_sizing_coupling`): the invoker already samples `driver.Stats(h)` (VmHWM high-water + cpu) on the invoke path for the `vm_release` span, stamping `fc.guest.cpu_ms` / `fc.guest.peak_rss_mib`. That sampling happens at teardown (body Close), which is **after** response headers are flushed. This plan moves a sample to just-after-guest-exec so it can ride response headers (see Task 2).

**Pro rulepacks baked** (`bazel/semgrep/guest/BUILD`, `bazel/semgrep/rules/BUILD`): `pro-python`, `pro-golang`, `pro-javascript`, `pro-kubernetes`, `pro-rust` under `/etc/semgrep/rules/pro-<lang>/` plus local rules. Guest reads `SEMGREP_SCAN_RULES` (default `/etc/semgrep/rules`).

**Monolith semgrep client** (`projects/monolith/semgrep/client.py`): `scan_files(files)` POSTs `{"files": files}` to `${FC_INVOKE_URL}/invoke/semgrep` with `auth_headers()` (SA token, TokenReview-gated), 5s connect / 90s read.

**Demo backend** (`projects/monolith/demos/firecracker_api.py`): private router, wraps handlers, roots each run as its own trace via `start_as_current_span(..., context=Context())`, returns `trace_id`. Registered only in `app/main.py` (`demos.register(app)`), excluded from public via BUILD.

**Demo frontend**: page `projects/monolith/frontend/src/routes/private/demos/firecracker/+page.svelte` (`PROJECTS` array, `activeKey` state); `projects/monolith/frontend/src/lib/private/components/demos/RunPanel.svelte` (per-project inputs + run logic + latency + findings render); `TraceWaterfall.svelte`. Nav hidden for `/demos/**` via the `hideNav` regex in `frontend/src/routes/+layout.svelte`.

---

## Task 1: Tune the semgrep workload for max concurrency

**Suggested model:** Sonnet (mechanical config), but confirm the node-4 RAM math.

**Files:**
- Modify: `projects/firecracker/substrate/chart/values.yaml` (workloads.semgrep.concurrency, container `limits.memory`, and the budget comment)
- Modify: `projects/firecracker/substrate/chart/Chart.yaml` + `projects/firecracker/substrate/deploy/application.yaml` (via bump-chart.sh)

**Step 1:** In `chart/values.yaml`, set `workloads.semgrep.concurrency: 15` (was 2). Leave `vcpus: 1`, `memMib: 1536` unchanged (more VMs beats fatter VMs for CPU-bound single-file scans; verified in memory).

**Step 2:** Raise the daemon container `resources.limits.memory` from `14Gi` to `26Gi`. Update the adjacent budget comment to reflect: `semgrep 15 * 1536Mi = ~22.5Gi at max drain (assumes agent+sandbox idle during a load test) + ~1Gi daemon overhead; 26Gi leaves node-4 headroom (16c/62Gi, ~35Gi free).`

**Step 3:** Locally verify the chart still renders:
```bash
helm template fc-invoke projects/firecracker/substrate/chart/ -f projects/firecracker/substrate/deploy/values.yaml | grep -A3 'limits:'
```
Expected: `memory: 26Gi` on the daemon container; the `FC_INVOKE_WORKLOADS` env JSON shows `"concurrency":15` for semgrep.

**Step 4:** Bump the chart:
```bash
bazel/tools/git/bump-chart.sh projects/firecracker/substrate
```
Expected: Chart.yaml `version` and application.yaml `targetRevision` advance together.

**Step 5:** Commit.
```bash
git add projects/firecracker/substrate/chart projects/firecracker/substrate/deploy
git commit -m "perf(fc-invoke): raise semgrep concurrency to 15 for load testing"
```

**Note:** This can land and roll out independently ahead of the rest (it is inert until driven hard). After merge, verify the live pod shows `FC_INVOKE_WORKLOADS` semgrep concurrency 15 and container limit 26Gi, and that ArgoCD is Synced/Healthy (watch for the stale-OCI-sync gotcha; force a sync via the k8s MCP if the live spec lags the reported version).

---

## Task 2: Surface per-scan CPU-ms and peak-RSS as invoke response headers

**Suggested model:** Opus (CI-only-verifiable Go daemon plumbing with a sampling-order subtlety).

**Files:**
- Modify: `projects/firecracker/substrate/node/invoker/invoker.go`
- Modify: `projects/firecracker/substrate/cluster/ingress/server.go` (only if it does not already copy all guest response headers through)
- Test: `projects/firecracker/substrate/node/invoker/invoker_test.go`

**Context / subtlety:** peak RSS (VmHWM) and cumulative CPU are readable from `driver.Stats(h)` at any point after the scan runs, but the existing sample fires at teardown (body Close), which is after headers are sent. To ride response headers we must sample **immediately after the guest exec returns and before the proxied response headers are written**. VmHWM is monotonic, so a post-exec sample already captures the scan's peak.

**Step 1: Write the failing test.** In `invoker_test.go`, using the existing fake driver/transport, assert that a successful `Invoke` response carries the three headers with the fake's stats:
```go
func TestInvokeStampsResourceHeaders(t *testing.T) {
    // fake driver returns Stats{CpuMs: 812, PeakRssMib: 631} post-exec
    // fake transport returns a 200 guest response
    resp, err := inv.Invoke(ctx, req)
    require.NoError(t, err)
    require.Equal(t, "812", resp.Header.Get("X-Fc-Cpu-Ms"))
    require.Equal(t, "631", resp.Header.Get("X-Fc-Peak-Rss-Mib"))
    // queue-wait measured around acquire_slot; assert header present and numeric
    require.Regexp(t, `^\d+$`, resp.Header.Get("X-Fc-Queue-Wait-Ms"))
}
```
Extend the fake driver so `Stats` can return a canned value; record the `acquire_slot` duration on the invoker for the queue-wait header.

**Step 2:** Cross-compile check (the mount/driver code is linux-gated): `GOOS=linux go build ./projects/firecracker/substrate/...` from the worktree (Go is vendored via bootstrap). CI's `bazel test` is authoritative.

**Step 3: Implement.** In `invoker.go`:
- Capture `queueWait := time.Since(t0)` around the `acquire_slot` acquire.
- After the guest exec returns a response but before returning it up to ingress, sample `st, _ := inv.driver.Stats(h)` and set on the response header: `X-Fc-Cpu-Ms`, `X-Fc-Peak-Rss-Mib`, `X-Fc-Queue-Wait-Ms` (integers; omit a header if Stats errored). Keep the existing `vm_release` teardown span untouched.
- Guard: only stamp on 2xx (a 502/503 path has no meaningful guest stats).

**Step 4:** Confirm ingress copies these through. In `cluster/ingress/server.go`, if the response-header copy is a blanket `for k, v := range resp.Header { w.Header()[k] = v }`, no change is needed; if it allowlists headers, add the three `X-Fc-*` keys. Note in the commit which case held.

**Step 5:** Bump the substrate chart again only if this ships in a separate PR from Task 1; if Tasks 1+2 ship together, one bump covers both (preferred: bundle Tasks 1+2 into one substrate PR).

**Step 6:** Commit.
```bash
git add projects/firecracker/substrate/node/invoker projects/firecracker/substrate/cluster/ingress
git commit -m "feat(fc-invoke): stamp per-scan cpu-ms and peak-rss on invoke response headers"
```

**Reviewer focus:** the sample-before-headers ordering; header omission on error paths; no regression to the agent workload's streamed responses (headers-only change, body still streams).

---

## Task 3: Author the Pro-triggering corpus (5 languages)

**Suggested model:** Sonnet (content authoring against a clear rubric), spot-checked for real Pro-taint shape.

**Files:**
- Create: `projects/monolith/demos/semgrep_corpus/python.sample`
- Create: `projects/monolith/demos/semgrep_corpus/golang.sample`
- Create: `projects/monolith/demos/semgrep_corpus/javascript.sample` (TypeScript, sent as `.ts`)
- Create: `projects/monolith/demos/semgrep_corpus/kubernetes.sample`
- Create: `projects/monolith/demos/semgrep_corpus/rust.sample`
- Create: `projects/monolith/demos/semgrep_corpus/__init__.py` (loader)
- Modify: `projects/monolith/BUILD` (add the corpus to backend data, keep it out of the public glob)
- Test: `projects/monolith/demos/semgrep_corpus_test.py`

**Why `.sample`, not real extensions:** committing deliberately-vulnerable `.py`/`.go` source would trip the repo's own semgrep CI, gazelle BUILD generation, and the `demos/**/*.py` backend glob. Storing the content under an inert `.sample` extension and sending it to the guest with a **virtual path** carrying the real extension (the guest keys language off the request `path`) sidesteps all of that. Add a `# gazelle:exclude` for the corpus dir if gazelle tries to touch it.

**Rubric for each file (100-400 lines):** a realistic module where a taint **source** (untrusted input: HTTP query/body, CLI arg, env, request header) reaches a dangerous **sink** (shell/exec, SQL string-concat, path traversal, SSRF, deserialization) through **at least one intervening function call**, so only Pro interprocedural taint connects them. Include a few benign functions for realistic length. Each file should reliably produce >= 1 ERROR/WARNING finding from its `pro-<lang>` pack. Mirror the existing single-scan sample's shape (`SEMGREP_SAMPLE` in the page) but longer and language-idiomatic:
- **python.sample** -> virtual path `corpus/app_python.py`: Flask/FastAPI request param -> helper -> `subprocess.run(..., shell=True)` and a second `os.system` path built from a header.
- **golang.sample** -> `corpus/app_go.go`: `r.URL.Query().Get` -> helper -> `exec.Command("sh","-c", ...)`; plus a `database/sql` string-concat query.
- **javascript.sample** -> `corpus/app_ts.ts`: Express `req.query`/`req.body` -> helper -> `child_process.exec` and a `fs.readFile(path.join(base, userPath))` traversal.
- **kubernetes.sample** -> `corpus/manifest.yaml`: a Deployment/Pod tripping Pro k8s rules (privileged container, `hostPath` mount, `runAsNonRoot: false`, missing resource limits, `allowPrivilegeEscalation: true`). No interprocedural taint here (k8s rules are structural); goal is >= 1 Pro finding.
- **rust.sample** -> `corpus/app_rs.rs`: an axum/warp handler taking a query param -> helper -> `std::process::Command::new("sh").arg("-c").arg(tainted)`.

**Loader** (`__init__.py`):
```python
from pathlib import Path

_DIR = Path(__file__).parent

# on-disk filename -> virtual path sent to the guest (extension drives language)
_CORPUS = {
    "python": ("python.sample", "corpus/app_python.py"),
    "golang": ("golang.sample", "corpus/app_go.go"),
    "javascript": ("javascript.sample", "corpus/app_ts.ts"),
    "kubernetes": ("kubernetes.sample", "corpus/manifest.yaml"),
    "rust": ("rust.sample", "corpus/app_rs.rs"),
}

def load_corpus() -> list[dict]:
    """Return [{lang, path, content}] for every Pro pack, sorted by lang."""
    out = []
    for lang, (fname, vpath) in sorted(_CORPUS.items()):
        content = (_DIR / fname).read_text()
        out.append({"lang": lang, "path": vpath, "content": content})
    return out
```

**Step 1: Write the failing test** (`semgrep_corpus_test.py`):
```python
from demos.semgrep_corpus import load_corpus

def test_corpus_covers_all_five_packs():
    corpus = load_corpus()
    langs = {c["lang"] for c in corpus}
    assert langs == {"python", "golang", "javascript", "kubernetes", "rust"}

def test_each_corpus_file_is_substantial_and_has_virtual_extension():
    for c in load_corpus():
        assert 100 <= len(c["content"].splitlines()) <= 400, c["lang"]
        assert c["path"].split(".")[-1] in {"py", "go", "ts", "yaml", "rs"}
```

**Step 2:** Add the corpus `.sample` files and `__init__.py`. Register the py_test in `projects/monolith/BUILD` by hand (gazelle will not pick up a new `*_test.py`; see repo convention) and add the `.sample` files as `data` on the backend library. Ensure the corpus dir is not swept into the public backend glob.

**Step 3:** Local check the loader without Bazel:
```bash
cd projects/monolith && python3 -c "from demos.semgrep_corpus import load_corpus; import json; print(json.dumps([{'lang':c['lang'],'path':c['path'],'lines':len(c['content'].splitlines())} for c in load_corpus()], indent=2))"
```
Expected: 5 entries, each 100-400 lines, correct virtual extensions.

**Step 4:** Commit.
```bash
git add projects/monolith/demos/semgrep_corpus projects/monolith/BUILD
git commit -m "feat(demos): add Semgrep Pro load-test corpus for five language packs"
```

**Deferred verification (post-deploy, live):** each file actually produces >= 1 Pro finding when scanned by the live guest. Captured naturally by the first live load-test run (Task 8); if any language returns zero findings, tune that `.sample` file.

---

## Task 4: Database schema for load runs and per-scan results

**Suggested model:** Sonnet.

**Files:**
- Create: `projects/monolith/chart/migrations/NNNN_semgrep_load_test.sql` (next Atlas version number; check the tail of the migrations dir and `atlas.sum`)
- Modify: `projects/monolith/chart/migrations/atlas.sum` (regen with the CI-pinned atlas; see repo convention)
- Test: covered by the backend store test in Task 5.

**Schema:**
```sql
-- demo schema is private-tier only; no public_reader grant.
CREATE SCHEMA IF NOT EXISTS demo;

CREATE TABLE demo.semgrep_load_run (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    started_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz,
    duration_s    integer NOT NULL DEFAULT 120,
    status        text NOT NULL DEFAULT 'running',  -- running | done | error
    config        jsonb NOT NULL,   -- {daemon_concurrency, client_concurrency, vcpus, mem_mib, node, corpus_langs}
    summary       jsonb             -- aggregate rollup, written at completion (see Task 6)
);

CREATE TABLE demo.semgrep_load_scan (
    id             bigserial PRIMARY KEY,
    run_id         uuid NOT NULL REFERENCES demo.semgrep_load_run(id) ON DELETE CASCADE,
    seq            integer NOT NULL,
    lang           text NOT NULL,
    path           text NOT NULL,
    status         text NOT NULL,          -- ok | error
    latency_ms     integer NOT NULL,       -- client-observed wall for the scan
    queue_wait_ms  integer,                -- X-Fc-Queue-Wait-Ms
    cpu_ms         integer,                -- X-Fc-Cpu-Ms
    peak_rss_mib   integer,                -- X-Fc-Peak-Rss-Mib
    findings_count integer NOT NULL DEFAULT 0,
    findings       jsonb,                  -- full finding list for click-in
    error          text,
    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_semgrep_load_scan_run ON demo.semgrep_load_scan(run_id, seq);
```

**Step 1:** Find the next migration number and create the file. Do not put bulk data here (256 KiB annotation cap applies to the migrations ConfigMap; this is schema only, fine).

**Step 2:** Regenerate `atlas.sum` with the CI-pinned atlas version (community v1.1.0 per repo convention) so CI does not reject the checksum.

**Step 3:** Local render check:
```bash
helm template monolith projects/monolith/chart/ -f projects/monolith/deploy/values.yaml | grep -c semgrep_load
```
Expected: the migration SQL appears in the `monolith-migrations` ConfigMap.

**Step 4:** Commit.
```bash
git add projects/monolith/chart/migrations
git commit -m "feat(demos): add demo.semgrep_load_run and semgrep_load_scan tables"
```

---

## Task 5: Backend drain orchestrator + buffered store

**Suggested model:** Opus (concurrency correctness + storage batching; async lifecycle).

**Files:**
- Create: `projects/monolith/demos/semgrep_loadtest.py` (drain runner + store)
- Modify: `projects/monolith/demos/firecracker_api.py` (three endpoints)
- Modify: `projects/monolith/semgrep/client.py` (a variant that also returns the `X-Fc-*` headers)
- Test: `projects/monolith/demos/semgrep_loadtest_test.py`

**Client change:** add `scan_files_with_meta(files) -> tuple[dict, dict]` returning `(result_json, {"cpu_ms","peak_rss_mib","queue_wait_ms"})` parsed from response headers (None-safe). Keep the existing `scan_files` untouched for the single-scan path.

**Drain runner** (`semgrep_loadtest.py`), core shape:
```python
CLIENT_CONCURRENCY = 20   # over-subscribe the daemon's 15 so its semaphore never idles
DURATION_S = 120

async def run_load_test(run_id, corpus, store) -> None:
    deadline = time.monotonic() + DURATION_S
    seq = itertools.count()
    sem = asyncio.Semaphore(CLIENT_CONCURRENCY)

    async def worker():
        while time.monotonic() < deadline:
            item = corpus[next(seq) % len(corpus)]   # round-robin the 5 langs
            async with sem:
                await _one_scan(run_id, item, store)

    workers = [asyncio.create_task(worker()) for _ in range(CLIENT_CONCURRENCY)]
    await asyncio.gather(*workers, return_exceptions=True)
    await store.finalize(run_id)   # compute + write summary, mark done
```
`_one_scan` times the call, invokes `scan_files_with_meta`, and enqueues a scan row (never raises out; on error records `status='error'`). The endpoint dispatches `run_load_test` on a detached task (like goosecracker dispatch) and returns `run_id` immediately.

**Buffered store** (efficiency requirement): accumulate scan rows in memory and flush in batches (every 200 rows or 500ms, whichever first) with a single multi-row `INSERT`; final flush in `finalize`. ~1-2k rows/run, so batching keeps Postgres write pressure negligible. `finalize` runs one aggregate query over `semgrep_load_scan` to build the `summary` JSONB (see Task 6) and updates the run row.

**Endpoints** (`firecracker_api.py`):
- `POST /api/demos/firecracker/semgrep/load-test` -> creates a `semgrep_load_run` row (config snapshot from env: daemon concurrency, vcpus, mem, node, corpus langs), dispatches the drain, returns `{run_id}`. Guard against a second concurrent run (return the in-flight run_id if one is `running`).
- `GET /api/demos/firecracker/semgrep/load-test/{run_id}` -> live status: run row + a cheap rollup query (count, throughput = count / elapsed, in-flight estimate, p50/p95 latency, per-lang counts, running cpu/rss aggregates). Frontend polls this every 1s.
- `GET /api/demos/firecracker/semgrep/load-test/{run_id}/scans?offset=&limit=` -> paginated per-scan rows (without `findings`) for the table; and `.../scans/{scan_id}` -> one scan **with** `findings` for the drill-down.

**Step 1: Write failing tests** with an httpx mock (respx or a fake transport) so no live daemon is needed:
```python
async def test_drain_records_rows_and_meta(monkeypatch):
    # fake scan_files_with_meta returns 1 finding + {cpu_ms:800, peak_rss_mib:620, queue_wait_ms:5}
    # run a 1-second drain against an in-memory store fake
    # assert: rows recorded, each has cpu_ms/peak_rss_mib, findings persisted, summary computed

def test_client_parses_resource_headers():
    # fake httpx response with X-Fc-* headers -> scan_files_with_meta returns them parsed
```
Shorten `DURATION_S` via a parameter for the test (inject duration, default 120).

**Step 2:** Implement runner, store, client variant, endpoints. Register the new `*_test.py` in `projects/monolith/BUILD` by hand.

**Step 3:** Local import/type check:
```bash
cd projects/monolith && python3 -c "import demos.semgrep_loadtest, demos.firecracker_api; print('ok')"
```

**Step 4:** Commit.
```bash
git add projects/monolith/demos projects/monolith/semgrep/client.py projects/monolith/BUILD
git commit -m "feat(demos): semgrep load-test drain orchestrator with buffered per-scan store"
```

**Reviewer focus:** the drain never blocks the event loop (all I/O awaited); over-subscription actually saturates the daemon (client concurrency > daemon concurrency); errors are recorded not raised; the single-run guard; batch-flush correctness (no lost tail on finalize).

---

## Task 6: Resource summary + daemon/node footprint sampler

**Suggested model:** Opus (the extrapolation model is the point of the feature; get the aggregation honest).

**Files:**
- Modify: `projects/monolith/demos/semgrep_loadtest.py` (add the sampler + summary builder)
- Modify: monolith ClusterRole (RBAC) if it lacks `metrics.k8s.io` read (grep first): `projects/monolith/chart/templates/*rbac*` or wherever the ClusterRole lives.
- Test: extend `semgrep_loadtest_test.py`.

**Sampler:** during the run, a background task samples every ~2s: the fc-invoke pod's CPU/RSS and node-4's CPU/RSS via `metrics.k8s.io` (the monolith already has a k8s client for the k8s MCP tools). Append `{t, pod_cpu_m, pod_rss_mib, node_cpu_m, node_rss_mib}` to an in-memory series; store min/mean/max/last in the run summary. Best-effort: if metrics.k8s.io is unavailable or RBAC is missing, log once and fall back to a derived daemon-RSS estimate (sum of concurrent per-scan `peak_rss_mib`), flagged `source: "derived"` in the summary.

**RBAC:** grep the monolith ClusterRole for `metrics.k8s.io`. If absent, add:
```yaml
- apiGroups: ["metrics.k8s.io"]
  resources: ["pods", "nodes"]
  verbs: ["get", "list"]
```
(Per CLAUDE.md, missing verbs fail silently as Forbidden; verify every verb the code calls.)

**Summary JSONB shape** (written by `finalize`), the multi-node extrapolation model:
```json
{
  "total_scans": 1840,
  "errors": 3,
  "wall_s": 120,
  "throughput_per_s": 15.3,
  "latency_ms": {"p50": 870, "p95": 1120, "max": 2400},
  "queue_wait_ms": {"p50": 3, "p95": 40},
  "per_scan_cpu_ms": {"p50": 800, "p95": 980},
  "per_scan_peak_rss_mib": {"p50": 620, "p95": 640},
  "per_lang": {"python": {"count": 370, "p50_ms": 910, "p50_cpu_ms": 850}, "...": {}},
  "daemon": {"pod_cpu_m": {"mean": 14200, "max": 15600}, "pod_rss_mib": {"mean": 21000, "max": 22800}, "source": "metrics"},
  "node": {"cpu_pct": {"mean": 92, "max": 98}, "rss_mib": {"mean": 48000, "max": 51000}},
  "extrapolation": {
    "vm_seconds": 1600,
    "scans_per_core_s": 0.96,
    "daemon_overhead_mib": 500,
    "per_node_throughput_per_s": 15.3,
    "note": "Throughput is core-bound at ~1 scan/core/s; N nodes ~= N x per_node_throughput minus a fixed ~500Mi daemon overhead per node."
  }
}
```
`extrapolation.per_node_throughput_per_s` is this run's measured throughput; `scans_per_core_s` = throughput / (concurrency vcpus) lets a reader multiply by any node's core count.

**Step 1:** Write a test that `finalize` builds a summary with the expected keys from seeded scan rows + a fake metrics series.

**Step 2:** Implement sampler + summary; add RBAC if needed; bump the monolith chart (RBAC/migration/backend change deploys).

**Step 3:** `helm template` render check for the RBAC rule if added.

**Step 4:** Commit.
```bash
git commit -m "feat(demos): resource-efficiency summary with daemon and node footprint sampling"
```

---

## Task 7: Frontend Single Scan / Load Test sub-tab

**Suggested model:** Sonnet (Svelte UI against clear data contracts), design-polished.

**Files:**
- Modify: `projects/monolith/frontend/src/lib/private/components/demos/RunPanel.svelte` (add `semgrepMode` sub-tab within the semgrep branch)
- Create: `projects/monolith/frontend/src/lib/private/components/demos/LoadTestPanel.svelte` (counters + table + drill-down)
- Modify: `projects/monolith/frontend/src/routes/private/demos/firecracker/+page.svelte` (tagline copy for the sub-tab, if needed)
- Modify: `projects/monolith/frontend/src/lib/private/demos/theme.css` (any new tokens; put hex in CSS not Svelte to satisfy `svelte-hardcoded-color-in-style`)

**Behavior:**
- Inside the semgrep tab, a two-button sub-tab: **Single Scan** (default, the existing single-file UI unchanged) and **Load Test**.
- Load Test view (the clean headline view): a header line stating the fixed 2-minute run and the live daemon concurrency; a **Start load test** button (disabled while a run is `running`); live counters that poll `GET .../load-test/{run_id}` every 1s: total scans, throughput (scans/s, large), in-flight, elapsed / 120s progress bar, p50/p95 latency, aggregate cpu-ms and peak-RSS, daemon pod CPU/RSS, node CPU%, and per-language mini-rows.
- On completion (`status: done`): render the summary on the main view (the **extrapolation block prominently**, since that is the deliverable): headline throughput, per-scan resource medians, daemon/node footprint, and the per-node -> fleet extrapolation. Keep this uncluttered.
- **The per-scan deep-dive lives behind a "See Receipts" tab** (Joe's request), NOT on the main view. Within the Load Test view, once a run has scans, show a **See Receipts** toggle/tab that reveals the paginated **per-scan table** (seq, lang, latency, cpu-ms, peak-rss, findings count, status). It fetches `.../scans?offset=&limit=` lazily (only when the Receipts tab is first opened, so the clean view stays cheap). Clicking a row fetches `.../scans/{id}` and shows that scan's findings (reuse the single-scan findings renderer) plus its timing/resource line. The main summary view stays clean; Receipts is opt-in.
- Naming: label the tab literally "See Receipts". Keep the summary/extrapolation as the default; Receipts is a peer view you switch to, not something rendered inline below the summary.
- Polling lifecycle mirrors `TraceWaterfall.svelte`: 1s while running, stop when `status != running`, hard timeout ~150s. The Receipts table is independent of the live poll (it reads committed rows on demand).

**Step 1:** Add `let semgrepMode = $state("single")` and the sub-tab buttons in the semgrep branch of RunPanel; render `LoadTestPanel` when `semgrepMode === "load"`.

**Step 2:** Build `LoadTestPanel.svelte` against the three endpoints. Keep all color tokens in `theme.css`.

**Step 3:** Local build check (mind the macOS `build/` vs `BUILD` collision):
```bash
cd projects/monolith/frontend && pnpm build && rm -rf build && git checkout -- BUILD && git status
```
Expected: clean build, `BUILD` intact, no stray `build/` dir.

**Step 4:** Commit.
```bash
git add projects/monolith/frontend
git commit -m "feat(demos): Single Scan / Load Test sub-tab with live counters and per-scan drill-down"
```

---

## Task 8: Format, push, CI, review, live run

**Step 1:** `bazel/tools/format/fast-format.sh` (formats + updates BUILD files + regenerates the home-cluster root). Commit any formatter changes.

**Step 2:** Push the branch, open the PR, watch CI: `gh pr checks <n> --watch`. Diagnose any red via `mcp__buildbuddy__get_invocation` (commitSha selector) -> `get_target` -> `get_log`; quote the assertion before hypothesizing. Common trip-wires to expect: new `*_test.py` not registered in BUILD; `demos/**` reaching the public glob; atlas.sum checksum; apko/chart-bump race on the substrate chart; `bdd_completeness_test` on new public callables (should not fire, private only).

**Step 3:** One comprehensive **Opus** code review of the full diff (per CLAUDE.md, one per PR): correctness of the drain concurrency, the header-sampling order in Go, RBAC verb coverage for metrics.k8s.io, tier isolation (private only), and migration ordering.

**Step 4:** After merge + rollout, run the live load test from the demo UI (or a one-shot backend trigger) and confirm: all 5 languages produce Pro findings, throughput saturates near ~15/s, the summary + extrapolation render, and per-scan drill-down works. If a language returns zero findings, tune its `.sample` file (fast follow).

**Step 5:** Notify Joe with a one-line summary of the live throughput + per-scan resource numbers via `monolith-monolith-agent-notify`.

---

---

## Addendum: generalize to both workloads (semgrep + sandbox)

Decision (2026-07-09, Joe): the load test covers **both** the semgrep workload and the python **sandbox** workload as peers, via one workload-parametric harness rather than two copies. This amends Tasks 1-7 as follows. The daemon changes (Tasks 1-2) already cover it: Task 2's header stamping is workload-agnostic (fires on any 2xx invoke), and Task 1 now also raises `workloads.sandbox.concurrency: 2 -> 15` in the same substrate PR (sandbox is 512Mi/VM, so 15 = ~7.5Gi, inside the 26Gi limit; load tests run one workload at a time so this never stacks with semgrep's 22.5Gi peak).

**Global one-run-at-a-time guard:** at most one load test runs at any moment across ALL workloads. This keeps each test's "other workloads idle" resource assumption honest and bounds the cgroup to a single workload's peak. Enforce in the create endpoint (if any run row is `running`, return it instead of starting a new one).

**Task 3 (corpus) becomes two corpora under `projects/monolith/demos/loadtest_corpus/`:**
- `semgrep/` : the 5 Pro-triggering `.sample` files (unchanged from Task 3 above), sent to `/invoke/semgrep` as `{"files":[{path,content}]}`.
- `sandbox/` : a handful (5-8) of varied Python scripts as `.sample` files, sent to `/invoke/sandbox` (run_python). The sandbox guest has a warm scientific-library page cache (pandas/numpy available, per the workload comment), so mix light and heavy: e.g. a tight numeric loop, a sort/merge, a regex/text pass, a small pandas dataframe aggregation, a json round-trip, a recursive fib. Vary runtime deliberately so the latency distribution is meaningful. Each 30-200 lines. The loader returns `{workload, lang_or_name, payload}` where payload is the workload-correct request shape.

**Task 4 (schema) uses workload-generic tables:** name them `demo.load_run` and `demo.load_scan` with a `workload text NOT NULL` column on both. Replace the semgrep-specific `findings`/`findings_count` with a generic `result jsonb` (findings for semgrep; `{stdout, exit, error, output_bytes}` for sandbox) plus a nullable `result_count int` (finding count for semgrep, null for sandbox). Everything else (latency_ms, queue_wait_ms, cpu_ms, peak_rss_mib, seq, status) is identical and workload-agnostic. Index `demo.load_scan(run_id, seq)`.

**Task 5 (drain) is parametrized by a workload registry** (a dict keyed by workload name) providing: the `/invoke/<workload>` endpoint, a payload-builder (semgrep wraps in `{"files":[...]}`; sandbox sends the run_python shape), a result-parser (extract `result` + `result_count`), and the corpus list. `run_load_test(run_id, workload, ...)` round-robins that workload's corpus. The over-subscription factor (client concurrency 20 vs daemon 15) and 2-minute duration are the same for both. The endpoints gain a `workload` path/param: `POST .../load-test/{workload}` (semgrep|sandbox).

**Task 6 (summary)** is workload-agnostic already (resources come from headers). `result_count` stats only populate for semgrep; the sandbox summary reports output/exit distribution instead. The extrapolation block is identical in shape for both.

**Task 7 (UI):** the same `LoadTestPanel.svelte` is reused, parametrized by `workload`. Add the Single / Load Test sub-tab under **both** the Sandbox and Semgrep top-level tabs (sandbox's "Single Scan" is its existing single-run UI). The "See Receipts" tab applies to both; the per-scan drill-down renders findings for semgrep and stdout/exit for sandbox.

---

## Out of scope (explicit YAGNI)

- SCA packs (sca_golang/python/javascript) and manifest corpus files.
- Load testing the **agent** (goose) workload (minutes-long model-bound runs; not a throughput drain, and warmBase:false).
- Running two load tests concurrently (global one-run guard forbids it by design).
- Making the load test public-tier.
- Multi-node execution (the feature only *models* the extrapolation; it does not run across nodes).
- A configurable duration or concurrency in the UI (fixed 2 minutes; concurrency is the daemon chart value).
- Guest-interior OTel nesting for semgrep (known upstream limitation).
