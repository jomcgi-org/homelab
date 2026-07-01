# fc-invoke Implementation Plan (PRs 1-2: daemon + semgrep cutover)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build `fc-invoke`, a single configurable host daemon that runs HTTP workloads in Firecracker microVMs (ADR 030), and cut the existing semgrep scanner over to it as the first, lowest-risk consumer, proving the HTTP-over-vsock contract in prod before the agent migration.

**Architecture:** One host daemon exposes `POST /invoke/{workload}` over HTTP, restores a warm-base microVM via the existing `fcvm/driver`, and reverse-proxies the request to an HTTP server inside the guest over vsock (reusing the Firecracker `CONNECT <port>` handshake the current scan transport already uses). A shared guest-side Go "shim" library provides the HTTP-over-vsock server, a pre/post hook chain, and workload-agnostic capabilities (git, object-store). Workloads are named Helm-values entries; the daemon holds no durable state.

**Tech Stack:** Go (stdlib `net/http`, `httptest`, `golang.org/x/sync/semaphore`), Firecracker via `fcvm/driver`, apko images, Bazel/gazelle BUILD, Helm chart + ArgoCD, deployed on node-4.

---

## Repo realities that shape every task

- **No local test loop.** Mac runners are not in the BuildBuddy `workflows` pool and the linux fallback is too slow. Each task writes the test and the implementation, commits, and **defers test execution to end-of-plan CI on the pushed branch** (`gh pr checks <n> --watch`, read failures via `mcp__buildbuddy__*`). The "Expected: PASS" notes describe what CI must show, not a local run. You may run the vendored `format` locally (it is fast and standalone) and eyeball `go` compile via your editor, nothing more.
- **Everything new lives under `projects/firecracker/` from the start.** Task 0 moves the shared packages (`fcvm`, `vsockproto`, `egress-proxy`, `substrate`) from `projects/agent_platform/` to `projects/firecracker/substrate/` and rewrites the two existing consumers' imports, so all new code has a clean home and clean import paths. **Path mapping used by every task below:** `projects/agent_platform/<pkg>` is now `projects/firecracker/substrate/<pkg>`; the daemon is `projects/firecracker/substrate/invoke/`; the shim is `projects/firecracker/substrate/shim/`; the semgrep guest is `projects/firecracker/semgrep/`. Where a task's `Files:` list still shows an `agent_platform/` path, read it through this mapping (the move lands them under `firecracker/` first).
- **Parallel execution waves (the dependency DAG).** Task 0 is serial and first (it rebases imports). Then: **wave 1 (parallel)** = shim server+hooks (Task 1-2), shim capabilities (Task 3), fc-invoke config (Task 4), which share no symbols; **wave 2 (parallel)** = vsockhttp transport (Task 5, needs the shim from wave 1), invoker (Task 6, needs substrate + config + the transport interface); **wave 3** = HTTP ingress (Task 7); **wave 4** = main wiring + apko (Task 8). Dispatch wave-1 implementers with worktree isolation so their parallel `format`/gazelle regens do not collide, then land them in sequence.
- **Conventional Commits enforced** by a `commit-msg` hook. **Never commit to main**; this plan runs in the `feat/fc-invoke` worktree.
- **`format` regenerates BUILD files** (gazelle) and the home-cluster kustomization. Run it before every commit that adds Go files or deploy manifests.
- **No em-dashes** in any file you write.

---

## PR 1: the fc-invoke daemon and the shared shim

The daemon and shim are built and unit-tested with fakes; no consumer is cut over yet. PR 1 is independently mergeable: it adds code, wires an apko image, and ships nothing into the request path.

### Task 0: establish the `projects/firecracker/` home (serial, first)

Move the shared packages so all new code has a clean home and imports. Pure relocation plus import rewrite; no logic change.

**Files:**

- Move: `projects/agent_platform/{fcvm,vsockproto,egress-proxy,substrate}` -> `projects/firecracker/substrate/{fcvm,vsockproto,egress-proxy,substrate}`
- Modify: every import of those packages in `projects/agent_platform/fc-agentd/**` and `projects/agent_platform/semgrep-scand/**` (and any other referencer found by grep)
- Add: `GuestHTTPPort uint32 = 1027` to `projects/firecracker/substrate/vsockproto/proto.go` (the inbound HTTP port the shim binds; alongside `ControlPort`/`ScanPort`)

