package server

import (
	"context"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/jomcgi/homelab/projects/embervm/noded/substrate"
	nodev1 "github.com/jomcgi/homelab/projects/embervm/proto/embervm/node/v1"
)

// TestPrimeRejectsUnderMemPressure: with a real (nonzero) memory headroom below
// the workload's need plus the floor, Prime rejects RESOURCE_EXHAUSTED with the
// machine-readable `pressure:mem` reason BEFORE claiming any resource, so no VM
// state is created (ADR embervm/014 decision 3, cheap rejection). Deliberately
// uses a known headroom reading, independent of the host cgroup.
func TestPrimeRejectsUnderMemPressure(t *testing.T) {
	drv := &fakeDriver{}
	client, srv := newTestServer(t, drv, &fakeTransport{}, 8)
	seedBase(srv, "echo__pressure01", "echo")
	// The workload needs 2048 MiB; the floor defaults to minSlotWorkloadMib (512).
	// Report 2000 MiB free (< 2048 + 512), so the predicate trips.
	srv.registry.sync([]workloadEntry{{Workload: "echo", MemMib: 2048}})
	srv.memHeadroom = func() (uint64, bool) { return 2000, true }

	_, err := client.Prime(context.Background(), &nodev1.PrimeRequest{SnapshotRef: "echo__pressure01"})
	if status.Code(err) != codes.ResourceExhausted {
		t.Fatalf("Prime code = %v, want ResourceExhausted", status.Code(err))
	}
	if !strings.Contains(err.Error(), string(reasonPressureMem)) {
		t.Fatalf("Prime error %q must carry reason %q", err.Error(), reasonPressureMem)
	}
	// No VM state may have been created by a cheap rejection.
	if got := drv.LiveCount(); got != 0 {
		t.Fatalf("after rejected Prime LiveCount = %d, want 0 (no VM created)", got)
	}
	if _, live := srv.vms.capacity(); live != 0 {
		t.Fatalf("after rejected Prime registry live = %d, want 0", live)
	}
}

// TestPrimeAdmitsWithHeadroom: the same setup but with ample headroom (need +
// floor comfortably covered) admits and primes a VM, proving the predicate is a
// real gate on the observed number, not an unconditional reject.
func TestPrimeAdmitsWithHeadroom(t *testing.T) {
	drv := &fakeDriver{}
	client, srv := newTestServer(t, drv, &fakeTransport{}, 8)
	seedBase(srv, "echo__pressure02", "echo")
	srv.registry.sync([]workloadEntry{{Workload: "echo", MemMib: 2048}})
	srv.memHeadroom = func() (uint64, bool) { return 8192, true } // 8192 >= 2048 + 512

	if _, err := client.Prime(context.Background(), &nodev1.PrimeRequest{SnapshotRef: "echo__pressure02"}); err != nil {
		t.Fatalf("Prime with ample headroom: %v", err)
	}
	if got := drv.LiveCount(); got != 1 {
		t.Fatalf("after admitted Prime LiveCount = %d, want 1", got)
	}
}

// TestPrimeUnknownHeadroomAdmits: explicit unknown readings (unlimited or
// unreadable) retain the fail-open behavior, unlike finite zero readings.
func TestPrimeUnknownHeadroomAdmits(t *testing.T) {
	drv := &fakeDriver{}
	client, srv := newTestServer(t, drv, &fakeTransport{}, 8)
	seedBase(srv, "echo__pressure03", "echo")
	srv.registry.sync([]workloadEntry{{Workload: "echo", MemMib: 16384}})
	// memHeadroom defaults to 0 in newTestServer (unknown); do not override.

	if _, err := client.Prime(context.Background(), &nodev1.PrimeRequest{SnapshotRef: "echo__pressure03"}); err != nil {
		t.Fatalf("Prime under unknown headroom must admit (fail open), got: %v", err)
	}
	if got := drv.LiveCount(); got != 1 {
		t.Fatalf("after admitted Prime LiveCount = %d, want 1", got)
	}
}

