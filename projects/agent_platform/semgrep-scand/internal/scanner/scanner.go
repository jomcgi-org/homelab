// Package scanner is the warm-base orchestration core of semgrep-scand. At startup
// it boots one semgrep-guest microVM, waits for its semgrep lsp to warm, and
// captures it into a Firecracker base snapshot. Each scan then RESTORES a fresh
// microVM from that warm base (~tens of ms, the rules already compiled in the
// snapshot memfile), runs one scan over the guest's vsock scan channel, and
// discards the microVM. If the warm base is unavailable (not yet built, or a
// restore fails) a scan falls back to a full cold boot + warm, so the snapshot is
// a latency optimisation, never load-bearing.
//
// The Firecracker driver and the guest vsock I/O are reached through narrow
// interfaces (vmDriver, guestTransport) so the orchestration is unit-testable with
// fakes: no Firecracker, no real vsock, no real semgrep.
package scanner

import (
	"context"
	"fmt"
	"log/slog"
	"sync"
	"time"

	"golang.org/x/sync/semaphore"

	"github.com/jomcgi/homelab/projects/agent_platform/substrate"
	"github.com/jomcgi/homelab/projects/agent_platform/vsockproto"
)

// vmDriver is the subset of the Firecracker driver the scanner needs. The shared
// fcvm/driver.Driver satisfies it; tests inject a fake.
type vmDriver interface {
	// Claim boots a microVM. With spec.BaseSnapshotRef set it restores from the warm
	// base for an instant ready start; otherwise it cold-boots the base rootfs.
	Claim(ctx context.Context, spec substrate.ClaimSpec) (substrate.Handle, error)
	// SnapshotBase captures a warmed microVM into a shared base bundle keyed by
	// baseKey; new microVMs restore from it.
	SnapshotBase(ctx context.Context, h substrate.Handle, baseKey string) (substrate.SnapshotRef, error)
	// Release kills the microVM and discards it. A scanner guest is single-use.
	Release(ctx context.Context, h substrate.Handle) error
	// RemoveBundle deletes a thread's on-disk bundle dir (its vsock sockets and
	// api socket). Called after Release so per-scan bundles do not accumulate on
	// the nvme disk. It is a no-op if the dir is already gone.
	RemoveBundle(threadID string) error
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
// boot/launch/readiness/restore failure), as opposed to a scan that ran and
// produced errors. The HTTP layer maps it to 503 via its GuestUnavailable()
// method; the wrapped cause stays inspectable through Unwrap.
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
	// BootReadyTimeout bounds the readiness wait (cold boot + base build); ScanTimeout
	// bounds the scan leg.
	BootReadyTimeout time.Duration
	ScanTimeout      time.Duration
	// WarmBase enables the snapshot-restore hot path. When false, every scan is a
	// full cold boot + warm (the pre-snapshot behaviour).
	WarmBase bool
	// BaseKey names the warm base bundle (one per guest-image version).
	BaseKey string
	// RestorePrime bounds the throwaway "prime" connection sent after a restore to
	// absorb Firecracker's vsock RX-queue race (the first post-restore connection is
	// reset or hangs; the next is clean).
	RestorePrime time.Duration
}

// Scanner runs scans by restoring a guest from a warm base per request, falling
// back to a cold boot when the base is unavailable.
type Scanner struct {
	driver    vmDriver
	transport guestTransport
	sem       *semaphore.Weighted
	cfg       Config
	logger    *slog.Logger

	baseMu       sync.Mutex
	baseRef      substrate.SnapshotRef // zero ID => no warm base; cold boot
	baseGen      uint64                // bumped on every successful build; guards invalidation
	baseBuilding bool
}

// New builds a Scanner. driver and transport must not be nil.
func New(driver vmDriver, transport guestTransport, cfg Config, logger *slog.Logger) *Scanner {
	if logger == nil {
		logger = slog.Default()
	}
	if cfg.RestorePrime <= 0 {
		cfg.RestorePrime = 300 * time.Millisecond
	}
	var sem *semaphore.Weighted
	if cfg.MaxConcurrent > 0 {
		sem = semaphore.NewWeighted(int64(cfg.MaxConcurrent))
	}
	return &Scanner{driver: driver, transport: transport, sem: sem, cfg: cfg, logger: logger}
}

