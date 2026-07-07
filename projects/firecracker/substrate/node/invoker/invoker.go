// Package invoker orchestrates single-use Firecracker microVM invocations for
// one named workload. At startup the daemon can optionally build a warm base
// snapshot; each Invoke then restores a fresh microVM from that base, sends an
// HTTP POST to the guest shim server, and releases the VM. When no warm base
// is available (not yet built or a restore failed) Invoke falls back to a cold
// boot, so the base is a latency optimisation, never load-bearing.
//
// vmDriver and transport are narrow interfaces so the orchestration is
// unit-testable with fakes: no Firecracker, no real vsock, no real HTTP.
package invoker

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"sync"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"
	"golang.org/x/sync/semaphore"

	"github.com/jomcgi/homelab/projects/firecracker/substrate/node/egress"
	"github.com/jomcgi/homelab/projects/firecracker/substrate/substrate"
)

// tracer spans the invocation lifecycle points (slot acquire, warm restore,
// guest readiness, guest exec). Spans nest under the root fc_invoke span via the
// ctx threaded down from the ingress handler.
var tracer = otel.Tracer("fc-invoke/invoker")

// vmDriver is the subset of fcvm/driver the invoker needs. The real
// *driver.Driver satisfies it; tests inject a fake.
type vmDriver interface {
	// Claim boots a microVM. With spec.BaseSnapshotRef set it restores from
	// the warm base for an instant ready start; otherwise it cold-boots from
	// the kernel and rootfs.
	Claim(ctx context.Context, spec substrate.ClaimSpec) (substrate.Handle, error)
	// SnapshotBase captures a warmed microVM into a shared base bundle keyed
	// by baseKey; new microVMs restore from it for an instant start.
	SnapshotBase(ctx context.Context, h substrate.Handle, baseKey string) (substrate.SnapshotRef, error)
	// Release kills the microVM process and discards it. An invoker guest is
	// single-use per request.
	Release(ctx context.Context, h substrate.Handle) error
	// RemoveBundle deletes a thread's on-disk bundle directory (its vsock
	// sockets and API socket). Called after Release so per-request bundle
	// directories do not accumulate on the NVMe disk.
	RemoveBundle(threadID string) error
	// VsockUDSPath returns the host unix-domain socket backing a thread's
	// vsock device; the transport addresses the guest shim HTTP port relative
	// to it.
	VsockUDSPath(threadID string) string
}

// transport is the host-side HTTP-over-vsock client. The real
// *vsockhttp.Transport satisfies it; tests inject a fake.
type transport interface {
	// WaitReady polls readyPath on the guest shim until it responds 200 or
	// ctx fires. It is bounded by BootReadyTimeout in the caller.
	WaitReady(ctx context.Context, udsPath, readyPath string) error
	// RoundTrip sends req to the guest shim reachable via udsPath and
	// returns its HTTP response. The caller is responsible for closing the
	// response body.
	RoundTrip(ctx context.Context, udsPath string, req *http.Request) (*http.Response, error)
}

// GuestUnavailableError marks a failure to boot or restore a guest, or a
// readiness timeout, as opposed to an HTTP error produced by a guest that did
// run. The HTTP layer maps it to 503 via GuestUnavailable(); the wrapped
// cause stays inspectable through Unwrap.
type GuestUnavailableError struct{ Err error }

// Error implements the error interface.
func (e *GuestUnavailableError) Error() string { return "guest unavailable: " + e.Err.Error() }

// Unwrap returns the wrapped cause unmodified, as the errors.Unwrap contract
// requires; it must not re-wrap, so the bare return is intentional.
func (e *GuestUnavailableError) Unwrap() error { return e.Err } // nosemgrep: no-bare-error-return

// GuestUnavailable returns true so HTTP layers can map this error to 503 and
// distinguish it from a 502 (guest ran but the HTTP round-trip failed).
func (e *GuestUnavailableError) GuestUnavailable() bool { return true }