**Step 1.** `git mv` the four package dirs. `grep -rl "agent_platform/\(fcvm\|vsockproto\|egress-proxy\|substrate\)"` to find every importer; rewrite the import paths to `firecracker/substrate/...`.

**Step 2.** Run `format` (gazelle regenerates all affected BUILD files and the home-cluster root). Eyeball that `fc-agentd` and `semgrep-scand` BUILD `deps` now point at the new paths.

**Step 3.** Add the `GuestHTTPPort` constant.

**Step 4.** Commit. CI verifies the whole module still builds with the moved packages (this is the gate; do not assert a local build).

```bash
git mv projects/agent_platform/fcvm projects/firecracker/substrate/fcvm   # repeat per pkg
# rewrite imports, then:
format
git add -A projects/agent_platform projects/firecracker projects/home-cluster
git commit -m "refactor(firecracker): move shared substrate packages to projects/firecracker/substrate"
```

**This task must merge (or at least be committed and CI-green) before wave 1 starts**, because every subsequent import path assumes the new home.

### Task 1: shim HTTP-over-vsock server + handler interface (guest side)

The guest-side half of the transport: a tiny HTTP server bound to a vsock port, dispatching to a workload `Handler`. Built with stdlib so it is testable over a plain `net.Listener` (a TCP/UDS listener in tests, a vsock listener in prod).

**Files:**

- Create: `projects/agent_platform/shim/server.go`
- Create: `projects/agent_platform/shim/server_test.go`
- Create: `projects/agent_platform/shim/BUILD` (gazelle will fill it; create empty then run `format`)

**Step 1: Write the failing test.** A `Handler` is any `func(ctx, *Request) (*Response, error)`; the server serves it over an arbitrary `net.Listener`, exposes `/shim/healthz` and `/shim/ready`, routes `/invoke` (and subpaths) to the handler, and 404s elsewhere.

```go
package shim

import (
	"context"
	"io"
	"net"
	"net/http"
	"testing"
)

func TestServerRoutesInvokeToHandler(t *testing.T) {
	h := func(_ context.Context, r *Request) (*Response, error) {
		body, _ := io.ReadAll(r.Body)
		return &Response{Status: 200, Body: []byte("handled:" + string(body))}, nil
	}
	ln, _ := net.Listen("tcp", "127.0.0.1:0")
	srv := NewServer(h)
	go srv.Serve(ln) //nolint
	defer srv.Close()

	resp, err := http.Post("http://"+ln.Addr().String()+"/invoke", "application/json", io.NopCloser(strNewReader("hello")))
	if err != nil {
		t.Fatalf("post: %v", err)
	}
	defer resp.Body.Close()
	got, _ := io.ReadAll(resp.Body)
	if string(got) != "handled:hello" {
		t.Fatalf("body = %q, want handled:hello", got)
	}
}

func TestServerReadyAndHealthz(t *testing.T) {
	srv := NewServer(func(context.Context, *Request) (*Response, error) { return &Response{Status: 200}, nil })
	for _, p := range []string{"/shim/healthz", "/shim/ready"} {
		req := httptestNewRequest(http.MethodGet, p)
		rec := httptestNewRecorder()
		srv.mux().ServeHTTP(rec, req)
		if rec.Code != 200 {
			t.Errorf("%s = %d, want 200", p, rec.Code)
		}
	}
}
```

(Use real `net/http/httptest` and `strings.NewReader`; the `strNewReader`/`httptest*` aliases above are shorthand for the plan, write the real calls.)

**Step 2: Run test to verify it fails.** CI (or local compile) fails: `NewServer`, `Request`, `Response` undefined.

**Step 3: Write minimal implementation.**