// TestStartServingRejectsUnderTapPressure: with the tap allocator drained
// (AvailableTaps() == 0), StartServing rejects RESOURCE_EXHAUSTED with the
// `pressure:taps` reason and never allocates a tap or cold-boots. Memory headroom
// is left unknown (0, fail-open) so the tap predicate is the sole tripwire.
func TestStartServingRejectsUnderTapPressure(t *testing.T) {
	s, fsn, fsd := newServingTestServer(t)
	fsn.availableTaps = 0 // allocator drained: tap pressure

	_, err := s.StartServing(context.Background(), &nodev1.StartServingRequest{
		Source:     &nodev1.StartServingRequest_Fresh{Fresh: &nodev1.FreshSource{ServingImageRef: "img-a"}},
		Port:       8081,
		HealthPath: servingHealthPath,
		Trace:      &nodev1.Trace{Workload: "wl-serve"},
		Resources:  &nodev1.ResourceSpec{Vcpus: 1, MemMib: 512},
	})
	if status.Code(err) != codes.ResourceExhausted {
		t.Fatalf("StartServing code = %v, want ResourceExhausted", status.Code(err))
	}
	if !strings.Contains(err.Error(), string(reasonPressureTaps)) {
		t.Fatalf("StartServing error %q must carry reason %q", err.Error(), reasonPressureTaps)
	}
	// Cheap rejection: no tap allocated, no cold boot claimed.
	if fsn.allocs != 0 {
		t.Fatalf("rejected StartServing allocated %d taps, want 0", fsn.allocs)
	}
	if fsd.claims != 0 {
		t.Fatalf("rejected StartServing cold-booted %d VMs, want 0", fsd.claims)
	}
}

// TestStartServingRejectsUnderMemPressure: with a real headroom below need+floor
// but taps available, StartServing rejects with `pressure:mem`. Confirms the
// memory arm applies to the tap-bearing class too, and that mem is checked before
// the tap allocation.
func TestStartServingRejectsUnderMemPressure(t *testing.T) {
	s, fsn, fsd := newServingTestServer(t)
	fsn.availableTaps = 1024                                   // taps plentiful
	s.memHeadroom = func() (uint64, bool) { return 600, true } // 600 < 512(need) + 512(floor)

	_, err := s.StartServing(context.Background(), &nodev1.StartServingRequest{
		Source:     &nodev1.StartServingRequest_Fresh{Fresh: &nodev1.FreshSource{ServingImageRef: "img-a"}},
		Port:       8082,
		HealthPath: servingHealthPath,
		Trace:      &nodev1.Trace{Workload: "wl-serve"},
		Resources:  &nodev1.ResourceSpec{Vcpus: 1, MemMib: 512},
	})
	if status.Code(err) != codes.ResourceExhausted {
		t.Fatalf("StartServing code = %v, want ResourceExhausted", status.Code(err))
	}
	if !strings.Contains(err.Error(), string(reasonPressureMem)) {
		t.Fatalf("StartServing error %q must carry reason %q", err.Error(), reasonPressureMem)
	}
	if fsn.allocs != 0 {
		t.Fatalf("mem-rejected StartServing allocated %d taps, want 0", fsn.allocs)
	}
	if fsd.claims != 0 {
		t.Fatalf("mem-rejected StartServing cold-booted %d VMs, want 0", fsd.claims)
	}
}

// TestUnderPressureFloorFallback: a zero/unset configured floor still applies
// minSlotWorkloadMib (the floor is never accidentally disabled). With need 0 and
// a headroom below the fallback floor, the predicate trips on `pressure:mem`.
func TestUnderPressureFloorFallback(t *testing.T) {
	_, srv := newTestServer(t, &fakeDriver{}, &fakeTransport{}, 8)
	srv.cfg.MemRejectFloorMib = 0 // unset: falls back to minSlotWorkloadMib
	srv.memHeadroom = func() (uint64, bool) { return minSlotWorkloadMib - 1, true }

	reason, exhausted := srv.underPressure(0, classMemOnly)
	if !exhausted || reason != reasonPressureMem {
		t.Fatalf("underPressure(0) = (%q, %v), want (%q, true) via the fallback floor", reason, exhausted, reasonPressureMem)
	}
	if got := srv.memRejectFloorMib(); got != minSlotWorkloadMib {
		t.Fatalf("memRejectFloorMib fallback = %d, want %d", got, minSlotWorkloadMib)
	}
}