// Config carries the per-workload knobs for an Invoker.
type Config struct {
	// Workload is the 7-knob workload definition from the fc-invoke config.
	Workload substrate.Workload
	// BaseKey names the warm base bundle (typically the workload name or
	// image identifier; one bundle per image version).
	BaseKey string
	// Arch pins the guest CPU architecture; FC snapshots are arch-affine and
	// a mismatched restore fails closed.
	Arch string
	// BootReadyTimeout bounds the readiness poll after a COLD boot (a real boot
	// plus in-guest warm-up can take seconds).
	BootReadyTimeout time.Duration
	// RestoreReadyTimeout bounds the readiness poll after a WARM restore. A
	// restored guest is already warm, so it answers /shim/ready almost
	// immediately; this short budget only needs to cover WaitReady retrying
	// past the Firecracker post-restore vsock RX-queue race (the first
	// connection can wedge; WaitReady abandons it per-attempt and reconnects).
	// If a restore is not ready within this budget it is treated as unavailable
	// and Invoke falls back to a cold boot. Defaults to 2s.
	RestoreReadyTimeout time.Duration
	// SidecarAddr is the pod-local egress-proxy sidecar TCP address (ADR 023
	// phase 6a). When Workload.EgressEnabled is true, each guest's vsock egress
	// connections are tunnelled here; the daemon holds no secrets and never
	// parses the bytes. Ignored when egress is disabled (e.g. semgrep).
	SidecarAddr string
}

// Invoker orchestrates invocations for one workload. The daemon creates one
// Invoker per workload entry (Task 8), sharing a single vmDriver and transport
// across all workloads.
type Invoker struct {
	driver    vmDriver
	transport transport
	sem       *semaphore.Weighted
	cfg       Config
	logger    *slog.Logger

	baseMu       sync.Mutex
	baseRef      substrate.SnapshotRef // zero ID means no warm base; use cold boot
	baseGen      uint64                // bumped on every successful BuildBase
	baseBuilding bool
}

// New builds an Invoker. driver and transport must not be nil.
func New(d vmDriver, t transport, cfg Config, logger *slog.Logger) *Invoker {
	if logger == nil {
		logger = slog.Default()
	}
	var sem *semaphore.Weighted
	if cfg.Workload.Concurrency > 0 {
		sem = semaphore.NewWeighted(int64(cfg.Workload.Concurrency))
	}
	if cfg.RestoreReadyTimeout <= 0 {
		cfg.RestoreReadyTimeout = 2 * time.Second
	}
	return &Invoker{driver: d, transport: t, sem: sem, cfg: cfg, logger: logger}
}

// BuildBase boots a cold guest, waits for its shim to be ready, and snapshots
// it into the warm base bundle. It is best-effort: a failure leaves the
// invoker with no base, so invocations cold-boot until a base is built. Safe
// to call concurrently; only one build runs at a time. No-ops when
// Workload.WarmBase is false.
func (inv *Invoker) BuildBase(ctx context.Context) error {
	if !inv.cfg.Workload.WarmBase {
		return nil
	}
	inv.baseMu.Lock()
	if inv.baseBuilding || inv.baseRef.ID != "" {
		inv.baseMu.Unlock()
		return nil
	}
	inv.baseBuilding = true
	inv.baseMu.Unlock()
	defer func() {
		inv.baseMu.Lock()
		inv.baseBuilding = false
		inv.baseMu.Unlock()
	}()

	// The build guest counts against the concurrency cap so K*MemMib stays a
	// true bound on microVM memory even while a rebuild runs alongside invocations.
	if inv.sem != nil {
		if err := inv.sem.Acquire(ctx, 1); err != nil {
			return fmt.Errorf("base build: acquire slot: %w", err)
		}
		defer inv.sem.Release(1)
	}

	start := time.Now()
	h, err := inv.driver.Claim(ctx, substrate.ClaimSpec{Arch: inv.cfg.Arch})
	if err != nil {
		return fmt.Errorf("base build: cold boot: %w", err)
	}
	// Always discard the build VM: the base lives in the snapshot bundle, not
	// in this live VM. context.Background() so cleanup runs even if ctx fires.
	defer inv.discard(h)

	readyCtx, cancel := context.WithTimeout(ctx, inv.cfg.BootReadyTimeout)
	defer cancel()
	if err := inv.transport.WaitReady(readyCtx, inv.driver.VsockUDSPath(h.ThreadID), inv.cfg.Workload.ReadyPath); err != nil {
		return fmt.Errorf("base build: guest readiness: %w", err)
	}
	ref, err := inv.driver.SnapshotBase(ctx, h, inv.cfg.BaseKey)
	if err != nil {
		return fmt.Errorf("base build: snapshot: %w", err)
	}
	inv.baseMu.Lock()
	inv.baseRef = ref
	inv.baseGen++
	inv.baseMu.Unlock()
	inv.logger.Info("warm base built", "key", inv.cfg.BaseKey, "size_mb", ref.SizeBytes/(1<<20), "took", time.Since(start))
	return nil
}