```go
// Package shim is the guest-side half of the fc-invoke substrate: an
// HTTP-over-vsock server that dispatches /invoke to a workload Handler, with a
// common /shim/* control surface. It is baked into guest images by Bazel; the
// fc-invoke daemon never imports it (ADR 030 decision 5).
package shim

import (
	"context"
	"io"
	"net"
	"net/http"
)

// Request is one inbound invocation. Body is the opaque payload the orchestrator
// POSTed to fc-invoke; Path carries any /invoke subpath for workload routing.
type Request struct {
	Path string
	Body io.Reader
}

// Response is the handler's reply, written back verbatim to the caller.
type Response struct {
	Status int
	Body   []byte
}

// Handler runs one invocation. The workload (semgrep, goose) implements it.
type Handler func(ctx context.Context, r *Request) (*Response, error)

// Server serves a Handler over any net.Listener (a vsock listener in prod, a
// TCP/UDS listener in tests).
type Server struct {
	h   Handler
	srv *http.Server
}

// NewServer builds a Server for the given Handler.
func NewServer(h Handler) *Server {
	s := &Server{h: h}
	s.srv = &http.Server{Handler: s.mux()}
	return s
}

func (s *Server) mux() *http.ServeMux {
	m := http.NewServeMux()
	m.HandleFunc("/shim/healthz", func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(200) })
	m.HandleFunc("/shim/ready", func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(200) })
	m.HandleFunc("/invoke", s.handle)
	m.HandleFunc("/invoke/", s.handle)
	return m
}

func (s *Server) handle(w http.ResponseWriter, r *http.Request) {
	resp, err := s.h(r.Context(), &Request{Path: r.URL.Path, Body: r.Body})
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadGateway)
		return
	}
	w.WriteHeader(resp.Status)
	_, _ = w.Write(resp.Body)
}

// Serve serves until the listener closes.
func (s *Server) Serve(ln net.Listener) error { return s.srv.Serve(ln) }

// Close stops the server.
func (s *Server) Close() error { return s.srv.Close() }
```

**Step 4: Run test to verify it passes.** CI shows the `shim` package tests PASS.

**Step 5: Commit.**

```bash
format
git add projects/agent_platform/shim/
git commit -m "feat(shim): HTTP-over-vsock server and handler interface"
```

### Task 2: shim hook chain (pre/post around the handler)

Hooks let a guest hydrate before and persist after the handler runs, without the handler knowing the order. Hooks are ordered; a pre-hook error aborts before the handler; post-hooks always run (to allow best-effort persist) and their errors are joined.

**Files:**

- Create: `projects/agent_platform/shim/hooks.go`
- Create: `projects/agent_platform/shim/hooks_test.go`
- Modify: `projects/agent_platform/shim/server.go` (wrap the handler with the hook chain)

**Step 1: Write the failing test.** A `Chain` runs pre-hooks in order, then the handler, then post-hooks in order; a failing pre-hook skips the handler; a failing post-hook does not skip the others.

```go
func TestChainRunsPreHandlerPost(t *testing.T) {
	var order []string
	pre := func(context.Context, *Request) error { order = append(order, "pre"); return nil }
	post := func(context.Context, *Request, *Response) error { order = append(order, "post"); return nil }
	h := func(context.Context, *Request) (*Response, error) { order = append(order, "h"); return &Response{Status: 200}, nil }
	c := Chain{Pre: []PreHook{pre}, Post: []PostHook{post}}
	if _, err := c.Run(context.Background(), &Request{}, h); err != nil {
		t.Fatal(err)
	}
	if strings.Join(order, ",") != "pre,h,post" {
		t.Fatalf("order = %v", order)
	}
}

func TestChainPreHookErrorSkipsHandler(t *testing.T) {
	ran := false
	c := Chain{Pre: []PreHook{func(context.Context, *Request) error { return errors.New("boom") }}}
	_, err := c.Run(context.Background(), &Request{}, func(context.Context, *Request) (*Response, error) { ran = true; return nil, nil })
	if err == nil || ran {
		t.Fatalf("want pre-hook error and handler skipped; err=%v ran=%v", err, ran)
	}
}
```