// BuildBase boots a guest, warms it, and snapshots it into the warm base bundle. It
// is best-effort: a failure leaves the scanner with no base, so scans cold-boot
// until a base is built. Safe to call concurrently; only one build runs at a time.
func (s *Scanner) BuildBase(ctx context.Context) error {
	if !s.cfg.WarmBase {
		return nil
	}
	s.baseMu.Lock()
	if s.baseBuilding || s.baseRef.ID != "" {
		s.baseMu.Unlock()
		return nil
	}
	s.baseBuilding = true
	s.baseMu.Unlock()
	defer func() {
		s.baseMu.Lock()
		s.baseBuilding = false
		s.baseMu.Unlock()
	}()

	// The build guest counts against the concurrency cap, so K*GuestMemMib stays a
	// true bound on microVM memory even while a (re)build runs alongside scans.
	if s.sem != nil {
		if err := s.sem.Acquire(ctx, 1); err != nil {
			return fmt.Errorf("base build: acquire slot: %w", err)
		}
		defer s.sem.Release(1)
	}

	start := time.Now()
	h, err := s.driver.Claim(ctx, substrate.ClaimSpec{Arch: s.cfg.Arch})
	if err != nil {
		return fmt.Errorf("base build: cold boot: %w", err)
	}
	// Always discard the build VM (the base lives in the snapshot bundle, not this
	// live VM), even on error and even if the request ctx was cancelled.
	defer s.discard(h)

	readyCtx, cancel := context.WithTimeout(ctx, s.cfg.BootReadyTimeout)
	defer cancel()
	if err := s.transport.WaitReady(readyCtx, s.driver.VsockUDSPath(h.ThreadID)); err != nil {
		return fmt.Errorf("base build: guest readiness: %w", err)
	}
	ref, err := s.driver.SnapshotBase(ctx, h, s.cfg.BaseKey)
	if err != nil {
		return fmt.Errorf("base build: snapshot: %w", err)
	}
	s.baseMu.Lock()
	s.baseRef = ref
	s.baseGen++
	s.baseMu.Unlock()
	s.logger.Info("warm base built", "key", s.cfg.BaseKey, "size_mb", ref.SizeBytes/(1<<20), "took", time.Since(start))
	return nil
}

// discard releases a microVM and removes its on-disk bundle. Called from a defer on
// every guest the scanner claims so per-scan bundle dirs (and their vsock sockets)
// do not accumulate on the nvme disk. Best-effort: failures are logged, not fatal.
// context.Background() so cleanup runs even when the request ctx was cancelled.
func (s *Scanner) discard(h substrate.Handle) {
	if rErr := s.driver.Release(context.Background(), h); rErr != nil {
		s.logger.Warn("scanner: release guest", "thread", h.ThreadID, "err", rErr)
	}
	if rErr := s.driver.RemoveBundle(h.ThreadID); rErr != nil {
		s.logger.Warn("scanner: remove guest bundle", "thread", h.ThreadID, "err", rErr)
	}
}

// currentBase returns the warm base ref, the build generation it came from, and
// whether it is usable. The generation lets invalidateBase avoid clobbering a base
// that a concurrent rebuild already replaced (the ref ID is a constant base key, so
// it cannot distinguish generations on its own).
func (s *Scanner) currentBase() (substrate.SnapshotRef, uint64, bool) {
	s.baseMu.Lock()
	defer s.baseMu.Unlock()
	return s.baseRef, s.baseGen, s.baseRef.ID != ""
}

// invalidateBase clears the warm base (e.g. after a restore failure) and kicks an
// asynchronous rebuild so subsequent scans get the fast path back. gen is the
// generation observed when the failing base was taken; the clear is skipped if a
// rebuild has since advanced the generation, so a late failure does not wipe a base
// another goroutine already replaced.
func (s *Scanner) invalidateBase(gen uint64) {
	s.baseMu.Lock()
	stale := s.baseGen == gen && s.baseRef.ID != ""
	if stale {
		s.baseRef = substrate.SnapshotRef{}
	}
	s.baseMu.Unlock()
	if !stale {
		return
	}
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), s.cfg.BootReadyTimeout+s.cfg.ScanTimeout)
		defer cancel()
		if err := s.BuildBase(ctx); err != nil {
			s.logger.Warn("scanner: warm base rebuild failed", "err", err)
		}
	}()
}

