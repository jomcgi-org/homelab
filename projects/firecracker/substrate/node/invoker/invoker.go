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
	// Stats reads the guest's host-side resource counters (whole-invocation CPU
	// and peak RSS) from /proc. It must be called while the guest is still
	// alive, i.e. before Release. Best-effort: the caller records the values on
	// a span and continues on error.
	Stats(h substrate.Handle) (substrate.GuestStats, error)
}

// transport is the host-side HTTP-over-vsock client. The real
// *vsockhttp.Transport satisfies it; tests inject a fake.
type transport interface {
	// WaitReady polls readyPath on the guest shim until it responds 200 or
	// ctx fires. It is bounded by BootReadyTimeout in the caller.
	WaitReady(ctx context.Context, udsPath, readyPath string) error
	// Prime shakes out Firecracker's post-restore vsock RX-queue race with a
	// tight-deadline liveness probe, so the following WaitReady and exec dials
	// land on a drained queue. Warm path only; best-effort and bounded by the
	// caller's restore budget.
	Prime(ctx context.Context, udsPath string) error
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

	// Wrap the whole build in a root span so the startup base build is a single
	// trace instead of the driver's orphan provision_rootfs/firecracker_boot
	// spans. Claim, WaitReady, and SnapshotBase all read their span from the ctx
	// argument, so threading buildCtx down parents the existing driver spans
	// under this root with no driver changes. This span is created off the daemon
	// ctx (which outlives the build), and the daemon keeps running afterwards, so
	// the batch processor flushes it on its normal interval. Span names/durations
	// only; no secrets in attributes.
	buildCtx, buildSpan := tracer.Start(ctx, "base_snapshot_build", trace.WithAttributes(
		attribute.String("fc.workload", inv.cfg.BaseKey),
	))
	defer buildSpan.End()

	// The build guest counts against the concurrency cap so K*MemMib stays a
	// true bound on microVM memory even while a rebuild runs alongside invocations.
	if inv.sem != nil {
		if err := inv.sem.Acquire(buildCtx, 1); err != nil {
			buildSpan.RecordError(err)
			buildSpan.SetStatus(codes.Error, err.Error())
			return fmt.Errorf("base build: acquire slot: %w", err)
		}
		defer inv.sem.Release(1)
	}

	start := time.Now()
	// Claim cold-boots the build guest; its provision_rootfs and firecracker_boot
	// spans nest under base_snapshot_build via buildCtx.
	h, err := inv.driver.Claim(buildCtx, substrate.ClaimSpec{Arch: inv.cfg.Arch})
	if err != nil {
		buildSpan.RecordError(err)
		buildSpan.SetStatus(codes.Error, err.Error())
		return fmt.Errorf("base build: cold boot: %w", err)
	}
	// Always discard the build VM: the base lives in the snapshot bundle, not
	// in this live VM. context.Background() so cleanup runs even if ctx fires.
	// No throughput concern here (no invocation slot rides on it), so tear down
	// memory then disk synchronously.
	defer func() {
		inv.releaseGuest(h)
		inv.removeGuestBundle(h.ThreadID)
	}()

	readyCtx, cancel := context.WithTimeout(buildCtx, inv.cfg.BootReadyTimeout)
	defer cancel()
	waitCtx, waitSpan := tracer.Start(readyCtx, "guest_wait_ready")
	readyErr := inv.transport.WaitReady(waitCtx, inv.driver.VsockUDSPath(h.ThreadID), inv.cfg.Workload.ReadyPath)
	if readyErr != nil {
		waitSpan.RecordError(readyErr)
		waitSpan.SetStatus(codes.Error, readyErr.Error())
	}
	waitSpan.End()
	if readyErr != nil {
		buildSpan.RecordError(readyErr)
		buildSpan.SetStatus(codes.Error, readyErr.Error())
		return fmt.Errorf("base build: guest readiness: %w", readyErr)
	}
	// snapshot_save wraps the pause + snapshot write + resume + publish; the warm
	// base bundle write is the second big cost of a startup build after the boot.
	saveCtx, saveSpan := tracer.Start(buildCtx, "snapshot_save")
	ref, err := inv.driver.SnapshotBase(saveCtx, h, inv.cfg.BaseKey)
	if err != nil {
		saveSpan.RecordError(err)
		saveSpan.SetStatus(codes.Error, err.Error())
		saveSpan.End()
		buildSpan.RecordError(err)
		buildSpan.SetStatus(codes.Error, err.Error())
		return fmt.Errorf("base build: snapshot: %w", err)
	}
	saveSpan.End()
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
		resp, td, err := inv.claimInvoke(ctx, warmSpec, session, body, true)
		if err == nil {
			return inv.ownResponse(ctx, resp, td, releaseSlot), nil
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
	resp, td, err := inv.claimInvoke(ctx, coldSpec, session, body, false)
	if err != nil {
		releaseSlot()
		return nil, err
	}
	return inv.ownResponse(ctx, resp, td, releaseSlot), nil
}

// claimInvoke is the shared implementation for both the warm and cold
// invocation paths. It claims a VM from the driver using spec, waits for the
// shim to be ready, and performs the HTTP round-trip.
//
// On success it returns the response and an invokeTeardown carrying the pieces
// the caller must run when the body closes (the guest handle, the egress-cancel,
// and the request-context cancel); the caller (ownResponse) orders those against
// the concurrency-slot release so the VM's memory is reclaimed before the slot is
// freed and the on-disk bundle removal runs off the slot. On any failure it tears
// the VM down and cancels its context eagerly (there is no body to own) and
// returns a nil response and a zero invokeTeardown.
//
// A Claim or WaitReady failure returns *GuestUnavailableError. A RoundTrip
// failure is returned as-is (the caller decides the 502/503 mapping).
func (inv *Invoker) claimInvoke(ctx context.Context, spec substrate.ClaimSpec, session string, body io.Reader, warm bool) (*http.Response, invokeTeardown, error) {
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
		return nil, invokeTeardown{}, &GuestUnavailableError{Err: fmt.Errorf("claim guest: %w", err)}
	}
	if restoreSpan != nil {
		restoreSpan.End()
	}

	uds := inv.driver.VsockUDSPath(h.ThreadID)

	// Warm restores can wedge their first host-initiated vsock connection on
	// Firecracker's post-restore RX-queue race (see vsockhttp.WaitReady). Shake
	// it out HERE, off the readiness path, with a tight-deadline primer so the
	// WaitReady and exec dials below land on a drained queue and answer on their
	// first attempt. Its own vsock_prime span makes the drain cost measurable and
	// self-diagnosing (a tight cluster means the tight retries win; a floor at the
	// restore budget would point at a guest-side drain-window instead). Cold boots
	// do not restore, so they never hit this race; prime only on the warm path.
	// Best-effort: a prime failure just leaves WaitReady to pay the race as before.
	if warm {
		primeCtx, cancelPrime := context.WithTimeout(ctx, inv.cfg.RestoreReadyTimeout)
		primeSpanCtx, primeSpan := tracer.Start(primeCtx, "vsock_prime")
		if perr := inv.transport.Prime(primeSpanCtx, uds); perr != nil {
			primeSpan.RecordError(perr)
			primeSpan.SetStatus(codes.Error, perr.Error())
			inv.logger.Warn("invoker: vsock prime did not complete; readiness poll will retry past the race", "thread", h.ThreadID, "err", perr)
		}
		primeSpan.End()
		cancelPrime()
	}

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
	// the forwarder's context is cancelled exactly when the guest is discarded.
	// It is used only on the EAGER error paths below (there is no body to own, so
	// throughput is irrelevant on a failed run): stop egress, reclaim VM memory,
	// then remove the on-disk bundle, all synchronously. The success path does not
	// use this; ownResponse orders Release, slot release, and async bundle removal
	// itself so the slot is freed as soon as memory is reclaimed.
	discardGuest := func() {
		egressCancel()
		inv.releaseGuest(h)
		inv.removeGuestBundle(h.ThreadID)
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
		return nil, invokeTeardown{}, &GuestUnavailableError{Err: fmt.Errorf("guest readiness: %w", readyErr)}
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
		return nil, invokeTeardown{}, fmt.Errorf("invoker: build request: %w", err)
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
			return nil, invokeTeardown{}, &GuestUnavailableError{Err: fmt.Errorf("warm round-trip: %w", err)}
		}
		// Cold path: the guest booted and proved ready, so a round-trip failure
		// is an HTTP-leg error (502 territory), not a guest-unavailable case.
		// No body to own, so clean up eagerly.
		return nil, invokeTeardown{}, err
	}
	// The guest returned response headers; the exec round-trip is complete. The
	// response body still streams lazily over the live guest, but that is guest
	// I/O owned by the caller, not part of the exec span.
	execSpan.End()
	// Success: the response body is a lazy stream over the still-live guest. Hand
	// the caller the teardown pieces (guest handle, egress-cancel, request-context
	// cancel); ownResponse folds them into the body's Close so the guest stays live
	// while the body streams, and orders VM Release ahead of the concurrency-slot
	// release so memory is reclaimed before the slot frees.
	return resp, invokeTeardown{h: h, egressCancel: egressCancel, cancelRT: cancelRT}, nil
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