**Step 3: Implementation.** `PreHook func(ctx, *Request) error`, `PostHook func(ctx, *Request, *Response) error`, `Chain{Pre, Post}` with a `Run` that enforces the above; `errors.Join` the post-hook errors. Then change `NewServer` to accept an optional `Chain` (e.g. `NewServer(h Handler, opts ...Option)` with `WithChain(Chain)`), and `handle` calls `chain.Run(ctx, req, h)`.

**Step 5: Commit.** `feat(shim): pre/post hook chain around the workload handler`

### Task 3: shim capabilities (object-store + git), with fakes

Reusable, workload-agnostic building blocks a guest handler composes into hooks. Keep them thin: an interface plus a real implementation; the goose-specific composition lives in the agent-guest handler later, not here.

**Files:**

- Create: `projects/agent_platform/shim/capabilities/objectstore.go`
- Create: `projects/agent_platform/shim/capabilities/objectstore_test.go`
- Create: `projects/agent_platform/shim/capabilities/git.go`
- Create: `projects/agent_platform/shim/capabilities/git_test.go`

**Step 1: Write the failing tests.** Define narrow interfaces so the hook code is testable with fakes (no real S3/git):

```go
// ObjectStore pulls/pushes a blob by key. The real impl wraps the cluster S3
// (SeaweedFS) the repo already uses; tests inject a map-backed fake.
type ObjectStore interface {
	Pull(ctx context.Context, key string) ([]byte, error)
	Push(ctx context.Context, key string, data []byte) error
}

// Git clones/pulls a mirror at a ref and pushes a branch. Real impl shells the
// vendored git; tests inject a fake recording calls.
type Git interface {
	Clone(ctx context.Context, mirror, ref, dest string) error
	Push(ctx context.Context, dest, branch string) error
}
```

Test the interfaces against a fake (round-trip a blob; record a clone). The real implementations are thin and tested in CI integration on node-4, not here, so PR 1 ships the interfaces + fakes + a minimal real impl that compiles.

**Step 3: Implementation.** Define the interfaces, a `MapObjectStore` test fake (or put it in `_test.go`), and a minimal real S3 impl behind the same interface using the repo's existing S3 access pattern (see `projects/monolith/chat/` SeaweedFS usage and `project_chat_blobs_seaweedfs` for the boto-equivalent in Go). Git real impl shells `git` with `os/exec`. Keep real impls behind the interface so the daemon/handler tests never touch them.

**Step 5: Commit.** `feat(shim): object-store and git capabilities with fakes`

### Task 4: fc-invoke workload config (registry from env/values)

The daemon reads its workload table from configuration. Mirror semgrep-scand's env-driven `config.Load()` style, but the workloads come from a JSON/YAML blob (the Helm values rendered into one env var or a mounted file).

**Files:**

- Create: `projects/agent_platform/fc-invoke/internal/config/config.go`
- Create: `projects/agent_platform/fc-invoke/internal/config/config_test.go`

**Step 1: Write the failing test.** `Load` parses a `Workload` table (the seven knobs from ADR 030) plus daemon-level settings (listen addr, node, arch, snapshot root, kernel/rootfs paths per workload image). Assert defaults and overrides, exactly like `semgrep-scand/internal/config` does.

```go
type Workload struct {
	Image          string        // logical name; resolved to a rootfs path + base key
	VCPUs          int
	MemMib         int
	Concurrency    int
	EgressEnabled  bool
	EgressSecrets  []string
	WarmBase       bool
	ReadyPath      string        // default /shim/ready
	Sessioned      bool
	RequestTimeout time.Duration
}
```

