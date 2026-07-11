# Semgrep Full Interfile Scan via Daemon Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** A full interfile Semgrep scan of `main` that runs on our own fc-invoke Firecracker daemon (`semgrep scan --pro` over the whole tree) and reports to the Semgrep App as a full scan, seeding the `jomcgi/homelab-selfhosted` baseline and becoming the sole source of cross-file findings.

**Architecture:** Per ADR tooling/011. Two scan paths, two Firecracker workloads. The existing warm `osemgrep-pro mcp --pro` workload (fast, single-file) is untouched and keeps serving the MCP tool and the PR webhook. A **new** `semgrep-full` workload runs a lean second guest-init that skips the warm scan-server, materializes a batch of files to a tmpfs tmpdir, and runs `semgrep scan --pro` as a subprocess (interfile on). The monolith gathers every scannable file at `main`, sends them to `/invoke/semgrep-full`, and reports with `is_full_scan=True, branch=main`. Diff-vs-full stays a `report.py` concern.

**Tech Stack:** Go (guest-init, shim over vsock), apko (image), Bazel (`rules_apko`, `pkg_tar`), fc-invoke substrate Helm chart, Python (monolith FastAPI + `report.py`), Argo CronWorkflow (schedule).

**Key design decision (settled):** The full path is a **second init binary in the same image** (Option A). The `semgrep-guest` image already carries the `semgrep` CLI, `git`, the `osemgrep-pro`/`semgrep-core` engine, and the rules at `/etc/semgrep/rules`, so no new image is needed. The `semgrep-full` workload points at the same image with a different `harnessInit`, a bigger footprint, and `warmBase:false`. This keeps the PR-critical warm workload byte-for-byte unchanged.

**Load-bearing unknown (de-risk FIRST):** whether the in-guest `semgrep scan --pro` (python CLI) actually locates the proprietary core and runs the interfile phase offline. Task 2's acceptance is an end-to-end deploy + the 2-file cross-file taint probe returning the interfile finding that `mcp --pro` missed. Everything after Task 2 assumes that passes.

**No local test loop:** Go/Python unit tests are written TDD but RUN IN CI (BuildBuddy). Guest engine behavior is only verifiable post-deploy on node-4. Batch guest changes and verify against a pushed branch's CI + a manual invoke.

---

## Task 1: Guest full-scan subprocess core (Go, locally unit-testable)

A pure Go core that materializes a `vsockproto.ScanRequest` batch into a tmpdir tree, runs `semgrep scan --pro`, and normalizes the result. Path materialization and cli_output parsing are unit-testable without the engine; the subprocess exec is behind a seam.

**Files:**
- Create: `projects/firecracker/semgrep/guest-init/internal/fullscan/fullscan.go`
- Create: `projects/firecracker/semgrep/guest-init/internal/fullscan/fullscan_test.go`
- Reference (reuse the cli_output parser): `projects/firecracker/semgrep/guest-init/internal/scandriver/driver.go:120-172` (`parseCliOutput`) — extract it into a shared spot or duplicate the small struct; prefer moving `cliOutput`/`parseCliOutput` into a shared `internal/cliout` package so both driver and fullscan use it (DRY).

**Step 1: Extract the shared cli_output parser**
- Create `internal/cliout/cliout.go` with the `cliOutput` struct and a `Parse(line []byte, stripPrefix string) (vsockproto.ScanResult, error)` that also strips a tmpdir prefix from result paths (the subprocess reports absolute tmpdir paths; we rewrite back to repo-relative). Move the existing struct out of `driver.go` and have `driver.go` call `cliout.Parse(line, "")` (no prefix for the mcp path).
- Add `internal/cliout/cliout_test.go`: assert a sample cli_output with paths under `/tmp/xxx/probe/a.py` maps to `probe/a.py` when `stripPrefix="/tmp/xxx"`, and findings/errors flatten identically to today.

