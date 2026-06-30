package scanner

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/firecracker/substrate/substrate"
	"github.com/jomcgi/homelab/projects/firecracker/substrate/vsockproto"
)

// fakeDriver records claim/release activity and tracks peak concurrency so the
// semaphore cap is observable.
type fakeDriver struct {
	mu            sync.Mutex
	claims        int
	releases      int
	live          int
	peakLive      int
	claimErr      error
	snapErr       error
	failBaseClaim bool // fail claims that carry a BaseSnapshotRef (restore failures)
	baseClaims    int  // claims that carried a BaseSnapshotRef (restores)
	coldClaims    int  // claims without a base (cold boots)
	snapshots     int
	removed       int
	idCounter     int32
}

func (f *fakeDriver) Claim(_ context.Context, spec substrate.ClaimSpec) (substrate.Handle, error) {
	if f.claimErr != nil {
		return substrate.Handle{}, f.claimErr
	}
	if spec.BaseSnapshotRef.ID != "" && f.failBaseClaim {
		return substrate.Handle{}, errors.New("fake: restore failed")
	}
	f.mu.Lock()
	f.claims++
	if spec.BaseSnapshotRef.ID != "" {
		f.baseClaims++
	} else {
		f.coldClaims++
	}
	f.live++
	if f.live > f.peakLive {
		f.peakLive = f.live
	}
	f.mu.Unlock()
	id := atomic.AddInt32(&f.idCounter, 1)
	return substrate.Handle{ThreadID: fmt.Sprintf("thread-%d", id)}, nil
}

func (f *fakeDriver) SnapshotBase(_ context.Context, _ substrate.Handle, key string) (substrate.SnapshotRef, error) {
	if f.snapErr != nil {
		return substrate.SnapshotRef{}, f.snapErr
	}
	f.mu.Lock()
	f.snapshots++
	f.mu.Unlock()
	return substrate.SnapshotRef{ID: key}, nil
}

func (f *fakeDriver) Release(_ context.Context, _ substrate.Handle) error {
	f.mu.Lock()
	f.releases++
	f.live--
	f.mu.Unlock()
	return nil
}

func (f *fakeDriver) RemoveBundle(_ string) error {
	f.mu.Lock()
	f.removed++
	f.mu.Unlock()
	return nil
}

func (f *fakeDriver) VsockUDSPath(threadID string) string { return "/tmp/" + threadID }

func (f *fakeDriver) snapshot() (claims, releases, peak int) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.claims, f.releases, f.peakLive
}

// fakeTransport returns canned readiness/scan outcomes. started/release let a
// test gate the scan leg to observe concurrency; blockReady simulates a guest
// that never announces readiness (so WaitReady runs out the boot deadline).
type fakeTransport struct {
	blockReady bool
	res        vsockproto.ScanResult
	scanErr    error

	started chan struct{}
	release chan struct{}
}

func (t *fakeTransport) WaitReady(ctx context.Context, _ string) error {
	if t.blockReady {
		<-ctx.Done()
		return fmt.Errorf("fake: guest never announced readiness: %w", ctx.Err())
	}
	return nil
}

func (t *fakeTransport) Scan(_ context.Context, _ string, _ vsockproto.ScanRequest) (vsockproto.ScanResult, error) {
	if t.started != nil {
		t.started <- struct{}{}
	}
	if t.release != nil {
		<-t.release
	}
	return t.res, t.scanErr
}

func testCfg() Config {
	return Config{MaxConcurrent: 4, BootReadyTimeout: time.Second, ScanTimeout: time.Second}
}

func TestScanSuccessReturnsFindings(t *testing.T) {
	d := &fakeDriver{}
	tr := &fakeTransport{res: vsockproto.ScanResult{
		Findings: []vsockproto.Finding{{Path: "a.py", RuleID: "r1"}},
	}}
	s := New(d, tr, testCfg(), nil)

	res, err := s.Scan(context.Background(), []vsockproto.ScanFile{{Path: "a.py", Content: "x=1"}})
	if err != nil {
		t.Fatalf("Scan() error = %v", err)
	}
	if len(res.Findings) != 1 || res.Findings[0].RuleID != "r1" {
		t.Errorf("findings = %+v, want one r1", res.Findings)
	}
	claims, releases, _ := d.snapshot()
	if claims != 1 || releases != 1 {
		t.Errorf("claims=%d releases=%d, want 1/1 (guest claimed then released)", claims, releases)
	}
}

