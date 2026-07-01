package invoker

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/firecracker/substrate/invoke/internal/config"
	"github.com/jomcgi/homelab/projects/firecracker/substrate/substrate"
)

// fakeDriver records every call made to it and returns canned values. All
// fields are safe to read only after the goroutine that owns the Invoker has
// finished (e.g. after Invoke returns). For concurrent tests, access recorded
// slices only after synchronising via channels or WaitGroups.
type fakeDriver struct {
	mu sync.Mutex

	// Canned return values. Defaults produce a successful cold boot.
	claimErr    error
	snapshotRef substrate.SnapshotRef // ID defaults to cfg.BaseKey when empty
	snapshotErr error

	// Recorded calls (append-only; read after test goroutine finishes).
	claimSpecs       []substrate.ClaimSpec
	releaseHandles   []substrate.Handle
	removedBundles   []string
	snapshotBaseKeys []string

	seq int // monotonic counter for generated threadIDs
}

func (f *fakeDriver) Claim(_ context.Context, spec substrate.ClaimSpec) (substrate.Handle, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.claimSpecs = append(f.claimSpecs, spec)
	if f.claimErr != nil {
		return substrate.Handle{}, f.claimErr
	}
	tid := spec.ThreadID
	if tid == "" {
		f.seq++
		tid = fmt.Sprintf("fake-thread-%d", f.seq)
	}
	return substrate.Handle{ThreadID: tid, ID: "vm-" + tid}, nil
}

func (f *fakeDriver) SnapshotBase(_ context.Context, h substrate.Handle, baseKey string) (substrate.SnapshotRef, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.snapshotBaseKeys = append(f.snapshotBaseKeys, baseKey)
	if f.snapshotErr != nil {
		return substrate.SnapshotRef{}, f.snapshotErr
	}
	ref := f.snapshotRef
	if ref.ID == "" {
		ref.ID = baseKey
	}
	_ = h
	return ref, nil
}

func (f *fakeDriver) Release(_ context.Context, h substrate.Handle) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.releaseHandles = append(f.releaseHandles, h)
	return nil
}

func (f *fakeDriver) RemoveBundle(threadID string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.removedBundles = append(f.removedBundles, threadID)
	return nil
}

func (f *fakeDriver) VsockUDSPath(threadID string) string {
	return "/fake/" + threadID + "/vsock.sock"
}

// claimCount returns the number of Claim calls recorded so far.
func (f *fakeDriver) claimCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.claimSpecs)
}

// releaseCount returns the number of Release calls recorded so far.
func (f *fakeDriver) releaseCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.releaseHandles)
}

// removeBundleCount returns the number of RemoveBundle calls recorded so far.
func (f *fakeDriver) removeBundleCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.removedBundles)
}

// lastClaimSpec returns the most recently recorded ClaimSpec, or zero if none.
func (f *fakeDriver) lastClaimSpec() substrate.ClaimSpec {
	f.mu.Lock()
	defer f.mu.Unlock()
	if len(f.claimSpecs) == 0 {
		return substrate.ClaimSpec{}
	}
	return f.claimSpecs[len(f.claimSpecs)-1]
}

// funcTransport implements the transport interface via function fields so each
// test can inject custom behaviour via closures without defining a new type.
// Nil fields are treated as no-ops that succeed.
type funcTransport struct {
	waitReadyFn func(ctx context.Context, udsPath, readyPath string) error
	roundTripFn func(ctx context.Context, udsPath string, req *http.Request) (*http.Response, error)
}

func (f *funcTransport) WaitReady(ctx context.Context, udsPath, readyPath string) error {
	if f.waitReadyFn != nil {
		return f.waitReadyFn(ctx, udsPath, readyPath) // nosemgrep: no-bare-error-return
	}
	return nil
}

func (f *funcTransport) RoundTrip(ctx context.Context, udsPath string, req *http.Request) (*http.Response, error) {
	if f.roundTripFn != nil {
		return f.roundTripFn(ctx, udsPath, req)
	}
	return okResponse("ok"), nil
}

// okResponse returns a minimal 200 response with the given body text.
func okResponse(body string) *http.Response {
	return &http.Response{
		StatusCode: http.StatusOK,
		Body:       io.NopCloser(strings.NewReader(body)),
	}
}

// defaultConfig returns a Config suitable for most tests: WarmBase enabled,
// short timeouts, concurrency of 4.
func defaultConfig() Config {
	return Config{
		Workload: config.Workload{
			WarmBase:       true,
			Concurrency:    4,
			ReadyPath:      "/shim/ready",
			RequestTimeout: 5 * time.Second,
		},
		BaseKey:          "test-base",
		Arch:             "amd64",
		BootReadyTimeout: 5 * time.Second,
	}
}

