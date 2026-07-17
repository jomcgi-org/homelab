package server

import (
	"context"
	"io"
	"log/slog"
	"net"
	"sync"
	"testing"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	nodev1 "github.com/jomcgi/homelab/projects/embervm/proto/embervm/node/v1"

	"github.com/jomcgi/homelab/projects/embervm/noded/config"
	"github.com/jomcgi/homelab/projects/embervm/noded/serving"
	"github.com/jomcgi/homelab/projects/embervm/noded/substrate"
)

// fakeGroupNet is an in-memory groupNetwork. It tracks held groups (id -> cidr)
// and records the create/delete calls so a handler test can assert idempotency and
// the attached-member refusal without real bridges.
type fakeGroupNet struct {
	mu           sync.Mutex
	groups       map[string]string // id -> cidr
	createErr    error
	createN      int
	deleteN      int
	ensureN      int
	adoptCalls   []string
	taps         map[string]bool // member -> tap created
	removedTaps  []string
	ensureTapErr error
	ensureCalls  []ensureTapCall
}

// ensureTapCall records one EnsureMemberTap invocation for pinned-world assertions.
type ensureTapCall struct {
	member string
	index  uint32
	wantIP string
}

func newFakeGroupNet() *fakeGroupNet {
	return &fakeGroupNet{groups: map[string]string{}, taps: map[string]bool{}}
}

func (f *fakeGroupNet) EnsureNetwork(context.Context) error {
	f.mu.Lock()
	f.ensureN++
	f.mu.Unlock()
	return nil
}

func (f *fakeGroupNet) CreateGroupNetwork(_ context.Context, id, cidr string) (string, string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.createErr != nil {
		return "", "", f.createErr
	}
	if existing, ok := f.groups[id]; ok {
		if existing != cidr {
			return "", "", status.Errorf(codes.FailedPrecondition, "group %q exists with a different cidr", id)
		}
		return "emg-" + id, "10.101.1.1", nil // idempotent hit: no create count bump
	}
	f.createN++
	f.groups[id] = cidr
	return "emg-" + id, "10.101.1.1", nil
}

func (f *fakeGroupNet) DeleteGroupNetwork(_ context.Context, id string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.deleteN++
	delete(f.groups, id)
	return nil
}

func (f *fakeGroupNet) AdoptGroupNetwork(id, cidr string, _ int64) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.adoptCalls = append(f.adoptCalls, id)
	f.groups[id] = cidr
	return nil
}

func (f *fakeGroupNet) Has(id string) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	_, ok := f.groups[id]
	return ok
}

func (f *fakeGroupNet) List() []serving.GroupNetworkInfo {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]serving.GroupNetworkInfo, 0, len(f.groups))
	for id, cidr := range f.groups {
		out = append(out, serving.GroupNetworkInfo{
			GroupInstanceID: id,
			Bridge:          "emg-" + id,
			CIDR:            cidr,
			GatewayIP:       "10.101.1.1",
		})
	}
	return out
}

func (f *fakeGroupNet) MemberAddressingFor(id, member string, index uint32) (string, string, net.IP, error) {
	return "emgt-" + member, "02:00:00:00:00:01", net.ParseIP("127.0.0.1"), nil
}

// EnsureMemberTap hands out the loopback IP as the "pinned" address (so the server's
// real TCP health-gate reaches the test's local endpoint) and records the pin call
// so a test can assert the pinned-world reconstruction on relight (same member +
// index re-pinned). ensureTapErr, when set, makes it fail (the pin-failure path).
func (f *fakeGroupNet) EnsureMemberTap(_ context.Context, id, member string, index uint32, wantIP net.IP) (string, string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.ensureTapErr != nil {
		return "", "", f.ensureTapErr
	}
	f.taps[member] = true
	f.ensureCalls = append(f.ensureCalls, ensureTapCall{member: member, index: index, wantIP: wantIP.String()})
	return "emgt-" + member, "02:00:00:00:00:01", nil
}