**Step 3: Implementation.** Parse a `FC_INVOKE_WORKLOADS` JSON env (or a file path in `FC_INVOKE_WORKLOADS_FILE`) into `map[string]Workload`, apply per-field defaults (ReadyPath `/shim/ready`, RequestTimeout 90s, Concurrency 4), and daemon globals from `FC_INVOKE_*` env (reuse the parsing helpers' shape from `semgrep-scand/internal/config`).

**Step 5: Commit.** `feat(fc-invoke): workload config registry`

### Task 5: host-side HTTP-over-vsock transport

Generalize `semgrep-scand`'s `vsockTransport.Scan` (which does the Firecracker `CONNECT <port>` handshake on the base UDS then speaks newline-JSON) into an HTTP round-trip: same handshake, then write an `http.Request` and read an `http.Response` on the stream.

**Files:**

- Create: `projects/agent_platform/fc-invoke/internal/vsockhttp/transport.go`
- Create: `projects/agent_platform/fc-invoke/internal/vsockhttp/transport_test.go`

**Step 1: Write the failing test.** Over a plain in-process `net.Pipe`/UDS (no Firecracker), a `RoundTrip(ctx, udsPath, *http.Request)` writes the request and reads the response. Stand up a real `shim.Server` on a UDS listener in the test and assert the transport gets the handler's body back. This is the integration seam between Task 1 and Task 5 with no VM.

```go
func TestRoundTripOverUDS(t *testing.T) {
	dir := t.TempDir()
	uds := filepath.Join(dir, "sock")
	ln, _ := net.Listen("unix", uds)
	srv := shim.NewServer(func(_ context.Context, r *shim.Request) (*shim.Response, error) {
		b, _ := io.ReadAll(r.Body)
		return &shim.Response{Status: 200, Body: []byte("ok:" + string(b))}, nil
	})
	go srv.Serve(ln) //nolint
	defer srv.Close()

	tr := NewTransport(WithDirectDial()) // test mode: skip the CONNECT handshake, dial uds directly
	req, _ := http.NewRequest("POST", "http://vsock/invoke", strings.NewReader("hi"))
	resp, err := tr.RoundTrip(context.Background(), uds, req)
	// assert resp body == "ok:hi"
}
```

**Step 3: Implementation.** Build an `http.Client` whose `Transport.DialContext` (a) dials the base UDS, (b) performs Firecracker's `CONNECT <GuestHTTPPort>\n` / `OK` handshake (lift this verbatim from `semgrep-scand/internal/scanner/transport.go`'s `Scan`), then (c) returns the connection for `net/http` to speak HTTP over. Add a test-only `WithDirectDial` that skips the handshake (dials the UDS as a raw HTTP server) so Task 1's server can back the test. Add a `GuestHTTPPort` constant to `vsockproto` (e.g. `1027`, alongside `ControlPort`/`ScanPort`).

**Step 5: Commit.** `feat(fc-invoke): host-side HTTP-over-vsock transport`

### Task 6: invoker core (concurrency + warm-base restore + proxy)

The heart: claim a slot, restore a warm-base VM via the driver, round-trip the request to the guest, release the VM. Reuse the proven `vmDriver`/`guestTransport` fake pattern from `semgrep-scand/internal/scanner` so this is fully unit-tested with no Firecracker.

**Files:**

- Create: `projects/agent_platform/fc-invoke/internal/invoker/invoker.go`
- Create: `projects/agent_platform/fc-invoke/internal/invoker/invoker_test.go`

**Step 1: Write the failing tests.** With a fake driver (records Claim/Restore/Release) and a fake transport (returns a canned response):

- a happy-path invoke restores from the workload's warm base, round-trips, returns the body, and **releases the VM even on a transport error** (defer-release);
- at `Concurrency` live VMs, the next invoke blocks then proceeds when one frees (semaphore), or returns a `GuestUnavailable`-style 503 marker on ctx timeout;
- a restore failure falls back to a cold boot (`BaseSnapshotRef` empty), mirroring `scanner.go`.

Model the interfaces on `scanner.go`'s `vmDriver` and `guestTransport`:

```go
type vmDriver interface {
	Claim(ctx context.Context, spec substrate.ClaimSpec) (substrate.Handle, error)
	Release(ctx context.Context, h substrate.Handle) error
	RemoveBundle(threadID string) error
	VsockUDSPath(threadID string) string
}
type transport interface {
	WaitReady(ctx context.Context, udsPath string) error
	RoundTrip(ctx context.Context, udsPath string, req *http.Request) (*http.Response, error)
}
```

**Step 3: Implementation.** Port `scanner.Scan`'s structure: acquire `semaphore.Weighted` (per-workload, sized at `Workload.Concurrency`), `Claim` with `BaseSnapshotRef` from the workload's warm base (reuse `SnapshotBase`/base-key plumbing or call into the existing driver base APIs), `WaitReady`, `RoundTrip`, then `defer Release + RemoveBundle`. Surface the same `GuestUnavailable() bool` error contract `scanner` defines so the HTTP layer can map 503 vs handler error.

