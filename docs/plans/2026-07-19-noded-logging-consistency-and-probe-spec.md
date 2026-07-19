# noded Log Consistency and Workload Probe Spec Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. This plan is intentionally split into two independent PRs (Part A and Part B); do not fold them into one branch.

**Goal:** Stop noded's container stream from interleaving three log dialects under one `noded` tag, and let a workload declare how it wants to be health-probed so a bare TCP probe stops spamming TLS-terminating guests with handshake errors.

**Architecture:** Two independent changes. (A) A logging cleanup: route noded's own `net/http` error output through slog, and tag guest stdout/stderr per workload and lifecycle phase at the launcher boundary so the stream is field-splittable. (B) A node-protocol change: add a per-workload probe spec (`kind: tcp | tls | http | https`) to the serving/stateful/group start requests, defaulted per class, so TLS endpoints get a completed handshake instead of an aborted connect.

**Tech Stack:** Go (`log/slog`, `net/http`, `crypto/tls`), gRPC / protobuf (`proto/embervm/node/v1/node.proto`), Elixir control plane (`control/lib/embervm`). Built and tested remotely via BuildBuddy CI (no local test loop; push the branch and watch `gh pr checks`).

---

## Background: why this plan exists

Investigation of a flood of `noded I0719 ... log.go:245] http: TLS handshake error from 10.101.0.1:<port>: EOF` lines established the full chain:

1. noded runs a per-VM health-probe loop every `DefaultProbeInterval = 5 * time.Second` (`noded/serving/probe.go:15`).
2. For opaque-L4 stateful and group VMs, the prober does a bare TCP **CONNECT and immediate close** (`noded/serving/probe_tcp.go`); health is "connect succeeded," not a status code.
3. When the probed guest endpoint terminates TLS, a connect-then-close looks like a client that opened a socket and hung up before the ClientHello, so the guest's `net/http` server logs `http: TLS handshake error from <peer>: EOF`. The `EOF` (not a cert error) is the fingerprint.
4. `10.101.0.1` is noded's OWN host-side bridge gateway (`compositeSupernet: 10.101.0.0/16`, `.1` = gateway), i.e. the source address of the probe, not an external caller.
5. The guest's output surfaces under the `noded` container tag because `noded/fcvm/driver/launcher.go:91-92` pipes Firecracker/guest stdout/stderr raw and unprefixed into noded's own stdout.

So the noise is a benign, healthy probe generating one spurious server-side error line per probe per endpoint per 5s, mis-attributed to noded because guest output is muxed unprefixed. Part A fixes the muxing/attribution; Part B fixes the probe/protocol mismatch so TLS endpoints stop being probed with an aborted handshake.

**Design decisions already settled (do not re-litigate):**
- The probe field is a **kind enum** (`tcp | tls | http | https`), not an http/https boolean. Serving picks from `{http, https}`; stateful and group pick from `{tcp, tls}`. A boolean would leave the L4 classes (the ones actually spamming) with no way to say "complete the handshake."
- **Do NOT enforce https/tls as a global default.** That inverts the bug: a tls probe against a plaintext endpoint fails outright and reports the VM unhealthy after the threshold, which is worse than log noise (false-negative health, possible eviction). Defaults stay per-class and match today's behavior: serving `http`, stateful/group `tcp`. Workloads opt up.
- TLS-variant probes complete the handshake with `InsecureSkipVerify` for now (a liveness probe authenticates nothing), leaving a seam to harden to SPIFFE verification later, consistent with `noded/cmd/auth.go:21` ("the upgrade path to mTLS/SPIFFE is additive").
- The probe spec is **declared by the control plane** in the start request; noded stays a mechanism.

---

## Part A: noded log stream consistency (PR 1)

Branch: `fix/noded-log-slog-and-guest-prefix`. This is a mechanical logging cleanup with no protocol change.

### Task A1: Route noded's own net/http errors through slog

**Files:**
- Modify: `projects/embervm/noded/cmd/main.go:236-240` (the health `http.Server`)

**Step 1: Set `ErrorLog` on the health server.** noded already builds an slog JSON logger at `main.go:43`. The health `http.Server` currently omits `ErrorLog`, so its errors fall back to the std `log` package (plain text, breaks the otherwise-uniform JSON stream). Add an slog-backed adapter:

```go
health := &http.Server{
	Addr:              cfg.HealthAddr,
	Handler:           healthHandler(),
	ReadHeaderTimeout: 5 * time.Second,
	ErrorLog:          slog.NewLogLogger(logger.Handler(), slog.LevelError),
}
```

**Step 2: Verify no other noded-side `net/http` server omits `ErrorLog`.** Grep the noded tree: `rg -n 'http.Server{' projects/embervm/noded`. As of writing only the health server exists; if a serving-side host `http.Server` is added later it must get the same `ErrorLog`. (The gRPC server routes through grpclog, out of scope here.)

