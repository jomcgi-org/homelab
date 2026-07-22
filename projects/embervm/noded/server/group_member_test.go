package server

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strconv"
	"sync"
	"testing"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	nodev1 "github.com/jomcgi/homelab/projects/embervm/proto/embervm/node/v1"

	"github.com/jomcgi/homelab/projects/embervm/noded/config"
	"github.com/jomcgi/homelab/projects/embervm/noded/substrate"
)

// fakeGroupMemberDriver cold-boots and banks/relights member VMs in memory, recording
// the env each FRESH boot carried and the set/member each bank wrote under, mirroring
// fakeStatefulDriver's shape.
type fakeGroupMemberDriver struct {
	mu           sync.Mutex
	live         int
	claims       int
	lastEnv      map[string]string
	banked       map[string]bool // "set/member" -> banked
	failClaim    error
	groupSetsDir string
}

func newFakeGroupMemberDriver(dir string) *fakeGroupMemberDriver {
	return &fakeGroupMemberDriver{banked: map[string]bool{}, groupSetsDir: dir + "/group"}
}

func (f *fakeGroupMemberDriver) ClaimGroupMember(_ context.Context, _ string, _ string, _ int, _ int, _ substrate.NICSpec, env map[string]string) (substrate.Handle, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.failClaim != nil {
		return substrate.Handle{}, f.failClaim
	}
	f.claims++
	f.live++
	f.lastEnv = env
	return substrate.Handle{ID: "grpm-vm-" + strconv.Itoa(f.claims), ThreadID: "t-" + strconv.Itoa(f.claims), Node: "node-4"}, nil
}

func (f *fakeGroupMemberDriver) SnapshotGroupMember(_ context.Context, _ substrate.Handle, setID, memberName string) (substrate.SnapshotRef, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	ref := "group/" + setID + "/" + memberName
	f.banked[setID+"/"+memberName] = true
	return substrate.SnapshotRef{ID: ref, SizeBytes: 5120}, nil
}

func (f *fakeGroupMemberDriver) RestoreGroupMember(_ context.Context, setID, memberName string) (substrate.Handle, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if !f.banked[setID+"/"+memberName] {
		return substrate.Handle{}, status.Errorf(codes.FailedPrecondition, "no such banked member %s/%s", setID, memberName)
	}
	f.live++
	return substrate.Handle{ID: "relit-" + setID + "-" + memberName, ThreadID: "t-relit", Node: "node-4"}, nil
}

func (f *fakeGroupMemberDriver) RemoveGroupMemberBundle(setID, memberName string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	delete(f.banked, setID+"/"+memberName)
	return nil
}

func (f *fakeGroupMemberDriver) ScanGroupBundleSets() []substrate.GroupBundleSetInfo {
	f.mu.Lock()
	defer f.mu.Unlock()
	bySet := map[string][]substrate.GroupBundleMemberInfo{}
	for key := range f.banked {
		// key is "set/member".
		var setID, member string
		for i := 0; i < len(key); i++ {
			if key[i] == '/' {
				setID, member = key[:i], key[i+1:]
				break
			}
		}
		bySet[setID] = append(bySet[setID], substrate.GroupBundleMemberInfo{
			MemberName:  member,
			SnapshotRef: "group/" + setID + "/" + member,
			SizeBytes:   5120,
		})
	}
	out := make([]substrate.GroupBundleSetInfo, 0, len(bySet))
	for setID, members := range bySet {
		out = append(out, substrate.GroupBundleSetInfo{SetID: setID, Members: members, CreatedAtUnixMs: 1})
	}
	return out
}

func (f *fakeGroupMemberDriver) GroupSetsDir() string { return f.groupSetsDir }

// groupMemberVMDriverAdapter makes fakeGroupMemberDriver satisfy the task vmDriver
// seam so the node cap + reap path see member VMs, mirroring statefulVMDriverAdapter.
type groupMemberVMDriverAdapter struct {
	*fakeGroupMemberDriver
}

func (a groupMemberVMDriverAdapter) Claim(_ context.Context, _ substrate.ClaimSpec) (substrate.Handle, error) {
	return substrate.Handle{}, status.Error(codes.Unimplemented, "unused")
}

func (a groupMemberVMDriverAdapter) Release(_ context.Context, _ substrate.Handle) error {
	a.fakeGroupMemberDriver.mu.Lock()
	if a.fakeGroupMemberDriver.live > 0 {
		a.fakeGroupMemberDriver.live--
	}
	a.fakeGroupMemberDriver.mu.Unlock()
	return nil
}
func (a groupMemberVMDriverAdapter) RemoveBundle(_ string) error         { return nil }
func (a groupMemberVMDriverAdapter) VsockUDSPath(threadID string) string { return "/tmp/" + threadID }
func (a groupMemberVMDriverAdapter) Stats(_ substrate.Handle) (substrate.GuestStats, error) {
	return substrate.GuestStats{}, nil
}

