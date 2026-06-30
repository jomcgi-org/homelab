# semgrep-scand Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Build `semgrep-scand`, a node-4 Firecracker-isolated semgrep diff scanner, plus a monolith MCP tool `semgrep_scan` that posts scan requests to it.

**Architecture:** A separate node-4 daemon restores (or, in the first cut, boots) a microVM running a warm, fully-offline `semgrep lsp` with the local + Pro rule packs compiled in memory, feeds the changed files over vsock, collects diagnostics, and discards the VM. It reuses the Firecracker primitives that today live inside `fc-agentd` by first extracting them into a shared `fcvm` package. A monolith MCP tool is a thin HTTP client over the daemon. Full design and rationale: **ADR 029** (`docs/decisions/agents/029-firecracker-semgrep-scan-service.md`).

**Tech Stack:** Go (daemon + guest init), Firecracker, vsock, apko + rules_apko + Bazel tar layers, pysemgrep (`semgrep lsp`) + the licensed Pro engine/packs (private GHCR), Helm, Python/FastMCP (monolith).

**Repo conventions baked into this plan:**

- **No local test loop.** Do not run `go test`/`pytest`/`bazel test` from a workstation. Author the test and the implementation together in each task; verification happens in end-of-plan BuildBuddy CI on the pushed branch. The "run it to fail" TDD step is replaced by "author test + impl, rely on CI."
- **One comprehensive code review per merged PR** at the end, not per task (per repo CLAUDE.md).
- No em-dashes anywhere. Conventional Commits. Chart.yaml version bumps must also bump `deploy/application.yaml` `targetRevision`.
- Container images are apko only, dual-arch (x86_64 + aarch64), non-root where possible; the FC-launching daemon container is the documented exception (privileged root).

**Suggested PR split:** Phase 0 (fcvm extraction) is a self-contained refactor and may merge as its own PR first to isolate any `fc-agentd` regression. Phases 1 to 4 form the feature PR. Phase 5 is a follow-on PR.

---

## Phase 0: Extract the shared `fcvm` package

Go `internal/` visibility means a sibling daemon cannot import `fc-agentd/internal`. Move the generic Firecracker primitives into a shared package both daemons import. Zero behavior change; `fc-agentd` must still build and pass its existing tests in CI.

### Task 0.1: Create `fcvm` and move the Firecracker REST client

**Files:**

- Create: `projects/agent_platform/fcvm/fcclient/client.go` (moved from `projects/agent_platform/fc-agentd/internal/fcclient/client.go`, including all request/response types)
- Create: `projects/agent_platform/fcvm/fcclient/BUILD`
- Delete: `projects/agent_platform/fc-agentd/internal/fcclient/`

**Steps:**

1. Move `client.go` verbatim into the new path; change the package clause only if needed (keep `package fcclient`). Move the colocated `*_test.go` with it.
2. Update the Go import path everywhere it is referenced in `fc-agentd` (search `internal/fcclient`).
3. Regenerate BUILD files with `format` (gazelle) so the new `go_library` + `go_test` targets and the updated `fc-agentd` deps are correct.
4. Verification (CI): `fc-agentd` compiles; the moved `fcclient` test still passes.
5. Commit: `refactor(agent-platform): extract fcclient into shared fcvm package`

### Task 0.2: Move launcher, provisioner, and FC driver primitives into `fcvm`

**Files:**

- Create: `projects/agent_platform/fcvm/driver/launcher.go`, `provisioner.go`, `driver.go` (moved from `fc-agentd/internal/driver/`), plus their `*_test.go`
- Create: `projects/agent_platform/fcvm/driver/BUILD`
- Modify: every `fc-agentd` file importing `internal/driver` and `internal/fcclient`
- Keep in `fc-agentd`: `internal/reconcile/`, `internal/store/`, `internal/control/`, `internal/config/` (thread-lifecycle specific)

**Steps:**

1. Move `launcher.go`, `provisioner.go`, `driver.go` and tests into `fcvm/driver/`. `driver.go` imports `projects/agent_platform/substrate` (already a shared, non-internal package) and `fcvm/fcclient`; both remain importable.
2. If `driver.go` references anything from `internal/config` or `internal/store`, invert the dependency: pass those values in via the existing `driver.Config` struct rather than importing back into `fcvm` (fcvm must not import fc-agentd internals; no import cycles).
3. Update `fc-agentd` imports (`reconcile`, `cmd/main.go`) to the new `fcvm/driver` path.
4. `format` to regenerate BUILD files.
5. Verification (CI): `fc-agentd` builds; `fcvm/driver` and `fc-agentd` tests pass.
6. Commit: `refactor(agent-platform): move FC driver primitives into shared fcvm package`