// Invoke round-trips one HTTP POST to the guest shim on behalf of the caller.
// It restores from the warm base when available (fast path) and falls back to
// a cold boot otherwise.
//
// Cleanup ownership: on success the returned response body is a lazy stream the
// caller reads AFTER Invoke returns (the HTTP handler does io.Copy then
// resp.Body.Close). Tearing the VM down, cancelling the request context, or
// releasing the concurrency slot when Invoke returns would truncate that body.
// So on the success path Invoke transfers all three cleanups (VM discard,
// request-context cancel, semaphore release) to the response body's Close,
// which runs them exactly once. The caller MUST close the body. On every
// error/early-return path there is no body to own, so cleanup runs eagerly.
//
// Error classification for the HTTP layer:
//   - *GuestUnavailableError: Claim, WaitReady, or semaphore failure; map to
//     503. A warm-path failure also triggers an async base rebuild.
//   - any other error: the RoundTrip itself failed after the guest started;
//     map to 502.
func (inv *Invoker) Invoke(ctx context.Context, session string, body io.Reader) (*http.Response, error) {
	if inv.sem != nil {
		// acquire_slot measures queue wait: how long the request blocked on the
		// per-workload concurrency cap before a microVM slot came free.
		_, span := tracer.Start(ctx, "acquire_slot")
		err := inv.sem.Acquire(ctx, 1)
		if err != nil {
			span.RecordError(err)
			span.SetStatus(codes.Error, err.Error())
		}
		span.End()
		if err != nil {
			return nil, &GuestUnavailableError{Err: fmt.Errorf("acquire invoke slot: %w", err)}
		}
	}
	// releaseSlot frees the concurrency slot (nil-safe). On success it is folded
	// into the body's Close so the slot is held until the guest is torn down; on
	// any failure below it runs eagerly.
	releaseSlot := func() {
		if inv.sem != nil {
			inv.sem.Release(1)
		}
	}

	base, gen, ok := inv.currentBase()
	// Record the warm/cold decision on the root fc_invoke span so a trace shows
	// at a glance whether it took the pool (warm restore) or a cold boot; the
	// branch's phase spans (snapshot_restore vs provision_rootfs+firecracker_boot)
	// then show where the time went.
	trace.SpanFromContext(ctx).SetAttributes(attribute.Bool("fc.pool_hit", ok))
	if ok {
		warmSpec := substrate.ClaimSpec{
			Arch:            inv.cfg.Arch,
			BaseSnapshotRef: base,
		}
		resp, cleanup, err := inv.claimInvoke(ctx, warmSpec, session, body, true)
		if err == nil {
			return ownResponse(resp, cleanup, releaseSlot), nil
		}
		// Only a *GuestUnavailableError (Claim or WaitReady failure) on the warm
		// path triggers base invalidation and a cold-boot fallback. A raw error
		// means the guest ran and only the HTTP leg failed; return it without
		// invalidating the base (it is still good) and without cold retry. The
		// failed attempt already discarded its own VM inside claimInvoke, so we
		// only release the concurrency slot here.
		var gue *GuestUnavailableError
		if !errors.As(err, &gue) {
			releaseSlot()
			return nil, err
		}
		inv.logger.Warn("invoker: warm path failed; falling back to cold boot", "err", err)
		inv.invalidateBase(gen)
	}

	coldSpec := substrate.ClaimSpec{
		Arch: inv.cfg.Arch,
	}
	resp, cleanup, err := inv.claimInvoke(ctx, coldSpec, session, body, false)
	if err != nil {
		releaseSlot()
		return nil, err
	}
	return ownResponse(resp, cleanup, releaseSlot), nil
}