// Scan runs one scan. It restores from the warm base when available (fast path) and
// otherwise cold-boots a guest (fallback). A boot/restore/readiness failure returns
// a *GuestUnavailableError (the HTTP layer's 503); a scan-leg failure is folded into
// ScanResult.Errors with a nil error (the HTTP layer's 200).
func (s *Scanner) Scan(ctx context.Context, files []vsockproto.ScanFile) (vsockproto.ScanResult, error) {
	// Cap concurrency before spending a boot/restore, so at most K guests are ever
	// live and K*GuestMemMib bounds the microVM memory in our cgroup.
	if s.sem != nil {
		if err := s.sem.Acquire(ctx, 1); err != nil {
			return vsockproto.ScanResult{}, &GuestUnavailableError{Err: fmt.Errorf("acquire scan slot: %w", err)}
		}
		defer s.sem.Release(1)
	}

	if base, gen, ok := s.currentBase(); ok {
		res, err := s.scanWarm(ctx, base, files)
		if err == nil {
			return res, nil
		}
		// Any warm-path failure — a restore that failed OR a scan that could not
		// reach the restored guest (a base that restores but whose semgrep is dead)
		// — invalidates the base and falls back to a cold boot, so a bad base never
		// wedges the service. Only a cold-boot scan-leg failure is returned as data.
		s.logger.Warn("scanner: warm path failed; falling back to cold boot", "err", err)
		s.invalidateBase(gen)
	}
	return s.scanCold(ctx, files)
}

// scanWarm restores a guest from the warm base, primes away the Firecracker vsock
// RX-queue race, and runs the scan WITHOUT a readiness wait (a restored guest
// resumes past its readiness announce, so it never re-sends a Hello).
func (s *Scanner) scanWarm(ctx context.Context, base substrate.SnapshotRef, files []vsockproto.ScanFile) (vsockproto.ScanResult, error) {
	h, err := s.driver.Claim(ctx, substrate.ClaimSpec{Arch: s.cfg.Arch, BaseSnapshotRef: base})
	if err != nil {
		return vsockproto.ScanResult{}, &GuestUnavailableError{Err: fmt.Errorf("restore guest: %w", err)}
	}
	defer s.discard(h)

	uds := s.driver.VsockUDSPath(h.ThreadID)

	// Prime: the first post-restore vsock connection hits Firecracker's RX-queue
	// race and is reset or hung. Send a throwaway empty scan with a short deadline to
	// absorb it; whether it EOFs fast or times out, the next connection is clean.
	primeCtx, primeCancel := context.WithTimeout(ctx, s.cfg.RestorePrime)
	_, _ = s.transport.Scan(primeCtx, uds, vsockproto.ScanRequest{})
	primeCancel()

	scanCtx, cancelScan := context.WithTimeout(ctx, s.cfg.ScanTimeout)
	defer cancelScan()
	res, err := s.transport.Scan(scanCtx, uds, vsockproto.ScanRequest{Files: files})
	if err != nil {
		// Unlike the cold path, a restored guest never proved it warmed (there is no
		// readiness handshake on restore), so a scan-leg failure here may mean the
		// base restores but its semgrep is dead. Surface it as unavailable so Scan
		// invalidates the base and retries on a cold boot rather than wedging.
		return vsockproto.ScanResult{}, &GuestUnavailableError{Err: fmt.Errorf("warm scan: %w", err)}
	}
	return res, nil
}

// scanCold boots a fresh guest, waits for it to warm, scans, and releases it. This
// is the fallback when no warm base is available; it is the pre-snapshot behaviour.
func (s *Scanner) scanCold(ctx context.Context, files []vsockproto.ScanFile) (vsockproto.ScanResult, error) {
	h, err := s.driver.Claim(ctx, substrate.ClaimSpec{Arch: s.cfg.Arch})
	if err != nil {
		return vsockproto.ScanResult{}, &GuestUnavailableError{Err: fmt.Errorf("claim guest: %w", err)}
	}
	defer s.discard(h)

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
		return vsockproto.ScanResult{Errors: []string{err.Error()}}, nil
	}
	return res, nil
}