**Step 2: Write the failing fullscan test**
```go
func TestMaterializeAndScan(t *testing.T) {
    req := vsockproto.ScanRequest{Files: []vsockproto.ScanFile{
        {Path: "pkg/a.py", Content: "import flask\n\ndef get():\n    return flask.request.args.get('x')\n"},
        {Path: "pkg/b.py", Content: "import os\nfrom pkg.a import get\n\ndef run():\n    os.system(get())\n"},
    }}
    // Fake runner returns a canned cli_output referencing the tmpdir it was given.
    var gotDir string
    fake := func(ctx context.Context, dir string) ([]byte, error) {
        gotDir = dir
        // assert files were written before the runner is called
        if _, err := os.Stat(filepath.Join(dir, "pkg/a.py")); err != nil {
            t.Fatalf("file not materialized: %v", err)
        }
        return []byte(`{"results":[{"check_id":"x","path":"` + filepath.Join(dir, "pkg/b.py") + `","start":{"line":5,"col":5},"extra":{"message":"m","severity":"ERROR"}}],"errors":[]}`), nil
    }
    res, err := Scan(context.Background(), req, fake)
    if err != nil { t.Fatal(err) }
    if len(res.Findings) != 1 || res.Findings[0].Path != "pkg/b.py" {
        t.Fatalf("want repo-relative pkg/b.py, got %+v", res.Findings)
    }
    // tmpdir cleaned up
    if _, err := os.Stat(gotDir); !os.IsNotExist(err) {
        t.Fatalf("tmpdir not cleaned: %v", err)
    }
}
```

**Step 3: Run it to see it fail** — CI (`bazel test //projects/firecracker/semgrep/guest-init/internal/fullscan/...`) on the pushed branch. Locally it won't run (no darwin pool). Expected: build failure (`Scan` undefined).

**Step 4: Implement `fullscan.go`**
```go
// Package fullscan runs a whole-tree `semgrep scan --pro` (interfile) over a
// batch of in-memory files, materialized into a tmpfs tmpdir. Unlike the warm
// mcp scan-server (single-file), this path sees the whole file set on disk at
// once, so cross-file dataflow fires.
package fullscan

// Runner executes the scan over a materialized tree dir and returns raw
// `semgrep --json` cli_output. Seam so tests avoid the real engine.
type Runner func(ctx context.Context, treeDir string) ([]byte, error)

// Scan writes req.Files into a fresh tmpdir tree, runs runner over it, parses the
// cli_output (rewriting tmpdir-absolute paths back to repo-relative), and cleans
// up. Path traversal outside the tree (e.g. "../x") is rejected.
func Scan(ctx context.Context, req vsockproto.ScanRequest, runner Runner) (vsockproto.ScanResult, error) {
    dir, err := os.MkdirTemp("/tmp", "sgfull-")
    if err != nil { return vsockproto.ScanResult{}, err }
    defer os.RemoveAll(dir)
    for _, f := range req.Files {
        clean := filepath.Clean("/" + f.Path) // anchor, then strip leading slash
        dst := filepath.Join(dir, clean)
        if !strings.HasPrefix(dst, dir+string(os.PathSeparator)) {
            return vsockproto.ScanResult{}, fmt.Errorf("fullscan: unsafe path %q", f.Path)
        }
        if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil { return vsockproto.ScanResult{}, err }
        if err := os.WriteFile(dst, []byte(f.Content), 0o644); err != nil { return vsockproto.ScanResult{}, err }
    }
    out, runErr := runner(ctx, dir)
    if runErr != nil {
        // A non-zero exit with findings still prints cli_output on stdout; only
        // treat an empty/undecodable output as a hard error.
        if len(out) == 0 { return vsockproto.ScanResult{}, runErr }
    }
    res, err := cliout.Parse(out, dir)
    if err != nil { return vsockproto.ScanResult{}, err }
    if runErr != nil { res.Errors = append(res.Errors, runErr.Error()) }
    return res, nil
}
```