### Task 0.3: Extract the egress forwarder if reused (optional)

**Files:** Create `projects/agent_platform/fcvm/control/egress.go` only if `semgrep-scand` will need guest egress (it does not for in-VM-only scanning). **Default: skip.** The semgrep guest has no outbound network need (fully offline), so do not port `control/egress.go` or `control/server.go`. Note this decision in the commit body of Task 0.2 and move on.

---

## Phase 1: The `semgrep-guest` microVM image and guest agent

A microVM that runs a warm, offline `semgrep lsp` and answers `{files}` scan requests over vsock with `{findings}`.

### Task 1.1: Bazel tar layers for the Pro engine + merged rules

**Files:**

- Create: `bazel/semgrep/guest/BUILD`

**Design:** apko cannot pull private-GHCR OCI artifacts, so bake the engine and rules as Bazel-built tar layers (the same mechanism `harness/BUILD` uses for the goose binary).

**Steps:**

1. Add `tar` targets (using `@aspect_bazel_lib//lib:tar.bzl`), per arch where the engine is arch-specific:
   - `engine_tar`: places `@semgrep_pro_engine_{amd64,arm64}//:engine` at `/opt/semgrep/semgrep-core-proprietary` (mode 0755), selected per platform.
   - `rules_tar`: flattens `//bazel/semgrep/rules:all_rules` (local rules + the four Pro packs) into `/etc/semgrep/rules/` (preserve unique filenames; the Pro packs are single large YAMLs, local rules are many small ones).
2. Confirm `MODULE.bazel` already `use_repo`s the pro engine + rule pack repos (it does for the CI semgrep tests); reuse those repos, do not re-declare.
3. Verification (CI): `bazel build //bazel/semgrep/guest:engine_tar //bazel/semgrep/guest:rules_tar` succeeds (needs `GHCR_TOKEN`, already in CI).
4. Commit: `build(semgrep): bazel tar layers for guest engine and merged rules`

### Task 1.2: pysemgrep venv layer (provides `semgrep lsp`)

**Files:**

- Create/modify: a pinned pip requirement for `semgrep==<match-engine-version>` (the Pro engine is currently 1.168.0; pin pysemgrep to the same minor so its bundled OSS core matches), wired through the repo's `@pip` / aspect_rules_py tooling
- Create: `bazel/semgrep/guest/` venv tar target placing the `semgrep` console script + its site-packages into the guest image

**Steps:**

1. Add `semgrep==1.168.0` (match the `bazel/semgrep/third_party/semgrep_pro/digests.bzl` engine version; if the pinned engine version changes, this pin moves with it) to the pip lock used for guest tooling.
2. Build a relocatable venv/site-packages tar layer for the guest. The guest sets `SEMGREP_CORE_BIN=/opt/semgrep/semgrep-core-proprietary` so pysemgrep drives the Pro engine (the spike confirmed this pairing works).
3. **Risk to flag in the commit body:** version coupling between pysemgrep and the Pro engine. If a Wolfi `semgrep` apko package at the right version exists, prefer it over a hand-built venv (simpler); investigate during implementation and pick the lighter option.
4. Verification (CI): the venv tar builds; a smoke target runs `semgrep --version` from the baked layout (deferred to image build).
5. Commit: `build(semgrep): pin pysemgrep venv layer for the guest lsp`

### Task 1.3: `semgrep-guest-init` Go binary

**Files:**

- Create: `projects/agent_platform/semgrep-guest-init/cmd/main.go`
- Create: `projects/agent_platform/semgrep-guest-init/internal/lspdriver/lspdriver.go` (stdio JSON-RPC client for `semgrep lsp`)
- Create: `projects/agent_platform/semgrep-guest-init/internal/scanserver/scanserver.go` (vsock scan request/response server)
- Reuse: the loopback bring-up from `fc-agent-init/cmd/loopback_linux.go` (copy or extract a tiny `fcvm/netutil`); reuse vsock listen from the AF_VSOCK pattern in `fc-agent-init/internal/vsockdial/`
- Test: `lspdriver_test.go` (fake LSP over pipes), `scanserver_test.go` (framing)

**Behavior:**

