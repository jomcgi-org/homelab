// Package scanner is the boot-and-warm orchestration core of semgrep-scand. Each
// Scan claims a fresh semgrep-guest microVM, waits for it to announce readiness
// over the control vsock, runs one scan over the guest's scan channel, and always
// releases (discards) the guest afterwards. A weighted semaphore caps how many
// guests are live at once.
//
// The Firecracker driver and the guest vsock I/O are reached through narrow
// interfaces (vmDriver, guestTransport) so the orchestration is unit-testable
// with fakes: no Firecracker, no real vsock, no real semgrep.
package scanner

import (
	"context"
	"fmt"
	"log/slog"
	"time"

	"golang.org/x/sync/semaphore"

	"github.com/jomcgi/homelab/projects/agent_platform/substrate"
	"github.com/jomcgi/homelab/projects/agent_platform/vsockproto"
)

// vmDriver is the subset of the Firecracker driver the scanner needs. The shared
// fcvm/driver.Driver satisfies it; tests inject a fake.
type vmDriver interface {
	// Claim boots a fresh microVM from the base rootfs and returns its handle.
	Claim(ctx context.Context, spec substrate.ClaimSpec) (substrate.Handle, error)
	// Release kills the microVM and discards it. The scanner never snapshots a
	// scanner guest, so Release is always a full teardown.
	Release(ctx context.Context, h substrate.Handle) error
	// VsockUDSPath is the host unix socket backing a thread's vsock device; the
	// transport addresses the guest's control + scan ports relative to it.
	VsockUDSPath(threadID string) string
}

// guestTransport waits for a freshly booted guest's readiness Hello and runs one
// scan over the guest's scan channel, both addressed by the per-VM vsock UDS path.
// The real implementation is vsockTransport; tests inject a fake.
type guestTransport interface {
	// WaitReady blocks until the guest announces readiness (KindHello on the
	// control vsock) or ctx (bounded by BootReadyTimeout) fires.
	WaitReady(ctx context.Context, udsPath string) error
	// Scan dials the guest's scan port, writes the request, and reads the result,
	// bounded by ctx (ScanTimeout).
	Scan(ctx context.Context, udsPath string, req vsockproto.ScanRequest) (vsockproto.ScanResult, error)
}

// GuestUnavailableError marks a failure to launch or warm a guest at all (a
// boot/launch/readiness failure), as opposed to a scan that ran and produced
// errors. The HTTP layer maps it to 503 via its GuestUnavailable() method; the
// wrapped cause stays inspectable through Unwrap.
type GuestUnavailableError struct{ Err error }

func (e *GuestUnavailableError) Error() string { return "guest unavailable: " + e.Err.Error() }

// Unwrap returns the wrapped cause unmodified, as the errors.Unwrap contract
// requires; it must not re-wrap, so the bare return is intentional.
func (e *GuestUnavailableError) Unwrap() error { return e.Err } // nosemgrep: no-bare-error-return

func (e *GuestUnavailableError) GuestUnavailable() bool { return true }

// Config carries the scanner's sizing and timing knobs.
type Config struct {
	// MaxConcurrent caps live guests. <= 0 means unbounded.
	MaxConcurrent int
	// Arch pins the claim's architecture (FC snapshots/guests are arch-affine).
	Arch string
	// BootReadyTimeout bounds the readiness wait; ScanTimeout bounds the scan leg.
	BootReadyTimeout time.Duration
	ScanTimeout      time.Duration
}

// Scanner runs scans by booting a guest per request.
type Scanner struct {
	driver    vmDriver
	transport guestTransport
	sem       *semaphore.Weighted
	cfg       Config
	logger    *slog.Logger
}

// New builds a Scanner. driver and transport must not be nil.
func New(driver vmDriver, transport guestTransport, cfg Config, logger *slog.Logger) *Scanner {
	if logger == nil {
		logger = slog.Default()
	}
	var sem *semaphore.Weighted
	if cfg.MaxConcurrent > 0 {
		sem = semaphore.NewWeighted(int64(cfg.MaxConcurrent))
	}
	return &Scanner{driver: driver, transport: transport, sem: sem, cfg: cfg, logger: logger}
}

// Scan boots a guest, scans the batch, and releases the guest. A boot/readiness
// failure returns a *GuestUnavailableError (the HTTP layer's 503); a scan-leg
// failure is folded into ScanResult.Errors with a nil error (the HTTP layer's
// 200), so a partial failure still returns whatever the guest produced.
func (s *Scanner) Scan(ctx context.Context, files []vsockproto.ScanFile) (vsockproto.ScanResult, error) {
	// Cap concurrency before spending the cost of a boot, so at most K guests are
	// ever live and K*GuestMemMib bounds the microVM memory in our cgroup.
	if s.sem != nil {
		if err := s.sem.Acquire(ctx, 1); err != nil {
			return vsockproto.ScanResult{}, &GuestUnavailableError{Err: fmt.Errorf("acquire scan slot: %w", err)}
		}
		defer s.sem.Release(1)
	}

	// A scanner guest needs no recipe/task/secrets; it boots the semgrep-guest
	// base rootfs and serves scans. Only Arch is pinned (guests are arch-affine);
	// the driver assigns a fresh ThreadID.
	h, err := s.driver.Claim(ctx, substrate.ClaimSpec{Arch: s.cfg.Arch})
	if err != nil {
		return vsockproto.ScanResult{}, &GuestUnavailableError{Err: fmt.Errorf("claim guest: %w", err)}
	}
	// Always discard the guest, even on error, and even if the request ctx was
	// cancelled (hence context.Background()): a scanner guest is single-use.
	defer func() {
		if rErr := s.driver.Release(context.Background(), h); rErr != nil {
			s.logger.Warn("scanner: release guest", "thread", h.ThreadID, "err", rErr)
		}
	}()

	uds := s.driver.VsockUDSPath(h.ThreadID)

	readyCtx, cancelReady := context.WithTimeout(ctx, s.cfg.BootReadyTimeout)
	defer cancelReady()
	if err := s.transport.WaitReady(readyCtx, uds); err != nil {
		return vsockproto.ScanResult{}, &GuestUnavailableError{Err: fmt.Errorf("guest readiness: %w", err)}
	}

	scanCtx, cancelScan := context.WithTimeout(ctx, s.cfg.ScanTimeout)
	defer cancelScan()
	res, err := s.transport.Scan(scanCtx, uds, vsockproto.ScanRequest{Files: files})
	if err != nil {
		// The guest booted and warmed; a failure here is a scan failure, returned
		// as data so the caller still gets a 200 with the error described.
		return vsockproto.ScanResult{Errors: []string{err.Error()}}, nil
	}
	return res, nil
}