// claimInvoke is the shared implementation for both the warm and cold
// invocation paths. It claims a VM from the driver using spec, waits for the
// shim to be ready, and performs the HTTP round-trip.
//
// On success it returns the response and a cleanup func that discards the VM
// and cancels the request context; the caller transfers that cleanup to the
// response body's Close so the guest stays live while the body streams. On any
// failure it discards the VM and cancels its context eagerly (there is no body
// to own) and returns a nil response and nil cleanup.
//
// A Claim or WaitReady failure returns *GuestUnavailableError. A RoundTrip
// failure is returned as-is (the caller decides the 502/503 mapping).
func (inv *Invoker) claimInvoke(ctx context.Context, spec substrate.ClaimSpec, session string, body io.Reader, warm bool) (*http.Response, func(), error) {
	// Each claim gets a fresh single-use microVM, so the host bundle + vsock
	// socket identity MUST be unique per claim, never the logical session. The
	// egress listen socket is derived from VsockUDSPath(threadID) as
	// "<uds>_<EgressPort>" (see internal/egress); if two turns of one session
	// (a sessioned workload reuses the same session id across turns) shared a
	// threadID they would share that socket path, letting a finishing
	// forwarder's deferred os.Remove race the next turn's Listen. A per-claim
	// threadID keeps the two turns on disjoint paths, so that race cannot occur.
	// The session still reaches the guest via the /invoke/{session} path below,
	// which is where session continuity actually lives; the host thread id is
	// purely a per-VM bundle name.
	spec.ThreadID = newThreadID()
	// On the warm path the Claim is a snapshot restore; wrap it in a
	// snapshot_restore span so it is the warm-path analog of the cold path's
	// provision_rootfs + firecracker_boot spans (emitted inside driver.Claim).
	// The cold path adds no wrapper here so warm and cold stay symmetric in the
	// waterfall: the pool_hit attribute plus which child spans appear tells them
	// apart.
	claimCtx := ctx
	var restoreSpan trace.Span
	if warm {
		claimCtx, restoreSpan = tracer.Start(ctx, "snapshot_restore")
	}
	h, err := inv.driver.Claim(claimCtx, spec)
	if err != nil {
		if restoreSpan != nil {
			restoreSpan.RecordError(err)
			restoreSpan.SetStatus(codes.Error, err.Error())
			restoreSpan.End()
		}
		return nil, nil, &GuestUnavailableError{Err: fmt.Errorf("claim guest: %w", err)}
	}
	if restoreSpan != nil {
		restoreSpan.End()
	}

	uds := inv.driver.VsockUDSPath(h.ThreadID)

	// Egress (ADR 023 phase 6a): when this workload has egress enabled, forward
	// the guest's vsock egress connections to the pod-local egress-proxy sidecar.
	// The forwarder runs for the life of the guest, bound to its own background
	// context (independent of the request context, so it survives while the
	// response body streams). egressCancel stops it and is folded into every path
	// that discards the guest below, so the forwarder goroutine never outlives the
	// VM. It is a no-op when egress is disabled (e.g. semgrep), leaving that path
	// completely unaffected.
	egressCancel := func() {}
	if inv.cfg.Workload.EgressEnabled {
		ectx, cancel := context.WithCancel(context.Background())
		egressCancel = cancel
		go func() {
			if err := egress.ServeEgress(ectx, inv.logger, uds, inv.cfg.SidecarAddr); err != nil {
				inv.logger.Warn("invoker: egress forwarder stopped", "thread", h.ThreadID, "err", err)
			}
		}()
	}
	// discardGuest tears the VM down and stops its egress forwarder together, so
	// the forwarder's context is cancelled exactly when the guest is discarded on
	// every path (each eager error return below, and the success body Close).
	discardGuest := func() {
		egressCancel()
		// Wrap the VM teardown (driver Release + bundle removal) in its own span.
		// On the success path this runs at response-body Close, AFTER the caller
		// already has its response, so it is off the critical path. Without its
		// own span it inflates the parent fc_invoke span past the caller's client
		// span and reads as extra exec time. Parent under ctx (which carries the
		// fc_invoke span) for nesting; inv.discard uses its own background context
		// internally so cleanup still runs even if ctx is cancelled, which is
		// independent of span parentage.
		_, teardownSpan := tracer.Start(ctx, "guest_teardown")
		inv.discard(h)
		teardownSpan.End()
	}

	// A warm restore is already warmed in the snapshot, so it answers /shim/ready
	// almost immediately; use the short RestoreReadyTimeout so a wedged or dead
	// restore fails fast and falls back to a cold boot. A cold boot needs the
	// full BootReadyTimeout to cover the real boot plus in-guest warm-up.
	// WaitReady itself retries past the post-restore vsock RX-queue race
	// (per-attempt deadline), so no separate "prime" is needed. The readiness
	// wait uses its own short-lived context, always cancelled here (it never
	// bounds the response body).
	readyTimeout := inv.cfg.BootReadyTimeout
	if warm {
		readyTimeout = inv.cfg.RestoreReadyTimeout
	}
	readyCtx, cancelReady := context.WithTimeout(ctx, readyTimeout)
	waitCtx, waitSpan := tracer.Start(readyCtx, "guest_wait_ready")
	readyErr := inv.transport.WaitReady(waitCtx, uds, inv.cfg.Workload.ReadyPath)
	if readyErr != nil {
		waitSpan.RecordError(readyErr)
		waitSpan.SetStatus(codes.Error, readyErr.Error())
	}
	waitSpan.End()
	cancelReady()
	if readyErr != nil {
		discardGuest()
		return nil, nil, &GuestUnavailableError{Err: fmt.Errorf("guest readiness: %w", readyErr)}
	}

	path := "/invoke"
	if session != "" {
		path = "/invoke/" + session
	}
	rtCtx, cancelRT := context.WithTimeout(ctx, inv.cfg.Workload.RequestTimeout)
	req, err := http.NewRequestWithContext(rtCtx, http.MethodPost, "http://vsock"+path, body)
	if err != nil {
		// This only fails for malformed method strings; treat as infrastructure
		// error, not a VM issue. No body to own, so clean up eagerly.
		cancelRT()
		discardGuest()
		return nil, nil, fmt.Errorf("invoker: build request: %w", err)
	}
	// guest_exec is the black-box guest-execution span: the time the guest spends
	// handling the request. Guests emit no spans of their own (intentional; guest
	// interior instrumentation is deferred), so this single span stands in for the
	// whole in-guest execution.
	execCtx, execSpan := tracer.Start(rtCtx, "guest_exec", trace.WithAttributes(
		attribute.String("fc.workload", inv.cfg.BaseKey),
		attribute.String("fc.session", session),
	))
	resp, err := inv.transport.RoundTrip(execCtx, uds, req)
	if err != nil {
		execSpan.RecordError(err)
		execSpan.SetStatus(codes.Error, err.Error())
		execSpan.End()
		cancelRT()
		discardGuest()
		if warm {
			// On the warm path a transport-level round-trip failure (the
			// connection broke mid-request) after readiness passed suggests the
			// restored guest is flaky. Surface it as unavailable so Invoke
			// invalidates the base and retries on a cold boot rather than
			// returning a hard 502.
			return nil, nil, &GuestUnavailableError{Err: fmt.Errorf("warm round-trip: %w", err)}
		}
		// Cold path: the guest booted and proved ready, so a round-trip failure
		// is an HTTP-leg error (502 territory), not a guest-unavailable case.
		// No body to own, so clean up eagerly.
		return nil, nil, err
	}
	// The guest returned response headers; the exec round-trip is complete. The
	// response body still streams lazily over the live guest, but that is guest
	// I/O owned by the caller, not part of the exec span.
	execSpan.End()
	// Success: the response body is a lazy stream over the still-live guest.
	// Hand the caller a cleanup that cancels the request context and discards
	// the VM; it will run only when the body is closed, after streaming. Cancel
	// the context AFTER discarding so an in-flight read is never aborted early.
	cleanup := func() {
		discardGuest()
		cancelRT()
	}
	return resp, cleanup, nil
}

