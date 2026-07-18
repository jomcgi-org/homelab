# EmberVM Bazel Skyframe Query Demo Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Implement ADR embervm/010: snapshot a warm Bazel server (Abseil analysis graph in JVM heap) as an EmberVM task-class base, and serve a public interactive `bazel cquery` demo at `/ember/bazel` where each visitor query runs in a disposable CoW clone (request, response, reap).

**Architecture:** This is a read-only consumer of shipped machinery. A new guest runtime (`projects/embervm/runtimes/bazel/`) bakes a pinned Bazel binary, an Abseil checkout, and a distdir of dep archives into an apko rootfs; its guest-init runs the warming `cquery` at base build, signals ready only after the warming client exits, and the existing `BuildBase` flow snapshots the warm JVM. Serving rides the existing task class: `Prime` (CoW restore) + `Assign` (one vsock HTTP request, VM destroyed after response). The public surface follows the postgres demo template exactly: `ember_public` FastAPI module, same-origin SvelteKit proxies, Turnstile session gate, door card on `/ember`.

**Tech Stack:** Go (guest-init), Starlark (repo rule, BUILD), apko/Wolfi, Elixir control plane (no changes), Python/FastAPI (`ember_public`), SvelteKit (frontend), Helm charts.

**Two PRs:**
- **PR 1 (embervm):** vendored artifacts + guest runtime + workload registration + embervm chart bump. Verifiable live via a control-plane task submit before anything is public.
- **PR 2 (monolith):** backend endpoint + frontend page + landing card + monolith and monolith-public chart bumps.

Per repo rules: no local test execution; implementers self-review, push, and CI on the PR branch is the test loop. One comprehensive code review per PR at the end, not per task.

---

## Load-bearing facts (verified against code, do not re-derive)

1. **Task-class lifecycle is already request-serve-reap.** `Prime(snapshot_ref) -> vm_id` restores a parked clone; `Assign(vm_id, GuestRequest, timeout_ms)` delivers one HTTP-over-vsock request and destroys the VM after the response regardless of outcome; `Destroy` is the out-of-band reap. (`projects/embervm/proto/embervm/node/v1/node.proto` lines 57-81.)
2. **`BuildBase` is the warming pipeline.** Cold-boot from rootfs, `WaitReady` polls guest `GET /shim/ready` until 200 (the guest decides when it is ready), then `SnapshotBase` pauses and cuts snapfile + memfile. So "snapshot after the warming client exits" (ADR condition 1) is implemented entirely inside guest-init: do not return 200 from `/shim/ready` until the warming `bazel cquery` subprocess has exited and a settle delay has passed.
3. **Base-snapshot rootfs is read-only and shared; mutable state must be RAM.** `noded/fcvm/driver/driver.go:49-52`: with `RootfsReadOnly` the rootfs is never written and one shared rootfs file backs every restored microVM; mutable state lives in RAM, captured in the memfile. Therefore Bazel's output_user_root (install base + output_base) MUST be on tmpfs.
4. **Vendoring pattern:** `multiarch_http_file` (`bazel/tools/http/multiarch_http_file.bzl`, see the `bb` example in `MODULE.bazel` around line 590) for single binaries; `k3s_archive` (`bazel/tools/http/k3s_archive.bzl`) for multi-file tar layers consumed by `apko_image(multiarch_tars=[...])`.
5. **Workload registration is chart values, three refs that must match:** the rootfs-builder `GUEST_IMAGE`, the `EMBERVM_NODED_IMAGES` entry, and the Workload template image ref all derive from one Bazel-pinned `<name>.guestImage` value (see `runtimePython` / `scratchPostgres` blocks in `projects/embervm/chart/values.yaml` around lines 424-540, and the corresponding `templates/workload-*.yaml`).
6. **Monolith submit contract:** `projects/monolith/faas/embervm_client.py` `submit(name, body=..., guest_path=..., read_timeout=...)` POSTs to the workload's sync submit with `X-Ember-Guest-Path` (MUST equal the workload's `invokePath`, else 404).
7. **Public tier rules** (`docs/runbooks/public-tier-checklist.md`): public pages never fetch `/api/...` directly (same-origin `+server.js` proxies), no private imports in the public binary (`app/modules_public.py` + `app/main_public_imports_test.py`), Turnstile gate active only when `TURNSTILE_SECRET_KEY` is set. No new DB tables here, so no `public_reader` grants.
8. **The flag-drift failure mode (ADR condition 2) is eliminated by construction:** a single Go function builds the cquery argv used by BOTH the warming run and every serving run. There is no `.bazelrc` and no control-plane-supplied flag.

