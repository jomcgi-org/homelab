package server

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"math/rand/v2"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	nodev1 "github.com/jomcgi/homelab/projects/embervm/proto/embervm/node/v1"

	"github.com/jomcgi/homelab/projects/embervm/noded/config"
)

// TestMain isolates every test in this package from the host's own cgroup. New
// wires the real budget reader, and since the admission ledger a BuildBase
// consults it too, so on a CI executor with a numeric memory limit a 2048 MiB test
// build could be refused by the executor's own headroom. Pointing the reader at an
// empty directory makes every unstubbed read report unknown (0), the fail-open
// value; a test that wants a real reading installs a fixture with
// withBudgetFsRoot.
func TestMain(m *testing.M) {
	dir, err := os.MkdirTemp("", "noded-server-cgroup-")
	if err != nil {
		fmt.Fprintln(os.Stderr, "create empty cgroup fixture:", err)
		os.Exit(1)
	}
	budgetFsRoot = dir
	selfCgroupPath = filepath.Join(dir, "self-cgroup-absent")
	code := m.Run()
	_ = os.RemoveAll(dir)
	os.Exit(code)
}

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
	if !strings.Contains(err.Error(), "noded: "+string(reasonPressureMem)) {
		t.Fatalf("Prime error %q must lead with reason %q", err.Error(), reasonPressureMem)
	}
	// The human tail names what the predicate actually compared: 2000 MiB
	// headroom less the 512 MiB floor admits at most 1488 MiB.
	if want := "(need 2048 MiB, admittable 1488 MiB, model observed)"; !strings.Contains(err.Error(), want) {
		t.Fatalf("Prime error %q must carry %q", err.Error(), want)
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

// TestAdmittablePredicate pins THE memory predicate for both models: the bound
// each term contributes, that unknown inputs contribute none, that the reserved
// model is also bounded by observed headroom, that boots in flight are charged
// memory and overhead, and that the arithmetic is signed. memAdmits must agree
// with need <= admittable at the boundary.
func TestAdmittablePredicate(t *testing.T) {
	for _, tc := range []struct {
		name       string
		facts      admissionFacts
		want       int64
		wantFinite bool
	}{
		{
			name:  "observed with unknown headroom is unbounded",
			facts: admissionFacts{floorMib: 512, budgetMib: 1000, claimedMib: 5000},
		},
		{
			name:       "observed is headroom minus floor",
			facts:      admissionFacts{headroomMib: 4085, headroomKnown: true, floorMib: 512},
			want:       3573,
			wantFinite: true,
		},
		{
			name:       "observed charges boots in flight",
			facts:      admissionFacts{headroomMib: 4085, headroomKnown: true, floorMib: 512, inflightMib: 3072, inflightVMs: 1},
			want:       501,
			wantFinite: true,
		},
		{
			name:       "observed ignores the declared budget",
			facts:      admissionFacts{headroomMib: 4085, headroomKnown: true, floorMib: 512, budgetMib: 1000, claimedMib: 999, live: 3, overheadMib: 64},
			want:       3573,
			wantFinite: true,
		},
		{
			name:  "reserved with unknown budget and headroom is unbounded",
			facts: admissionFacts{model: "reserved", floorMib: 512, claimedMib: 9999, live: 2},
		},
		{
			name:       "reserved with unknown budget keeps the observed bound",
			facts:      admissionFacts{model: "reserved", headroomMib: 2000, headroomKnown: true, floorMib: 512},
			want:       1488,
			wantFinite: true,
		},
		{
			name:       "reserved charges claims plus overhead for every live VM and the new one",
			facts:      admissionFacts{model: "reserved", budgetMib: 7936, claimedMib: 6144, live: 2, overheadMib: 64, floorMib: 512},
			want:       1600,
			wantFinite: true,
		},
		{
			name:       "reserved charges in-flight memory and in-flight overhead",
			facts:      admissionFacts{model: "reserved", budgetMib: 7936, claimedMib: 6144, live: 2, inflightMib: 1536, inflightVMs: 1, overheadMib: 64},
			want:       0,
			wantFinite: true,
		},
		{
			name:       "reserved is bounded by observed headroom too",
			facts:      admissionFacts{model: "reserved", budgetMib: 3840, overheadMib: 64, headroomMib: 2000, headroomKnown: true, floorMib: 512},
			want:       1488,
			wantFinite: true,
		},
		{
			name:       "reserved is the tighter bound against generous headroom",
			facts:      admissionFacts{model: "reserved", budgetMib: 3840, claimedMib: 3072, live: 1, overheadMib: 64, headroomMib: 3700, headroomKnown: true, floorMib: 512},
			want:       640,
			wantFinite: true,
		},
		{
			name:       "reserved goes negative once claims pass the budget",
			facts:      admissionFacts{model: "reserved", budgetMib: 3840, claimedMib: 3800, live: 1, overheadMib: 64},
			want:       -88,
			wantFinite: true,
		},
		{
			name:       "known exhausted headroom is negative, not unknown",
			facts:      admissionFacts{headroomMib: 1, headroomKnown: true, floorMib: 512},
			want:       -511,
			wantFinite: true,
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got, finite := tc.facts.admittableMib()
			if finite != tc.wantFinite || (finite && got != tc.want) {
				t.Fatalf("admittableMib() = (%d, %v), want (%d, %v)", got, finite, tc.want, tc.wantFinite)
			}
			if !finite {
				if !tc.facts.memAdmits(1 << 40) {
					t.Fatal("an unbounded predicate refused a boot")
				}
				return
			}
			if tc.want >= 0 && !tc.facts.memAdmits(uint64(tc.want)) {
				t.Fatalf("need %d == admittable was refused", tc.want)
			}
			if tc.facts.memAdmits(uint64(max(tc.want+1, 0))) {
				t.Fatalf("need %d above admittable %d was admitted", max(tc.want+1, 0), tc.want)
			}
		})
	}
}

// TestReservedModelRefusesTheOvercommitObservedAdmitted replays the 2026-10-10
// capacity row that preceded a whole-brick OOMKill: an 8gi brick (budget 7936)
// already holding 15360 MiB of declared claude-runtime guests still showed 3806
// MiB of observed headroom, because restored guests fault their memory in
// lazily. The observed model admits another 3072 MiB guest there; the reserved
// model, which charges declared memory, refuses it.
func TestReservedModelRefusesTheOvercommitObservedAdmitted(t *testing.T) {
	facts := admissionFacts{budgetMib: 7936, claimedMib: 15360, live: 5, headroomMib: 3806, headroomKnown: true, floorMib: 512, overheadMib: 64}
	if !facts.memAdmits(3072) {
		t.Fatal("observed model refused; this test documents that it admits the overcommit")
	}
	facts.model = admissionModelReserved
	if facts.memAdmits(3072) {
		t.Fatal("reserved model admitted a 3072 MiB guest onto a brick already declaring 15360 of 7936 MiB")
	}
}