func TestReservedAdmissionBoundaryAndOverhead(t *testing.T) {
	_, srv := newTestServer(t, &fakeDriver{live: 1, claimedMib: 400}, &fakeTransport{}, 8)
	srv.cfg.AdmissionModel = "reserved"
	srv.memBudget = func() (uint64, bool) { return 1000, true }
	if got := srv.memExhausted(600); got {
		t.Fatal("reserved admission rejected the exact claimed+need boundary")
	}
	if got := srv.memExhausted(601); !got {
		t.Fatal("reserved admission admitted above the claimed+need boundary")
	}
	srv.cfg.VMOverheadMib = 1
	if got := srv.memExhausted(599); !got {
		t.Fatal("positive VM overhead did not tighten reserved admission")
	}
	srv.cfg.VMOverheadMib = 0
	if got := srv.memExhausted(600); got {
		t.Fatal("zero VM overhead changed the boundary")
	}
}

func TestBothModelsRejectLazyAllocationOvercommit(t *testing.T) {
	for _, model := range []string{"observed", "reserved"} {
		t.Run(model, func(t *testing.T) {
			drv := &fakeDriver{}
			_, srv := newTestServer(t, drv, &fakeTransport{}, 8)
			srv.cfg.AdmissionModel = model
			srv.cfg.VMOverheadMib = 64
			srv.memBudget = func() (uint64, bool) { return 3840, true }
			// Lazy allocation leaves plenty of measured headroom even after launch.
			srv.memHeadroom = func() (uint64, bool) { return 4000, true }
			if srv.memExhausted(3072) {
				t.Fatal("first 3072 MiB guest should fit")
			}
			drv.live, drv.claimedMib = 1, 3072
			if !srv.memExhausted(3072) {
				t.Fatal("second 3072 MiB guest overcommits 3840 MiB capacity")
			}
			if srv.memExhausted(640) || !srv.memExhausted(641) {
				t.Fatal("live plus incoming overhead must consume the exact remaining boundary")
			}
			srv.memHeadroom = func() (uint64, bool) { return 1, true }
			if !srv.memExhausted(640) {
				t.Fatal("declared capacity must not override actual pressure")
			}
		})
	}
}

func TestObservedModelRefusesOnLowHeadroomEvenWhenBudgetReadsZero(t *testing.T) {
	_, srv := newTestServer(t, &fakeDriver{}, &fakeTransport{}, 8)
	// The admission model is unset, so this is the observed model. A zero budget
	// with positive headroom is the memory.max <= DaemonReserveMib case.
	srv.memBudget = func() (uint64, bool) { return 0, false }
	srv.memHeadroom = func() (uint64, bool) { return minSlotWorkloadMib - 1, true }

	if got := srv.memExhausted(0); !got {
		t.Fatal("observed model admitted on low headroom even when budget reads zero")
	}
}

func TestUnknownBudgetAdmitsBothModels(t *testing.T) {
	_, srv := newTestServer(t, &fakeDriver{live: 2, claimedMib: 9999}, &fakeTransport{}, 8)
	srv.memBudget = func() (uint64, bool) { return 0, false }
	for _, model := range []string{"observed", "reserved"} {
		srv.cfg.AdmissionModel = model
		if got := srv.memExhausted(1 << 30); got {
			t.Fatalf("unknown budget in %s model exhausted admission", model)
		}
	}
}

