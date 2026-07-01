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
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"sync"
	"time"

	"golang.org/x/sync/semaphore"

	"github.com/jomcgi/homelab/projects/firecracker/substrate/invoke/internal/config"
	"github.com/jomcgi/homelab/projects/firecracker/substrate/substrate"
)

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
	Workload config.Workload
	// BaseKey names the warm base bundle (typically the workload name or
	// image identifier; one bundle per image version).
	BaseKey string
	// Arch pins the guest CPU architecture; FC snapshots are arch-affine and
	// a mismatched restore fails closed.
	Arch string
	// BootReadyTimeout bounds the readiness poll after a cold boot or restore.
	BootReadyTimeout time.Duration
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
// a cold boot otherwise. The VM is always released when Invoke returns.
//
// Error classification for the HTTP layer:
//   - *GuestUnavailableError: Claim, WaitReady, or semaphore failure; map to
//     503. A warm-path failure also triggers an async base rebuild.
//   - any other error: the RoundTrip itself failed after the guest started;
//     map to 502.
func (inv *Invoker) Invoke(ctx context.Context, session string, body io.Reader) (*http.Response, error) {
	if inv.sem != nil {
		if err := inv.sem.Acquire(ctx, 1); err != nil {
			return nil, &GuestUnavailableError{Err: fmt.Errorf("acquire invoke slot: %w", err)}
		}
		defer inv.sem.Release(1)
	}

	if base, gen, ok := inv.currentBase(); ok {
		warmSpec := substrate.ClaimSpec{
			Arch:            inv.cfg.Arch,
			ThreadID:        session,
			BaseSnapshotRef: base,
		}
		resp, err := inv.claimInvoke(ctx, warmSpec, session, body)
		if err == nil {
			return resp, nil
		}
		// Only a *GuestUnavailableError (Claim or WaitReady failure) on the warm
		// path triggers base invalidation and a cold-boot fallback. A raw error
		// means the guest ran and only the HTTP leg failed; return it without
		// invalidating the base (it is still good) and without cold retry.
		var gue *GuestUnavailableError
		if !errors.As(err, &gue) {
			return nil, err
		}
		inv.logger.Warn("invoker: warm path failed; falling back to cold boot", "err", err)
		inv.invalidateBase(gen)
	}

	coldSpec := substrate.ClaimSpec{
		Arch:     inv.cfg.Arch,
		ThreadID: session,
	}
	return inv.claimInvoke(ctx, coldSpec, session, body)
}

// claimInvoke is the shared implementation for both the warm and cold
// invocation paths. It claims a VM from the driver using spec, waits for the
// shim to be ready, and performs the HTTP round-trip. The VM is always
// discarded via a defer, so callers never leak a live guest.
//
// A Claim or WaitReady failure returns *GuestUnavailableError. A RoundTrip
// failure is returned as-is (the caller decides the 502/503 mapping).
func (inv *Invoker) claimInvoke(ctx context.Context, spec substrate.ClaimSpec, session string, body io.Reader) (*http.Response, error) {
	h, err := inv.driver.Claim(ctx, spec)
	if err != nil {
		return nil, &GuestUnavailableError{Err: fmt.Errorf("claim guest: %w", err)}
	}
	// Critical: always release and remove the bundle, even on transport error.
	// context.Background() so cleanup runs even when the request ctx was
	// cancelled. This is the single defer that makes every error path safe.
	defer func() { inv.discard(h) }()

	uds := inv.driver.VsockUDSPath(h.ThreadID)

	readyCtx, cancelReady := context.WithTimeout(ctx, inv.cfg.BootReadyTimeout)
	defer cancelReady()
	if err := inv.transport.WaitReady(readyCtx, uds, inv.cfg.Workload.ReadyPath); err != nil {
		return nil, &GuestUnavailableError{Err: fmt.Errorf("guest readiness: %w", err)}
	}

	path := "/invoke"
	if session != "" {
		path = "/invoke/" + session
	}
	rtCtx, cancelRT := context.WithTimeout(ctx, inv.cfg.Workload.RequestTimeout)
	defer cancelRT()
	req, err := http.NewRequestWithContext(rtCtx, http.MethodPost, "http://vsock"+path, body)
	if err != nil {
		// This only fails for malformed method strings; treat as infrastructure
		// error, not a VM issue.
		return nil, fmt.Errorf("invoker: build request: %w", err)
	}
	// RoundTrip errors are NOT wrapped as GuestUnavailableError: the guest ran
	// and the round-trip itself failed (HTTP 502 territory).
	return inv.transport.RoundTrip(rtCtx, uds, req)
}

// discard releases a microVM and removes its on-disk bundle. Called via defer
// from claimInvoke on every guest the invoker claims. Best-effort: individual
// failures are logged, not returned, so a Release hiccup does not mask the
// original result. context.Background() ensures cleanup runs even when the
// request context was cancelled.
func (inv *Invoker) discard(h substrate.Handle) {
	if err := inv.driver.Release(context.Background(), h); err != nil {
		inv.logger.Warn("invoker: release guest", "thread", h.ThreadID, "err", err)
	}
	if err := inv.driver.RemoveBundle(h.ThreadID); err != nil {
		inv.logger.Warn("invoker: remove guest bundle", "thread", h.ThreadID, "err", err)
	}
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
