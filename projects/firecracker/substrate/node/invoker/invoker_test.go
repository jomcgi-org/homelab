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

	statsCalls        int  // number of Stats calls
	statsAfterRelease bool // set if Stats was called after Release for a handle (a bug)

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

func (f *fakeDriver) Stats(h substrate.Handle) (substrate.GuestStats, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.statsCalls++
	// Stats must be sampled while the guest is alive, i.e. BEFORE Release. Flag a
	// violation if this handle was already released.
	for _, r := range f.releaseHandles {
		if r.ID == h.ID {
			f.statsAfterRelease = true
		}
	}
	return substrate.GuestStats{CPUMillis: 20, PeakRSSMib: 128}, nil
}

// statsCount returns the number of Stats calls recorded so far.
func (f *fakeDriver) statsCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.statsCalls
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

// isReleased reports whether any VM has been Released. Tests that claim a
// single VM use it to prove the guest is still alive while the body streams.
func (f *fakeDriver) isReleased() bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.releaseHandles) > 0
}

// waitForRemoveBundleCount polls until the fake driver has recorded want
// RemoveBundle calls, failing the test after a short timeout. On the success
// path RemoveBundle runs asynchronously off the freed concurrency slot (disk
// cleanup no longer gates the memory slot), so reading the count directly after
// body Close would race the cleanup goroutine.
func waitForRemoveBundleCount(t *testing.T, drv *fakeDriver, want int) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for {
		if got := drv.removeBundleCount(); got == want {
			return
		}
		if time.Now().After(deadline) {
			t.Errorf("RemoveBundle called %d time(s), want %d", drv.removeBundleCount(), want)
			return
		}
		time.Sleep(time.Millisecond)
	}
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
	primeFn     func(ctx context.Context, udsPath string) error
}

func (f *funcTransport) WaitReady(ctx context.Context, udsPath, readyPath string) error {
	if f.waitReadyFn != nil {
		return f.waitReadyFn(ctx, udsPath, readyPath) // nosemgrep: no-bare-error-return
	}
	return nil
}