// reservedBurstServer is a reserved-model brick whose fake driver holds every
// Claim in flight until gate closes, sized so exactly fit Primes of needMib fit.
func reservedBurstServer(t *testing.T, fit int, needMib, overheadMib uint64, maxLive int) (nodev1.NodeServiceClient, *Server, *fakeDriver, chan struct{}) {
	t.Helper()
	gate := make(chan struct{})
	drv := &fakeDriver{claimMib: needMib, claimStarted: make(chan struct{}, 64), blockClaim: gate}
	client, srv := newTestServer(t, drv, &fakeTransport{}, maxLive)
	srv.cfg.AdmissionModel = admissionModelReserved
	srv.cfg.VMOverheadMib = int(overheadMib)
	// Room for exactly fit guests plus their overhead, and less than one more.
	budget := uint64(fit)*(needMib+overheadMib) + needMib/2
	srv.memBudget = func() uint64 { return budget }
	seedBase(srv, "echo__burst", "echo")
	srv.registry.sync([]workloadEntry{{Workload: "echo", VCPUs: 1, MemMib: uint32(needMib)}})
	return client, srv, drv, gate
}

func recvWithin[T any](t *testing.T, ch <-chan T, what string) T {
	t.Helper()
	select {
	case v := <-ch:
		return v
	case <-time.After(5 * time.Second):
		t.Fatalf("timed out waiting for %s", what)
	}
	var zero T
	return zero
}

// TestConcurrentPrimesAdmitExactlyWhatFits is the S2 race: a burst of Primes
// lands on a brick that fits only some of them, and every boot is still in
// flight (admitted, not yet in the driver's live map) when the others are
// decided. Exactly fit Primes may reach the driver; every other one is refused
// pressure:mem while they are in flight, and the claims never vanish from the
// ledger as they move into the live map.
func TestConcurrentPrimesAdmitExactlyWhatFits(t *testing.T) {
	const (
		burst = 8
		fit   = 3
		need  = 1024
		o     = 64
	)
	client, srv, drv, gate := reservedBurstServer(t, fit, need, o, 16)
	released := false
	defer func() {
		if !released {
			close(gate)
		}
	}()

	results := make(chan error, burst)
	for range burst {
		go func() {
			_, err := client.Prime(context.Background(), &nodev1.PrimeRequest{SnapshotRef: "echo__burst"})
			results <- err
		}()
	}
	for i := range fit {
		recvWithin(t, drv.claimStarted, fmt.Sprintf("admitted Claim %d", i+1))
	}
	for i := range burst - fit {
		err := recvWithin(t, results, fmt.Sprintf("refusal %d", i+1))
		if status.Code(err) != codes.ResourceExhausted || !strings.Contains(err.Error(), string(reasonPressureMem)) {
			t.Fatalf("Prime beyond the fit = %v, want ResourceExhausted %s", err, reasonPressureMem)
		}
	}
	if extra := len(drv.claimStarted); extra != 0 {
		t.Fatalf("%d more Claims reached the driver than fit", extra)
	}

	// While the admitted boots are in flight NodeStatus already carries them, so
	// the control plane sees the reservation before any VM is live.
	ns := srv.nodeStatus()
	if got, want := ns.GetMemInflightMib(), uint64(fit*need); got != want {
		t.Errorf("mem_inflight_mib = %d, want %d", got, want)
	}
	if got := ns.GetInflightVms(); got != fit {
		t.Errorf("inflight_vms = %d, want %d", got, fit)
	}
	budget := int64(fit*(need+o) + need/2)
	if want := budget - fit*need - o*(fit+1); !ns.GetAdmissionReported() || ns.GetMemAdmittableMib() != want {
		t.Errorf("admission = (%v, %d), want (true, %d)", ns.GetAdmissionReported(), ns.GetMemAdmittableMib(), want)
	}
	if got := ns.GetAdmissionRefusals()[string(reasonPressureMem)]; got != burst-fit {
		t.Errorf("admission_refusals[pressure:mem] = %d, want %d", got, burst-fit)
	}

	close(gate)
	released = true
	for i := range fit {
		if err := recvWithin(t, results, fmt.Sprintf("admitted Prime %d", i+1)); err != nil {
			t.Fatalf("admitted Prime failed: %v", err)
		}
	}
	if got := drv.LiveCount(); got != fit {
		t.Fatalf("LiveCount = %d, want %d", got, fit)
	}
	ns = srv.nodeStatus()
	if ns.GetMemInflightMib() != 0 || ns.GetInflightVms() != 0 {
		t.Fatalf("in flight after every boot landed = (%d MiB, %d VMs), want zero", ns.GetMemInflightMib(), ns.GetInflightVms())
	}
	if got, want := ns.GetMemReservedMib(), uint64(fit*need); got != want {
		t.Fatalf("mem_reserved_mib = %d, want %d (the claims moved into the live map)", got, want)
	}
	// The claims are now declared by the live map, so one more is still refused.
	if _, err := client.Prime(context.Background(), &nodev1.PrimeRequest{SnapshotRef: "echo__burst"}); status.Code(err) != codes.ResourceExhausted {
		t.Fatalf("Prime after the burst landed = %v, want ResourceExhausted", err)
	}
}

// TestPrimeAdmissionReleasesOnBootFailureAndEarlyReturn: a ticket must never
// leak. A Claim that fails and a handler that returns between admission and the
// boot both give the reservation back, so a brick that fits exactly one guest
// admits the next Prime each time.
func TestPrimeAdmissionReleasesOnBootFailureAndEarlyReturn(t *testing.T) {
	const need = 1024
	drv := &fakeDriver{claimMib: need}
	client, srv := newTestServer(t, drv, &fakeTransport{}, 8)
	srv.cfg.AdmissionModel = admissionModelReserved
	srv.memBudget = func() uint64 { return need }
	seedBase(srv, "echo__release", "echo")
	srv.registry.sync([]workloadEntry{{Workload: "echo", VCPUs: 1, MemMib: need}})
	ctx := context.Background()

	drv.mu.Lock()
	drv.failClaim = fmt.Errorf("restore exploded")
	drv.mu.Unlock()
	if _, err := client.Prime(ctx, &nodev1.PrimeRequest{SnapshotRef: "echo__release"}); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("Prime with failing Claim = %v, want FailedPrecondition", err)
	}
	if snap := srv.admissionStatus(); snap.inflightMib != 0 || snap.inflightVMs != 0 {
		t.Fatalf("failed boot leaked its reservation: %+v", snap)
	}
	drv.mu.Lock()
	drv.failClaim = nil
	drv.mu.Unlock()

	// Early return after admission: session volume fields on a node with no
	// volume manager fail FailedPrecondition before any Claim.
	_, err := client.Prime(ctx, &nodev1.PrimeRequest{
		SnapshotRef: "echo__release", LineageId: "lin-1", VolumeMount: "/workspace", VolumeSizeBytes: 1 << 20,
	})
	if status.Code(err) != codes.FailedPrecondition || !strings.Contains(err.Error(), "volume manager not configured") {
		t.Fatalf("Prime without a volume manager = %v, want the early FailedPrecondition", err)
	}
	if snap := srv.admissionStatus(); snap.inflightMib != 0 || snap.inflightVMs != 0 {
		t.Fatalf("early return leaked its reservation: %+v", snap)
	}

	if _, err := client.Prime(ctx, &nodev1.PrimeRequest{SnapshotRef: "echo__release"}); err != nil {
		t.Fatalf("Prime after released reservations: %v", err)
	}
	if got := drv.LiveCount(); got != 1 {
		t.Fatalf("LiveCount = %d, want 1", got)
	}
}