// TestRelightChargesWorkloadNeed (#4186): Relight used to ask the memory
// admission question with need=0, so a banked 4 GiB session admitted on a brick
// with floor-only headroom and then faulted multiple GiB back in. The need is
// resolved from the registry the same way Prime's is (primeNeedMib), using the
// banked snapshot's recorded workload, with the request trace as the fallback a
// post-rescan bundle (no recorded identity) needs. The registry-absent case still
// degrades to the floor-only gate, exactly as Prime does.
func TestRelightChargesWorkloadNeed(t *testing.T) {
	for _, tc := range []struct {
		name          string
		snapWorkload  string
		traceWorkload string
		registry      []workloadEntry
		headroom      uint64
		wantCode      codes.Code
	}{
		{
			name:         "snapshot workload need above headroom rejects",
			snapWorkload: "sandbox-session",
			registry:     []workloadEntry{{Workload: "sandbox-session", MemMib: 4096}},
			headroom:     2000, // < 4096 + 512 floor
			wantCode:     codes.ResourceExhausted,
		},
		{
			name:         "snapshot workload need within headroom admits",
			snapWorkload: "sandbox-session",
			registry:     []workloadEntry{{Workload: "sandbox-session", MemMib: 4096}},
			headroom:     8192, // >= 4096 + 512
			wantCode:     codes.OK,
		},
		{
			name:          "trace workload is the fallback when the bundle has no identity",
			snapWorkload:  "",
			traceWorkload: "sandbox-session",
			registry:      []workloadEntry{{Workload: "sandbox-session", MemMib: 4096}},
			headroom:      2000,
			wantCode:      codes.ResourceExhausted,
		},
		{
			name:          "snapshot workload outranks a different trace workload",
			snapWorkload:  "echo",
			traceWorkload: "sandbox-session",
			registry:      []workloadEntry{{Workload: "sandbox-session", MemMib: 4096}, {Workload: "echo", MemMib: 256}},
			headroom:      2000, // >= 256 + 512, < 4096 + 512
			wantCode:      codes.OK,
		},
		{
			name:         "registry-absent workload gates on the floor alone",
			snapWorkload: "sandbox-session",
			registry:     nil,
			headroom:     2000, // >= 0 + 512
			wantCode:     codes.OK,
		},
		{
			name:         "unknown headroom fails open regardless of need",
			snapWorkload: "sandbox-session",
			registry:     []workloadEntry{{Workload: "sandbox-session", MemMib: 16384}},
			headroom:     0,
			wantCode:     codes.OK,
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			drv := &fakeDriver{}
			client, srv := newSessionTestServer(t, drv, &fakeTransport{}, 8)
			srv.registry.sync(tc.registry)
			srv.memHeadroom = func() (uint64, bool) { return tc.headroom, tc.headroom != 0 }

			drv.mu.Lock()
			if drv.sessionBundles == nil {
				drv.sessionBundles = map[string]string{}
			}
			drv.sessionBundles["need-ref"] = "state"
			drv.mu.Unlock()
			srv.sessionSnap.add(sessionSnapshotEntry{snapshotRef: "need-ref", workload: tc.snapWorkload})

			_, err := client.Relight(context.Background(), &nodev1.RelightRequest{
				Trace:       &nodev1.Trace{Workload: tc.traceWorkload},
				SnapshotRef: "need-ref",
			})
			if got := status.Code(err); got != tc.wantCode {
				t.Fatalf("Relight code = %v (%v), want %v", got, err, tc.wantCode)
			}
			if tc.wantCode == codes.ResourceExhausted {
				if !strings.Contains(err.Error(), string(reasonPressureMem)) {
					t.Fatalf("Relight error %q must carry reason %q", err.Error(), reasonPressureMem)
				}
				// Cheap rejection: the snapshot was never restored.
				if drv.restoreSessions != 0 {
					t.Fatalf("rejected Relight restored %d sessions, want 0", drv.restoreSessions)
				}
			}
		})
	}
}

func TestKnownZeroMemoryRefusesBothModels(t *testing.T) {
	for _, model := range []string{"observed", "reserved"} {
		_, srv := newTestServer(t, &fakeDriver{}, &fakeTransport{}, 8)
		srv.cfg.AdmissionModel = model
		srv.memBudget = func() (uint64, bool) { return 0, true }
		if !srv.memExhausted(1) {
			t.Fatalf("%s admitted known zero budget", model)
		}
		srv.memBudget = func() (uint64, bool) { return 4096, true }
		if !srv.memExhausted(0) {
			t.Fatalf("%s admitted unknown guest sizing against a finite budget", model)
		}
		srv.memHeadroom = func() (uint64, bool) { return 0, true }
		if !srv.memExhausted(1) {
			t.Fatalf("%s admitted known zero headroom", model)
		}
	}
}

func TestConcurrentAdmissionReservesBeforeDriverPublication(t *testing.T) {
	for _, model := range []string{"observed", "reserved"} {
		_, srv := newTestServer(t, &fakeDriver{}, &fakeTransport{}, 8)
		srv.cfg.AdmissionModel = model
		srv.cfg.VMOverheadMib = 64
		srv.memBudget = func() (uint64, bool) { return 3840, true }
		srv.memHeadroom = func() (uint64, bool) { return 4000, true }
		start := make(chan struct{})
		releases := make(chan func(), 16)
		var workers sync.WaitGroup
		for i := 0; i < 16; i++ {
			workers.Add(1)
			go func() {
				defer workers.Done()
				<-start
				release, err := srv.reserveAdmission(3072, classMemOnly)
				if err == nil {
					releases <- release
				} else if status.Code(err) != codes.ResourceExhausted {
					t.Errorf("unexpected admission error: %v", err)
				}
			}()
		}
		close(start)
		workers.Wait()
		close(releases)
		if len(releases) != 1 {
			t.Fatalf("%s admitted %d concurrent 3072 MiB guests, want 1", model, len(releases))
		}
		for release := range releases {
			release()
			release() // Idempotent cleanup cannot subtract another request's claim.
		}
		if srv.pendingMib != 0 || srv.pendingVMs != 0 || srv.memExhausted(3072) {
			t.Fatal("aborted admission leaked its reservation")
		}
	}
}