**Step 5: Commit.** `feat(fc-invoke): invoker core with concurrency and warm-base restore`

### Task 7: HTTP ingress (`/invoke/{workload}`, `/healthz`)

The daemon's public surface. Routes `/invoke/{workload}[/{session}]` to the invoker for the named workload, maps errors per ADR 030's table (503 `GuestUnavailable` vs proxied handler error), and serves `/healthz`. Mirror `semgrep-scand/internal/server` (it already encodes the 503-vs-200 status policy).

**Files:**

- Create: `projects/agent_platform/fc-invoke/internal/server/server.go`
- Create: `projects/agent_platform/fc-invoke/internal/server/server_test.go`

**Step 1: Write the failing tests.** With a fake invoker:

- `POST /invoke/semgrep` with a body returns the invoker's response body and status;
- an unknown workload returns 404;
- a `GuestUnavailable` error returns 503; any other invoker error returns 502;
- `GET /healthz` returns 200;
- the request body is capped (reuse `server.WithMaxBytes` from semgrep-scand).

**Step 3: Implementation.** A `Handler` over an `Invoker` interface (`Invoke(ctx, workload, session string, body io.Reader) (*http.Response, error)`), parsing `{workload}` and optional `{session}` from the path, enforcing `requestTimeout` via `context.WithTimeout`, and the status mapping. Lift the body-cap and error-classification shape from `semgrep-scand/internal/server/server.go`.

**Step 5: Commit.** `feat(fc-invoke): HTTP ingress and status mapping`

### Task 8: daemon main + apko image + BUILD

Wire config -> driver -> invoker(s) -> server, build the dual-arch apko image, and register Bazel targets. No deploy yet (PR 2 ships the chart).

**Files:**

- Create: `projects/agent_platform/fc-invoke/cmd/main.go`
- Create: `projects/agent_platform/fc-invoke/apko.yaml`, `apko.lock.json`
- Create: `projects/agent_platform/fc-invoke/image/BUILD` (image + push targets)
- Create/modify: `projects/agent_platform/fc-invoke/BUILD` (via `format`)

**Step 1 (wiring).** Model `cmd/main.go` on `semgrep-scand/cmd/main.go`: load config, call `driver.ExecMountTrampoline()` first (the re-exec trampoline for per-instance vsock mount namespaces), build one `driver.New(...)` per distinct guest image (or a driver keyed by workload), build an `invoker` per workload sized at its concurrency, build the warm bases in the background (`go scn.BuildBase`), and serve the ingress with a `ReadHeaderTimeout`.

**Step 2 (image).** Copy `semgrep-scand/apko.yaml` as the template (non-root uid 65532, dual-arch x86_64+aarch64). The daemon binary needs the same Firecracker access the existing daemons have; match `fc-agentd`/`semgrep-scand` apko packages and the `dmsetup` base if CoW is reused (it is not in PR 1; plain copy provisioner is fine).

**Step 3.** Run `format` to generate BUILD files and gazelle the Go targets. Add the `apko_image` + `oci_push` targets by copying `semgrep-scand/image/BUILD`.

**Step 5: Commit.** `feat(fc-invoke): daemon entrypoint and apko image`

**End of PR 1.** Push the branch, open the PR, watch CI (`gh pr checks <n> --watch`). All new packages are unit-tested with fakes; nothing is in the request path yet. Get one end-of-PR Opus code review (per CLAUDE.md cadence) before merge.

---

## PR 2: cut semgrep over to fc-invoke

Turn `semgrep-guest` into an HTTP workload, register it in fc-invoke, repoint the MCP tool, and retire the `semgrep-scand` daemon (keeping the guest). The MCP tool name and contract do not change, so this is invisible to callers.

### Task 9: semgrep-guest HTTP handler

Wrap the existing warm-LSP scan as a `shim.Handler`: decode the `{files:[...]}` body, run the in-guest semgrep scan (the logic currently behind the ScanPort RPC), encode findings. Reuse the existing scan internals in `semgrep-guest-init/internal/lspdriver`.