// ownedBody wraps a response body so closing it runs a cleanup function exactly
// once. This transfers ownership of the microVM, its request context, and the
// concurrency slot to the response lifetime: the guest stays live until the
// caller finishes streaming the body and closes it. Double Close (e.g. a
// server defer plus a manual close) cleans up once.
type ownedBody struct {
	io.ReadCloser
	once    sync.Once
	cleanup func()
}

// Close closes the underlying body and runs the transferred cleanup exactly
// once. Subsequent calls are no-ops.
func (b *ownedBody) Close() error {
	var err error
	b.once.Do(func() {
		err = b.ReadCloser.Close()
		b.cleanup()
	})
	return err // nosemgrep: no-bare-error-return
}

// ownResponse replaces resp.Body with an ownedBody whose Close runs the VM/ctx
// cleanup followed by the concurrency-slot release, so the guest is torn down
// and the slot freed only after the caller has streamed and closed the body.
func ownResponse(resp *http.Response, cleanup, releaseSlot func()) *http.Response {
	inner := resp.Body
	if inner == nil {
		inner = http.NoBody
	}
	resp.Body = &ownedBody{
		ReadCloser: inner,
		cleanup: func() {
			cleanup()
			releaseSlot()
		},
	}
	return resp
}

// discard releases a microVM and removes its on-disk bundle. It runs for every
// guest the invoker claims: eagerly on failure paths, and from the response
// body's Close on the success path. Best-effort: individual failures are
// logged, not returned, so a Release hiccup does not mask the original result.
// context.Background() ensures cleanup runs even when the request context was
// cancelled.
func (inv *Invoker) discard(h substrate.Handle) {
	if err := inv.driver.Release(context.Background(), h); err != nil {
		inv.logger.Warn("invoker: release guest", "thread", h.ThreadID, "err", err)
	}
	if err := inv.driver.RemoveBundle(h.ThreadID); err != nil {
		inv.logger.Warn("invoker: remove guest bundle", "thread", h.ThreadID, "err", err)
	}
}