func (a groupMemberVMDriverAdapter) LiveCount() int {
	a.fakeGroupMemberDriver.mu.Lock()
	defer a.fakeGroupMemberDriver.mu.Unlock()
	return a.fakeGroupMemberDriver.live
}

// fakeGroupClock scripts the clock-resync outcome: err set means the read-back was
// out of tolerance (or a timeout), nil means verified within one second. It records
// that Resync was called so a test can assert the resync ran on RELIGHT (and never
// on FRESH).
type fakeGroupClock struct {
	mu      sync.Mutex
	calls   int
	err     error
	lastUDS string
}

func (c *fakeGroupClock) Resync(_ context.Context, uds string) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.calls++
	c.lastUDS = uds
	if c.err != nil {
		return fmt.Errorf("fake group clock resync: %w", c.err)
	}
	return nil
}

// newGroupMemberTestServer wires a Server with the full group member lifecycle
// (network + member driver + clock), a provisioned image "src-a" the FRESH source
// resolves against, and short ready timeouts.
func newGroupMemberTestServer(t *testing.T) (*Server, *fakeGroupNet, *fakeGroupMemberDriver, *fakeGroupClock) {
	t.Helper()
	dir := t.TempDir()
	gn := newFakeGroupNet()
	gr := newFakeGroupRecords(t.TempDir())
	gmd := newFakeGroupMemberDriver(dir)
	clock := &fakeGroupClock{}
	s := New(Options{
		Config: config.Config{
			Arch: "amd64", Node: "node-4", MaxLiveVMs: 8, SnapshotRoot: dir,
			BootReadyTimeout:    2 * time.Second,
			RestoreReadyTimeout: 2 * time.Second,
			Images:              map[string]config.Image{"src-a": {RootfsPath: "/rootfs/a"}},
		},
		Driver:       groupMemberVMDriverAdapter{gmd},
		Transport:    &fakeTransport{},
		GroupNet:     gn,
		GroupRecords: gr,
		GroupDriver:  gmd,
		GroupClock:   clock,
		Logger:       slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	s.memHeadroom = func() uint64 { return 0 }
	// A composite member FRESH is a plain rootfs cold boot: the source names a
	// PROVISIONED IMAGE directly (the control plane sends member.image_ref), not a
	// built base snapshot. "src-a" is provisioned above in Config.Images.
	// Stand up the group network so StartGroupMember's existence check passes.
	if _, err := s.CreateGroupNetwork(context.Background(), &nodev1.CreateGroupNetworkRequest{
		GroupInstanceId: "grp-A", Cidr: "10.101.1.0/24",
	}); err != nil {
		t.Fatalf("CreateGroupNetwork: %v", err)
	}
	return s, gn, gmd, clock
}

// startFreshMember FRESH-boots a member against a ready loopback TCP endpoint.
func startFreshMember(t *testing.T, s *Server, port uint32, member string, index uint32) *nodev1.StartGroupMemberResponse {
	t.Helper()
	resp, err := s.StartGroupMember(context.Background(), &nodev1.StartGroupMemberRequest{
		Mode:            nodev1.StartGroupMemberMode_START_GROUP_MEMBER_MODE_FRESH,
		GroupInstanceId: "grp-A",
		MemberName:      member,
		MemberIndex:     index,
		Ip:              "127.0.0.1",
		Source:          "src-a",
		HealthPort:      port,
		Env:             map[string]string{"EMBER_GROUP_ROLE": "worker"},
	})
	if err != nil {
		t.Fatalf("StartGroupMember(fresh): %v", err)
	}
	return resp
}

// TestStartGroupMemberFreshResolvesFromPushedRegistry proves the
// artifact-decoupling Phase 2 prod condition: with cfg.Images EMPTY, a composite
// group member cold boot resolves its per-member rootfs from the
// control-plane-PUSHED registry (by the member image_ref) instead of the config
// table. A composite CR carries several member images under one workload, so this
// resolution MUST be by image_ref, not by the group workload.
func TestStartGroupMemberFreshResolvesFromPushedRegistry(t *testing.T) {
	port := tcpHealthServer(t)
	s, _, _, _ := newGroupMemberTestServer(t)

	s.cfg.Images = map[string]config.Image{}
	s.registry.sync([]workloadEntry{
		{Workload: "image:src-a", ImageRef: "src-a", RootfsRef: "/rootfs/a", HarnessInit: "/init"},
	})

	resp := startFreshMember(t, s, port, "worker-0", 0)
	if resp.GetVmId() == "" {
		t.Fatal("StartGroupMember(fresh) resolved no VM from the pushed registry")
	}
}

// TestStartGroupMemberFreshRefusedWhileStale proves a stale registry refuses a
// FRESH member cold boot (new-work placement) with FailedPrecondition.
func TestStartGroupMemberFreshRefusedWhileStale(t *testing.T) {
	port := tcpHealthServer(t)
	s, _, _, _ := newGroupMemberTestServer(t)
	s.registry.mu.Lock()
	s.registry.stale = true
	s.registry.mu.Unlock()

	_, err := s.StartGroupMember(context.Background(), &nodev1.StartGroupMemberRequest{
		Mode:            nodev1.StartGroupMemberMode_START_GROUP_MEMBER_MODE_FRESH,
		GroupInstanceId: "grp-A",
		MemberName:      "worker-0",
		Ip:              "127.0.0.1",
		HealthPort:      port,
		Source:          "src-a",
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("FRESH StartGroupMember while stale: err = %v, want FailedPrecondition", err)
	}
}

// TestStartGroupMemberFreshBootsOnGroupBridge proves a FRESH member cold-boots on the
// group bridge (tap pinned), health-gates, carries its env, is reported in
// group_member_vms, and bumps the network's member_count. No clock resync on FRESH.
func TestStartGroupMemberFreshBootsOnGroupBridge(t *testing.T) {
	port := tcpHealthServer(t)
	s, gn, gmd, clock := newGroupMemberTestServer(t)

	resp := startFreshMember(t, s, port, "worker-0", 0)
	if resp.GetVmId() == "" {
		t.Error("no vm_id returned")
	}
	if resp.GetIp() != "127.0.0.1" {
		t.Errorf("ip = %q want the pinned 127.0.0.1", resp.GetIp())
	}
	if resp.GetWasRelight() {
		t.Error("a FRESH boot must report was_relight=false")
	}
	if gmd.claims != 1 {
		t.Errorf("ClaimGroupMember calls = %d want 1", gmd.claims)
	}
	if gmd.lastEnv["EMBER_GROUP_ROLE"] != "worker" {
		t.Errorf("env not threaded into the FRESH boot: %v", gmd.lastEnv)
	}
	if !gn.taps["worker-0"] {
		t.Error("member tap was not pinned on the group bridge")
	}
	if clock.calls != 0 {
		t.Errorf("FRESH boot must NOT clock-resync (calls=%d)", clock.calls)
	}

	ns := s.nodeStatus()
	if len(ns.GetGroupMemberVms()) != 1 {
		t.Fatalf("group_member_vms = %d want 1", len(ns.GetGroupMemberVms()))
	}
	m := ns.GetGroupMemberVms()[0]
	if m.GetGroupInstanceId() != "grp-A" || m.GetMemberName() != "worker-0" || !m.GetHealthy() {
		t.Errorf("member status = %+v", m)
	}
	// member_count on the network must reflect the live member.
	if len(ns.GetGroupNetworks()) != 1 || ns.GetGroupNetworks()[0].GetMemberCount() != 1 {
		t.Errorf("group network member_count = %v want 1", ns.GetGroupNetworks())
	}
}

// TestStartGroupMemberEntryInstallsDNAT proves the entry member (entry_guest_port >
// 0) installs the entry DNAT exposing {pod IP, vmPort} -> {tap, entry_guest_port},
// and a non-entry member (0) installs none. Regression for the entry-EOF: the entry
// endpoint the control plane publishes was unreachable because StartGroupMember never
// called EnsureEntryDNAT.
func TestStartGroupMemberEntryInstallsDNAT(t *testing.T) {
	port := tcpHealthServer(t)
	s, gn, _, _ := newGroupMemberTestServer(t)

	// A non-entry member installs no DNAT.
	startFreshMember(t, s, port, "worker-1", 1)
	if len(gn.entryDNATs) != 0 {
		t.Fatalf("non-entry member installed an entry DNAT: %+v", gn.entryDNATs)
	}

	// The entry member (entry_guest_port > 0) installs the DNAT at that guest port.
	if _, err := s.StartGroupMember(context.Background(), &nodev1.StartGroupMemberRequest{
		Mode:            nodev1.StartGroupMemberMode_START_GROUP_MEMBER_MODE_FRESH,
		GroupInstanceId: "grp-A",
		MemberName:      "server",
		MemberIndex:     0,
		Ip:              "127.0.0.1",
		Source:          "src-a",
		HealthPort:      port,
		EntryGuestPort:  6443,
	}); err != nil {
		t.Fatalf("StartGroupMember(entry): %v", err)
	}
	if len(gn.entryDNATs) != 1 {
		t.Fatalf("entry member entry DNAT calls = %d want 1 (%+v)", len(gn.entryDNATs), gn.entryDNATs)
	}
	got := gn.entryDNATs[0]
	if got.groupInstanceID != "grp-A" || got.entryIP != "127.0.0.1" || got.guestPort != 6443 {
		t.Errorf("entry DNAT = %+v want {grp-A 127.0.0.1 6443}", got)
	}
}

// TestStartGroupMemberEntryReportsEndpoint proves the ENTRY member's response
// carries the daemon's endpoint projection ({pod IP, vmPort}) when DNAT is
// enabled, and that non-entry members (and a DNAT-disabled daemon) report none.
// Regression for the F-bug: the control plane used to publish its OWN pod IP,
// an address the entry DNAT does not live at.
func TestStartGroupMemberEntryReportsEndpoint(t *testing.T) {
	port := tcpHealthServer(t)
	s, gn, _, _ := newGroupMemberTestServer(t)
	gn.podIP = "10.42.1.95"

	// Non-entry member: no endpoint reported.
	worker := startFreshMember(t, s, port, "worker-1", 1)
	if worker.GetEndpointIp() != "" || worker.GetEndpointPort() != 0 {
		t.Errorf("non-entry member reported an endpoint: %q:%d", worker.GetEndpointIp(), worker.GetEndpointPort())
	}

	// Entry member: the response carries the daemon's projection.
	resp, err := s.StartGroupMember(context.Background(), &nodev1.StartGroupMemberRequest{
		Mode:            nodev1.StartGroupMemberMode_START_GROUP_MEMBER_MODE_FRESH,
		GroupInstanceId: "grp-A",
		MemberName:      "server",
		MemberIndex:     0,
		Ip:              "127.0.0.1",
		Source:          "src-a",
		HealthPort:      port,
		EntryGuestPort:  6443,
	})
	if err != nil {
		t.Fatalf("StartGroupMember(entry): %v", err)
	}
	if resp.GetEndpointIp() != "10.42.1.95" || resp.GetEndpointPort() != 30000+6443 {
		t.Errorf("entry endpoint = %q:%d want 10.42.1.95:%d", resp.GetEndpointIp(), resp.GetEndpointPort(), 30000+6443)
	}
}

// TestStartGroupMemberEntryNoDNATReportsNoEndpoint proves a DNAT-disabled daemon
// (no pod IP; EntryEndpoint falls back to the tap) reports NO endpoint, so the
// control plane falls back to its own derivation instead of publishing a
// node-internal tap address.
func TestStartGroupMemberEntryNoDNATReportsNoEndpoint(t *testing.T) {
	port := tcpHealthServer(t)
	s, _, _, _ := newGroupMemberTestServer(t)

	resp, err := s.StartGroupMember(context.Background(), &nodev1.StartGroupMemberRequest{
		Mode:            nodev1.StartGroupMemberMode_START_GROUP_MEMBER_MODE_FRESH,
		GroupInstanceId: "grp-A",
		MemberName:      "server",
		MemberIndex:     0,
		Ip:              "127.0.0.1",
		Source:          "src-a",
		HealthPort:      port,
		EntryGuestPort:  6443,
	})
	if err != nil {
		t.Fatalf("StartGroupMember(entry): %v", err)
	}
	if resp.GetEndpointIp() != "" || resp.GetEndpointPort() != 0 {
		t.Errorf("DNAT-disabled daemon reported an endpoint: %q:%d", resp.GetEndpointIp(), resp.GetEndpointPort())
	}
}

// TestGroupMemberReadyBudget proves the request's ready_budget_seconds overrides
// the daemon default and 0 keeps it. Regression for the silent 60s reap: noded's
// own BootReadyTimeout undercut the workload's wakeTimeoutSeconds, so the k3s
// agents (kubelet up only after the full join) were reaped mid-join with the
// control plane still inside its wake bound.
func TestGroupMemberReadyBudget(t *testing.T) {
	req := &nodev1.StartGroupMemberRequest{ReadyBudgetSeconds: 180}
	if got := groupMemberReadyBudget(req, 60*time.Second); got != 180*time.Second {
		t.Errorf("override budget = %v want 180s", got)
	}
	if got := groupMemberReadyBudget(&nodev1.StartGroupMemberRequest{}, 60*time.Second); got != 60*time.Second {
		t.Errorf("default budget = %v want 60s", got)
	}
}

// TestStartGroupMemberEntryDNATFailureReaps proves an entry DNAT install failure
// reaps the member (no half-published entry whose DNAT never landed).
func TestStartGroupMemberEntryDNATFailureReaps(t *testing.T) {
	port := tcpHealthServer(t)
	s, gn, _, _ := newGroupMemberTestServer(t)
	gn.entryDNATErr = errors.New("nft apply failed")

	_, err := s.StartGroupMember(context.Background(), &nodev1.StartGroupMemberRequest{
		Mode:            nodev1.StartGroupMemberMode_START_GROUP_MEMBER_MODE_FRESH,
		GroupInstanceId: "grp-A",
		MemberName:      "server",
		MemberIndex:     0,
		Ip:              "127.0.0.1",
		Source:          "src-a",
		HealthPort:      port,
		EntryGuestPort:  6443,
	})
	if status.Code(err) != codes.Internal {
		t.Fatalf("entry DNAT failure code = %v want Internal", status.Code(err))
	}
	if len(gn.removedTaps) != 1 {
		t.Errorf("entry DNAT failure did not remove the member tap: %v", gn.removedTaps)
	}
	if len(s.nodeStatus().GetGroupMemberVms()) != 0 {
		t.Error("a member whose entry DNAT failed must not be published")
	}
}

// TestStartGroupMemberFreshHealthGateFailureReaps proves a member that never health-
// gates is reaped and its tap removed, and no member is published.
func TestStartGroupMemberFreshHealthGateFailure(t *testing.T) {
	s, gn, _, _ := newGroupMemberTestServer(t)
	// Use a port with no listener so the health-gate times out.
	_, err := s.StartGroupMember(context.Background(), &nodev1.StartGroupMemberRequest{
		Mode:            nodev1.StartGroupMemberMode_START_GROUP_MEMBER_MODE_FRESH,
		GroupInstanceId: "grp-A", MemberName: "worker-0", MemberIndex: 0,
		Ip: "127.0.0.1", Source: "src-a", HealthPort: 1, // port 1: nothing listens
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("health-gate failure: got %v want FailedPrecondition", err)
	}
	if len(gn.removedTaps) == 0 {
		t.Error("a failed member start must remove its tap")
	}
	if len(s.nodeStatus().GetGroupMemberVms()) != 0 {
		t.Error("no member should be published on a health-gate failure")
	}
}

// TestStartGroupMemberFreshUnprovisionedImageFails proves a FRESH source that is not
// a provisioned image on this node is rejected with FailedPrecondition (no VM start,
// no tap). This is the regression guard for the composite-boot outage: the control
// plane sends member.image_ref as the source and it must resolve directly against
// Config.Images. An image absent from that table (a provisioning gap, or a source
// that is a base key rather than an image_ref) fails here rather than silently.
func TestStartGroupMemberFreshUnprovisionedImageFails(t *testing.T) {
	s, gn, gmd, _ := newGroupMemberTestServer(t)
	_, err := s.StartGroupMember(context.Background(), &nodev1.StartGroupMemberRequest{
		Mode:            nodev1.StartGroupMemberMode_START_GROUP_MEMBER_MODE_FRESH,
		GroupInstanceId: "grp-A", MemberName: "worker-0", MemberIndex: 0,
		Ip: "127.0.0.1", Source: "not-provisioned", HealthPort: 1,
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("unprovisioned FRESH source: got %v want FailedPrecondition", err)
	}
	if gmd.claims != 0 {
		t.Errorf("no VM should be claimed for an unresolvable source (claims=%d)", gmd.claims)
	}
	if len(gn.taps) != 0 {
		t.Error("no tap should be pinned for an unresolvable source")
	}
}

// TestStartGroupMemberRelightResyncsClockAndPinsWorld proves a RELIGHT recreates the
// SAME pinned tap world (same member + index), resumes the bundle, runs the clock
// resync BEFORE the health gate, and reports was_relight=true.
func TestStartGroupMemberRelightResyncsClockAndPinsWorld(t *testing.T) {
	port := tcpHealthServer(t)
	s, gn, _, clock := newGroupMemberTestServer(t)

	fresh := startFreshMember(t, s, port, "worker-0", 0)
	bankResp, err := s.StopGroupMember(context.Background(), &nodev1.StopGroupMemberRequest{
		VmId: fresh.GetVmId(), Mode: nodev1.StopGroupMemberMode_STOP_GROUP_MEMBER_MODE_BANK,
		SetId: "set-1", MemberName: "worker-0",
	})
	if err != nil {
		t.Fatalf("StopGroupMember(bank): %v", err)
	}

	// Reset the pin-call record so we can assert the relight re-pins the same world.
	gn.mu.Lock()
	gn.ensureCalls = nil
	gn.mu.Unlock()

	relit, err := s.StartGroupMember(context.Background(), &nodev1.StartGroupMemberRequest{
		Mode:            nodev1.StartGroupMemberMode_START_GROUP_MEMBER_MODE_RELIGHT,
		GroupInstanceId: "grp-A", MemberName: "worker-0", MemberIndex: 0,
		Ip: "127.0.0.1", SnapshotRef: bankResp.GetSnapshotRef(), HealthPort: port,
	})
	if err != nil {
		t.Fatalf("StartGroupMember(relight): %v", err)
	}
	if !relit.GetWasRelight() {
		t.Error("a verified relight must report was_relight=true")
	}
	if clock.calls != 1 {
		t.Errorf("relight must clock-resync exactly once (calls=%d)", clock.calls)
	}
	// Pinned-world reconstruction: the relight re-pinned the SAME member + index.
	gn.mu.Lock()
	defer gn.mu.Unlock()
	if len(gn.ensureCalls) != 1 || gn.ensureCalls[0].member != "worker-0" || gn.ensureCalls[0].index != 0 {
		t.Errorf("relight did not re-pin the same member/index: %+v", gn.ensureCalls)
	}
	if gn.ensureCalls[0].wantIP != "127.0.0.1" {
		t.Errorf("relight pinned a different IP: %q", gn.ensureCalls[0].wantIP)
	}
}

// TestStartGroupMemberRelightClockFailureFailsCall proves a clock resync that reports
// a read-back more than one second off FAILS the relight (FAILED_PRECONDITION) and
// reaps the VM.
func TestStartGroupMemberRelightClockFailure(t *testing.T) {
	port := tcpHealthServer(t)
	s, gn, _, clock := newGroupMemberTestServer(t)
	fresh := startFreshMember(t, s, port, "worker-0", 0)
	bankResp, err := s.StopGroupMember(context.Background(), &nodev1.StopGroupMemberRequest{
		VmId: fresh.GetVmId(), Mode: nodev1.StopGroupMemberMode_STOP_GROUP_MEMBER_MODE_BANK,
		SetId: "set-1", MemberName: "worker-0",
	})
	if err != nil {
		t.Fatalf("bank: %v", err)
	}

	clock.err = errors.New("guest clock read-back 3s off host (limit 1s)")
	prevRemoved := len(gn.removedTaps)
	_, err = s.StartGroupMember(context.Background(), &nodev1.StartGroupMemberRequest{
		Mode:            nodev1.StartGroupMemberMode_START_GROUP_MEMBER_MODE_RELIGHT,
		GroupInstanceId: "grp-A", MemberName: "worker-0", MemberIndex: 0,
		Ip: "127.0.0.1", SnapshotRef: bankResp.GetSnapshotRef(), HealthPort: port,
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("clock resync failure: got %v want FailedPrecondition", err)
	}
	if len(gn.removedTaps) <= prevRemoved {
		t.Error("a failed relight must remove its tap")
	}
	if len(s.nodeStatus().GetGroupMemberVms()) != 0 {
		t.Error("no member should be published on a clock-resync failure")
	}
}

// TestStopGroupMemberBankWritesSetDirLayout proves BANK snapshots under the caller-
// supplied set dir, records the bundle grouped by set, and returns the ref+size.
func TestStopGroupMemberBankWritesSetDirLayout(t *testing.T) {
	port := tcpHealthServer(t)
	s, _, gmd, _ := newGroupMemberTestServer(t)
	fresh := startFreshMember(t, s, port, "worker-0", 0)

	bankResp, err := s.StopGroupMember(context.Background(), &nodev1.StopGroupMemberRequest{
		VmId: fresh.GetVmId(), Mode: nodev1.StopGroupMemberMode_STOP_GROUP_MEMBER_MODE_BANK,
		SetId: "set-1", MemberName: "worker-0",
	})
	if err != nil {
		t.Fatalf("bank: %v", err)
	}
	if bankResp.GetSnapshotRef() != "group/set-1/worker-0" {
		t.Errorf("bank ref = %q want group/set-1/worker-0", bankResp.GetSnapshotRef())
	}
	if bankResp.GetSizeBytes() == 0 {
		t.Error("bank should report a size")
	}
	if !gmd.banked["set-1/worker-0"] {
		t.Error("driver did not bank under set-1/worker-0")
	}
	// The banked bundle is reported grouped by set in NodeStatus.
	ns := s.nodeStatus()
	if len(ns.GetGroupBundleSets()) != 1 {
		t.Fatalf("group_bundle_sets = %d want 1", len(ns.GetGroupBundleSets()))
	}
	set := ns.GetGroupBundleSets()[0]
	if set.GetSetId() != "set-1" || len(set.GetMembers()) != 1 || set.GetMembers()[0].GetMemberName() != "worker-0" {
		t.Errorf("bundle set = %+v", set)
	}
	// The live member is gone (banked destroys it).
	if len(ns.GetGroupMemberVms()) != 0 {
		t.Error("a banked member must no longer be live")
	}
}

// TestStopGroupMemberDestroy proves DESTROY tears the member down with no snapshot.
func TestStopGroupMemberDestroy(t *testing.T) {
	port := tcpHealthServer(t)
	s, gn, gmd, _ := newGroupMemberTestServer(t)
	fresh := startFreshMember(t, s, port, "worker-0", 0)

	resp, err := s.StopGroupMember(context.Background(), &nodev1.StopGroupMemberRequest{
		VmId: fresh.GetVmId(), Mode: nodev1.StopGroupMemberMode_STOP_GROUP_MEMBER_MODE_DESTROY,
	})
	if err != nil {
		t.Fatalf("destroy: %v", err)
	}
	if resp.GetSnapshotRef() != "" || resp.GetSizeBytes() != 0 {
		t.Errorf("DESTROY must produce no snapshot: %+v", resp)
	}
	if len(gmd.banked) != 0 {
		t.Error("DESTROY must not bank anything")
	}
	if len(gn.removedTaps) == 0 {
		t.Error("DESTROY must remove the member tap")
	}
	if len(s.nodeStatus().GetGroupMemberVms()) != 0 {
		t.Error("member should be gone after destroy")
	}
	// Idempotent: destroying an unknown vm is OK.
	if _, err := s.StopGroupMember(context.Background(), &nodev1.StopGroupMemberRequest{
		VmId: "ghost", Mode: nodev1.StopGroupMemberMode_STOP_GROUP_MEMBER_MODE_DESTROY,
	}); err != nil {
		t.Errorf("destroy of an unknown vm should be idempotent OK: %v", err)
	}
}

// TestStopGroupMemberConcurrentStopRefused proves a second in-flight stop on the same
// vm_id is refused (the concurrent-stop refusal per vm_id).
func TestStopGroupMemberConcurrentStopRefused(t *testing.T) {
	port := tcpHealthServer(t)
	s, _, _, _ := newGroupMemberTestServer(t)
	fresh := startFreshMember(t, s, port, "worker-0", 0)

	// Manually mark the member in-flight (as a first stop would), then a second stop
	// must be refused.
	e := s.groupMembers.get(fresh.GetVmId())
	if e == nil {
		t.Fatal("member not registered")
	}
	if _, ok := s.groupMembers.beginStop(fresh.GetVmId()); !ok {
		t.Fatal("first beginStop should succeed")
	}
	_, err := s.StopGroupMember(context.Background(), &nodev1.StopGroupMemberRequest{
		VmId: fresh.GetVmId(), Mode: nodev1.StopGroupMemberMode_STOP_GROUP_MEMBER_MODE_BANK,
		SetId: "set-1", MemberName: "worker-0",
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("second concurrent stop: got %v want FailedPrecondition", err)
	}
}

// TestStartGroupMemberRelightUnknownBundleFails proves a relight of an unknown ref
// fails (FAILED_PRECONDITION) and never deletes the bundle (a lost member surfaces).
func TestStartGroupMemberRelightUnknownBundleFails(t *testing.T) {
	s, _, _, _ := newGroupMemberTestServer(t)
	_, err := s.StartGroupMember(context.Background(), &nodev1.StartGroupMemberRequest{
		Mode:            nodev1.StartGroupMemberMode_START_GROUP_MEMBER_MODE_RELIGHT,
		GroupInstanceId: "grp-A", MemberName: "worker-0", MemberIndex: 0,
		Ip: "127.0.0.1", SnapshotRef: "group/set-x/worker-0", HealthPort: 8080,
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("relight of unknown bundle: got %v want FailedPrecondition", err)
	}
}

// TestGroupMemberVerbsUnimplementedWithoutDriver proves a Server without a group
// member driver returns Unimplemented (builds without group support untouched).
func TestGroupMemberVerbsUnimplementedWithoutDriver(t *testing.T) {
	s := New(Options{
		Config:    config.Config{Arch: "amd64", Node: "node-4", SnapshotRoot: t.TempDir()},
		Driver:    &fakeDriver{},
		Transport: &fakeTransport{},
		Logger:    slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	if _, err := s.StartGroupMember(context.Background(), &nodev1.StartGroupMemberRequest{GroupInstanceId: "g", MemberName: "m", HealthPort: 1, Ip: "127.0.0.1"}); status.Code(err) != codes.Unimplemented {
		t.Errorf("StartGroupMember without a driver should be Unimplemented, got %v", err)
	}
	if _, err := s.StopGroupMember(context.Background(), &nodev1.StopGroupMemberRequest{VmId: "v"}); status.Code(err) != codes.Unimplemented {
		t.Errorf("StopGroupMember without a driver should be Unimplemented, got %v", err)
	}
}

// TestReconcileGroupBundlesFromDisk proves the boot rescan re-seeds the banked-group
// inventory (grouped by set) so a restarted daemon reports surviving warmth; live
// members do NOT survive a restart.
func TestReconcileGroupBundlesFromDisk(t *testing.T) {
	dir := t.TempDir()
	gmd := newFakeGroupMemberDriver(dir)
	gmd.banked["set-1/worker-0"] = true
	gmd.banked["set-1/worker-1"] = true
	gmd.banked["set-2/leader"] = true

	s := New(Options{
		Config:       config.Config{Arch: "amd64", Node: "node-4", SnapshotRoot: dir},
		Driver:       groupMemberVMDriverAdapter{gmd},
		Transport:    &fakeTransport{},
		GroupNet:     newFakeGroupNet(),
		GroupRecords: newFakeGroupRecords(t.TempDir()),
		GroupDriver:  gmd,
		Logger:       slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	s.memHeadroom = func() uint64 { return 0 }
	s.ReconcileGroupBundlesFromDisk()

	ns := s.nodeStatus()
	if len(ns.GetGroupBundleSets()) != 2 {
		t.Fatalf("rescan found %d bundle sets want 2", len(ns.GetGroupBundleSets()))
	}
	// No live members after a restart.
	if len(ns.GetGroupMemberVms()) != 0 {
		t.Error("no live members should survive a restart")
	}
	bySet := map[string]int{}
	for _, set := range ns.GetGroupBundleSets() {
		bySet[set.GetSetId()] = len(set.GetMembers())
	}
	if bySet["set-1"] != 2 || bySet["set-2"] != 1 {
		t.Errorf("bundle set membership wrong: %v", bySet)
	}
}

// TestReconcileGroupReadsInstanceSidecar proves a boot-scan reconciliation
// recovers the group_instance_id from the on-disk sidecar (#38 F2), so a member
// banked (with its sidecar) before a restart re-seeds with its REAL group binding
// and is remotely evictable, while a pre-sidecar member seeds with "".
func TestReconcileGroupReadsInstanceSidecar(t *testing.T) {
	dir := t.TempDir()
	gmd := newFakeGroupMemberDriver(dir)
	gmd.banked["set-1/worker-0"] = true // has a sidecar (below)
	gmd.banked["set-1/worker-1"] = true // pre-sidecar (no sidecar file)

	// Write the group_instance_id sidecar beside worker-0's bundle on real disk,
	// where readGroupInstanceSidecar looks (GroupSetsDir/set/member/...).
	sidecarDir := filepath.Join(gmd.GroupSetsDir(), "set-1", "worker-0")
	if err := os.MkdirAll(sidecarDir, 0o700); err != nil {
		t.Fatalf("mkdir sidecar dir: %v", err)
	}
	if err := os.WriteFile(filepath.Join(sidecarDir, groupInstanceSidecar), []byte("grp-alpha"), 0o600); err != nil {
		t.Fatalf("write gid sidecar: %v", err)
	}

	s := New(Options{
		Config:       config.Config{Arch: "amd64", Node: "node-4", SnapshotRoot: dir},
		Driver:       groupMemberVMDriverAdapter{gmd},
		Transport:    &fakeTransport{},
		GroupNet:     newFakeGroupNet(),
		GroupRecords: newFakeGroupRecords(t.TempDir()),
		GroupDriver:  gmd,
		Logger:       slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	s.memHeadroom = func() uint64 { return 0 }
	s.ReconcileGroupBundlesFromDisk()

	byRef := map[string]string{} // snapshotRef -> groupInstanceID
	for _, e := range s.groupBundles.snapshot() {
		byRef[e.snapshotRef] = e.groupInstanceID
	}
	if got := byRef["group/set-1/worker-0"]; got != "grp-alpha" {
		t.Fatalf("worker-0 groupInstanceID = %q, want grp-alpha (from sidecar)", got)
	}
	if got := byRef["group/set-1/worker-1"]; got != "" {
		t.Fatalf("worker-1 (pre-sidecar) groupInstanceID = %q, want \"\"", got)
	}
}