**Files:**

- Create: `projects/agent_platform/semgrep-guest-init/internal/handler/handler.go`
- Create: `projects/agent_platform/semgrep-guest-init/internal/handler/handler_test.go`

**Step 1: Write the failing test.** Given a fake lsp driver returning findings, the handler decodes a `{files}` JSON body and returns the same `{findings, errors}` JSON the daemon returns today (so the MCP response shape is byte-identical). Assert the JSON contract matches `vsockproto.ScanResult`'s JSON.

**Step 3: Implementation.** A `shim.Handler` that JSON-decodes `ScanRequest`, calls the existing `lspdriver` scan, JSON-encodes `ScanResult`. The wire types stay `vsockproto.ScanRequest/ScanResult` (reuse them as the HTTP body schema, so nothing about the finding shape changes).

**Step 5: Commit.** `feat(semgrep): HTTP handler over the warm LSP scan`

### Task 10: semgrep-guest-init runs the shim server

Switch the guest's PID-1 from "listen on ScanPort for RPC" to "serve the shim HTTP server on the guest HTTP vsock port, and signal readiness via `/shim/ready` once the LSP is warm." The warm-base readiness probe fc-invoke waits on becomes an HTTP GET, not the `KindHello` control message.

**Files:**

- Modify: `projects/agent_platform/semgrep-guest-init/cmd/main.go`
- Modify: `projects/agent_platform/semgrep-guest-init/internal/lspdriver/*` (expose a "warm and ready" signal to gate `/shim/ready`)
- Test: `projects/agent_platform/semgrep-guest-init/cmd/main_test.go` (or a small server-wiring test)

**Step 1: Write the failing test.** Boot the shim server with the semgrep handler over a UDS in-process, drive `/shim/ready` returning 503 until a "warm" flag flips, then 200. Assert fc-invoke's `WaitReady` (HTTP readiness) would succeed only after warm.

**Step 3: Implementation.** Replace the ScanPort listen loop with `shim.NewServer(handler.New(lsp), shim.WithChain(...))` bound to a vsock listener on `vsockproto.GuestHTTPPort`. Keep the LSP warm-up; gate `/shim/ready` on it (a custom ready func or a 503-until-warm middleware). Bring `lo` up at PID-1 (the raw-FC requirement noted in the egress ADRs) if not already.

**Step 5: Commit.** `feat(semgrep): serve the shim HTTP server from guest PID-1`

### Task 11: semgrep workload config + fc-invoke chart and deploy

Register the `semgrep` workload and ship the daemon to the cluster.

**Files:**

- Create: `projects/agent_platform/fc-invoke/chart/` (Chart.yaml, templates: Deployment with node-4 affinity + `/dev/kvm`, RBAC if any, the rendered workloads ConfigMap/env)
- Create: `projects/agent_platform/fc-invoke/deploy/{application.yaml,values.yaml,kustomization.yaml}` (copy `semgrep-scand/deploy/` as the template; same node-4 placement, devmapper, OnePasswordItems for any egress secrets, semgrep has none)
- Modify: home-cluster root via `format`

**Step 1.** Copy `semgrep-scand/chart` and `semgrep-scand/deploy` as the starting point (same Firecracker host requirements). Replace the daemon image with `fc-invoke`, and put the `workloads:` map (the `semgrep` entry: `image: semgrep-guest`, `egress.enabled: false`, `warmBase.build: true`, `concurrency: 4`, `requestTimeout: 90s`, `sessioned: false`) into `values.yaml`. Use `helm_images_values` to pin BOTH the `fc-invoke` daemon image and the `semgrep-guest` image digests at chart-build.

**Step 2.** `helm template` locally to verify the daemon Deployment and the workloads config render (NEVER `helm install`).

**Step 5: Commit.** `feat(fc-invoke): chart, deploy, and the semgrep workload`

### Task 12: repoint the monolith MCP tool

Point `semgrep_scan` at fc-invoke. The tool, its args, and its response shape do not change.

**Files:**