// TestObservedModelChargesInflightBoots: the observed model has no declared
// ledger, but the boots it has admitted and not yet landed are subtracted from
// the observed headroom, so two concurrent restores cannot both pass against the
// same reading.
func TestObservedModelChargesInflightBoots(t *testing.T) {
	gate := make(chan struct{})
	drv := &fakeDriver{claimStarted: make(chan struct{}, 4), blockClaim: gate}
	client, srv := newTestServer(t, drv, &fakeTransport{}, 8)
	srv.memHeadroom = func() uint64 { return 4085 } // fits one 3072 MiB guest plus the 512 floor
	seedBase(srv, "echo__observed", "echo")
	srv.registry.sync([]workloadEntry{{Workload: "echo", VCPUs: 2, MemMib: 3072}})

	first := make(chan error, 1)
	go func() {
		_, err := client.Prime(context.Background(), &nodev1.PrimeRequest{SnapshotRef: "echo__observed"})
		first <- err
	}()
	recvWithin(t, drv.claimStarted, "first Claim")
	_, err := client.Prime(context.Background(), &nodev1.PrimeRequest{SnapshotRef: "echo__observed"})
	close(gate)
	if status.Code(err) != codes.ResourceExhausted || !strings.Contains(err.Error(), string(reasonPressureMem)) {
		t.Fatalf("second Prime while the first is in flight = %v, want ResourceExhausted %s", err, reasonPressureMem)
	}
	if err := recvWithin(t, first, "first Prime"); err != nil {
		t.Fatalf("first Prime: %v", err)
	}
}

// TestSlotBackstopCountsInflightBoots: the live-VM backstop counts boots in
// flight, so a cap of one cannot be overrun by two concurrent Primes. The
// refusal keeps its historical "live-VM cap" text and counts under "slots".
func TestSlotBackstopCountsInflightBoots(t *testing.T) {
	gate := make(chan struct{})
	drv := &fakeDriver{claimStarted: make(chan struct{}, 4), blockClaim: gate}
	client, srv := newTestServer(t, drv, &fakeTransport{}, 1)
	seedBase(srv, "echo__slots", "echo")

	first := make(chan error, 1)
	go func() {
		_, err := client.Prime(context.Background(), &nodev1.PrimeRequest{SnapshotRef: "echo__slots"})
		first <- err
	}()
	recvWithin(t, drv.claimStarted, "first Claim")
	_, err := client.Prime(context.Background(), &nodev1.PrimeRequest{SnapshotRef: "echo__slots"})
	close(gate)
	if status.Code(err) != codes.ResourceExhausted || !strings.Contains(err.Error(), "live-VM cap 1 reached (live 0, in flight 1)") {
		t.Fatalf("second Prime at the cap = %v, want the live-VM cap refusal", err)
	}
	if err := recvWithin(t, first, "first Prime"); err != nil {
		t.Fatalf("first Prime: %v", err)
	}
	if got := srv.nodeStatus().GetAdmissionRefusals()[string(reasonSlots)]; got != 1 {
		t.Fatalf("admission_refusals[slots] = %d, want 1", got)
	}
}

