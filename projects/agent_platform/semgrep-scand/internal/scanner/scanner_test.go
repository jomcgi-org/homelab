package scanner

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/agent_platform/substrate"
	"github.com/jomcgi/homelab/projects/agent_platform/vsockproto"
)

// fakeDriver records claim/release activity and tracks peak concurrency so the
// semaphore cap is observable.
type fakeDriver struct {
	mu        sync.Mutex
	claims    int
	releases  int
	live      int
	peakLive  int
	claimErr  error
	idCounter int32
}

func (f *fakeDriver) Claim(_ context.Context, _ substrate.ClaimSpec) (substrate.Handle, error) {
	if f.claimErr != nil {
		return substrate.Handle{}, f.claimErr
	}
	f.mu.Lock()
	f.claims++
	f.live++
	if f.live > f.peakLive {
		f.peakLive = f.live
	}
	f.mu.Unlock()
	id := atomic.AddInt32(&f.idCounter, 1)
	return substrate.Handle{ThreadID: fmt.Sprintf("thread-%d", id)}, nil
}

func (f *fakeDriver) Release(_ context.Context, _ substrate.Handle) error {
	f.mu.Lock()
	f.releases++
	f.live--
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
