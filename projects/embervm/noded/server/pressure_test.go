package server

import (
	"context"
	"strings"
	"testing"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	nodev1 "github.com/jomcgi/homelab/projects/embervm/proto/embervm/node/v1"
)

// TestPrimeRejectsUnderMemPressure: with a real (nonzero) memory headroom below
// the workload's need plus the floor, Prime rejects RESOURCE_EXHAUSTED with the
// machine-readable `pressure:mem` reason BEFORE claiming any resource, so no VM
// state is created (ADR embervm/014 decision 3, cheap rejection). Deliberately
// uses a nonzero headroom: a zero headroom is "unknown cgroup", which fails open.
func TestPrimeRejectsUnderMemPressure(t *testing.T) {
	drv := &fakeDriver{}
	client, srv := newTestServer(t, drv, &fakeTransport{}, 8)
	seedBase(srv, "echo__pressure01", "echo")
	// The workload needs 2048 MiB; the floor defaults to minSlotWorkloadMib (512).
	// Report 2000 MiB free (< 2048 + 512), so the predicate trips.
	srv.registry.sync([]workloadEntry{{Workload: "echo", MemMib: 2048}})
	srv.memHeadroom = func() uint64 { return 2000 }

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
	srv.memHeadroom = func() uint64 { return 8192 } // 8192 >= 2048 + 512

	if _, err := client.Prime(context.Background(), &nodev1.PrimeRequest{SnapshotRef: "echo__pressure02"}); err != nil {
		t.Fatalf("Prime with ample headroom: %v", err)
	}
	if got := drv.LiveCount(); got != 1 {
		t.Fatalf("after admitted Prime LiveCount = %d, want 1", got)
	}
}

// TestPrimeUnknownHeadroomAdmits: a zero headroom is "unknown cgroup" (unlimited
// or unreadable), which fails OPEN, so Prime admits regardless of need. This is
// the invariant that keeps every existing memHeadroom==0 test green.
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
	fsn.availableTaps = 1024                     // taps plentiful
	s.memHeadroom = func() uint64 { return 600 } // 600 < 512(need) + 512(floor)

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
	srv.memHeadroom = func() uint64 { return minSlotWorkloadMib - 1 }

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
	srv.memBudget = func() uint64 { return 1000 }
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

func TestObservedAdmissionMatchesExistingPredicate(t *testing.T) {
	_, srv := newTestServer(t, &fakeDriver{live: 1, claimedMib: 9999}, &fakeTransport{}, 8)
	srv.cfg.AdmissionModel = "observed"
	srv.memBudget = func() uint64 { return 4096 }
	for _, headroom := range []uint64{0, 511, 512, 1024} {
		srv.memHeadroom = func() uint64 { return headroom }
		for _, need := range []uint64{0, 512, 1024} {
			if got, want := srv.memExhausted(need), srv.memPressured(need); got != want {
				t.Fatalf("observed admission headroom=%d need=%d = %v, want existing %v", headroom, need, got, want)
			}
		}
	}
}

func TestObservedModelRefusesOnLowHeadroomEvenWhenBudgetReadsZero(t *testing.T) {
	_, srv := newTestServer(t, &fakeDriver{}, &fakeTransport{}, 8)
	// The admission model is unset, so this is the observed model. A zero budget
	// with positive headroom is the memory.max <= DaemonReserveMib case.
	srv.memBudget = func() uint64 { return 0 }
	srv.memHeadroom = func() uint64 { return minSlotWorkloadMib - 1 }

	if got := srv.memExhausted(0); !got {
		t.Fatal("observed model admitted on low headroom even when budget reads zero")
	}
}

func TestUnknownBudgetAdmitsBothModels(t *testing.T) {
	_, srv := newTestServer(t, &fakeDriver{live: 2, claimedMib: 9999}, &fakeTransport{}, 8)
	srv.memBudget = func() uint64 { return 0 }
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
			srv.memHeadroom = func() uint64 { return tc.headroom }

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