// invokeTeardown carries the pieces claimInvoke closes over that the success
// path must run when the response body is closed: the guest handle (to Release
// and to remove its bundle), the egress-forwarder cancel, and the request-context
// cancel. ownResponse orders these against the concurrency-slot release so the
// VM's memory is reclaimed (Release) BEFORE the slot is freed, and the on-disk
// bundle removal runs asynchronously off the freed slot.
type invokeTeardown struct {
	h            substrate.Handle
	egressCancel func()
	cancelRT     func()
}

// ownResponse replaces resp.Body with an ownedBody whose Close, in order:
//
//	(a) stops the egress forwarder, cancels the request context, and calls
//	    driver.Release to kill the microVM process, reclaiming its MEMORY (the
//	    resource the concurrency slot bounds);
//	(b) releases the concurrency slot, so a queued invocation can start
//	    immediately once memory is free, without waiting on disk cleanup;
//	(c) removes the on-disk bundle asynchronously (DISK, not memory), off the
//	    freed slot.
//
// The slot is therefore never freed before Release returns (the memory bound
// holds), and the ~40ms bundle removal no longer occupies capacity. ownedBody's
// sync.Once runs this exactly once, so Release happens once and exactly one
// cleanup goroutine is spawned per claimed guest. The caller MUST close the body.
func (inv *Invoker) ownResponse(ctx context.Context, resp *http.Response, td invokeTeardown, releaseSlot func()) *http.Response {
	inner := resp.Body
	if inner == nil {
		inner = http.NoBody
	}
	resp.Body = &ownedBody{
		ReadCloser: inner,
		cleanup: func() {
			// (a) Stop the egress forwarder (so it never outlives the VM), cancel
			// the request context, then reclaim the VM's memory. Wrap Release in a
			// vm_release span parented under ctx (the fc_invoke span); it runs at
			// body Close, after the caller already has its response, so it is off
			// the critical path. releaseGuest uses its own background context, so
			// the kill runs even if ctx was cancelled.
			td.egressCancel()
			td.cancelRT()
			_, releaseSpan := tracer.Start(ctx, "vm_release")
			// Sample the guest's whole-invocation resource use from /proc BEFORE
			// Release kills the process (afterwards /proc/<pid> is gone). Record
			// it on vm_release: it is the live span here (fc_invoke and guest_exec
			// have already ended by body Close), and it is where the values are
			// queryable per invocation. Best-effort; a read failure just omits the
			// attributes. These describe the whole invocation, not the release.
			if stats, err := inv.driver.Stats(td.h); err == nil {
				releaseSpan.SetAttributes(
					attribute.Int64("fc.guest.cpu_ms", stats.CPUMillis),
					attribute.Int64("fc.guest.peak_rss_mib", stats.PeakRSSMib),
				)
			} else {
				inv.logger.Debug("invoker: guest stats unavailable", "thread", td.h.ThreadID, "err", err)
			}
			inv.releaseGuest(td.h)
			releaseSpan.End()
			// (b) VM memory reclaimed: free the slot now so the next queued
			// invocation does not wait on disk cleanup it does not need.
			releaseSlot()
			// (c) Remove the on-disk bundle asynchronously, off the slot. It only
			// reclaims disk, so it must not gate the memory slot. Best-effort: it
			// logs its own errors, and a defensive recover keeps a stray panic in
			// this detached goroutine from taking down the single-replica daemon.
			// On abrupt process exit a bundle may be left behind for restart-time
			// cleanup, which is acceptable. Start bundle_cleanup from ctx so it
			// still nests under fc_invoke.
			threadID := td.h.ThreadID
			go func() {
				defer func() {
					if r := recover(); r != nil {
						inv.logger.Error("invoker: bundle_cleanup panic", "thread", threadID, "recover", r)
					}
				}()
				_, bundleSpan := tracer.Start(ctx, "bundle_cleanup")
				inv.removeGuestBundle(threadID)
				bundleSpan.End()
			}()
		},
	}
	return resp
}

// releaseGuest kills a microVM process, reclaiming its MEMORY (the resource the
// concurrency slot bounds). Best-effort: a failure is logged, not returned, so a
// Release hiccup does not mask the original result. context.Background() ensures
// the kill runs even when the request context was cancelled.
func (inv *Invoker) releaseGuest(h substrate.Handle) {
	if err := inv.driver.Release(context.Background(), h); err != nil {
		inv.logger.Warn("invoker: release guest", "thread", h.ThreadID, "err", err)
	}
}

// removeGuestBundle deletes a guest's on-disk bundle directory (its vsock/API
// sockets and snapshot files), reclaiming DISK, not memory. It is safe to run
// after the concurrency slot is freed because it never gates the memory bound.
// Best-effort: a failure is logged, not returned. context.Background() is not
// needed (it takes no context), but the same rationale applies: it runs to
// completion regardless of the request's lifetime.
func (inv *Invoker) removeGuestBundle(threadID string) {
	if err := inv.driver.RemoveBundle(threadID); err != nil {
		inv.logger.Warn("invoker: remove guest bundle", "thread", threadID, "err", err)
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