1. PID1: bring `lo` UP (non-negotiable, the documented FC raw-boot gotcha), set the offline env (empty `SEMGREP_APP_TOKEN`, isolated `HOME`, throwaway `SEMGREP_SETTINGS_FILE`, `SEMGREP_SEND_METRICS=off`, `SEMGREP_ENABLE_VERSION_CHECK=0`, `SEMGREP_CORE_BIN` to the baked engine).
2. Start `semgrep lsp` as a subprocess; drive stdio JSON-RPC: `initialize` with `initializationOptions.scan.configuration=["/etc/semgrep/rules"]`, `onlyGitDirty=false`, `jobs=<vcpus>`; wait for the rules-loaded state (first `publishDiagnostics` cycle or the rules-refreshed signal).
3. Once warm, send `KindHello` (then a ready marker) to the host control vsock so the daemon knows it is snapshot-ready / scan-ready.
4. Listen on a dedicated **scan vsock port** (add a `ScanPort` constant to `vsockproto`). Per request: decode `ScanRequest{files:[{path,content}]}`, write each file under a scratch workspace dir, `didOpen` (or `didChange`) each, collect `publishDiagnostics`, translate to `Finding{path,line,col,ruleId,severity,message}`, reply `ScanResult{findings,errors}`.
5. Keep the LSP resident across requests (it is the warm process the snapshot captures).

**Steps:** author the LSP driver with a fake-LSP unit test (initialize handshake, didOpen, diagnostics translation); author the scan server framing test; then `main.go` wiring. `format` for BUILD files. Commit: `feat(semgrep): semgrep-guest-init resident lsp + vsock scan server`

### Task 1.4: Extend `vsockproto` with the scan protocol

**Files:**

- Modify: `projects/agent_platform/vsockproto/proto.go`
- Test: `proto_test.go`

**Steps:**

1. Add `ScanPort uint32 = 1026` and types: `ScanRequest{Files []ScanFile}`, `ScanFile{Path, Content string}`, `ScanResult{Findings []Finding, Errors []string}`, `Finding{Path string, Line, Col int, RuleID, Severity, Message string}`. Keep newline-delimited JSON framing consistent with the existing `Conn`.
2. Author a round-trip encode/decode test.
3. Commit: `feat(agent-platform): vsock scan request/response protocol`

### Task 1.5: `semgrep-guest` apko image

**Files:**

- Create: `projects/agent_platform/semgrep-guest/apko.yaml`, `apko.lock.json`, `BUILD`

**Steps:**

1. Wolfi base with `ca-certificates-bundle`, `busybox`, `git` (semgrep may shell to git even with `onlyGitDirty=false`; include it), plus the Python runtime the venv needs. `entrypoint: /usr/local/bin/semgrep-guest-init`. Non-root user where possible (the LSP does not need root); if the raw-FC PID1 needs root for the `lo` ioctl, run as root (match `fc-agent-init`).
2. `apko_image` with `multiarch_tars`: `//bazel/semgrep/guest:engine_tar`, `:rules_tar`, the pysemgrep venv tar, and the `semgrep-guest-init` cross-compiled tar.
3. Verification (CI): `bazel build //projects/agent_platform/semgrep-guest:image` succeeds dual-arch.
4. Commit: `build(semgrep): semgrep-guest apko image`

---

## Phase 2: `semgrep-scand` daemon (boot-and-warm path)

End-to-end working scanner using boot-per-scan (no snapshot yet). Slow (~3 to 5s/scan) but proves the whole pipeline and is the ADR's own fallback path.

### Task 2.1: Daemon skeleton, config, HTTP `/scan`

**Files:**

- Create: `projects/agent_platform/semgrep-scand/cmd/main.go`
- Create: `projects/agent_platform/semgrep-scand/internal/config/config.go` (env: `MaxConcurrent`, `GuestMemMib` default 2048, `GuestOomScoreAdj` default 1000, `BaseRootfsPath`, `NvmeRoot`, FC binary/kernel paths, `ListenAddr`)
- Create: `projects/agent_platform/semgrep-scand/internal/server/server.go` (HTTP `POST /scan` accepting `{files:[{path,content}]}` returning `{findings,errors}`, plus `GET /healthz`)
- Test: `config_test.go`, `server_test.go` (HTTP handler against a fake scanner)

**Steps:** author config parsing + tests; HTTP server with a `Scanner` interface dependency (so the handler is testable without FC); commit `feat(semgrep): semgrep-scand skeleton, config, /scan endpoint`.

### Task 2.2: Boot-and-warm scan orchestration via `fcvm`

**Files:**

- Create: `projects/agent_platform/semgrep-scand/internal/scanner/scanner.go` (implements the `Scanner` interface)
- Test: `scanner_test.go` (with a fake `fcvm` substrate + fake guest conn)