func TestBootTimeoutSurfacesAndReleases(t *testing.T) {
	d := &fakeDriver{}
	tr := &fakeTransport{blockReady: true}
	cfg := testCfg()
	cfg.BootReadyTimeout = 20 * time.Millisecond
	s := New(d, tr, cfg, nil)

	_, err := s.Scan(context.Background(), []vsockproto.ScanFile{{Path: "a.py"}})
	var gu *GuestUnavailableError
	if !errors.As(err, &gu) {
		t.Fatalf("error = %v, want *GuestUnavailableError on boot timeout", err)
	}
	if !gu.GuestUnavailable() {
		t.Error("GuestUnavailable() = false, want true")
	}
	// Even though boot failed, the guest must still be released.
	claims, releases, _ := d.snapshot()
	if claims != 1 || releases != 1 {
		t.Errorf("claims=%d releases=%d, want 1/1 (released despite boot failure)", claims, releases)
	}
}

func TestClaimFailureIsGuestUnavailable(t *testing.T) {
	d := &fakeDriver{claimErr: errors.New("no KVM")}
	s := New(d, &fakeTransport{}, testCfg(), nil)

	_, err := s.Scan(context.Background(), []vsockproto.ScanFile{{Path: "a.py"}})
	var gu *GuestUnavailableError
	if !errors.As(err, &gu) {
		t.Fatalf("error = %v, want *GuestUnavailableError when Claim fails", err)
	}
}

func TestScanErrorSurfacesInResultErrors(t *testing.T) {
	d := &fakeDriver{}
	tr := &fakeTransport{scanErr: errors.New("dial reset")}
	s := New(d, tr, testCfg(), nil)

	res, err := s.Scan(context.Background(), []vsockproto.ScanFile{{Path: "a.py"}})
	if err != nil {
		t.Fatalf("Scan() error = %v, want nil (scan failure is data)", err)
	}
	if len(res.Errors) != 1 || res.Errors[0] != "dial reset" {
		t.Errorf("Errors = %v, want the scan error surfaced", res.Errors)
	}
	// The guest is still released.
	if _, releases, _ := d.snapshot(); releases != 1 {
		t.Errorf("releases = %d, want 1", releases)
	}
}