func TestPrimeReservationCoversReadinessAndReleaseFailure(t *testing.T) {
	for _, failRelease := range []bool{false, true} {
		drv := &admissionTestDriver{fakeDriver: &fakeDriver{}, perVMMib: 3072}
		if failRelease {
			drv.failRelease = errors.New("reap unconfirmed")
		}
		ready := make(chan string, 1)
		resume := make(chan struct{})
		tr := &fakeTransport{waitReadyStarted: ready, waitReadyContinue: resume, waitReadyErr: errors.New("not ready")}
		client, srv := newTestServer(t, drv, tr, 8)
		seedBase(srv, "admission-base", "echo")
		srv.registry.sync([]workloadEntry{{Workload: "echo", MemMib: 3072}})
		// Model a claim appearing in the live map only after admission.
		srv.cfg.RestoreReadyTimeout = 5 * time.Second
		srv.memBudget = func() (uint64, bool) { return 3840, true }
		srv.memHeadroom = func() (uint64, bool) { return 8192, true }
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		done := make(chan error, 1)
		go func() {
			_, err := client.Prime(ctx, &nodev1.PrimeRequest{SnapshotRef: "admission-base"})
			done <- err
		}()
		select {
		case <-ready:
		case <-ctx.Done():
			t.Fatal("Prime did not reach readiness")
		}
		if !srv.memExhausted(3072) {
			t.Fatal("in-flight readiness did not retain its pending reservation")
		}
		close(resume)
		if err := <-done; status.Code(err) != codes.FailedPrecondition {
			t.Fatalf("Prime readiness failure = %v", err)
		}
		if srv.pendingMib != 0 || srv.pendingVMs != 0 {
			t.Fatal("failed RPC leaked its pending reservation")
		}
		if srv.memExhausted(3072) != failRelease {
			t.Fatal("only a confirmed reap may free live memory")
		}
		if got := drv.LiveCount(); (got != 0) != failRelease {
			t.Fatalf("live claims after reap = %d, failed release = %v", got, failRelease)
		}
	}
}

func TestAdmissionArithmeticCannotOverflow(t *testing.T) {
	if memoryFits(4096, 0, 1, ^uint64(0), 3072) || memoryFits(4096, ^uint64(0), 2, 0) {
		t.Fatal("overflowing memory charges were admitted")
	}
	_, srv := newTestServer(t, &fakeDriver{}, &fakeTransport{}, 1)
	release, err := srv.reserveAdmission(1, classMemOnly)
	if err != nil {
		t.Fatal(err)
	}
	defer release()
	if _, err := srv.reserveAdmission(1, classMemOnly); status.Code(err) != codes.ResourceExhausted {
		t.Fatalf("pending boot bypassed live-VM cap: %v", err)
	}
}

func TestBuildReservationRetainedUntilConfirmedReap(t *testing.T) {
	for _, failRelease := range []bool{false, true} {
		_, srv := newTestServer(t, &fakeDriver{}, &fakeTransport{waitReadyErr: errors.New("not ready")}, 8)
		srv.memBudget = func() (uint64, bool) { return 3840, true }
		build := &fakeDriver{}
		if failRelease {
			build.failRelease = errors.New("reap unconfirmed")
		}
		if _, err := srv.runBuild(t.Context(), build, "build-base", "echo", "/ready", nil, 3072); err == nil {
			t.Fatal("expected readiness failure")
		}
		if got := srv.memExhausted(3072); got != failRelease {
			t.Fatalf("build reservation retained=%v, failed release=%v", got, failRelease)
		}
	}
}

// admissionTestDriver models the production live-map projection without KVM.
type admissionTestDriver struct {
	*fakeDriver
	perVMMib uint64
}

func (d *admissionTestDriver) LiveMemoryClaims() (uint64, uint64) {
	d.mu.Lock()
	defer d.mu.Unlock()
	return uint64(d.live), uint64(d.live) * d.perVMMib
}