func (f *funcTransport) Prime(ctx context.Context, udsPath string) error {
	if f.primeFn != nil {
		return f.primeFn(ctx, udsPath) // nosemgrep: no-bare-error-return
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
		Workload: substrate.Workload{
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

	// The Invoke VM's cleanup is transferred to the response body's Close, so
	// only the BuildBase VM has been released so far (1), not the Invoke VM.
	if got := drv.releaseCount(); got != 1 {
		t.Errorf("Release called %d time(s) before body close, want 1 (BuildBase VM only)", got)
	}

	// Closing the body tears down the Invoke VM: now both VMs are released and
	// their bundles removed (BuildBase VM + Invoke VM).
	if err := resp.Body.Close(); err != nil {
		t.Errorf("resp.Body.Close: %v", err)
	}
	if got := drv.releaseCount(); got != 2 {
		t.Errorf("Release called %d time(s) after body close, want 2 (BuildBase VM + Invoke VM)", got)
	}
	// The BuildBase VM's bundle was removed synchronously; the Invoke VM's runs
	// asynchronously off the freed slot, so wait for both to land.
	waitForRemoveBundleCount(t, drv, 2)
}

// TestInvokeWarmPathPrimesVsock asserts the warm restore path shakes out the
// Firecracker post-restore vsock RX-queue race by calling transport.Prime before
// the readiness wait. Prime is invoked synchronously on the Invoke goroutine, so
// a plain counter is race-free here.
func TestInvokeWarmPathPrimesVsock(t *testing.T) {
	drv := &fakeDriver{}
	primeCount := 0
	tr := &funcTransport{primeFn: func(_ context.Context, _ string) error {
		primeCount++
		return nil
	}}
	inv := New(drv, tr, defaultConfig(), nil)

	if err := inv.BuildBase(context.Background()); err != nil {
		t.Fatalf("BuildBase: %v", err)
	}
	resp, err := inv.Invoke(context.Background(), "sess-1", strings.NewReader("body"))
	if err != nil {
		t.Fatalf("Invoke: %v", err)
	}
	t.Cleanup(func() { _ = resp.Body.Close() })

	if primeCount < 1 {
		t.Errorf("Prime called %d time(s) on the warm path, want >= 1", primeCount)
	}
}

// TestInvokeColdPathSkipsPrime asserts a cold boot never primes: it does not
// restore a snapshot, so there is no post-restore race to shed. This locks the
// warm-only invariant so the cold path stays untouched.
func TestInvokeColdPathSkipsPrime(t *testing.T) {
	drv := &fakeDriver{}
	primeCount := 0
	tr := &funcTransport{primeFn: func(_ context.Context, _ string) error {
		primeCount++
		return nil
	}}
	cfg := defaultConfig()
	cfg.Workload.WarmBase = false // cold boot only; no restore, so nothing to prime
	inv := New(drv, tr, cfg, nil)

	resp, err := inv.Invoke(context.Background(), "sess-1", strings.NewReader("body"))
	if err != nil {
		t.Fatalf("Invoke: %v", err)
	}
	t.Cleanup(func() { _ = resp.Body.Close() })

	if primeCount != 0 {
		t.Errorf("Prime called %d time(s) on the cold path, want 0", primeCount)
	}
}

// TestInvokeSamplesGuestStatsBeforeRelease verifies the teardown reads the
// guest's resource counters (for the fc.guest.* span attributes) while the VM
// is still alive, i.e. before Release kills the process. Sampling after Release
// would read a dead /proc entry, so ordering is the correctness point.
func TestInvokeSamplesGuestStatsBeforeRelease(t *testing.T) {
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
	// Stats is sampled in the body-close teardown, just before Release.
	if err := resp.Body.Close(); err != nil {
		t.Errorf("resp.Body.Close: %v", err)
	}
	if got := drv.statsCount(); got < 1 {
		t.Errorf("Stats called %d time(s), want >= 1 (sampled at teardown)", got)
	}
	if drv.statsAfterRelease {
		t.Error("Stats was called after Release; it must sample the live guest before teardown")
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

// TestInvokeWarmRoundTripFailureFallsBackToCold verifies that a transport-level
// round-trip failure on the WARM path (a flaky restore whose connection breaks
// after readiness passed) is treated as guest-unavailable: the base is
// invalidated and Invoke retries on a cold boot rather than returning a hard
// 502. The cold retry succeeds, so the caller gets a 200.
func TestInvokeWarmRoundTripFailureFallsBackToCold(t *testing.T) {
	drv := &fakeDriver{}
	// The first round-trip (warm attempt) fails at the transport level; the
	// second (cold fallback) succeeds. WaitReady succeeds throughout (default).
	rtCalls := 0
	tr := &funcTransport{
		roundTripFn: func(_ context.Context, _ string, _ *http.Request) (*http.Response, error) {
			rtCalls++
			if rtCalls == 1 {
				return nil, errors.New("warm vsock connection broke")
			}
			return okResponse("ok"), nil
		},
	}
	inv := New(drv, tr, defaultConfig(), nil) // WarmBase enabled
	if err := inv.BuildBase(context.Background()); err != nil {
		t.Fatalf("BuildBase: %v", err)
	}

	resp, err := inv.Invoke(context.Background(), "sess-warm-flaky", strings.NewReader("body"))
	if err != nil {
		t.Fatalf("Invoke should have fallen back to cold and succeeded, got: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Errorf("status = %d, want 200", resp.StatusCode)
	}

	// BuildBase claimed one VM; the warm attempt claimed another (failed); the
	// cold fallback claimed a third (succeeded). The failed warm attempt's VM
	// must have been released eagerly.
	if c := drv.claimCount(); c != 3 {
		t.Errorf("Claim called %d time(s), want 3 (BuildBase + warm + cold)", c)
	}
	if rtCalls != 2 {
		t.Errorf("RoundTrip called %d time(s), want 2 (warm fail + cold success)", rtCalls)
	}
	// The last claim (cold fallback) must carry an empty base ref.
	if spec := drv.lastClaimSpec(); spec.BaseSnapshotRef.ID != "" {
		t.Errorf("cold-fallback BaseSnapshotRef.ID = %q, want empty", spec.BaseSnapshotRef.ID)
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

// lifeCheckedBody is a response body whose Read fails once the fake driver has
// Released the VM. It proves the invoker keeps the guest alive while the body
// streams: if cleanup ran eagerly (the pre-fix bug), the first Read after
// Invoke returns would error instead of yielding the payload.
type lifeCheckedBody struct {
	drv    *fakeDriver
	data   []byte
	off    int
	closed bool
}

func (b *lifeCheckedBody) Read(p []byte) (int, error) {
	if b.drv.isReleased() {
		return 0, errors.New("read after guest release: body streamed over a dead VM")
	}
	if b.off >= len(b.data) {
		return 0, io.EOF
	}
	n := copy(p, b.data[b.off:])
	b.off += n
	return n, nil
}

func (b *lifeCheckedBody) Close() error {
	b.closed = true
	return nil
}

// TestInvokeSuccessDefersCleanupToBodyClose proves the success-path fix: the VM,
// its request context, and the concurrency slot are torn down by the response
// body's Close, not eagerly when Invoke returns. The body is readable only
// because the guest is still alive, closing it runs cleanup exactly once, and a
// second Close is a no-op (sync.Once). This test fails before the fix (the body
// read errors because the VM was released as Invoke returned).
func TestInvokeSuccessDefersCleanupToBodyClose(t *testing.T) {
	drv := &fakeDriver{}
	payload := "streamed-guest-output"
	body := &lifeCheckedBody{drv: drv, data: []byte(payload)}
	tr := &funcTransport{
		roundTripFn: func(_ context.Context, _ string, _ *http.Request) (*http.Response, error) {
			return &http.Response{StatusCode: http.StatusOK, Body: body}, nil
		},
	}
	cfg := defaultConfig()
	cfg.Workload.WarmBase = false
	cfg.Workload.Concurrency = 1
	inv := New(drv, tr, cfg, nil)

	resp, err := inv.Invoke(context.Background(), "sess", strings.NewReader("in"))
	if err != nil {
		t.Fatalf("Invoke: %v", err)
	}

	// Cleanup must NOT have run yet: the VM is still alive for the body stream.
	if c := drv.releaseCount(); c != 0 {
		t.Fatalf("Release called %d time(s) before body close, want 0", c)
	}
	if c := drv.removeBundleCount(); c != 0 {
		t.Fatalf("RemoveBundle called %d time(s) before body close, want 0", c)
	}

	// The body must be fully readable because the guest is still alive. Before
	// the fix this errors: cleanup released the VM as Invoke returned.
	got, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatalf("ReadAll(resp.Body): %v", err)
	}
	if string(got) != payload {
		t.Errorf("body = %q, want %q", string(got), payload)
	}

	// Still no cleanup until Close.
	if c := drv.releaseCount(); c != 0 {
		t.Fatalf("Release called %d time(s) after read but before close, want 0", c)
	}

	// Closing the body runs cleanup exactly once: one Release, one RemoveBundle,
	// and the underlying body is closed.
	if err := resp.Body.Close(); err != nil {
		t.Errorf("first Close: %v", err)
	}
	if c := drv.releaseCount(); c != 1 {
		t.Errorf("Release called %d time(s) after close, want 1", c)
	}
	// RemoveBundle runs asynchronously off the freed slot; wait for it.
	waitForRemoveBundleCount(t, drv, 1)
	if !body.closed {
		t.Error("underlying body Close was not called")
	}

	// The concurrency slot must have been freed by the close: with Concurrency=1
	// a fresh Invoke can acquire it immediately (a bounded ctx would time out if
	// the slot were still held).
	tr.roundTripFn = func(_ context.Context, _ string, _ *http.Request) (*http.Response, error) {
		return okResponse("second"), nil
	}
	ctx2, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	resp2, err := inv.Invoke(ctx2, "sess2", strings.NewReader("in2"))
	if err != nil {
		t.Fatalf("second Invoke (slot should be free after first body close): %v", err)
	}
	if err := resp2.Body.Close(); err != nil {
		t.Errorf("resp2.Body.Close: %v", err)
	}

	// A second Close of the first body is a no-op (sync.Once): it must not run
	// cleanup again.
	before := drv.releaseCount()
	if err := resp.Body.Close(); err != nil {
		t.Errorf("second Close of first body: %v", err)
	}
	if after := drv.releaseCount(); after != before {
		t.Errorf("double Close ran cleanup again: Release count %d -> %d", before, after)
	}
}