// TestStartServingHoldsTicketUntilClaimReturns: a serving boot (the path the
// node-local activator also takes) holds its reservation exactly while the
// driver boots it, and gives it back on success, on a failed boot and on an
// early return.
func TestStartServingHoldsTicketUntilClaimReturns(t *testing.T) {
	_, port := healthServer(t, servingHealthPath)
	s, _, fsd := newServingTestServer(t)
	var during admissionSnapshot
	fsd.onClaim = func() { during = s.admissionStatus() }

	startFresh(t, s, port)
	if during.inflightMib != 512 || during.inflightVMs != 1 {
		t.Fatalf("ledger during ClaimServing = %+v, want 512 MiB / 1 VM in flight", during)
	}
	if snap := s.admissionStatus(); snap.inflightMib != 0 || snap.inflightVMs != 0 {
		t.Fatalf("ledger after a landed boot = %+v, want empty", snap)
	}

	fsd.mu.Lock()
	fsd.failClaim = fmt.Errorf("cold boot exploded")
	fsd.mu.Unlock()
	_, err := s.StartServing(context.Background(), &nodev1.StartServingRequest{
		Resources:  &nodev1.ResourceSpec{Vcpus: 1, MemMib: 512},
		Source:     &nodev1.StartServingRequest_Fresh{Fresh: &nodev1.FreshSource{ServingImageRef: "img-a"}},
		Port:       port,
		HealthPath: servingHealthPath,
		Trace:      &nodev1.Trace{Workload: "wl-serve"},
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("StartServing with failing ClaimServing = %v, want FailedPrecondition", err)
	}
	if snap := s.admissionStatus(); snap.inflightMib != 0 || snap.inflightVMs != 0 {
		t.Fatalf("failed boot leaked its reservation: %+v", snap)
	}

	// Early return after admission: an unknown relight ref never reaches the driver.
	_, err = s.StartServing(context.Background(), &nodev1.StartServingRequest{
		Resources: &nodev1.ResourceSpec{Vcpus: 1, MemMib: 512},
		Source:    &nodev1.StartServingRequest_Relight{Relight: &nodev1.RelightSource{SnapshotRef: "serv-missing"}},
		Port:      port,
		Trace:     &nodev1.Trace{Workload: "wl-serve"},
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("StartServing relight of an unknown ref = %v, want FailedPrecondition", err)
	}
	if snap := s.admissionStatus(); snap.inflightMib != 0 || snap.inflightVMs != 0 {
		t.Fatalf("early return leaked its reservation: %+v", snap)
	}
}

// TestBootVerbsRefuseMissingResources: a tap or group boot without a nonzero
// ResourceSpec is refused InvalidArgument before admission, instead of being
// charged 0 MiB and booted at the driver default (1 vCPU / 1024 MiB). Nothing is
// reserved, counted as a capacity refusal, allocated or booted.
func TestBootVerbsRefuseMissingResources(t *testing.T) {
	for _, tc := range []struct {
		name string
		res  *nodev1.ResourceSpec
	}{
		{name: "missing", res: nil},
		{name: "zero", res: &nodev1.ResourceSpec{}},
		{name: "no memory", res: &nodev1.ResourceSpec{Vcpus: 1}},
		{name: "no vcpus", res: &nodev1.ResourceSpec{MemMib: 512}},
	} {
		res := tc.res
		t.Run("serving "+tc.name, func(t *testing.T) {
			s, fsn, fsd := newServingTestServer(t)
			_, err := s.StartServing(context.Background(), &nodev1.StartServingRequest{
				Resources: res,
				Source:    &nodev1.StartServingRequest_Fresh{Fresh: &nodev1.FreshSource{ServingImageRef: "img-a"}},
				Port:      8080,
				Trace:     &nodev1.Trace{Workload: "wl-serve"},
			})
			assertInvalidResources(t, s, err)
			if fsn.allocs != 0 || fsd.claims != 0 {
				t.Fatalf("refused StartServing allocated %d taps and booted %d VMs", fsn.allocs, fsd.claims)
			}
		})
		t.Run("stateful "+tc.name, func(t *testing.T) {
			s, fsn, fsd := newStatefulTestServer(t)
			_, err := s.StartStateful(context.Background(), &nodev1.StartStatefulRequest{
				Resources:       res,
				Mode:            nodev1.StartStatefulMode_START_STATEFUL_MODE_FRESH,
				Trace:           &nodev1.Trace{Workload: "wl-state"},
				Port:            5432,
				BootImageRef:    "img-a",
				CreateIfMissing: true,
				VolumeSizeBytes: 1 << 20,
			})
			assertInvalidResources(t, s, err)
			if fsn.allocs != 0 || fsd.claimCount != 0 {
				t.Fatalf("refused StartStateful allocated %d taps and booted %d VMs", fsn.allocs, fsd.claimCount)
			}
		})
		t.Run("group member "+tc.name, func(t *testing.T) {
			s, _, gmd, _ := newGroupMemberTestServer(t)
			_, err := s.StartGroupMember(context.Background(), &nodev1.StartGroupMemberRequest{
				Resources:       res,
				Mode:            nodev1.StartGroupMemberMode_START_GROUP_MEMBER_MODE_FRESH,
				GroupInstanceId: "grp-A",
				MemberName:      "server",
				Source:          "src-a",
				Ip:              "127.0.0.1",
				HealthPort:      8080,
				Trace:           &nodev1.Trace{Workload: "wl-group"},
			})
			assertInvalidResources(t, s, err)
			if gmd.claims != 0 {
				t.Fatalf("refused StartGroupMember booted %d VMs", gmd.claims)
			}
		})
	}
}

func assertInvalidResources(t *testing.T, s *Server, err error) {
	t.Helper()
	if status.Code(err) != codes.InvalidArgument || !strings.Contains(err.Error(), "nonzero vcpus and mem_mib") {
		t.Fatalf("boot without resources = %v, want InvalidArgument naming the resources", err)
	}
	snap := s.admissionStatus()
	if snap.inflightMib != 0 || snap.inflightVMs != 0 || len(snap.refusals) != 0 {
		t.Fatalf("invalid request touched the admission ledger: %+v", snap)
	}
}

// buildAdmissionServer wires a BuildBase-capable Server whose transport blocks
// the build guest's readiness until gate closes.
func buildAdmissionServer(t *testing.T, gate <-chan struct{}) (*Server, *fakeDriver, *fakeDriver, chan string) {
	t.Helper()
	snapshotRoot := t.TempDir()
	rootfs := writeExt4Rootfs(t, t.TempDir(), "rootfs.ext4", testRootfsUUIDA)
	build := &fakeDriver{snapshotRoot: snapshotRoot}
	drv := &fakeDriver{}
	started := make(chan string, 8)
	tr := &fakeTransport{waitReadyStarted: started, waitReadyContinue: gate}
	s := New(Options{
		Config: config.Config{
			Arch: "amd64", Node: "node-4", SnapshotRoot: snapshotRoot, MaxLiveVMs: 8,
			BootReadyTimeout:    5 * time.Second,
			RestoreReadyTimeout: 5 * time.Second,
			Images:              map[string]config.Image{"img:1": {RootfsPath: rootfs}},
		},
		Driver:         drv,
		Transport:      tr,
		NewBuildDriver: func(BuildDriverSpec) BuildDriver { return build },
		Logger:         slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	return s, drv, build, started
}

func buildRequest(memMib uint32) *nodev1.BuildBaseRequest {
	return &nodev1.BuildBaseRequest{
		Trace:            &nodev1.Trace{Workload: "echo"},
		ImageRef:         "img:1",
		WorkloadRevision: "r1",
		ReadyPath:        "/shim/ready",
		Resources:        &nodev1.ResourceSpec{Vcpus: 2, MemMib: memMib},
	}
}

// TestBuildBaseHoldsTicketForWholeBuild (S19): a build guest runs on its own
// driver, outside LiveCount and ClaimedMib, so its admission ticket is held
// until the build finishes. While it runs, NodeStatus reports it in flight and a
// Prime that only fits the idle brick is refused; once it finishes the memory is
// admittable again.
func TestBuildBaseHoldsTicketForWholeBuild(t *testing.T) {
	gate := make(chan struct{})
	s, _, _, started := buildAdmissionServer(t, gate)
	released := false
	defer func() {
		if !released {
			close(gate)
		}
	}()
	s.cfg.AdmissionModel = admissionModelReserved
	s.memBudget = func() uint64 { return 3000 }
	seedBase(s, "echo__afterbuild", "echo")
	s.registry.sync([]workloadEntry{{Workload: "echo", VCPUs: 1, MemMib: 1024}})

	done := make(chan error, 1)
	go func() {
		_, err := s.BuildBase(context.Background(), buildRequest(2048))
		done <- err
	}()
	recvWithin(t, started, "build guest readiness wait")

	ns := s.nodeStatus()
	if ns.GetMemInflightMib() != 2048 || ns.GetInflightVms() != 1 {
		t.Fatalf("in flight during the build = (%d MiB, %d VMs), want (2048, 1)", ns.GetMemInflightMib(), ns.GetInflightVms())
	}
	if got := ns.GetMemAdmittableMib(); got != 3000-2048 {
		t.Fatalf("mem_admittable_mib during the build = %d, want %d", got, 3000-2048)
	}
	if _, err := s.Prime(context.Background(), &nodev1.PrimeRequest{SnapshotRef: "echo__afterbuild"}); status.Code(err) != codes.ResourceExhausted {
		t.Fatalf("Prime during the build = %v, want ResourceExhausted", err)
	}

	close(gate)
	released = true
	if err := recvWithin(t, done, "BuildBase"); err != nil {
		t.Fatalf("BuildBase: %v", err)
	}
	if snap := s.admissionStatus(); snap.inflightMib != 0 || snap.inflightVMs != 0 {
		t.Fatalf("ledger after the build = %+v, want empty", snap)
	}
	if _, err := s.Prime(context.Background(), &nodev1.PrimeRequest{SnapshotRef: "echo__afterbuild"}); err != nil {
		t.Fatalf("Prime after the build: %v", err)
	}
}

// TestBuildBaseRefusedUnderMemPressureLeavesNoBuildState: a refused build is a
// cheap refusal like any other boot: no build guest is claimed and no FAILED base
// or build_error is recorded, so the control plane retries instead of seeing a
// broken build.
func TestBuildBaseRefusedUnderMemPressureLeavesNoBuildState(t *testing.T) {
	gate := make(chan struct{})
	close(gate)
	s, _, build, _ := buildAdmissionServer(t, gate)
	s.memHeadroom = func() uint64 { return 1000 } // < 3072 + 512

	_, err := s.BuildBase(context.Background(), buildRequest(3072))
	if status.Code(err) != codes.ResourceExhausted || !strings.Contains(err.Error(), string(reasonPressureMem)) {
		t.Fatalf("BuildBase under pressure = %v, want ResourceExhausted %s", err, reasonPressureMem)
	}
	if claims, _, _, _ := build.counts(); claims != 0 {
		t.Fatalf("refused build claimed %d guests", claims)
	}
	if _, ok := s.bases.get(baseKeyFor("echo", "img:1", "r1", s.cfg.CpuVendor, testRootfsUUIDA)); ok {
		t.Fatal("refused build left a base registry entry")
	}
	if got := s.nodeStatus().GetBuildError(); got != "" {
		t.Fatalf("refused build recorded build_error %q", got)
	}
}

// TestBuildBaseAlreadyBuiltBypassesAdmission: an idempotent repeat of a built
// base needs no build guest, so it is answered even on a brick that would refuse
// a new boot.
func TestBuildBaseAlreadyBuiltBypassesAdmission(t *testing.T) {
	gate := make(chan struct{})
	close(gate)
	s, _, _, _ := buildAdmissionServer(t, gate)
	if _, err := s.BuildBase(context.Background(), buildRequest(2048)); err != nil {
		t.Fatalf("first BuildBase: %v", err)
	}
	s.memHeadroom = func() uint64 { return 1 } // known exhausted
	resp, err := s.BuildBase(context.Background(), buildRequest(2048))
	if err != nil || !resp.GetAlreadyBuilt() {
		t.Fatalf("repeat BuildBase on a full brick = (%+v, %v), want AlreadyBuilt", resp, err)
	}
}

// jailerCountingDriver adds the optional launch counters a jailer-aware driver
// implements.
type jailerCountingDriver struct {
	*fakeDriver
	jailed, directExec, refused uint64
}

func (d *jailerCountingDriver) JailerLaunchCounts() (bool, uint64, uint64, uint64) {
	return true, d.jailed, d.directExec, d.refused
}

// TestNodeStatusReportsAdmissionLedger covers fields 43-49: nothing is reported
// while no bound is known, the reserved model reports its own predicate, refusals
// are counted per token, and the jailer facts come from config and the optional
// driver counters (absent, not zero, without them).
func TestNodeStatusReportsAdmissionLedger(t *testing.T) {
	drv := &fakeDriver{live: 2, claimedMib: 6144}
	client, srv := newTestServer(t, drv, &fakeTransport{}, 8)
	ns := srv.nodeStatus()
	if ns.GetAdmissionReported() || ns.GetMemAdmittableMib() != 0 {
		t.Fatalf("observed model with unknown headroom reported (%v, %d), want nothing", ns.GetAdmissionReported(), ns.GetMemAdmittableMib())
	}
	if ns.GetJailerArmed() || ns.GetLaunchModes() != nil || ns.GetAdmissionRefusals() != nil {
		t.Fatalf("default NodeStatus carried jailer or refusal facts: armed %v modes %v refusals %v", ns.GetJailerArmed(), ns.GetLaunchModes(), ns.GetAdmissionRefusals())
	}

	srv.cfg.AdmissionModel = admissionModelReserved
	srv.cfg.VMOverheadMib = 64
	srv.memBudget = func() uint64 { return 7936 }
	srv.admission.inflightMib, srv.admission.inflightVMs = 1024, 1
	ns, err := client.GetNodeStatus(context.Background(), &nodev1.GetNodeStatusRequest{})
	if err != nil {
		t.Fatalf("GetNodeStatus: %v", err)
	}
	if want := int64(7936 - 6144 - 1024 - 64*4); !ns.GetAdmissionReported() || ns.GetMemAdmittableMib() != want {
		t.Fatalf("reserved admission = (%v, %d), want (true, %d)", ns.GetAdmissionReported(), ns.GetMemAdmittableMib(), want)
	}
	if ns.GetMemInflightMib() != 1024 || ns.GetInflightVms() != 1 || ns.GetMemReservedMib() != 6144 {
		t.Fatalf("ledger = inflight (%d MiB, %d VMs) reserved %d, want (1024, 1) and 6144", ns.GetMemInflightMib(), ns.GetInflightVms(), ns.GetMemReservedMib())
	}
	srv.admission.inflightMib, srv.admission.inflightVMs = 0, 0

	seedBase(srv, "echo__ledger", "echo")
	srv.registry.sync([]workloadEntry{{Workload: "echo", VCPUs: 1, MemMib: 4096}})
	for range 2 {
		if _, err := client.Prime(context.Background(), &nodev1.PrimeRequest{SnapshotRef: "echo__ledger"}); status.Code(err) != codes.ResourceExhausted {
			t.Fatalf("oversized Prime = %v, want ResourceExhausted", err)
		}
	}
	if got := srv.nodeStatus().GetAdmissionRefusals(); got[string(reasonPressureMem)] != 2 || len(got) != 1 {
		t.Fatalf("admission_refusals = %v, want pressure:mem=2 only", got)
	}

	counting := &jailerCountingDriver{fakeDriver: &fakeDriver{}, jailed: 7, directExec: 1, refused: 2}
	_, armed := newTestServer(t, counting, &fakeTransport{}, 8)
	armed.cfg.JailerEnabled = true
	ns = armed.nodeStatus()
	if !ns.GetJailerArmed() {
		t.Fatal("jailer_armed = false with the jailer enabled")
	}
	if want := map[string]uint64{"jailed": 7, "direct_exec": 1, "refused": 2}; fmt.Sprint(ns.GetLaunchModes()) != fmt.Sprint(want) {
		t.Fatalf("launch_modes = %v, want %v", ns.GetLaunchModes(), want)
	}
}

// TestNodeStatusAdmittableMatchesAdmit is the parity property the control plane
// relies on: for any brick state, a boot of need N is admitted exactly when the
// reported bound is absent or N <= mem_admittable_mib. It sweeps random states of
// both models (known and unknown budget and headroom, claims, live VMs, boots in
// flight, overhead and floor) and needs around the boundary.
func TestNodeStatusAdmittableMatchesAdmit(t *testing.T) {
	rng := rand.New(rand.NewPCG(4185, 5255))
	drv := &fakeDriver{}
	_, srv := newTestServer(t, drv, &fakeTransport{}, 0) // no slot ceiling: memory only
	for i := range 300 {
		model := "observed"
		if rng.IntN(2) == 0 {
			model = admissionModelReserved
		}
		var budget, headroom uint64
		if rng.IntN(4) != 0 {
			budget = 256 + rng.Uint64N(16384)
		}
		if rng.IntN(4) != 0 {
			headroom = 1 + rng.Uint64N(16384)
		}
		srv.cfg.AdmissionModel = model
		srv.cfg.VMOverheadMib = rng.IntN(257)
		srv.cfg.MemRejectFloorMib = rng.IntN(1025)
		srv.memBudget = func() uint64 { return budget }
		srv.memHeadroom = func() uint64 { return headroom }
		drv.mu.Lock()
		drv.live = rng.IntN(10)
		drv.claimedMib = rng.Uint64N(20000)
		drv.mu.Unlock()
		srv.admission.mu.Lock()
		srv.admission.inflightVMs = rng.IntN(4)
		srv.admission.inflightMib = 0
		if srv.admission.inflightVMs > 0 {
			srv.admission.inflightMib = rng.Uint64N(8192)
		}
		srv.admission.mu.Unlock()

		ns := srv.nodeStatus()
		reported, admittable := ns.GetAdmissionReported(), ns.GetMemAdmittableMib()
		needs := []uint64{0, 1, rng.Uint64N(20000)}
		if reported {
			for _, n := range []int64{admittable - 1, admittable, admittable + 1} {
				needs = append(needs, uint64(max(n, 0)))
			}
		}
		for _, need := range needs {
			ticket, err := srv.admit("Prime", need, classMemOnly)
			ticket.release()
			admitted := err == nil
			want := !reported || int64(need) <= admittable
			if admitted != want {
				t.Fatalf("state %d (model %s budget %d headroom %d): admit(%d) = %v (%v), reported (%v, %d)",
					i, model, budget, headroom, need, admitted, err, reported, admittable)
			}
		}
	}
}

// TestAdmissionRefusalLogRateLimited (S7): every refusal is counted, but each
// token logs at most once per window at Info, with the facts the decision used
// and the number of refusals swallowed since its last line.
func TestAdmissionRefusalLogRateLimited(t *testing.T) {
	drv := &fakeDriver{}
	client, srv := newTestServer(t, drv, &fakeTransport{}, 8)
	var buf bytes.Buffer
	srv.logger = slog.New(slog.NewJSONHandler(&buf, nil))
	now := time.Unix(1_700_000_000, 0)
	srv.admission.now = func() time.Time { return now }
	srv.memHeadroom = func() uint64 { return 2000 }
	seedBase(srv, "echo__logs", "echo")
	srv.registry.sync([]workloadEntry{{Workload: "echo", VCPUs: 1, MemMib: 2048}})
	prime := func() {
		t.Helper()
		if _, err := client.Prime(context.Background(), &nodev1.PrimeRequest{SnapshotRef: "echo__logs"}); status.Code(err) != codes.ResourceExhausted {
			t.Fatalf("Prime = %v, want ResourceExhausted", err)
		}
	}

	prime()
	now = now.Add(time.Second)
	prime()
	now = now.Add(time.Second)
	prime()
	records := refusalRecords(t, &buf)
	if len(records) != 1 {
		t.Fatalf("refusal log records in one window = %d, want 1: %v", len(records), records)
	}
	first := records[0]
	if first["level"] != "INFO" || first["verb"] != "Prime" || first["reason"] != string(reasonPressureMem) || first["model"] != "observed" {
		t.Fatalf("refusal record = %v, want an INFO Prime pressure:mem observed record", first)
	}
	for key, want := range map[string]float64{"need_mib": 2048, "admittable_mib": 1488, "headroom_mib": 2000, "floor_mib": 512, "live": 0, "inflight_vms": 0, "suppressed": 0} {
		if first[key] != want {
			t.Errorf("refusal record %s = %v, want %v", key, first[key], want)
		}
	}

	now = now.Add(refusalLogInterval)
	prime()
	records = refusalRecords(t, &buf)
	if len(records) != 2 || records[1]["suppressed"] != float64(2) {
		t.Fatalf("records after the window = %v, want a second record reporting 2 suppressed", records)
	}
	if got := srv.nodeStatus().GetAdmissionRefusals()[string(reasonPressureMem)]; got != 4 {
		t.Fatalf("admission_refusals[pressure:mem] = %d, want every refusal counted (4)", got)
	}
}

func refusalRecords(t *testing.T, buf *bytes.Buffer) []map[string]any {
	t.Helper()
	var out []map[string]any
	for _, line := range strings.Split(strings.TrimSpace(buf.String()), "\n") {
		if line == "" {
			continue
		}
		var rec map[string]any
		if err := json.Unmarshal([]byte(line), &rec); err != nil {
			t.Fatalf("decode log line %q: %v", line, err)
		}
		if rec["msg"] == "noded: admission refused" {
			out = append(out, rec)
		}
	}
	return out
}

// TestReservedRefusalMessageNamesTheModel: a reserved refusal reports what the
// reserved predicate compared, not the observed floor arithmetic.
func TestReservedRefusalMessageNamesTheModel(t *testing.T) {
	_, srv := newTestServer(t, &fakeDriver{live: 1, claimedMib: 400}, &fakeTransport{}, 8)
	srv.cfg.AdmissionModel = admissionModelReserved
	srv.memBudget = func() uint64 { return 1000 }
	_, err := srv.admit("Prime", 601, classMemOnly)
	if want := "noded: pressure:mem (need 601 MiB, admittable 600 MiB, model reserved)"; status.Convert(err).Message() != want {
		t.Fatalf("reserved refusal = %q, want %q", status.Convert(err).Message(), want)
	}
}

// TestPrimeVolumeColdBootCarriesWorkloadSizing (S18): a session Prime that may
// fall back to a cold boot carries the workload's registry sizing, the size it
// was admitted at, so the shared restore driver does not boot it at its own 1
// vCPU / 1024 MiB default. A Prime with no volume never cold boots and carries
// none.
func TestPrimeVolumeColdBootCarriesWorkloadSizing(t *testing.T) {
	drv := &fakeDriver{}
	client, srv := newTestServer(t, drv, &fakeTransport{}, 8)
	seedBase(srv, "sess__sizing", "claude-runtime")
	srv.registry.sync([]workloadEntry{{Workload: "claude-runtime", VCPUs: 2, MemMib: 3072}})

	if _, err := client.Prime(context.Background(), &nodev1.PrimeRequest{SnapshotRef: "sess__sizing", VolumeDiskPath: "/sessions/l/workspace.img"}); err != nil {
		t.Fatalf("volume Prime: %v", err)
	}
	if spec := drv.claimSpec(); spec.ColdBootVCPUs != 2 || spec.ColdBootMemMib != 3072 {
		t.Fatalf("volume Prime cold-boot sizing = (%d vCPUs, %d MiB), want (2, 3072)", spec.ColdBootVCPUs, spec.ColdBootMemMib)
	}
	if _, err := client.Prime(context.Background(), &nodev1.PrimeRequest{SnapshotRef: "sess__sizing"}); err != nil {
		t.Fatalf("plain Prime: %v", err)
	}
	if spec := drv.claimSpec(); spec.ColdBootVCPUs != 0 || spec.ColdBootMemMib != 0 {
		t.Fatalf("plain Prime carried cold-boot sizing (%d, %d), want none", spec.ColdBootVCPUs, spec.ColdBootMemMib)
	}
}

// TestPrimeNeedFallsBackToBaseRecordedMemMib: without a registry entry a Prime
// is charged the memory its base was built at (the driver's mem_mib sidecar, else
// the memfile size), so a reserved brick never charges 0 MiB for a real VM.
func TestPrimeNeedFallsBackToBaseRecordedMemMib(t *testing.T) {
	for _, tc := range []struct {
		name  string
		files map[string]func(path string) error
		want  uint32
	}{
		{name: "no metadata charges nothing", want: 0},
		{
			name:  "mem_mib sidecar",
			files: map[string]func(string) error{"mem_mib": func(p string) error { return os.WriteFile(p, []byte("3072\n"), 0o600) }},
			want:  3072,
		},
		{
			name: "memfile size rounded up",
			files: map[string]func(string) error{"memfile": func(p string) error {
				f, err := os.Create(p)
				if err != nil {
					return err
				}
				defer f.Close()
				return f.Truncate(2048<<20 + 1)
			}},
			want: 2049,
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			_, srv := newTestServer(t, &fakeDriver{}, &fakeTransport{}, 8)
			seedBase(srv, "echo__recorded", "echo")
			for name, write := range tc.files {
				if err := write(filepath.Join(srv.cfg.SnapshotRoot, "bases", "echo__recorded", name)); err != nil {
					t.Fatal(err)
				}
			}
			if _, mem := srv.primeSizing("echo", "echo__recorded"); mem != tc.want {
				t.Fatalf("primeSizing memMib = %d, want %d", mem, tc.want)
			}
			// The registry, when present, outranks the recorded size.
			srv.registry.sync([]workloadEntry{{Workload: "echo", VCPUs: 1, MemMib: 512}})
			if _, mem := srv.primeSizing("echo", "echo__recorded"); mem != 512 {
				t.Fatalf("primeSizing with a registry entry = %d, want 512", mem)
			}
		})
	}
}

// TestRelightNeedFallsBackToBundleRecordedMemMib: a banked session whose
// workload has no registry entry is charged the size recorded with its bundle.
func TestRelightNeedFallsBackToBundleRecordedMemMib(t *testing.T) {
	drv := &fakeDriver{}
	client, srv := newSessionTestServer(t, drv, &fakeTransport{}, 8)
	srv.cfg.AdmissionModel = admissionModelReserved
	srv.memBudget = func() uint64 { return 2000 }
	drv.mu.Lock()
	drv.sessionBundles = map[string]string{"big-ref": "state"}
	drv.mu.Unlock()
	srv.sessionSnap.add(sessionSnapshotEntry{snapshotRef: "big-ref", workload: "unsynced"})
	if err := os.MkdirAll(filepath.Join(drv.SessionsDir(), "big-ref"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(drv.SessionsDir(), "big-ref", "mem_mib"), []byte("3072"), 0o600); err != nil {
		t.Fatal(err)
	}
	_, err := client.Relight(context.Background(), &nodev1.RelightRequest{SnapshotRef: "big-ref", SessionId: "s-1"})
	if status.Code(err) != codes.ResourceExhausted || !strings.Contains(err.Error(), "need 3072 MiB") {
		t.Fatalf("Relight of a 3072 MiB bundle into a 2000 MiB budget = %v, want a pressure:mem refusal at need 3072", err)
	}
	if drv.restoreSessions != 0 {
		t.Fatalf("refused Relight restored %d sessions", drv.restoreSessions)
	}
}

// TestRestoreNeedChargesTheLargerOfRegistryAndBundle: a workload's memMib
// lowered after its base was built, or while a session was banked, must not
// shrink the reservation below the guest the restore lands, because the driver
// counts the restored VM at its snapshot's size (admission.tla, NoOvercommit).
func TestRestoreNeedChargesTheLargerOfRegistryAndBundle(t *testing.T) {
	drv := &fakeDriver{}
	_, srv := newSessionTestServer(t, drv, &fakeTransport{}, 8)
	srv.registry.sync([]workloadEntry{{Workload: "echo", VCPUs: 1, MemMib: 1024}})

	seedBase(srv, "echo__big", "echo")
	if err := os.WriteFile(filepath.Join(srv.cfg.SnapshotRoot, "bases", "echo__big", "mem_mib"), []byte("3072"), 0o600); err != nil {
		t.Fatal(err)
	}
	_, size := srv.primeSizing("echo", "echo__big")
	if size != 1024 {
		t.Fatalf("primeSizing memMib = %d, want the registry's 1024 for a cold boot", size)
	}
	if got := srv.primeAdmitMib(size, "echo__big"); got != 3072 {
		t.Fatalf("primeAdmitMib = %d, want the base snapshot's 3072", got)
	}
	if got := srv.primeAdmitMib(4096, "echo__big"); got != 4096 {
		t.Fatalf("primeAdmitMib above the snapshot = %d, want 4096", got)
	}

	if err := os.MkdirAll(filepath.Join(drv.SessionsDir(), "banked"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(drv.SessionsDir(), "banked", "mem_mib"), []byte("3072"), 0o600); err != nil {
		t.Fatal(err)
	}
	if got := srv.relightNeedMib("echo", "banked"); got != 3072 {
		t.Fatalf("relightNeedMib = %d, want the banked bundle's 3072 over the registry's 1024", got)
	}
	srv.registry.sync([]workloadEntry{{Workload: "echo", VCPUs: 1, MemMib: 4096}})
	if got := srv.relightNeedMib("echo", "banked"); got != 4096 {
		t.Fatalf("relightNeedMib = %d, want the registry's 4096 over the bundle's 3072", got)
	}
}

// writeBundleMemMib records a banked bundle's guest size the way the driver's
// mem_mib sidecar does.
func writeBundleMemMib(t *testing.T, dir string, memMib int) {
	t.Helper()
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "mem_mib"), []byte(fmt.Sprint(memMib)), 0o600); err != nil {
		t.Fatal(err)
	}
}

// TestTapAndGroupRelightChargeTheLargerOfDeclaredAndBundle: a serving, stateful
// or group-member RELIGHT lands the VM at its banked bundle's recorded size, so
// a request declaring a smaller memMib (lowered while the bundle was banked) is
// charged the bundle's size and refused on a reserved brick it would overcommit,
// before any tap or restore (admission.tla A2, NoOvercommit). A FRESH boot of
// the same declared size is still charged only what it declares.
func TestTapAndGroupRelightChargeTheLargerOfDeclaredAndBundle(t *testing.T) {
	res := &nodev1.ResourceSpec{Vcpus: 1, MemMib: 512}
	reserve := func(s *Server) {
		s.cfg.AdmissionModel = admissionModelReserved
		s.memBudget = func() uint64 { return 2000 }
	}
	assertBundleRefusal := func(t *testing.T, s *Server, err error) {
		t.Helper()
		if status.Code(err) != codes.ResourceExhausted || !strings.Contains(err.Error(), "need 3072 MiB") {
			t.Fatalf("relight of a 3072 MiB bundle declaring 512 MiB into a 2000 MiB budget = %v, want a pressure:mem refusal at need 3072", err)
		}
		if snap := s.admissionStatus(); snap.inflightMib != 0 || snap.inflightVMs != 0 {
			t.Fatalf("refused relight left a reservation: %+v", snap)
		}
	}

	t.Run("serving", func(t *testing.T) {
		s, fsn, fsd := newServingTestServer(t)
		reserve(s)
		writeBundleMemMib(t, filepath.Join(fsd.ServingDir(), "serv-big"), 3072)
		s.servingSnap.add(servingSnapshotEntry{snapshotRef: "serv-big", workload: "wl-serve"})
		_, err := s.StartServing(context.Background(), &nodev1.StartServingRequest{
			Resources: res,
			Source:    &nodev1.StartServingRequest_Relight{Relight: &nodev1.RelightSource{SnapshotRef: "serv-big"}},
			Port:      8080,
			Trace:     &nodev1.Trace{Workload: "wl-serve"},
		})
		assertBundleRefusal(t, s, err)
		if fsn.allocs != 0 || fsd.liveCount() != 0 {
			t.Fatalf("refused serving relight allocated %d taps and restored %d VMs", fsn.allocs, fsd.liveCount())
		}
		if got := s.servingAdmitMib(&nodev1.StartServingRequest{
			Resources: res,
			Source:    &nodev1.StartServingRequest_Fresh{Fresh: &nodev1.FreshSource{ServingImageRef: "serv-big"}},
		}); got != 512 {
			t.Fatalf("FRESH serving need = %d, want the declared 512", got)
		}
	})

	t.Run("stateful", func(t *testing.T) {
		s, fsn, fsd := newStatefulTestServer(t)
		reserve(s)
		writeBundleMemMib(t, filepath.Join(fsd.StatefulDir(), "stf-big"), 3072)
		relight := &nodev1.StartStatefulRequest{
			Resources:          res,
			Mode:               nodev1.StartStatefulMode_START_STATEFUL_MODE_RELIGHT,
			RelightSnapshotRef: "stf-big",
			BootImageRef:       "img-a",
			Port:               5432,
			Trace:              &nodev1.Trace{Workload: "wl-state"},
		}
		_, err := s.StartStateful(context.Background(), relight)
		assertBundleRefusal(t, s, err)
		if fsn.allocs != 0 || fsd.claimCount != 0 || fsd.restores != 0 {
			t.Fatalf("refused stateful relight allocated %d taps, booted %d and restored %d VMs", fsn.allocs, fsd.claimCount, fsd.restores)
		}
		cold := &nodev1.StartStatefulRequest{Resources: res, Mode: nodev1.StartStatefulMode_START_STATEFUL_MODE_COLD, RelightSnapshotRef: "stf-big"}
		if got := s.statefulAdmitMib(cold); got != 512 {
			t.Fatalf("COLD stateful need = %d, want the declared 512", got)
		}
	})

	t.Run("group member", func(t *testing.T) {
		s, _, gmd, _ := newGroupMemberTestServer(t)
		reserve(s)
		writeBundleMemMib(t, filepath.Join(gmd.GroupSetsDir(), "set-1", "server"), 3072)
		gmd.banked["set-1/server"] = true
		_, err := s.StartGroupMember(context.Background(), &nodev1.StartGroupMemberRequest{
			Resources:       res,
			Mode:            nodev1.StartGroupMemberMode_START_GROUP_MEMBER_MODE_RELIGHT,
			GroupInstanceId: "grp-A",
			MemberName:      "server",
			SnapshotRef:     "group/set-1/server",
			Ip:              "127.0.0.1",
			HealthPort:      8080,
			Trace:           &nodev1.Trace{Workload: "wl-group"},
		})
		assertBundleRefusal(t, s, err)
		if gmd.claims != 0 || gmd.restores != 0 {
			t.Fatalf("refused member relight booted %d and restored %d VMs", gmd.claims, gmd.restores)
		}
	})
}

// exhaustedCgroup installs a cgroup fixture whose numeric limit is fully used,
// the reading that used to collide with the unknown sentinel (S16).
func exhaustedCgroup(t *testing.T) {
	t.Helper()
	withBudgetFsRoot(t, map[string]string{
		"memory.max":     "4294967296\n",
		"memory.current": "4294967296\n",
		"memory.stat":    "file 0\nshmem 0\n",
	})
}

// TestPrimeRejectsWhenCgroupExhausted (S16): a brick whose cgroup is genuinely
// full refuses through the real reader end to end, instead of reading 0, taking
// it for "unknown" and failing open into an imminent OOM.
func TestPrimeRejectsWhenCgroupExhausted(t *testing.T) {
	exhaustedCgroup(t)
	drv := &fakeDriver{}
	client, srv := newTestServer(t, drv, &fakeTransport{}, 8)
	srv.memHeadroom = newBudget(0).MemHeadroomMib
	seedBase(srv, "echo__exhausted", "echo")
	srv.registry.sync([]workloadEntry{{Workload: "echo", VCPUs: 1, MemMib: 3072}})

	_, err := client.Prime(context.Background(), &nodev1.PrimeRequest{SnapshotRef: "echo__exhausted"})
	if status.Code(err) != codes.ResourceExhausted || !strings.Contains(err.Error(), string(reasonPressureMem)) {
		t.Fatalf("Prime into an exhausted cgroup = %v, want ResourceExhausted %s", err, reasonPressureMem)
	}
	if got := drv.LiveCount(); got != 0 {
		t.Fatalf("LiveCount = %d, want 0", got)
	}
}

// TestActivatorServingWakeRejectsWhenCgroupExhausted (S16): the node-local
// activator wake has no control-plane gate in front of it, so the daemon's own
// predicate is the only thing between a full brick and another boot.
func TestActivatorServingWakeRejectsWhenCgroupExhausted(t *testing.T) {
	exhaustedCgroup(t)
	s, fsn, fsd := newServingTestServer(t)
	s.memHeadroom = newBudget(0).MemHeadroomMib
	_, err := s.startServing(context.Background(), &nodev1.StartServingRequest{
		Resources: &nodev1.ResourceSpec{Vcpus: 1, MemMib: 128},
		Source:    &nodev1.StartServingRequest_Fresh{Fresh: &nodev1.FreshSource{ServingImageRef: "img-a"}},
		Port:      8080,
		Trace:     &nodev1.Trace{Workload: "wl-serve"},
	}, nodev1.InstanceOrigin_INSTANCE_ORIGIN_ACTIVATOR)
	if status.Code(err) != codes.ResourceExhausted || !strings.Contains(err.Error(), string(reasonPressureMem)) {
		t.Fatalf("activator wake into an exhausted cgroup = %v, want ResourceExhausted %s", err, reasonPressureMem)
	}
	if fsn.allocs != 0 || fsd.claims != 0 {
		t.Fatalf("refused wake allocated %d taps and booted %d VMs", fsn.allocs, fsd.claims)
	}
}