type snapshotAdmissionDriver struct{ *admissionTestDriver }

func (d *snapshotAdmissionDriver) SnapshotMemoryMib(string, string, string) uint64 { return 3072 }

func TestPrimeReservesOlderSnapshotAfterWorkloadShrink(t *testing.T) {
	drv := &snapshotAdmissionDriver{&admissionTestDriver{fakeDriver: &fakeDriver{live: 1}, perVMMib: 2048}}
	client, srv := newTestServer(t, drv, &fakeTransport{}, 8)
	seedBase(srv, "old-large", "echo")
	srv.registry.sync([]workloadEntry{{Workload: "echo", MemMib: 512}})
	srv.memBudget = func() (uint64, bool) { return 3840, true }
	srv.memHeadroom = func() (uint64, bool) { return 8192, true }
	_, err := client.Prime(t.Context(), &nodev1.PrimeRequest{SnapshotRef: "old-large"})
	if status.Code(err) != codes.ResourceExhausted {
		t.Fatalf("oversized snapshot admitted: %v", err)
	}
	if drv.claims != 0 || srv.pendingMib != 0 || srv.pendingVMs != 0 {
		t.Fatal("rejected restore created a claim")
	}
}

func TestOlderSnapshotReservationCoversConcurrencyAndFailure(t *testing.T) {
	drv := &snapshotAdmissionDriver{&admissionTestDriver{fakeDriver: &fakeDriver{}, perVMMib: 3072}}
	ready := make(chan string, 1)
	resume := make(chan struct{})
	tr := &fakeTransport{waitReadyStarted: ready, waitReadyContinue: resume, waitReadyErr: errors.New("not ready")}
	client, srv := newTestServer(t, drv, tr, 8)
	seedBase(srv, "old-large", "echo")
	srv.registry.sync([]workloadEntry{{Workload: "echo", MemMib: 512}})
	srv.cfg.RestoreReadyTimeout = 5 * time.Second
	srv.memBudget = func() (uint64, bool) { return 3840, true }
	srv.memHeadroom = func() (uint64, bool) { return 8192, true }
	done := make(chan error, 1)
	go func() {
		_, err := client.Prime(t.Context(), &nodev1.PrimeRequest{SnapshotRef: "old-large"})
		done <- err
	}()
	select {
	case <-ready:
	case <-time.After(5 * time.Second):
		t.Fatal("restore did not reach readiness")
	}
	srv.admissionMu.Lock()
	pending := srv.pendingMib
	srv.admissionMu.Unlock()
	if pending != 3072 {
		t.Errorf("pending=%d, want snapshot size 3072", pending)
	}
	if _, err := client.Prime(t.Context(), &nodev1.PrimeRequest{SnapshotRef: "old-large"}); status.Code(err) != codes.ResourceExhausted {
		t.Errorf("concurrent restore admitted: %v", err)
	}
	close(resume)
	if err := <-done; status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("readiness error: %v", err)
	}
	if srv.pendingMib != 0 || drv.LiveCount() != 0 || srv.memExhausted(3072) {
		t.Fatal("confirmed cleanup retained snapshot reservation")
	}
}

type internallyReapedBuildDriver struct{ *fakeDriver }

func (d *internallyReapedBuildDriver) SnapshotBase(ctx context.Context, h substrate.Handle, _ string) (substrate.SnapshotRef, error) {
	err := d.fakeDriver.Release(ctx, h)
	return substrate.SnapshotRef{}, &fakeSnapshotTeardownError{cause: errors.New("resume after base snapshot"), confirmed: err == nil}
}

func (d *internallyReapedBuildDriver) Release(ctx context.Context, h substrate.Handle) error {
	if d.LiveCount() == 0 {
		return errors.New("unknown handle")
	}
	return d.fakeDriver.Release(ctx, h)
}

func TestBuildSnapshotInternalReapFreesReservation(t *testing.T) {
	for _, failReap := range []bool{false, true} {
		_, srv := newTestServer(t, &fakeDriver{}, &fakeTransport{}, 8)
		srv.memBudget = func() (uint64, bool) { return 3840, true }
		build := &internallyReapedBuildDriver{&fakeDriver{}}
		if failReap {
			build.failRelease = errors.New("reap failed")
		}
		if _, err := srv.runBuild(t.Context(), build, "build-base", "echo", "/ready", nil, 3072); err == nil {
			t.Fatal("expected snapshot failure")
		}
		if srv.memExhausted(3072) != failReap {
			t.Fatalf("retained=%v, failed reap=%v", srv.memExhausted(3072), failReap)
		}
		if !failReap && (srv.pendingVMs != 0 || srv.pendingMib != 0 || build.releases != 1) {
			t.Fatal("confirmed internal reap did not free reservation exactly once")
		}
	}
}