**Behavior:**

1. Admission: a weighted semaphore capped at `MaxConcurrent` so at most `K` live guests (each hard-capped at `GuestMemMib`, a true cgroup ceiling).
2. Per scan: `Claim` a guest via `fcvm/driver` (Copy or Devmapper provisioner of the `semgrep-guest` base rootfs) with `OOMScoreAdj=GuestOomScoreAdj`; wait for the guest `KindHello`/ready on the control vsock; dial the guest **scan vsock port**; send `ScanRequest`; read `ScanResult`; `Release` (discard) the guest in a deferred cleanup.
3. Map `ScanResult` to the HTTP response.

**Steps:** author scanner with fake-substrate test; wire into the server; commit `feat(semgrep): boot-and-warm scan orchestration`.

### Task 2.3: Daemon apko image + Helm chart + deploy

**Files:**

- Create: `projects/agent_platform/semgrep-scand/apko.yaml`, `apko.lock.json`, `BUILD` (Wolfi + busybox + lvm2/util-linux for dmsetup, entrypoint the Go binary), `cmd/BUILD`
- Create: `projects/agent_platform/semgrep-scand/chart/` (copy and adapt `fc-agentd/chart/`: privileged root container gated by `firecracker.enabled`, hostPath mounts `/dev/kvm`, `/opt/kata` ro, `/dev/mapper`, `/disks/nvme-02`; single replica, node-affine to node-4, `Recreate`; rootfs-builder initContainer building the `semgrep-guest` base ext4; `priorityClassName: homelab-disposable`; resources limit = `MaxConcurrent * GuestMemMib + daemon overhead`; a `Service` exposing the HTTP port)
- Create: `projects/agent_platform/semgrep-scand/deploy/` (`application.yaml` multi-source OCI chart + `$values`, `values.yaml`, `kustomization.yaml`)
- Modify: regenerate the home-cluster root kustomization via `format`

**Steps:** adapt the chart (drop the Postgres/database block, the egress sidecar, and the Discord outbox; keep the FC + rootfs-builder plumbing); set `Chart.yaml` version and `deploy/application.yaml` `targetRevision` in sync; `helm template` render locally to verify; commit `feat(semgrep): semgrep-scand image, chart, and deploy`.

### Task 2.4: rootfs-builder for the `semgrep-guest` base

**Files:**