func (f *fakeGroupNet) RemoveMemberTap(_ context.Context, id, tap string, ip net.IP) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.removedTaps = append(f.removedTaps, tap)
}

func (f *fakeGroupNet) GatewayIP(id string) net.IP { return net.IPv4(10, 101, 1, 1) }
func (f *fakeGroupNet) PrefixLen(id string) int    { return 24 }

func (f *fakeGroupNet) EntryEndpoint(ip net.IP, port uint32) (string, uint32) {
	return ip.String(), port
}

// fakeGroupRecords is an in-memory groupRecordStore.
type fakeGroupRecords struct {
	mu       sync.Mutex
	records  map[string]substrate.GroupNetworkRecord
	writeN   int
	removeN  int
	writeErr error
	dir      string
}

func newFakeGroupRecords(dir string) *fakeGroupRecords {
	return &fakeGroupRecords{records: map[string]substrate.GroupNetworkRecord{}, dir: dir}
}

func (f *fakeGroupRecords) WriteGroupNetworkRecord(rec substrate.GroupNetworkRecord) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.writeErr != nil {
		return f.writeErr
	}
	f.writeN++
	f.records[rec.GroupInstanceID] = rec
	return nil
}

func (f *fakeGroupRecords) RemoveGroupNetworkRecord(id string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.removeN++
	delete(f.records, id)
	return nil
}

func (f *fakeGroupRecords) ScanGroupNetworks() []substrate.GroupNetworkRecord {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]substrate.GroupNetworkRecord, 0, len(f.records))
	for _, r := range f.records {
		out = append(out, r)
	}
	return out
}

func (f *fakeGroupRecords) GroupNetworksDir() string { return f.dir }