### Design decisions already made (do not reopen)

- **Abseil pinned to an LTS with WORKSPACE support, run with `--noenable_bzlmod` and `--distdir`.** Bzlmod would need vendored BCR registry metadata or network; WORKSPACE mode with a distdir of checksummed archives is strictly simpler and fully offline. Use Bazel 7.x (latest 7.x patch) and Abseil 20240116.2 (or the newest LTS whose WORKSPACE mode still works under `--noenable_bzlmod`; the enumeration step in Task 1 confirms).
- **All Bazel mutable state on tmpfs:** `--output_user_root=/tmp/bazel`, `HOME=/tmp/home`. Convenience symlinks suppressed (`--experimental_convenience_symlinks=ignore`) because the workspace at `/opt/abseil` is on the read-only rootfs.
- **JVM sizing v1:** `startup --host_jvm_args=-Xmx1g`. Epsilon GC is an ADR open question, not v1.
- **One query per clone.** Task-class `Assign` destroys the VM after the response. That IS the reap; no idle TTL logic needed.
- **The cold-vs-warm "recorded" panel is v1-static:** the measured table from the ADR rendered as a figure panel, labelled honestly (macOS warm-server, pre-snapshot). A video/animation and the Envoy tier are follow-ups, out of scope.
- **Workload name: `bazel-query`.** Guest vsock port 1027, `readyPath: /shim/ready`, `invokePath: /query`.

---

## PR 1: embervm guest runtime + workload

Worktree: this plan's branch `feat/embervm-skyframe-demo` (already created). PR 1 and PR 2 are separate branches/PRs; create `feat/embervm-bazel-query-base` for PR 1 work if executing tasks in parallel sessions, otherwise reuse this branch for PR 1 and a second worktree for PR 2.

### Task 1: Enumerate Abseil's external deps and pin all artifacts

This is research producing a table, no repo code yet.