- Modify: `projects/monolith/semgrep/mcp.py` (`SEMGREP_SCAND_URL` -> `FC_INVOKE_URL` + `/invoke/semgrep`)
- Modify: `projects/monolith/semgrep/mcp_test.py` (update the URL/path expectation)
- Modify: monolith `values.yaml` (set `FC_INVOKE_URL` env from the fc-invoke service DNS name; never hardcode the `.svc.cluster.local` in Go/Py defaults, inject from values)
- Modify: monolith `Chart.yaml` + `deploy/application.yaml` `targetRevision` (bump together; chart-version-bot normally does this, keep in sync if manual)

**Step 1: Write the failing test.** Update `mcp_test.py` to assert the POST goes to `{FC_INVOKE_URL}/invoke/semgrep` with the `{files}` body, and the parsed response is unchanged.

**Step 3: Implementation.** Swap the env var name and URL path; keep the timeout and error handling. Grep the test tree for the old `SEMGREP_SCAND_URL` and update every assertion in the same commit (CLAUDE.md: bump-config-and-tests-together).

**Step 5: Commit.** `feat(semgrep): point the MCP scan tool at fc-invoke`

### Task 13: retire semgrep-scand (keep the guest)

Remove the now-dead daemon; the `semgrep-guest` image lives on as the workload.

**Files:**

- Delete: `projects/agent_platform/semgrep-scand/` (the daemon: cmd, internal/{scanner,server,config}, chart, deploy, image)
- Keep: `projects/agent_platform/semgrep-guest`, `semgrep-guest-init` (now serving the shim)
- Modify: remove `semgrep-scand` from the home-cluster root and any ArgoCD app list via `format`
- Modify: `projects/agent_platform/vsockproto`: leave `ScanRequest/ScanResult` (now the HTTP body schema) but the `ScanPort` constant can be removed if nothing references it (grep first)

**Step 1.** Grep for every reference to `semgrep-scand` and `ScanPort` across the repo; confirm only the deleted daemon used them. **Do this only after Task 12 is merged and verified live**, so there is no window where the MCP tool points at a deleted service.

**Step 5: Commit.** `chore(semgrep): retire semgrep-scand daemon, fc-invoke owns scanning`

### Task 14: end-to-end verification

**Steps:**

1. After PR 2 merges, watch the rollout: `kubectl get applications -n argocd | grep -E 'fc-invoke|monolith'` and the fc-invoke pod on node-4 (`kubectl get pods -n <ns> -o wide`).
2. Confirm the warm base builds: fc-invoke logs show a successful base snapshot for `semgrep`.
3. Drive a real scan through the MCP tool (or `curl` the in-cluster fc-invoke `/invoke/semgrep` via a port-forward) with a known-bad file; confirm findings come back and match what semgrep-scand returned pre-cutover.
4. Confirm restore latency in logs is in the tens-of-ms range (warm base working), and that a forced base-miss falls back to cold boot without error.
5. Post a one-line `monolith-agent-notify` summary if the cutover is clean.

**End of PR 2.** One end-of-PR Opus code review against the full diff before merge.

---

## Out of scope (later PRs, not this plan)

- PR 3-4: session routing + the agent (goose) workload cutover, thinning `fc-agentd` to the orchestrator role.
- PR 5: the `projects/firecracker/{substrate,goosecracker,semgrep}` rename (one atomic mechanical move + import rewrite + `format`).
- CoW rootfs provisioner and event-driven dispatch (ADR 026 decision 1) as fc-invoke internals.
- The CI webhook consumer on `/invoke`.

## Key references

- Design: `docs/plans/2026-06-30-fc-invoke-design.md`; ADR: `docs/decisions/agents/030-fc-invoke-configurable-firecracker-surface.md`
- Port-from: `projects/agent_platform/semgrep-scand/{cmd,internal/{scanner,server,config}}` (the orchestration shape this generalizes), `internal/scanner/transport.go` (the CONNECT handshake to lift)
- Driver: `projects/agent_platform/fcvm/driver/driver.go` (Claim/Restore/Release/SnapshotBase), `substrate/substrate.go` (the interface + fake)
- Wire types: `projects/agent_platform/vsockproto/proto.go`