type snapshotRequirement struct {
	t                 *testing.T
	kind, ref, member string
}

func (s snapshotRequirement) SnapshotMemoryMib(kind, ref, member string) uint64 {
	s.t.Helper()
	if kind != s.kind || ref != s.ref || member != s.member {
		s.t.Fatalf("snapshot lookup %q/%q/%q, want %q/%q/%q", kind, ref, member, s.kind, s.ref, s.member)
	}
	return 3072
}

type (
	sizedServingDriver struct {
		*fakeServingDriver
		snapshotRequirement
	}
	sizedStatefulDriver struct {
		*fakeStatefulDriver
		snapshotRequirement
	}
	sizedGroupDriver struct {
		*fakeGroupMemberDriver
		snapshotRequirement
	}
)

func TestRelightClassesChargeSnapshotBeforeRestore(t *testing.T) {
	for _, kind := range []string{"session", "serving", "stateful", "group"} {
		t.Run(kind, func(t *testing.T) {
			var s *Server
			var restore func() error
			resources := &nodev1.ResourceSpec{MemMib: 512}
			switch kind {
			case "session":
				drv := &snapshotAdmissionDriver{&admissionTestDriver{fakeDriver: &fakeDriver{}, perVMMib: 3072}}
				_, s = newTestServer(t, drv, &fakeTransport{}, 8)
				s.sessionDriver = drv
				s.sessionSnap.add(sessionSnapshotEntry{snapshotRef: "old", workload: "echo"})
				s.registry.sync([]workloadEntry{{Workload: "echo", MemMib: 512}})
				restore = func() error { _, err := s.Relight(t.Context(), &nodev1.RelightRequest{SnapshotRef: "old"}); return err }
			case "serving":
				var d *fakeServingDriver
				s, _, d = newServingTestServer(t)
				s.servingDriver = &sizedServingDriver{d, snapshotRequirement{t, kind, "old", ""}}
				restore = func() error {
					_, err := s.StartServing(t.Context(), &nodev1.StartServingRequest{Trace: &nodev1.Trace{Workload: "echo"}, Resources: resources, Source: &nodev1.StartServingRequest_Relight{Relight: &nodev1.RelightSource{SnapshotRef: "old"}}})
					return err
				}
			case "stateful":
				var d *fakeStatefulDriver
				s, _, d = newStatefulTestServer(t)
				s.statefulDriver = &sizedStatefulDriver{d, snapshotRequirement{t, kind, "old", ""}}
				restore = func() error {
					_, err := s.StartStateful(t.Context(), &nodev1.StartStatefulRequest{Trace: &nodev1.Trace{Workload: "echo"}, Resources: resources, Mode: nodev1.StartStatefulMode_START_STATEFUL_MODE_RELIGHT, RelightSnapshotRef: "old"})
					return err
				}
			case "group":
				var d *fakeGroupMemberDriver
				s, _, d, _ = newGroupMemberTestServer(t)
				s.groupDriver = &sizedGroupDriver{d, snapshotRequirement{t, kind, "old", "member"}}
				restore = func() error {
					_, err := s.StartGroupMember(t.Context(), &nodev1.StartGroupMemberRequest{GroupInstanceId: "grp-A", MemberName: "member", SnapshotRef: "group/old/member", Resources: resources, Mode: nodev1.StartGroupMemberMode_START_GROUP_MEMBER_MODE_RELIGHT})
					return err
				}
			}
			s.driver = &fakeDriver{live: 1, claimedMib: 2048}
			s.memBudget = func() (uint64, bool) { return 3840, true }
			s.memHeadroom = func() (uint64, bool) { return 8192, true }
			if err := restore(); status.Code(err) != codes.ResourceExhausted {
				t.Fatalf("restore escaped snapshot admission: %v", err)
			}
			if s.pendingVMs != 0 || s.pendingMib != 0 {
				t.Fatal("rejected restore left a pending claim")
			}
		})
	}
}