**Step 3: Commit.**

```bash
git add projects/embervm/noded/cmd/main.go
git commit -m "fix(embervm): route noded health server errors through slog"
```

### Task A2: Tag guest stdout/stderr per workload and phase

**Files:**
- Create: `projects/embervm/noded/fcvm/driver/logtag.go` (a line-prefixing `io.Writer`)
- Create: `projects/embervm/noded/fcvm/driver/logtag_test.go`
- Modify: `projects/embervm/noded/fcvm/driver/launcher.go:91-92` (the `cmd.Stdout = os.Stdout` mux point)

**Design:** Wrap the guest's stdout/stderr in a writer that emits one structured slog record per line, carrying `source=guest`, `workload=<name>`, and `phase=<init|vm>`. This is the split the operator asked for (`workload_name - init-*` / `workload_name - vm-*`), but as slog fields rather than a text prefix so a parser splits on a field, not a regex. Guest bytes that are already JSON are carried as a `line` string value (do not attempt to re-parse; double-decoding guest output is a known hazard and out of scope).

**Step 1: Write the failing test** for the line-splitting writer (buffers partial lines, flushes on `\n`, tags each complete line). Table-drive: multi-line input, a trailing partial line, an empty line.

```go
// logtag_test.go
func TestLineTagWriter_TagsEachLine(t *testing.T) {
	var rec []map[string]any
	w := newTagWriter("scratch-postgres", "vm", func(m map[string]any) { rec = append(rec, m) })
	_, _ = w.Write([]byte("boot ok\npanic: x\n"))
	// expect two records, each with workload=scratch-postgres, phase=vm
	...
}
```

**Step 2:** Push the branch and let CI run the Go unit tests (no local `go test`; see CLAUDE.md "No local test loop"). Expected: FAIL (writer undefined).

**Step 3: Implement `newTagWriter`** in `logtag.go`: an `io.Writer` that accumulates until `\n`, then emits a slog record at the guest's line level (default INFO; best-effort detect `panic:`/`error`). Keep it allocation-light; it is on the guest console hot path.

**Step 4: Wire it at the launcher boundary.** The launcher has `vmID` but needs the workload name and phase threaded in. Replace:

```go
cmd.Stdout = os.Stdout
cmd.Stderr = os.Stderr
```

with the tag writer for the VM phase (`phase=vm`), and add the same wrapping at the guest-init exec path (`phase=init`) so the two phases are distinguishable. The workload name is available from the assign/start request; thread it through the `ExecLauncher` / cold-boot fields (see `driverExtras` in `noded/cmd/main.go:301`). If threading the name is more than a couple of hops, fall back to `vmID` and record it as `workload=<vmID>` with a TODO, rather than expanding scope.

**Step 5:** Push, watch CI green.

**Step 6: Commit.**

```bash
git add projects/embervm/noded/fcvm/driver/logtag.go projects/embervm/noded/fcvm/driver/logtag_test.go projects/embervm/noded/fcvm/driver/launcher.go
git commit -m "feat(embervm): tag guest stdout/stderr with workload and lifecycle phase"
```

### Task A3: Chart bump and PR

**Step 1:** Bump the embervm chart so the change deploys: `bazel/tools/git/bump-chart.sh projects/embervm` (updates `Chart.yaml` and `deploy/application.yaml` together).

**Step 2:** Push, open PR, let CI build the dual-arch image and run tests. Merge with `gh pr merge --rebase` on green.

**Verification after rollout:** tail the noded pod and confirm (a) noded's own lines are all JSON, and (b) guest lines now carry `workload`/`phase` fields. The TLS-handshake lines will still appear until Part B (they are the guest's own output); Part A only re-attributes and structures them.

---

## Part B: workload probe spec (PR 2)

Branch: `feat/embervm-workload-probe-spec`. This is a node-protocol contract change and gets its own review and CI cycle.

### Task B1: Add `ProbeSpec` to the node proto

**Files:**
- Modify: `projects/embervm/proto/embervm/node/v1/node.proto`

**Step 1: Define the message and enum.** Add near the serving/stateful request messages:

```proto
// ProbeSpec declares how the daemon health-probes this workload's endpoint.
// kind is validated against the workload class by the control plane before the
// request is sent (serving: HTTP/HTTPS; stateful/group: TCP/TLS).
message ProbeSpec {
  enum Kind {
    KIND_UNSPECIFIED = 0; // daemon applies the per-class default
    TCP = 1;   // bare TCP CONNECT success (L4 default)
    TLS = 2;   // complete a TLS handshake, then close (L4, TLS-terminating)
    HTTP = 3;  // GET {path} over plaintext (serving default)
    HTTPS = 4; // GET {path} over TLS
  }
  Kind kind = 1;
  string path = 2; // HTTP/HTTPS only; ignored for TCP/TLS
}
```