**Step 5: Add the real Runner (the engine exec), behind the same seam**
```go
// SemgrepRunner runs the python `semgrep scan --pro` CLI over treeDir and returns
// stdout (cli_output JSON). Rules and settings come from the guest env
// (SEMGREP_SCAN_RULES, SEMGREP_SETTINGS_FILE) set by the full init. --experimental
// selects the osemgrep path; --pro enables interfile; --metrics=off and
// --disable-version-check keep it offline. NOTE: the exact flags/env that make the
// python CLI locate the proprietary core offline are verified in Task 2 post-deploy;
// adjust here from that finding.
func SemgrepRunner(rulesDir string) Runner {
    return func(ctx context.Context, treeDir string) ([]byte, error) {
        cmd := exec.CommandContext(ctx, "semgrep", "scan", "--experimental", "--pro",
            "--config", rulesDir, "--json", "--metrics=off", "--disable-version-check", treeDir)
        cmd.Stderr = os.Stderr
        return cmd.Output()
    }
}
```

**Step 6: Commit**
```bash
git add projects/firecracker/semgrep/guest-init/internal/cliout projects/firecracker/semgrep/guest-init/internal/fullscan projects/firecracker/semgrep/guest-init/internal/scandriver
git commit -m "feat(semgrep-guest): full-scan subprocess core (materialize tree, run semgrep scan --pro)"
```

---

## Task 2: Full-scan guest-init binary + image bake + workload, then PROVE interfile end-to-end

Wire the fullscan core behind a shim server in a new lean init, bake it into the existing image, add the `semgrep-full` fc-invoke workload, deploy, and run the cross-file taint probe.

**Files:**
- Create: `projects/firecracker/semgrep/guest-init/cmd-full/main.go` (lean init: no scandriver warm loop; PATH/locale/tmpfs setup + `setupEnv` reused; serve `handler.NewFull(fullscan.SemgrepRunner(rulesDir))` on the vsock shim; readiness = immediately true, no warm child)
- Modify: `projects/firecracker/semgrep/guest-init/internal/handler/handler.go` — add `NewFull(scan func(vsockproto.ScanRequest) (vsockproto.ScanResult, error)) shim.Handler` (same decode/encode as `New`, different scanner func). Keep `New` untouched.
- Modify: `projects/firecracker/semgrep/guest/apko.yaml` — no content change expected (semgrep CLI + git already present); confirm `semgrep` package stays (do NOT drop it in the pending image-shrink follow-up while this path needs it — add a comment).
- Modify: `projects/firecracker/semgrep/guest/BUILD` — bake the second init binary at `/usr/local/bin/semgrep-full-guest-init` (mirror the existing `semgrep-guest-init` bake).
- Modify: `bazel/semgrep/guest/BUILD` if the init binary is layered there.
- Modify: `projects/firecracker/substrate/chart/values.yaml` — add the `semgrep-full` workload.

**Step 1: `handler.NewFull`** — factor `New`'s body to share the decode→scan→encode with a `Scanner`-shaped func; `NewFull` takes the fullscan closure. Add a handler test asserting a fake full-scanner's result is echoed and a decode error → non-nil (502), mirroring the existing handler_test.

**Step 2: The lean full init `cmd-full/main.go`** — copy the non-scandriver parts of `cmd/main.go` (loopback, tmpfs, PATH/locale, `setupEnv`), then:
```go
rulesDir := envOr("SEMGREP_SCAN_RULES", defaultRulesDir)
h := handler.NewFull(func(req vsockproto.ScanRequest) (vsockproto.ScanResult, error) {
    return fullscan.Scan(context.Background(), req, fullscan.SemgrepRunner(rulesDir))
})
ln, _ := scanserver.ListenVsock(vsockproto.GuestHTTPPort)
srv := shim.NewServer(h, shim.WithReady(func() bool { return true })) // no warm child to wait on
```
Extract the shared `setupEnv`/`envOr`/loopback/tmpfs helpers into an `internal/guestenv` package so both inits use them (DRY), rather than copy-paste.