// TestInvokeHappyPathRestoresAndReleases verifies that after BuildBase an
// Invoke restores from the stored base (ClaimSpec carries the baseRef),
// completes the round-trip, and cleans up exactly once (one Release, one
// RemoveBundle).
func TestInvokeHappyPathRestoresAndReleases(t *testing.T) {
	drv := &fakeDriver{}
	tr := &funcTransport{}
	inv := New(drv, tr, defaultConfig(), nil)

	if err := inv.BuildBase(context.Background()); err != nil {
		t.Fatalf("BuildBase: %v", err)
	}

	resp, err := inv.Invoke(context.Background(), "sess-1", strings.NewReader("body"))
	if err != nil {
		t.Fatalf("Invoke: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Errorf("status = %d, want 200", resp.StatusCode)
	}

	// The last Claim (for the Invoke, not the BuildBase) must carry the baseRef.
	last := drv.lastClaimSpec()
	if last.BaseSnapshotRef.ID == "" {
		t.Error("expected Invoke to restore from warm base (BaseSnapshotRef.ID non-empty)")
	}
	if last.BaseSnapshotRef.ID != "test-base" {
		t.Errorf("BaseSnapshotRef.ID = %q, want %q", last.BaseSnapshotRef.ID, "test-base")
	}

	// BuildBase claimed one VM; Invoke claimed one more. Release and RemoveBundle
	// must each be called exactly twice (once per claimed VM).
	if got := drv.releaseCount(); got != 2 {
		t.Errorf("Release called %d time(s), want 2 (BuildBase VM + Invoke VM)", got)
	}
	if got := drv.removeBundleCount(); got != 2 {
		t.Errorf("RemoveBundle called %d time(s), want 2", got)
	}
}

// TestInvokeReleasesVMOnTransportError verifies that when RoundTrip fails the
// VM is still Released and RemoveBundle is still called (defer fires), and that
// the returned error is NOT a *GuestUnavailableError (it is a raw 502 error).
func TestInvokeReleasesVMOnTransportError(t *testing.T) {
	drv := &fakeDriver{}
	transportErr := errors.New("transport: connection refused")
	tr := &funcTransport{
		roundTripFn: func(_ context.Context, _ string, _ *http.Request) (*http.Response, error) {
			return nil, transportErr
		},
	}
	cfg := defaultConfig()
	cfg.Workload.WarmBase = false // cold boot only; simpler for this test
	inv := New(drv, tr, cfg, nil)

	_, err := inv.Invoke(context.Background(), "sess-rt-err", strings.NewReader("body"))
	if err == nil {
		t.Fatal("expected error from Invoke, got nil")
	}
	// The error must NOT be a GuestUnavailableError: the VM ran.
	var gue *GuestUnavailableError
	if errors.As(err, &gue) {
		t.Errorf("error is GuestUnavailableError, want raw transport error")
	}
	if !errors.Is(err, transportErr) {
		t.Errorf("error = %v, want to wrap transportErr", err)
	}

	// The VM must still be released even though RoundTrip failed.
	if got := drv.releaseCount(); got != 1 {
		t.Errorf("Release called %d time(s), want 1", got)
	}
	if got := drv.removeBundleCount(); got != 1 {
		t.Errorf("RemoveBundle called %d time(s), want 1", got)
	}
}

// TestInvokeReadinessFailureIsGuestUnavailable verifies that a WaitReady
// failure maps to *GuestUnavailableError (HTTP 503) and that the VM is still
// released via the defer in claimInvoke.
func TestInvokeReadinessFailureIsGuestUnavailable(t *testing.T) {
	drv := &fakeDriver{}
	readyErr := errors.New("timed out waiting for guest ready")
	tr := &funcTransport{
		waitReadyFn: func(_ context.Context, _, _ string) error {
			return readyErr
		},
	}
	cfg := defaultConfig()
	cfg.Workload.WarmBase = false
	inv := New(drv, tr, cfg, nil)

	_, err := inv.Invoke(context.Background(), "sess-ready-fail", strings.NewReader("body"))
	if err == nil {
		t.Fatal("expected error from Invoke, got nil")
	}

	// Must satisfy the GuestUnavailable contract.
	type unavailable interface{ GuestUnavailable() bool }
	u, ok := err.(unavailable)
	if !ok || !u.GuestUnavailable() {
		t.Errorf("error %v does not satisfy GuestUnavailable() bool", err)
	}

	// The VM must be released even on a readiness failure.
	if got := drv.releaseCount(); got != 1 {
		t.Errorf("Release called %d time(s), want 1", got)
	}
	if got := drv.removeBundleCount(); got != 1 {
		t.Errorf("RemoveBundle called %d time(s), want 1", got)
	}
}

// TestInvokeConcurrencyCap verifies that with Concurrency=1 a second
// concurrent Invoke cannot proceed while the first holds the semaphore. The
// second call uses an already-cancelled context and must return
// *GuestUnavailableError without ever reaching the driver or transport.
func TestInvokeConcurrencyCap(t *testing.T) {
	drv := &fakeDriver{}

	// entered is closed (once) when the first Invoke enters RoundTrip.
	entered := make(chan struct{})
	var enteredOnce sync.Once
	// unblock is closed to let the first Invoke's RoundTrip return.
	unblock := make(chan struct{})

	tr := &funcTransport{
		roundTripFn: func(ctx context.Context, _ string, _ *http.Request) (*http.Response, error) {
			enteredOnce.Do(func() { close(entered) })
			select {
			case <-unblock:
				return okResponse("done"), nil
			case <-ctx.Done():
				return nil, ctx.Err()
			}
		},
	}

	cfg := defaultConfig()
	cfg.Workload.WarmBase = false
	cfg.Workload.Concurrency = 1
	inv := New(drv, tr, cfg, nil)

	// Launch the first Invoke in a goroutine; it will block inside RoundTrip.
	g1Resp := make(chan *http.Response, 1)
	g1Err := make(chan error, 1)
	go func() {
		resp, err := inv.Invoke(context.Background(), "s1", strings.NewReader("body"))
		g1Resp <- resp
		g1Err <- err
	}()

	// Wait until the first Invoke is inside RoundTrip (semaphore slot held).
	select {
	case <-entered:
	case <-time.After(3 * time.Second):
		t.Fatal("timed out waiting for first Invoke to enter RoundTrip")
	}

	// The second Invoke uses an already-cancelled context so the semaphore
	// Acquire returns immediately with an error.
	cancelledCtx, cancel := context.WithCancel(context.Background())
	cancel()
	_, err2 := inv.Invoke(cancelledCtx, "s2", strings.NewReader("body"))
	if err2 == nil {
		t.Fatal("expected error from second Invoke (concurrency cap), got nil")
	}
	var gue *GuestUnavailableError
	if !errors.As(err2, &gue) {
		t.Errorf("second Invoke error = %v, want *GuestUnavailableError", err2)
	}

	// The second Invoke must not have touched the driver (no extra Claim).
	if c := drv.claimCount(); c != 1 {
		t.Errorf("Claim called %d time(s), want 1 (second Invoke should not reach driver)", c)
	}

	// Release the first Invoke and verify it succeeded.
	close(unblock)
	if err := <-g1Err; err != nil {
		t.Errorf("first Invoke returned error: %v", err)
	}
	if r := <-g1Resp; r != nil {
		r.Body.Close()
	}
}

// TestInvokeColdFallbackWhenNoBase verifies that when no base has been built,
// Invoke calls Claim with an empty BaseSnapshotRef (cold boot) and still
// returns a successful response.
func TestInvokeColdFallbackWhenNoBase(t *testing.T) {
	drv := &fakeDriver{}
	tr := &funcTransport{}
	cfg := defaultConfig()
	// WarmBase enabled, but BuildBase never called, so baseRef stays zero.
	inv := New(drv, tr, cfg, nil)

	resp, err := inv.Invoke(context.Background(), "sess-cold", strings.NewReader("body"))
	if err != nil {
		t.Fatalf("Invoke: %v", err)
	}
	defer resp.Body.Close()

	// The single Claim must have been called with an empty BaseSnapshotRef.
	if c := drv.claimCount(); c != 1 {
		t.Errorf("Claim called %d time(s), want 1", c)
	}
	spec := drv.lastClaimSpec()
	if spec.BaseSnapshotRef.ID != "" {
		t.Errorf("BaseSnapshotRef.ID = %q, want empty (cold boot)", spec.BaseSnapshotRef.ID)
	}
}

// TestBuildBaseStoresRef verifies that BuildBase boots a VM, snapshots it, and
// stores the resulting ref so that a subsequent Invoke restores from it
// (BaseSnapshotRef on the Claim spec is the stored base ID).
func TestBuildBaseStoresRef(t *testing.T) {
	drv := &fakeDriver{}
	tr := &funcTransport{}
	inv := New(drv, tr, defaultConfig(), nil)

	if err := inv.BuildBase(context.Background()); err != nil {
		t.Fatalf("BuildBase: %v", err)
	}

	// After BuildBase the internal base ref must be set.
	ref, _, ok := inv.currentBase()
	if !ok {
		t.Fatal("currentBase returned ok=false after BuildBase")
	}
	if ref.ID == "" {
		t.Fatal("currentBase ref.ID is empty after BuildBase")
	}

	// A subsequent Invoke must restore from that ref.
	resp, err := inv.Invoke(context.Background(), "sess-base", strings.NewReader("body"))
	if err != nil {
		t.Fatalf("Invoke after BuildBase: %v", err)
	}
	defer resp.Body.Close()

	// The Invoke's Claim (second claim overall) must carry the stored base ref.
	drv.mu.Lock()
	invokeSpec := drv.claimSpecs[len(drv.claimSpecs)-1]
	drv.mu.Unlock()

	if invokeSpec.BaseSnapshotRef.ID != ref.ID {
		t.Errorf("Invoke ClaimSpec.BaseSnapshotRef.ID = %q, want %q", invokeSpec.BaseSnapshotRef.ID, ref.ID)
	}
}