// newGroupTestServer builds a Server wired with the group seams (no gRPC dial; the
// handlers are called in-process).
func newGroupTestServer(t *testing.T, gn *fakeGroupNet, gr *fakeGroupRecords) *Server {
	t.Helper()
	s := New(Options{
		Config:       config.Config{Arch: "amd64", Node: "node-4", SnapshotRoot: t.TempDir()},
		Driver:       &fakeDriver{},
		Transport:    &fakeTransport{},
		GroupNet:     gn,
		GroupRecords: gr,
		Logger:       slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	s.memHeadroom = func() uint64 { return 0 }
	return s
}

// TestCreateGroupNetworkPersistsAndReports asserts a create issues one create +
// one record write and shows up in NodeStatus.group_networks with member_count 0.
func TestCreateGroupNetworkPersistsAndReports(t *testing.T) {
	gn := newFakeGroupNet()
	gr := newFakeGroupRecords(t.TempDir())
	s := newGroupTestServer(t, gn, gr)

	resp, err := s.CreateGroupNetwork(context.Background(), &nodev1.CreateGroupNetworkRequest{
		GroupInstanceId: "grp-A",
		Cidr:            "10.101.1.0/24",
	})
	if err != nil {
		t.Fatalf("CreateGroupNetwork: %v", err)
	}
	if resp.GetBridgeName() != "emg-grp-A" || resp.GetGatewayIp() != "10.101.1.1" {
		t.Errorf("unexpected response: %+v", resp)
	}
	if gn.createN != 1 || gr.writeN != 1 {
		t.Errorf("createN=%d writeN=%d, want 1/1", gn.createN, gr.writeN)
	}

	ns := s.nodeStatus()
	if len(ns.GetGroupNetworks()) != 1 {
		t.Fatalf("group_networks = %v", ns.GetGroupNetworks())
	}
	g := ns.GetGroupNetworks()[0]
	if g.GetGroupInstanceId() != "grp-A" || g.GetCidr() != "10.101.1.0/24" || g.GetMemberCount() != 0 {
		t.Errorf("group network status = %+v", g)
	}
	// group_member_vms and group_bundle_sets are present-but-empty in Task 4.
	if len(ns.GetGroupMemberVms()) != 0 || len(ns.GetGroupBundleSets()) != 0 {
		t.Errorf("group members/bundle sets should be empty in Task 4")
	}
}

// TestCreateGroupNetworkIdempotent asserts a re-issue with the same cidr writes the
// record again (idempotent overwrite) but the underlying create is a no-op hit.
func TestCreateGroupNetworkIdempotent(t *testing.T) {
	gn := newFakeGroupNet()
	gr := newFakeGroupRecords(t.TempDir())
	s := newGroupTestServer(t, gn, gr)
	req := &nodev1.CreateGroupNetworkRequest{GroupInstanceId: "grp-A", Cidr: "10.101.1.0/24"}
	if _, err := s.CreateGroupNetwork(context.Background(), req); err != nil {
		t.Fatalf("first create: %v", err)
	}
	if _, err := s.CreateGroupNetwork(context.Background(), req); err != nil {
		t.Fatalf("idempotent re-create: %v", err)
	}
	if gn.createN != 1 {
		t.Errorf("idempotent re-create bumped createN to %d, want 1", gn.createN)
	}
}

// TestCreateGroupNetworkValidationError asserts a manager validation/overlap error
// maps to FAILED_PRECONDITION and writes NO record.
func TestCreateGroupNetworkValidationError(t *testing.T) {
	gn := newFakeGroupNet()
	gn.createErr = status.Error(codes.FailedPrecondition, "bad cidr")
	gr := newFakeGroupRecords(t.TempDir())
	s := newGroupTestServer(t, gn, gr)
	_, err := s.CreateGroupNetwork(context.Background(), &nodev1.CreateGroupNetworkRequest{
		GroupInstanceId: "grp-A", Cidr: "10.200.1.0/24",
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("want FailedPrecondition, got %v", err)
	}
	if gr.writeN != 0 {
		t.Errorf("no record should be written on a validation failure, got writeN=%d", gr.writeN)
	}
}

// TestCreateGroupNetworkRecordWriteRollback asserts that if the record write fails,
// the just-created network is torn back down (no half-created group).
func TestCreateGroupNetworkRecordWriteRollback(t *testing.T) {
	gn := newFakeGroupNet()
	gr := newFakeGroupRecords(t.TempDir())
	gr.writeErr = status.Error(codes.Internal, "disk full")
	s := newGroupTestServer(t, gn, gr)
	_, err := s.CreateGroupNetwork(context.Background(), &nodev1.CreateGroupNetworkRequest{
		GroupInstanceId: "grp-A", Cidr: "10.101.1.0/24",
	})
	if status.Code(err) != codes.Internal {
		t.Fatalf("want Internal, got %v", err)
	}
	if gn.deleteN != 1 {
		t.Errorf("a record-write failure must roll back the network (deleteN=%d, want 1)", gn.deleteN)
	}
}

// TestDeleteGroupNetworkRefusesWhenAttached asserts a live member blocks the
// delete with FAILED_PRECONDITION and leaves the network + record intact.
func TestDeleteGroupNetworkRefusesWhenAttached(t *testing.T) {
	gn := newFakeGroupNet()
	gr := newFakeGroupRecords(t.TempDir())
	s := newGroupTestServer(t, gn, gr)
	if _, err := s.CreateGroupNetwork(context.Background(), &nodev1.CreateGroupNetworkRequest{
		GroupInstanceId: "grp-A", Cidr: "10.101.1.0/24",
	}); err != nil {
		t.Fatalf("create: %v", err)
	}
	// Simulate a live member (what Task 5 will add).
	s.groupMembers.add(&groupMemberEntry{
		vmID:            "m1",
		groupInstanceID: "grp-A",
		memberName:      "worker-0",
		ip:              net.ParseIP("10.101.1.10"),
	})

	_, err := s.DeleteGroupNetwork(context.Background(), &nodev1.DeleteGroupNetworkRequest{GroupInstanceId: "grp-A"})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("delete with an attached member must be FailedPrecondition, got %v", err)
	}
	if !gn.Has("grp-A") {
		t.Error("the network must remain after a refused delete")
	}
	if gn.deleteN != 0 {
		t.Errorf("no teardown should have run (deleteN=%d)", gn.deleteN)
	}

	// After the member leaves, delete succeeds and removes the record.
	s.groupMembers.remove("m1")
	if _, err := s.DeleteGroupNetwork(context.Background(), &nodev1.DeleteGroupNetworkRequest{GroupInstanceId: "grp-A"}); err != nil {
		t.Fatalf("delete after member left: %v", err)
	}
	if gn.Has("grp-A") {
		t.Error("network still held after delete")
	}
	if gr.removeN == 0 {
		t.Error("record not removed on delete")
	}
}

// TestDeleteGroupNetworkIdempotent asserts deleting an unknown group is a no-op OK.
func TestDeleteGroupNetworkIdempotent(t *testing.T) {
	gn := newFakeGroupNet()
	gr := newFakeGroupRecords(t.TempDir())
	s := newGroupTestServer(t, gn, gr)
	if _, err := s.DeleteGroupNetwork(context.Background(), &nodev1.DeleteGroupNetworkRequest{GroupInstanceId: "ghost"}); err != nil {
		t.Errorf("deleting an unknown group should be a no-op OK, got %v", err)
	}
}

// TestReconcileGroupNetworksFromDisk asserts the boot rescan adopts every valid
// record into the manager.
func TestReconcileGroupNetworksFromDisk(t *testing.T) {
	gn := newFakeGroupNet()
	dir := t.TempDir()
	gr := newFakeGroupRecords(dir)
	gr.records["grp-A"] = substrate.GroupNetworkRecord{GroupInstanceID: "grp-A", SubnetCIDR: "10.101.1.0/24"}
	gr.records["grp-B"] = substrate.GroupNetworkRecord{GroupInstanceID: "grp-B", SubnetCIDR: "10.101.2.0/24"}
	s := newGroupTestServer(t, gn, gr)

	s.ReconcileGroupNetworksFromDisk()
	if len(gn.adoptCalls) != 2 {
		t.Fatalf("adopted %d groups, want 2: %v", len(gn.adoptCalls), gn.adoptCalls)
	}
	if !gn.Has("grp-A") || !gn.Has("grp-B") {
		t.Error("both records should be adopted into the manager")
	}
}

// TestGroupVerbsUnimplementedWithoutManager asserts that a Server built without a
// group manager returns Unimplemented (task/serving-only builds untouched).
func TestGroupVerbsUnimplementedWithoutManager(t *testing.T) {
	s := New(Options{
		Config:    config.Config{Arch: "amd64", Node: "node-4", SnapshotRoot: t.TempDir()},
		Driver:    &fakeDriver{},
		Transport: &fakeTransport{},
		Logger:    slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	if _, err := s.CreateGroupNetwork(context.Background(), &nodev1.CreateGroupNetworkRequest{GroupInstanceId: "g", Cidr: "10.101.1.0/24"}); status.Code(err) != codes.Unimplemented {
		t.Errorf("CreateGroupNetwork without a manager should be Unimplemented, got %v", err)
	}
	if _, err := s.DeleteGroupNetwork(context.Background(), &nodev1.DeleteGroupNetworkRequest{GroupInstanceId: "g"}); status.Code(err) != codes.Unimplemented {
		t.Errorf("DeleteGroupNetwork without a manager should be Unimplemented, got %v", err)
	}
	// NodeStatus must still assemble with empty group fields.
	ns := s.nodeStatus()
	if len(ns.GetGroupNetworks()) != 0 {
		t.Errorf("group_networks should be empty without a manager")
	}
}