**Step 3: `semgrep-full` workload in `substrate/chart/values.yaml`** (start generous; trimmed in Task 4):
```yaml
  semgrep-full:
    image: semgrep-guest        # same image as `semgrep`
    rootfsPath: /disks/nvme-02/fc-invoke/semgrep/rootfs.ext4  # shared rootfs
    harnessInit: /usr/local/bin/semgrep-full-guest-init
    # Whole-tree interfile scan holds cross-file state for ~750 files. Sized
    # generously pending a measured run (Task 4); interfile peak >> the 697Mi
    # rule-compile peak the 1.5G single-file workload was sized to.
    vcpus: 4
    memMib: 8192
    concurrency: 1              # one full scan at a time; scheduled, not hot
    egressEnabled: false
    warmBase: false             # subprocess per invoke; no snapshot to restore
    readyPath: /shim/ready
    sessioned: false
    requestTimeout: 600s        # whole-repo interfile is seconds-to-minutes
```
Confirm `concurrency * memMib` still fits the node-4 budget alongside the existing `semgrep` (16*1536) and `agent` (2*4096) workloads; if not, drop `semgrep` concurrency or document the node headroom in the values comment.

**Step 4: Push branch, watch CI** (`gh pr checks --watch`): image builds dual-arch, substrate chart renders. Fix build errors from CI logs (`mcp__buildbuddy__*`).

**Step 5: Deploy + PROVE interfile (acceptance gate)**
- After merge/rollout, invoke the new workload with the exact Task-from-earlier 2-file probe (`flask.request → os.system` split across `pkg/a.py` and `pkg/b.py`). Fastest path: a temporary `monolith-semgrep-scan`-style call routed to `/invoke/semgrep-full` (add the client in Task 3 first, or a one-off `kubectl`-driven invoke).
- **Expected:** a taint finding (`os-system-injection` / `tainted-os-command-stdlib-flask`) on `pkg/b.py` line 5, and run metadata with `interfile_languages_used` NON-empty. This is the exact finding `mcp --pro` missed.
- **If interfile does NOT fire:** stop and debug the engine invocation (does the python CLI find the proprietary core? try `SEMGREP_CORE_BIN=/opt/semgrep/osemgrep-pro`, `osemgrep-pro scan --pro`, or `--pro-intrafile` vs default). Do not proceed to Task 3+ until interfile is proven. Record the working invocation in `fullscan.SemgrepRunner`.

**Step 6: Commit** (guest init + workload)
```bash
git commit -m "feat(semgrep-guest): semgrep-full workload — lean interfile subprocess init"
```

---

## Task 3: Monolith full-scan client + report full-scan mode + gather-all-of-main