- Create: `projects/agent_platform/semgrep-scand/chart/templates/rootfs-builder-configmap.yaml` (adapt fc-agentd's: `crane export` the `semgrep-guest` image, `mkfs.ext4 -d` to `/disks/nvme-02/semgrep-base/rootfs.ext4`, idempotent marker; size it for the ~2Gi+ guest plus headroom)

**Steps:** adapt the script; ensure the image ref is wired from values; commit `feat(semgrep): in-cluster semgrep-guest base rootfs builder`.

---

## Phase 3: Snapshot-warm hot path

Add the base snapshot so each scan is `restore (~28ms) + match`, not a full boot. **This phase validates the deferred assumption: that a warm `semgrep lsp` survives Firecracker freeze/thaw and scans correctly on the first request after restore.** If it does not, the daemon stays on the boot-and-warm path (Phase 2) and this phase is shelved; no rework of Phases 0 to 2.

### Task 3.1: Base-snapshot manager

**Files:**

- Create: `projects/agent_platform/semgrep-scand/internal/basesnap/basesnap.go`
- Test: `basesnap_test.go`

**Behavior:**

1. On startup (or on demand), if no current base snapshot exists or it is stale: `Claim` a guest, wait for the warm-ready `KindHello`, `Snapshot`/`SnapshotBase` (via `fcvm/driver`) into `bases/<version>/`, then write a current-base pointer atomically (rename), and GC the previous memfile.
2. The snapshot is non-load-bearing: the scan path checks the base is valid and falls back to boot-and-warm if missing or corrupt.

**Steps:** author with a fake substrate test; commit `feat(semgrep): base-snapshot manager with atomic pointer swap`.

### Task 3.2: Restore-per-scan in the scanner

**Files:**

- Modify: `internal/scanner/scanner.go`

**Steps:** when a valid current base exists, `Restore(base)` instead of `Claim`; otherwise fall back to boot-and-warm. Same vsock scan request/response, same `Release`. Author a test covering both the restore path and the fallback. Commit `feat(semgrep): restore-from-base hot path with boot-and-warm fallback`.

### Task 3.3: Runtime validation (on deploy, not a unit test)

**Steps:** after the chart deploys to node-4, submit a scan and confirm: the daemon restored (not booted), the restored LSP returned findings warm, and end-to-end latency is sub-second for a typical file. Record the result. This is the first-boot validation called out in ADR 029. Not a CI gate; a post-deploy check.

---

## Phase 4: Monolith MCP tool `semgrep_scan`

### Task 4.1: The tool

**Files:**

- Create: `projects/monolith/semgrep/mcp.py`
- Test: `projects/monolith/semgrep/mcp_test.py`

**Behavior:**

```python
@mcp.tool
async def semgrep_scan(files: list[dict], format: str = "json") -> dict:
    """Scan changed files with Semgrep SAST via the in-cluster semgrep-scand service.

    Posts whole changed files to semgrep-scand, which runs the full local plus Pro
    ruleset in an isolated microVM and returns security findings.

    Args:
        files: List of objects, each with "path" and "content" of a changed file.
        format: Output format, "json" (default) or "sarif".

    Returns:
        A dict with "findings" (list) and "errors" (list).
    """
```

- Read `SEMGREP_SCAND_URL` from env (never hardcode the `.svc` URL); return a clear error if unset.
- `httpx.AsyncClient` POST to `${SEMGREP_SCAND_URL}/scan`; `raise_for_status`; return parsed JSON. Handle `ConnectError`/`HTTPStatusError` with structured error dicts (mirror `chat/vision.py`).
- Docstring must avoid the Context-Forge-forbidden characters (`&&`, `;`, `||`, `$(`, `|`, `>` space, `<` space); the compliance test enforces this.

**Steps:** author the tool + a registration test (assert `semgrep_scan` is registered on the shared MCP instance, mirroring `agent/mcp_test.py`); commit `feat(monolith): semgrep_scan MCP tool`.

### Task 4.2: Register and wire the URL

**Files:**

- Modify: `projects/monolith/app/main.py` (add `import semgrep.mcp  # noqa: F401`)
- Modify: `projects/monolith/chart/values.yaml` (+ deploy values): add `SEMGREP_SCAND_URL` env pointing at the in-cluster service (Helm release-prefixed name; inject from values, do not hardcode)
- Modify: `projects/monolith/chart/Chart.yaml` + `deploy/application.yaml` `targetRevision` (kept in sync)

**Steps:** add the import; wire the env; bump chart version + targetRevision; `helm template` render to verify the env lands; commit `feat(monolith): register semgrep_scan and wire SEMGREP_SCAND_URL`.

### Task 4.3: Post-deploy Context Forge refresh (operational note)

**Steps:** after the monolith rolls, the new MCP tool is invisible to the `homelab` connector until Context Forge re-discovers it. Run the `refresh-context-forge-tools` skill. This is an operational step, not code; record it in the PR description.

---

## Phase 5 (follow-on PR): rebuild trigger and enforcement hook

Lower priority; can ship after the core feature is live.

### Task 5.1: Base-snapshot daily rebuild trigger

Wire a rebuild off the `update-semgrep-pro.yaml` digest bump (new engine/rules digest implies a new `semgrep-guest` image implies a base-snapshot rebuild) plus an on-demand rebuild endpoint/MCP tool mirroring the agent tier's base-rebuild pattern.

### Task 5.2: Claude-side Stop hook

A deterministic Stop hook (settings.json) that, on turn end, runs `git diff` filtered to `.py/.go/.js/.jsx/.ts/.tsx/.rs`, sends whole changed files (fanned out concurrently) to `semgrep-scand` (directly, since hooks are shell commands and cannot invoke MCP tools), and blocks the turn with an actionable reason on ERROR-level findings; the next turn's scan clears the block. Note: `.rs` gets OSS coverage only (no Pro Rust pack).

---

## Verification summary (end-of-plan CI)

| Surface               | Check                                                              |
| --------------------- | ------------------------------------------------------------------ |
| `fcvm` extraction     | `fc-agentd` builds; fc-agentd + fcvm Go tests pass                 |
| Guest agent           | `lspdriver` + `scanserver` + `vsockproto` Go tests pass            |
| Images                | `semgrep-guest` and `semgrep-scand` apko images build dual-arch    |
| Daemon                | `semgrep-scand` Go tests (config, server, scanner with fakes) pass |
| Charts                | `helm template` renders `semgrep-scand` and the updated monolith   |
| MCP tool              | `semgrep_scan` registration + description-compliance tests pass    |
| Runtime (post-deploy) | warm-survives-restore validation; sub-second typical scan          |