**Step 2: Add the field to the start requests.** Add `ProbeSpec probe = <next>;` to `StartServingRequest` (line 711), `StartStatefulRequest` (line 774), and the composite group start request (the message backing `newGroupTCPProber`). Update the health-probing doc comments at `node.proto:164` and `node.proto:230` to reference the spec and the per-class defaults. An unset `probe` (or `KIND_UNSPECIFIED`) means "daemon default per class," preserving current behavior for old callers.

**Step 3: Regenerate stubs and commit.** Run `bazel/tools/format/fast-format.sh` (regenerates proto stubs + BUILD). Commit:

```bash
git commit -am "feat(embervm): add ProbeSpec to node serving/stateful/group start requests"
```

### Task B2: Teach the probers to honor the kind

**Files:**
- Modify: `projects/embervm/noded/serving/probe.go` (HTTP prober: add HTTPS path)
- Modify: `projects/embervm/noded/serving/probe_tcp.go` (L4 prober: add TLS handshake path)
- Test: `projects/embervm/noded/serving/probe_test.go`, `probe_tcp.go`'s test
- Modify: `projects/embervm/noded/server/server.go` (the `newProber` / `newTCPProber` / `newGroupTCPProber` factories thread the kind through)

**Step 1: Write failing tests.** For the TCP prober: a `tls` probe against a test TLS listener completes the handshake and reports healthy AND the server logs no handshake error (assert against a captured `ErrorLog`). For the HTTP prober: an `https` probe against an `httptest.NewTLSServer` returns healthy. Keep the existing `tcp`/`http` cases green (defaults unchanged).

**Step 2:** Push, CI FAIL (kind not plumbed).

**Step 3: Implement.**
- `probe_tcp.go`: when kind is `tls`, replace the bare `net.Dial` with a `tls.Dial` using `&tls.Config{InsecureSkipVerify: true}` (leave a `// TODO: SPIFFE verify` marker), then close. A completed handshake + close is silent on the peer, which is the fix. When kind is `tcp` (default), behavior is unchanged.
- `probe.go`: when kind is `https`, give the probe HTTP client a `Transport` with a TLS config (`InsecureSkipVerify: true`, same TODO). When `http` (default), unchanged.
- `server.go`: the three factory closures take the resolved kind from the start request (defaulted per class) and pass it into `NewProber` / `NewTCPProber`.

**Step 4:** Push, CI green.

**Step 5: Commit.**

```bash
git commit -am "feat(embervm): probers honor ProbeSpec kind (tls handshake, https GET)"
```

### Task B3: Control-plane validation and wiring

**Files:**
- Modify: the control-plane modules that build `StartServing` / `StartStateful` / group-start requests under `projects/embervm/control/lib/embervm` (grep for the request builders; the serving/stateful/group managers)

**Step 1: Validate kind against class.** Before sending, the CP maps the workload's declared probe (from its spec) to a `ProbeSpec.kind` and rejects a class-inappropriate kind (e.g. `http` on a stateful workload) with a clear error, rather than letting noded receive a nonsensical kind. Unset stays unset (daemon default).

**Step 2: Surface the knob in the workload spec.** Wherever a workload class is declared, add an optional probe declaration that the CP translates to the proto enum. Default absent = per-class default.

**Step 3: Add a CP test** for the validation matrix (serving accepts http/https + rejects tcp/tls; stateful/group accept tcp/tls + reject http/https).

**Step 4: Commit.**

```bash
git commit -am "feat(embervm): control plane validates and wires workload probe kind"
```

### Task B4: Chart bump, PR, and endpoint migration note

**Step 1:** Bump the embervm chart: `bazel/tools/git/bump-chart.sh projects/embervm`.

**Step 2:** Push, open PR, CI green, `gh pr merge --rebase`.

**Step 3: Post-merge, migrate the noisy endpoints.** Identify which live workloads terminate TLS (the `scratch-postgres` stateful VM from the original log, and any k3s-runtime serving/group VM whose apiserver on 6443 is TLS) and set their probe kind to `tls` / `https` in their workload spec. Confirm the `http: TLS handshake error` lines disappear from the noded stream after rollout. Leave plaintext workloads on the default; do not blanket-flip to tls.

---

## What this plan deliberately does NOT do

- Does not change `DefaultProbeInterval` (5s) or `DefaultUnhealthyThreshold` (3). The cadence is fine; the mismatch was the problem.
- Does not enforce TLS globally or add cert verification now (InsecureSkipVerify with a SPIFFE TODO).
- Does not re-parse or re-encode guest JSON output; guest lines are carried as opaque strings with source/workload/phase tags.
- Does not touch the gRPC server's grpclog path.