**Files:**
- Modify: `projects/monolith/semgrep_scan/client.py` — add `scan_files_full(files)` POSTing to `/invoke/semgrep-full` (a thin variant of `scan_files`; longer read timeout ~600s to match the workload). DRY: factor the shared POST into a private `_post_invoke(workload, files, timeout)`.
- Modify: `projects/monolith/semgrep_scan/report.py` — support full-scan reporting: `_build_project_metadata` gains an `is_full_scan: bool` (default False) that flips `is_full_scan`, sets `on="unknown"` (or the App's full-scan sentinel), and omits `pull_request_id`; `report_pr_scan` gains `is_full_scan: bool = False` threaded through. Keep the PR default behavior identical.
- Create: `projects/monolith/semgrep_scan/full_scan.py` — `gather_main_files(repo) -> list[dict]` (all scannable files at `main` via the GitHub trees API: `GET /repos/{repo}/git/trees/{main_sha}?recursive=1`, filter by `_SCANNABLE_EXTS`, fetch each blob's content; reuse `_is_scannable`/exts from `router.py` — factor those into a shared spot). Plus `run_full_scan(repo="jomcgi/homelab")` that gathers, calls `scan_files_full`, and `report_pr_scan(..., is_full_scan=True, branch="main")`, logging `scan_execution_duration` + file count.
- Test: `projects/monolith/semgrep_scan/report_test.py` — assert `is_full_scan=True` produces `project_metadata.is_full_scan==True` and no `pull_request_id`; `full_scan_test.py` — assert `gather_main_files` filters exts and shapes `{path, content}` (mock the GitHub client like `router_test._fake_github_client`).
- Remember `feedback_knowledge_gazelle_exclude` / `reference_monolith_gazelle_pytest_targets`: new `*_test.py` needs a hand-added `py_test` target in the package BUILD.

**Steps:** TDD each (failing test → impl → CI). Commit per unit: `feat(monolith): full-scan client + report is_full_scan mode`, then `feat(monolith): gather-all-of-main + run_full_scan`.

**Gather-source note (YAGNI):** v1 uses the GitHub trees+blobs API (simple, already how PRs fetch content). The git-mirror hydration option (ADR 011 open question) is a later optimization; do not build it now unless the trees API rate-limits on ~750 blobs (if it does, batch via the git blobs API or fall back to the in-cluster mirror). Log the file count so a rate-limit truncation is visible.

---

## Task 4: One-off baseline seed + footprint right-size

**Files:**
- Modify: `projects/monolith/semgrep_scan/mcp.py` or an admin route — add a guarded trigger for `run_full_scan` (single-caller, internal). Simplest: a `monolith-agent`-style internal endpoint or reuse the scheduler trigger from Task 5 with a manual kick.

**Steps:**
1. Trigger `run_full_scan("jomcgi/homelab")` once against the deployed `semgrep-full` workload.
2. Verify in the Semgrep App read API (token `op://k8s-homelab/semgrep-mcp/SEMGREP_API_TOKEN`): a scan on `jomcgi/homelab-selfhosted` with `isFullScan:true`, `branch:main`, non-zero `findingsCounts`, and the **findings tab now populated** + **primary branch `main` selectable** (the original symptoms from the screenshot).
3. Read the run's `fc.guest.*` span attrs (SigNoz) / the workload peak RSS + wall time. Trim `semgrep-full` `memMib`/`vcpus`/`requestTimeout` to the measured peak + headroom (mirror the sizing rationale comment style already in `values.yaml`). Bump substrate chart, push, verify.
4. Commit: `chore(substrate): right-size semgrep-full from measured interfile run`.

---

## Task 5: Scheduled recurring baseline (Argo CronWorkflow)

**Context:** `register_job` is retired; jobs offload to Argo CronWorkflow (memory `project_monolith_jobs_offload`). The schedule triggers the monolith full-scan, it does not run semgrep itself.

**Files:**
- Create: an Argo `CronWorkflow` (under the monolith deploy or a jobs manifest dir consistent with the existing CronWorkflow registry) that, on a daily-ish cadence, calls the internal `run_full_scan` trigger from Task 4 (HTTP to an in-cluster monolith endpoint, or a `kubectl create` of a Workflow that curls it).
- Reference: `reference_argo_cronworkflow_manual_trigger` for the one-off trigger shape; the existing CronWorkflow registry for schema/labels.

**Steps:**
1. Add the CronWorkflow (schedule e.g. `0 6 * * *`; `concurrencyPolicy: Forbid` so runs never overlap the single-concurrency workload).
2. Deploy, then manually trigger once (`kubectl create` from the cron template) to confirm the scheduled path reports a full scan identical to Task 4's manual run.
3. Add an alert on last-successful-full-scan age (ADR 011 risk row) — a follow-up if the alerting wiring is non-trivial; note it explicitly rather than silently skipping.
4. Commit: `feat(monolith): scheduled interfile full scan on main (Argo CronWorkflow)`.

---

## End-of-plan verification (one comprehensive review)

Per repo CLAUDE.md: ONE code review against the full merged diff at the end (not per task). Confirm: PR fast path (`mcp --pro`) untouched; `semgrep-full` workload isolated; `report.py` PR default unchanged; the App shows a populated `main` baseline; footprint sized from a real run; schedule live. Then update ADR 011 Open Questions with the resolved footprint + gather-source + cadence (ADRs are rationale; record what was decided).

## Risks carried from ADR 011

- Interfile may not fire in-guest offline (Task 2 gate). Mitigation: the gate stops the plan before dependent work.
- Footprint OOM on whole-tree interfile. Mitigation: start at 4cpu/8G, measure, trim.
- GitHub trees/blobs rate limit on ~750 files. Mitigation: log count; fall back to git-mirror if truncated.
- Node-4 memory budget with a third workload. Mitigation: verify `concurrency*memMib` fit in Task 2 Step 3.