**Step 1:** On the workstation (allowed: this is scratch research against upstream Abseil, not the repo's forbidden local test loop), download the pinned Bazel and Abseil release into the session scratchpad and enumerate what a warming `cquery` fetches:

```bash
cd <scratchpad>
curl -LO https://github.com/bazelbuild/bazel/releases/download/7.4.1/bazel-7.4.1-darwin-arm64 && chmod +x bazel-7.4.1-darwin-arm64
curl -L https://github.com/abseil/abseil-cpp/releases/download/20240116.2/abseil-cpp-20240116.2.tar.gz | tar xz
cd abseil-cpp-20240116.2
../bazel-7.4.1-darwin-arm64 --output_user_root=/tmp/absl-scratch cquery //absl/... --noenable_bzlmod --output=label >/dev/null
../bazel-7.4.1-darwin-arm64 --output_user_root=/tmp/absl-scratch query 'kind(http_archive, //external:*)' --noenable_bzlmod --output=build
```

**Step 2:** From the `--output=build` output, record for every http_archive that the cquery actually needed (expect roughly: `bazel_skylib`, `rules_cc`, `platforms`, `com_google_googletest`, `com_github_google_benchmark`, possibly `rules_license`, plus Bazel's own `@local_config_*` which are generated, not fetched): the first URL and its sha256. Where the WORKSPACE lacks a sha256, download the archive and compute one.

**Step 3:** Confirm the run works fully offline: clear the scratch output root, re-run the cquery with `--distdir=<dir-of-downloaded-archives>` and the network blocked conceptually (`--repository_cache=` empty, no re-download should occur; verify by watching for "Fetching" lines, there must be none after the distdir is populated).

**Step 4:** Also record the Bazel linux binary URLs + sha256s (`bazel-7.4.1-linux-x86_64`, `bazel-7.4.1-linux-arm64`) from the upstream release checksums.

**Output:** a table of (name, url, sha256) committed as a comment block inside the repo rule in Task 2. No commit in this task.

### Task 2: Vendor the artifacts (MODULE.bazel + repo rule)

**Files:**
- Create: `bazel/tools/http/bazel_demo_workspace.bzl`
- Modify: `MODULE.bazel` (next to the `k3s_archive` block, around line 574)

**Step 1:** Add the Bazel binary via the existing `multiarch_http_file` rule in `MODULE.bazel`:

```starlark
multiarch_http_file(
    name = "bazel_demo_bin",
    amd64_sha256 = "<sha>",
    amd64_url = "https://github.com/bazelbuild/bazel/releases/download/7.4.1/bazel-7.4.1-linux-x86_64",
    arm64_sha256 = "<sha>",
    arm64_url = "https://github.com/bazelbuild/bazel/releases/download/7.4.1/bazel-7.4.1-linux-arm64",
    binary_name = "bazel",
    package_dir = "/usr/local/bin",
)
```

**Step 2:** Write `bazel_demo_workspace.bzl` mirroring `k3s_archive.bzl` (same genrule-emitting shape, same `:tar` alias with `multiarch_tar` tag). It downloads:
- the Abseil release archive, `download_and_extract`ed, re-tarred so the tree lands at `/opt/abseil/` in the image (strip the top-level `abseil-cpp-<ver>/` prefix);
- each dep archive from Task 1's table verbatim (`repository_ctx.download`, NOT extract) into `/opt/distdir/<original-filename>` (distdir matches by filename + sha256, so keep upstream filenames exactly).

The content is arch-independent; emit identical `tar_amd64` and `tar_arm64` genrules so `apko_image`'s `multiarch_tars` label-suffixing works (see `k3s_archive.bzl` lines 69-105).

**Step 3:** Register it in `MODULE.bazel`:

```starlark
bazel_demo_workspace = use_repo_rule("//bazel/tools/http:bazel_demo_workspace.bzl", "bazel_demo_workspace")

bazel_demo_workspace(
    name = "bazel_demo_workspace",
    abseil_url = "https://github.com/abseil/abseil-cpp/releases/download/20240116.2/abseil-cpp-20240116.2.tar.gz",
    abseil_sha256 = "<sha>",
    abseil_strip_prefix = "abseil-cpp-20240116.2",
    distdir_urls = [...],     # parallel string lists, or repeated attr pairs like k3s_archive
    distdir_sha256s = [...],
)
```

(Exact attr shape is the implementer's call; keep it a plain repository_rule like `k3s_archive`, and follow `feedback_bzlmod_bcr_check`: this is a repo rule, not a BCR module, so no BCR lookup applies.)

**Step 4:** Run `bazel/tools/format/fast-format.sh`. Commit: `feat(embervm): vendor pinned bazel + abseil workspace for the skyframe demo base`

### Task 3: Guest runtime image (apko + guest-init)

**Files:**
- Create: `projects/embervm/runtimes/bazel/apko.yaml`
- Create: `projects/embervm/runtimes/bazel/BUILD`
- Create: `projects/embervm/runtimes/bazel/guest-init/cmd/main.go`
- Create: `projects/embervm/runtimes/bazel/guest-init/cmd/main_test.go` (pure-function tests)
- Create: `projects/embervm/runtimes/bazel/README.md`

**apko.yaml** (mirror `runtimes/k3s/k3s-server/apko.yaml` commentary style):

```yaml
contents:
  repositories:
    - https://packages.wolfi.dev/os
  keyring:
    - https://packages.wolfi.dev/os/wolfi-signing.rsa.pub
  packages:
    - busybox
    - ca-certificates-bundle
    # Bazel's cc toolchain autodetection (local_config_cc) probes a real
    # compiler during the LOADING phase of the warming run; without one,
    # analysis of cc_* targets fails toolchain resolution. gcc + glibc-dev
    # are baked so the probe succeeds inside the zero-egress guest.
    - gcc
    - glibc-dev
    - binutils
    # bazel shells out to a handful of coreutils beyond busybox applets
    - coreutils
archs:
  - x86_64
  - aarch64
accounts:
  run-as: 0    # guest-init needs mounts; microVM boundary is the isolation statement (k3s precedent)
environment:
  PATH: /usr/local/bin:/usr/bin:/bin
entrypoint:
  command: /usr/local/bin/ember-bazel-init
```

**BUILD:** copy the k3s-server BUILD shape: `platform_transition_filegroup` + `tar` per arch for guest-init at `/usr/local/bin/ember-bazel-init`, then

```starlark
apko_image(
    name = "image",
    config = "apko.yaml",
    contents = "@embervm_runtime_bazel_lock//:contents",
    multiarch_tars = [
        "@bazel_demo_bin//:tar",          # /usr/local/bin/bazel  (check actual target name multiarch_http_file emits; the bb example resolves via :tar or the binary label, mirror it)
        "@bazel_demo_workspace//:tar",    # /opt/abseil + /opt/distdir
        ":guest_init_tar",
    ],
    repository = "ghcr.io/jomcgi/homelab/projects/embervm/runtimes/bazel",
)
```

Add the apko lock (`apko.lock.json`): generated via the repo's lock flow; on darwin this needs podman (memory `embervm-r4-stateful`: apko-lock-darwin=podman). Register `embervm_runtime_bazel_lock` in `MODULE.bazel` next to the other runtime locks (grep `embervm_runtime_k3s_server_lock` for the pattern).

**guest-init/cmd/main.go** structure (single flow; the snapshot resumes the process mid-serve, so there are no boot lanes):

```go
// PID 1 for the bazel-query demo guest (ADR embervm/010).
// Boot: mount /proc + a large tmpfs at /tmp, run the warming cquery, and only
// after the warming CLIENT has exited (ADR condition 1) flip ready. noded's
// WaitReady then sees /shim/ready 200 and cuts the base snapshot with the warm
// bazel SERVER still resident. Restored clones resume inside the vsock serve
// loop; each serves exactly one POST /query (task-class Assign destroys the VM
// after the response).

const (
    workspaceDir  = "/opt/abseil"
    outputRoot    = "/tmp/bazel"   // tmpfs: captured by the memfile, rootfs stays read-only
    homeDir       = "/tmp/home"
    distDir       = "/opt/distdir"
    warmExpr      = "//absl/..."
    queryTimeout  = 15 * time.Second
    maxOutput     = 256 << 10      // bytes; truncate + mark
    settleDelay   = 10 * time.Second // post-warming idle GC settle before ready
)

// buildArgv is the SINGLE source of truth for every bazel invocation, warming
// and serving alike, so serving flags can never drift from warming flags
// (ADR condition 2: any delta silently discards the analysis cache).
func buildArgv(expr string) []string {
    return []string{
        "/usr/local/bin/bazel",
        "--output_user_root=" + outputRoot,
        "cquery", expr,
        "--noenable_bzlmod",
        "--distdir=" + distDir,
        "--experimental_convenience_symlinks=ignore",
        "--output=label",
    }
}
```

plus:

- `validateExpr(expr string) error`: length cap 512; single line; allowed charset `[A-Za-z0-9_/:.@~+*-]` plus `() ,="'` and space; reject any token starting with `-` (flag smuggling); reject empty. (Defense in depth; the edge validates first. `--output=starlark` injection is impossible because argv is fixed and expr is one argv element.)
- warming: `exec.Command` of `buildArgv(warmExpr)` with `Dir: workspaceDir`, `HOME=/tmp/home` env; on non-zero exit, log and do NOT flip ready (the base build then fails loudly on BootReadyTimeout rather than snapshotting a cold-or-broken server).
- startup flags: pass `--host_jvm_args=-Xmx1g` as a startup option (`bazel --output_user_root=... --host_jvm_args=-Xmx1g cquery ...`; startup options go before the command. Keep them inside buildArgv so they are identical everywhere).
- vsock HTTP server on port 1027 (copy the listener/mux shape from `runtimes/postgres/guest-init/cmd/main.go`): `GET /shim/healthz` always 200; `GET /shim/ready` 200 only after ready; `POST /query` parses `{"expression": "..."}`, validates, runs `buildArgv(expr)` with a `queryTimeout` context, and returns JSON:

```json
{"labels": "<stdout, truncated at maxOutput>", "truncated": false,
 "analyzed_line": "Analyzed 123 targets (0 packages loaded, 0 targets configured) ...",
 "wall_ms": 240}
```

  `analyzed_line` is grepped from stderr (`^Analyzed ` prefix); it is the ADR's proof-of-restore and drift detector. Non-zero bazel exit returns 422 with the tail of stderr (visitor typo'd query expressions are the normal case, surface bazel's error text).

**main_test.go:** table tests for `validateExpr` (accepts `deps(//absl/strings)`, `kind("cc_library", //absl/...)`, `somepath(//absl/base, //absl/time)`; rejects `--output=starlark`, `foo --flag`, 600-char strings, newlines) and for `buildArgv` (expr lands as exactly one element; flags list matches a golden slice; goldens make accidental flag edits loud in review). Follow TDD order: write tests first, then the implementation; CI runs them.

Run `bazel/tools/format/fast-format.sh` (gazelle will create the go BUILD targets). Commit: `feat(embervm): bazel skyframe demo guest runtime (warm cquery base, one query per clone)`

### Task 4: Workload registration + embervm chart bump

**Files:**
- Modify: `projects/embervm/chart/values.yaml` (new workload block, mirror `scratchPostgres`/`runtimePython` around lines 424-540)
- Create: `projects/embervm/chart/templates/workload-bazel-query.yaml` (mirror `templates/workload-sandbox.yaml`)
- Modify: whatever registers guest images into the rootfs-builder + `EMBERVM_NODED_IMAGES` table (follow the three-refs-derive-from-one-value pattern; grep `runtimePython.guestImage` in templates)
- Modify: the embervm `BUILD` `helm_chart(images = {...})` pin map: add `bazelQuery.guestImage` -> `//projects/embervm/runtimes/bazel:image.info` (grep how `runtimePython.guestImage` is pinned)

**Workload spec values (initial sizing, conservative; node-4 RAM arithmetic in the values comment):**

```yaml
bazelQueryWorkload:
  enabled: true
  name: bazel-query
  port: 1027
  readyPath: /shim/ready
  invokePath: /query
  vcpus: 2
  memMib: 3072      # -Xmx1g heap + install base + output_base(external/ extracted) on tmpfs + OS
  floor: 1          # primed clones kept warm; restore is demand-paged so miss cost is small
  cap: 2            # governor is RAM (ADR): raise only after measuring per-clone dirtied RSS
  timeoutSeconds: 20
rootfsPath: /disks/nvme-02/embervm-noded/bazel-query/rootfs.ext4
```

Invocation block: `resultMaxBytes: 1048576`, retries on `transport` only (a guest 5xx here is a visitor's bad query, never retry it), `resultTtlSeconds: 600`, no dead-letter, no triggers.

**Steps:** template + values + image pin, `helm template embervm projects/embervm/chart/ -f projects/embervm/deploy/values.yaml` renders clean and shows the three matching image refs, `bazel/tools/format/fast-format.sh`, then `bazel/tools/git/bump-chart.sh projects/embervm`. Commit: `feat(embervm): register bazel-query task workload for the skyframe demo`

### Task 5: PR 1 review, CI, merge, live verification

1. Self-review the full diff, then one comprehensive Opus code review of the PR diff (repo cadence: one review per PR).
2. Push, open PR (`gh pr create`), watch `gh pr checks <n> --watch`; iterate via `mcp__buildbuddy__get_invocation` (+ `get_target`/`get_log`) on failures, quoting errors verbatim before hypothesizing.
3. Merge (`gh pr merge --rebase`, update-branch first if BEHIND). ArgoCD syncs embervm.
4. **Live verification (the real test):**
   - Watch the base build: `kubectl logs -n embervm -l app=embervm-noded --tail=200` for the BuildBase of `bazel-query` (expect a minutes-long warming, then snapshot). BaseBuilder retries with backoff on failure; a wedged warming shows as BootReadyTimeout loops.
   - Submit a query through the control plane from inside the cluster (mirror how the FaaS smoke does it; simplest is `kubectl exec` into a monolith pod and use the existing client):
     `POST {EMBERVM_URL}/v1/workloads/bazel-query/tasks?wait=true` with `X-Ember-Guest-Path: /query` and body `{"expression":"deps(//absl/strings)"}`.
   - Assert the response JSON contains labels, and `analyzed_line` contains `(0 packages loaded, 0 targets configured)`. That line is the ADR's proof of pure Skyframe reuse; if it ever shows nonzero packages loaded, flags drifted or the snapshot is cold, stop and fix before PR 2.
   - Record the observed end-to-end latency (this is ADR Open Question 5's first Linux data point; note it in the PR).

---

## PR 2: public demo surface

New worktree from post-merge main: `git -C ~/repos/homelab worktree add -b feat/ember-bazel-demo-page /tmp/claude-worktrees/ember-bazel-demo-page origin/main`

### Task 6: Backend endpoint in ember_public

**Files:**
- Create: `projects/monolith/ember_public/bazel_router.py`
- Create: `projects/monolith/ember_public/bazel_core.py`
- Create: `projects/monolith/ember_public/bazel_core_test.py` (+ py_test target; new *_test.py needs an explicit py_test, memory `monolith-gazelle-pytest-targets`)
- Modify: `projects/monolith/ember_public/__init__.py` / `module.py` (include the new router in `register_public`)

**bazel_core.py:**
- `EXPR_RE` validation identical in spirit to guest-init's `validateExpr` (length 512, charset allow-list, no token starting with `-`); this is the primary gate, the guest's is defense in depth.
- Session gate + rate limit: reuse the postgres demo helpers in `ember_public/core.py` (Turnstile `siteverify`, session cookie minted at the demo's own prefix, token-bucket ~1 query per 3s per session, module-level `asyncio.Semaphore(2)` matching the workload `cap`).
- `run_query(expr)`: `faas.embervm_client.submit("bazel-query", body=json.dumps({"expression": expr}).encode(), guest_path="/query", read_timeout=25.0)`; map transport error -> 502, `EmberVMTimeout` -> 504, pass guest 422 (bad query) through with bazel's error text, forward the guest JSON on 200. Log a warning (drift alarm) if `analyzed_line` lacks `(0 packages loaded`.

**bazel_router.py:** `APIRouter(prefix="/api/ember/bazel", tags=["ember"])` with `POST /query`, `POST /session`, and `GET /status` (cheap cached introspection like the postgres one if trivially reusable, else omit status for v1).

TDD: write `bazel_core_test.py` first (validation table, semaphore behavior with a stubbed submit, error mapping). Commit: `feat(monolith): public bazel skyframe query endpoint for the ember demo`

### Task 7: Frontend page, proxies, landing card

**Files:**
- Create: `projects/monolith/frontend/src/routes/public/ember/bazel/+page.svelte` and `+page.server.js`
- Create: `projects/monolith/frontend/src/routes/public/ember/bazel/api/{query,session}/+server.js` (same-origin proxies, copy the postgres ones verbatim including the Set-Cookie Path rescope)
- Modify: `projects/monolith/frontend/src/routes/public/ember/+page.svelte` (new `.door` card, lines ~514-534)

**Page content (follow the postgres demo's console pattern and the /ember typeset voice):**
- A query console: input box preloaded with `deps(//absl/strings)`, a few one-click example chips (`kind("cc_library", //absl/...)`, `somepath(//absl/base, //absl/time)`), stopwatch, results list (truncate display), and the proof badge rendering `analyzed_line` verbatim with `0 packages loaded, 0 targets configured` highlighted, captioned as the reuse proof.
- The recorded cold-vs-warm panel: a static figure of the ADR's measured table (13.8s cold vs 0.31s warm analysis, Abseil), explicitly captioned as measured on a warm server pre-snapshot (macOS); wording leaves room to swap in the Linux relight numbers from Task 5's measurement.
- Landing door card: `label "live demo"`, title like "Query a frozen Bazel brain", go-text `ember/bazel`.

Reuse `/lib/public/ember/ember.css` tokens; no new fetches from the page except the same-origin proxies. Remember `feedback_vite_ssr_noexternal` if any new dep is added (avoid adding deps). Commit: `feat(monolith): /ember/bazel interactive skyframe query demo page`

### Task 8: Chart bumps + PR 2 review, CI, merge, live verification

1. `bazel/tools/git/bump-chart.sh projects/monolith` AND `bazel/tools/git/bump-chart.sh projects/monolith-public` (both tiers serve /ember content; memory `jomcgi-dev-served-by-monolith`).
2. Self-review, one comprehensive Opus review of the PR diff, push, PR, CI watch, rebase-merge.
3. Live verification (public-tier checklist):
   - `curl -sS https://jomcgi.dev/ember` shows the new door; `curl -sS https://jomcgi.dev/ember/bazel` returns 200.
   - Full visitor flow in a browser context: mint session (Turnstile), run the default query, observe labels + proof badge + latency.
   - Confirm `main_public_imports_test` passed in CI (no private imports leaked).
   - Rate limit: second immediate query returns 429.

---

## Out of scope (recorded as follow-ups, do not implement)

- Envoy headline tier (multi-GB heap, Linux measurement): ADR Open Question 4.
- Epsilon/no-op GC tuning and per-clone dirtied-RAM measurement to raise `cap`: ADR Open Question 1.
- A live RBE build tier (host-side credential proxy): ADR Open Question 3.
- Video/animated recorded comparison replacing the static panel.

## Risks the implementer must not "fix" creatively

- If the warming cquery fails in CI-built images (e.g. a missing distdir archive), the fix is adding the archive to `bazel_demo_workspace`, never giving the guest network.
- If clones re-analyze (`packages loaded` nonzero), the fix is in `buildArgv` unification or snapshot timing, never in widening flags per-request.
- Do not add flags support, `--output` choices, or a `build` verb to the public surface under any circumstances (`--output=starlark` is code execution; ADR Security).