func TestWarmBaseRestoreScans(t *testing.T) {
	d := &fakeDriver{}
	tr := &fakeTransport{res: vsockproto.ScanResult{
		Findings: []vsockproto.Finding{{Path: "a.py", RuleID: "r1"}},
	}}
	cfg := testCfg()
	cfg.WarmBase = true
	cfg.BaseKey = "k"
	cfg.RestorePrime = time.Millisecond
	s := New(d, tr, cfg, nil)

	if err := s.BuildBase(context.Background()); err != nil {
		t.Fatalf("BuildBase() error = %v", err)
	}
	res, err := s.Scan(context.Background(), []vsockproto.ScanFile{{Path: "a.py", Content: "x=1"}})
	if err != nil {
		t.Fatalf("Scan() error = %v", err)
	}
	if len(res.Findings) != 1 || res.Findings[0].RuleID != "r1" {
		t.Errorf("findings = %+v, want one r1", res.Findings)
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.snapshots != 1 {
		t.Errorf("snapshots = %d, want 1 (one base built)", d.snapshots)
	}
	if d.baseClaims != 1 {
		t.Errorf("baseClaims = %d, want 1 (scan restored from the base)", d.baseClaims)
	}
	if d.coldClaims != 1 {
		t.Errorf("coldClaims = %d, want 1 (only the base build cold-booted)", d.coldClaims)
	}
	if d.releases != 2 || d.removed != 2 {
		t.Errorf("releases=%d removed=%d, want 2/2 (base-build + scan guests each released and cleaned up)", d.releases, d.removed)
	}
}

func TestScanFansOutAcrossFiles(t *testing.T) {
	d := &fakeDriver{}
	tr := &fakeTransport{res: vsockproto.ScanResult{
		Findings: []vsockproto.Finding{{Path: "f", RuleID: "r1"}},
	}}
	cfg := testCfg()
	cfg.WarmBase = true
	cfg.BaseKey = "k"
	cfg.RestorePrime = time.Millisecond
	s := New(d, tr, cfg, nil)
	if err := s.BuildBase(context.Background()); err != nil {
		t.Fatalf("BuildBase() error = %v", err)
	}

	files := []vsockproto.ScanFile{{Path: "a.py"}, {Path: "b.py"}, {Path: "c.py"}}
	res, err := s.Scan(context.Background(), files)
	if err != nil {
		t.Fatalf("Scan() error = %v", err)
	}
	// Each of the 3 files scans on its own restored guest; their findings merge.
	if len(res.Findings) != 3 {
		t.Errorf("findings = %d, want 3 (one per file, merged across guests)", len(res.Findings))
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.baseClaims != 3 {
		t.Errorf("baseClaims = %d, want 3 (each file restored its own guest)", d.baseClaims)
	}
	if d.removed != 4 {
		t.Errorf("removed = %d, want 4 (base-build guest + 3 per-file guests cleaned up)", d.removed)
	}
}

func TestScanFanOutAllUnavailableIs503(t *testing.T) {
	// Every Claim fails: every per-file scan is guest-unavailable, so the whole
	// batch must surface a *GuestUnavailableError (the HTTP 503), not a 200.
	d := &fakeDriver{claimErr: errors.New("no kvm")}
	s := New(d, &fakeTransport{}, testCfg(), nil)

	_, err := s.Scan(context.Background(), []vsockproto.ScanFile{{Path: "a.py"}, {Path: "b.py"}})
	var gu *GuestUnavailableError
	if !errors.As(err, &gu) {
		t.Fatalf("error = %v, want *GuestUnavailableError when every file fails to get a guest", err)
	}
}

func TestWarmBaseRestoreFailsOverToColdBoot(t *testing.T) {
	d := &fakeDriver{}
	tr := &fakeTransport{res: vsockproto.ScanResult{
		Findings: []vsockproto.Finding{{Path: "a.py", RuleID: "r1"}},
	}}
	cfg := testCfg()
	cfg.WarmBase = true
	cfg.BaseKey = "k"
	cfg.RestorePrime = time.Millisecond
	s := New(d, tr, cfg, nil)

	// Seed a base ref directly, then make restores (base claims) fail so the scan
	// must fall back to a cold boot.
	s.baseRef = substrate.SnapshotRef{ID: "k"}
	d.failBaseClaim = true

	res, err := s.Scan(context.Background(), []vsockproto.ScanFile{{Path: "a.py"}})
	if err != nil {
		t.Fatalf("Scan() error = %v, want nil (cold-boot fallback succeeds)", err)
	}
	if len(res.Findings) != 1 {
		t.Errorf("findings = %+v, want the cold-boot scan to return r1", res.Findings)
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.coldClaims < 1 {
		t.Errorf("coldClaims = %d, want >=1 (fell back to cold boot)", d.coldClaims)
	}
}

func TestSemaphoreCapsConcurrency(t *testing.T) {
	d := &fakeDriver{}
	tr := &fakeTransport{
		started: make(chan struct{}, 2),
		release: make(chan struct{}),
	}
	cfg := testCfg()
	cfg.MaxConcurrent = 1
	s := New(d, tr, cfg, nil)

	go func() { _, _ = s.Scan(context.Background(), []vsockproto.ScanFile{{Path: "a.py"}}) }()
	go func() { _, _ = s.Scan(context.Background(), []vsockproto.ScanFile{{Path: "b.py"}}) }()

	// First scan reaches the gated Scan leg; the second must be blocked at the
	// semaphore before it can Claim.
	<-tr.started
	// Give the second goroutine a chance to (wrongly) proceed past the semaphore.
	time.Sleep(20 * time.Millisecond)
	if claims, _, peak := d.snapshot(); claims != 1 || peak != 1 {
		t.Fatalf("claims=%d peak=%d while one scan is in flight, want 1/1 (cap=1)", claims, peak)
	}

	// Release the first scan; the second proceeds.
	tr.release <- struct{}{}
	<-tr.started
	tr.release <- struct{}{}

	// Both scans ran, but never concurrently.
	deadline := time.Now().Add(time.Second)
	for {
		claims, releases, peak := d.snapshot()
		if claims == 2 && releases == 2 {
			if peak != 1 {
				t.Errorf("peak live = %d, want 1 (semaphore cap=1)", peak)
			}
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("timed out: claims=%d releases=%d", claims, releases)
		}
		time.Sleep(5 * time.Millisecond)
	}
}