// newThreadID returns a per-invocation unique host thread id. It names the
// microVM's on-disk bundle and, transitively, its egress listen socket; making
// it unique per claim (rather than reusing the session) is what keeps two turns
// of one sessioned workload from colliding on the same egress socket path.
func newThreadID() string {
	var b [8]byte
	_, _ = rand.Read(b[:])
	return "inv-" + hex.EncodeToString(b[:])
}

// currentBase returns the warm base ref, the build generation it was taken
// from, and whether the base is currently usable (non-zero ID).
func (inv *Invoker) currentBase() (substrate.SnapshotRef, uint64, bool) {
	inv.baseMu.Lock()
	defer inv.baseMu.Unlock()
	return inv.baseRef, inv.baseGen, inv.baseRef.ID != ""
}

// invalidateBase clears the warm base after a restore or readiness failure and
// kicks an asynchronous rebuild so subsequent invocations get the fast path
// back. gen is the generation observed when the failing base was taken; the
// clear is skipped if a concurrent rebuild has since advanced the generation,
// so a late failure from a stale base does not wipe a freshly-built one.
func (inv *Invoker) invalidateBase(gen uint64) {
	inv.baseMu.Lock()
	stale := inv.baseGen == gen && inv.baseRef.ID != ""
	if stale {
		inv.baseRef = substrate.SnapshotRef{}
	}
	inv.baseMu.Unlock()
	if !stale {
		return
	}
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), inv.cfg.BootReadyTimeout+inv.cfg.Workload.RequestTimeout)
		defer cancel()
		if err := inv.BuildBase(ctx); err != nil {
			inv.logger.Warn("invoker: warm base rebuild failed", "err", err)
		}
	}()
}
