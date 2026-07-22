package server

import (
	"context"
	"io"
	"log/slog"
	"net"
	"os"
	"path/filepath"
	"testing"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"

	nodev1 "github.com/jomcgi/homelab/projects/embervm/proto/embervm/node/v1"

	"github.com/jomcgi/homelab/projects/embervm/noded/config"
	"github.com/jomcgi/homelab/projects/embervm/noded/substrate"
)

// These tests exercise the #38 local eviction arms over a REAL in-process gRPC
// dial (bufconn) AND a REAL on-disk SnapshotRoot: they assert the bundle dir
// actually leaves disk, which is the ONLY assertion that catches the misroute
// (a stateful/group-member ref falling through to RemoveSessionBundle, which
// no-op-removes a nonexistent sessions/<ref> and returns a false success). A
// faked-RPC test returning :ok would mask exactly this bug.

// diskGroupMemberDriver is a group-member-driver stub whose RemoveGroupMemberBundle
// does a REAL os.RemoveAll of group/<set>/<member> under the temp root (mirroring
// the production *driver.Driver), so a group eviction test asserts the member dir
// actually leaves disk. The other methods are unused by these eviction tests.
type diskGroupMemberDriver struct {
	groupRoot string // <SnapshotRoot>/group
}

func (d *diskGroupMemberDriver) GroupSetsDir() string { return d.groupRoot }

func (d *diskGroupMemberDriver) RemoveGroupMemberBundle(setID, memberName string) error {
	return os.RemoveAll(filepath.Join(d.groupRoot, setID, memberName))
}

// The remaining groupMemberDriver methods are never reached by these local
// eviction tests (they never start or restore a member VM), so they return
// Unimplemented / zero values to satisfy the interface.
func (d *diskGroupMemberDriver) ClaimGroupMember(_ context.Context, _, _ string, _, _ int, _ substrate.NICSpec, _ map[string]string) (substrate.Handle, error) {
	return substrate.Handle{}, status.Error(codes.Unimplemented, "unused")
}

func (d *diskGroupMemberDriver) SnapshotGroupMember(_ context.Context, _ substrate.Handle, _, _ string) (substrate.SnapshotRef, error) {
	return substrate.SnapshotRef{}, status.Error(codes.Unimplemented, "unused")
}

func (d *diskGroupMemberDriver) RestoreGroupMember(_ context.Context, _, _ string) (substrate.Handle, error) {
	return substrate.Handle{}, status.Error(codes.Unimplemented, "unused")
}

func (d *diskGroupMemberDriver) ScanGroupBundleSets() []substrate.GroupBundleSetInfo { return nil }

// newEvictTestServer wires a Server behind a REAL bufconn gRPC dial with a
// disk-scanning stateful driver AND a disk-real group-member driver sharing ONE
// on-disk SnapshotRoot, so an eviction routed through the gRPC client mutates the
// real temp dir the test then inspects. Returns the client, the server (for
// direct registry seeding of live VMs), and the SnapshotRoot.
func newEvictTestServer(t *testing.T) (nodev1.NodeServiceClient, *Server, string) {
	t.Helper()
	root := t.TempDir()
	volRoot := t.TempDir()
	// The fakeDriver serves BOTH the task vmDriver seam and the sessionDriver seam
	// so a ref in NO typed inventory falls through to the idempotent session path
	// (an unknown ref is a no-op success), matching a production noded which always
	// has a session driver wired.
	drv := &fakeDriver{sessionsDir: filepath.Join(root, "sessions"), sessionBundles: map[string]string{}}
	s := New(Options{
		// CpuVendor is REQUIRED: artifactPrefix refuses a vendor-bound kind (STATEFUL)
		// with an empty vendor, so a STATEFUL EvictArtifact would fail InvalidArgument
		// before reaching the local arm. A real node always has its vendor configured.
		Config:         config.Config{Arch: "amd64", Node: "node-4", CpuVendor: "amd", SnapshotRoot: root, VolumeRoot: volRoot},
		Driver:         drv,
		SessionDriver:  drv,
		StatefulDriver: newDiskScanStatefulDriver(root),
		GroupDriver:    &diskGroupMemberDriver{groupRoot: filepath.Join(root, "group")},
		Transport:      &fakeTransport{},
		Logger:         slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	s.memHeadroom = func() uint64 { return 0 }

	lis := bufconn.Listen(1 << 20)
	gs := grpc.NewServer()
	nodev1.RegisterNodeServiceServer(gs, s)
	go func() { _ = gs.Serve(lis) }()
	t.Cleanup(gs.Stop)

	conn, err := grpc.NewClient(
		"passthrough:///bufnet",
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) {
			return lis.DialContext(ctx)
		}),
		grpc.WithTransportCredentials(insecure.NewCredentials()),
	)
	if err != nil {
		t.Fatalf("dial bufconn: %v", err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	return nodev1.NewNodeServiceClient(conn), s, root
}

func statefulDirExists(root, ref string) bool {
	_, err := os.Stat(filepath.Join(root, "stateful", ref))
	return err == nil
}

func groupMemberDirExists(root, setID, member string) bool {
	_, err := os.Stat(filepath.Join(root, "group", setID, member))
	return err == nil
}

// TestEvictArtifactLocalStatefulRemovesDisk proves a typed STATEFUL local evict
// over bufconn actually removes stateful/<ref> from disk and drops it from
// NodeStatus. Against the pre-#38 code this FAILS: the STATEFUL ref fell into the
// session path, RemoveAll(sessions/<ref>) no-op'd, and stateful/<ref> survived.
func TestEvictArtifactLocalStatefulRemovesDisk(t *testing.T) {
	client, s, root := newEvictTestServer(t)
	ctx := context.Background()

	ref := "scratch-postgres__g5"
	writeBundleFiles(t, filepath.Join(root, "stateful", ref), map[string]string{"snapfile": "snap", "memfile": "mem", "gen": "5"})
	s.statefulBundles.add(statefulBundleEntry{snapshotRef: ref, workload: "scratch-postgres", generation: 5})

	if !statefulDirExists(root, ref) {
		t.Fatalf("precondition: stateful/%s should exist before evict", ref)
	}
	if _, err := client.EvictArtifact(ctx, &nodev1.EvictArtifactRequest{
		Artifact: &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_STATEFUL, Workload: "scratch-postgres", Ref: ref},
		Remote:   false,
	}); err != nil {
		t.Fatalf("EvictArtifact(stateful, local): %v", err)
	}
	if statefulDirExists(root, ref) {
		t.Fatalf("stateful/%s should be GONE from disk after local evict (pre-#38 misroute leaves it)", ref)
	}
	if _, ok := s.statefulBundles.get(ref); ok {
		t.Fatalf("stateful bundle %s should be dropped from NodeStatus inventory after evict", ref)
	}
}

// TestEvictSnapshotStatefulRemovesDisk proves the same removal via the raw
// EvictSnapshot verb (the path the reaper's per-bundle stateful evict and the
// GroupSweeper drive), dispatching on the banked-bundle inventory.
func TestEvictSnapshotStatefulRemovesDisk(t *testing.T) {
	client, s, root := newEvictTestServer(t)
	ctx := context.Background()

	ref := "demo-postgres__g2"
	writeBundleFiles(t, filepath.Join(root, "stateful", ref), map[string]string{"snapfile": "snap", "memfile": "mem"})
	s.statefulBundles.add(statefulBundleEntry{snapshotRef: ref, workload: "demo-postgres", generation: 2})

	if _, err := client.EvictSnapshot(ctx, &nodev1.EvictSnapshotRequest{SnapshotRef: ref}); err != nil {
		t.Fatalf("EvictSnapshot(stateful ref): %v", err)
	}
	if statefulDirExists(root, ref) {
		t.Fatalf("stateful/%s should be GONE from disk after EvictSnapshot", ref)
	}
}

// TestEvictStatefulInUseRefused proves a STATEFUL evict is refused
// FAILED_PRECONDITION while a live stateful VM was relit from the ref, and the
// dir SURVIVES (the reaper's remote-guard then withholds the S3 delete too).
func TestEvictStatefulInUseRefused(t *testing.T) {
	client, s, root := newEvictTestServer(t)
	ctx := context.Background()

	ref := "scratch-postgres__live"
	writeBundleFiles(t, filepath.Join(root, "stateful", ref), map[string]string{"snapfile": "snap", "memfile": "mem"})
	s.statefulBundles.add(statefulBundleEntry{snapshotRef: ref, workload: "scratch-postgres", generation: 3})
	// A live stateful VM relit from this ref (snapshotRef set == relight).
	s.statefulVMs.add(&statefulEntry{vmID: "live-vm-1", workload: "scratch-postgres", snapshotRef: ref, generation: 3})

	_, err := client.EvictArtifact(ctx, &nodev1.EvictArtifactRequest{
		Artifact: &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_STATEFUL, Workload: "scratch-postgres", Ref: ref},
		Remote:   false,
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("in-use stateful evict: want FAILED_PRECONDITION, got %v", err)
	}
	if !statefulDirExists(root, ref) {
		t.Fatalf("stateful/%s must SURVIVE a refused in-use evict", ref)
	}
}

// TestEvictStatefulIdempotentAlreadyGone proves a second evict of an
// already-removed ref returns success (RemoveStatefulBundle is idempotent). The
// second call goes through the typed STATEFUL path even with no inventory entry.
func TestEvictStatefulIdempotentAlreadyGone(t *testing.T) {
	client, _, root := newEvictTestServer(t)
	ctx := context.Background()

	ref := "gone__g1"
	// No bundle on disk, no inventory entry: desired end-state already holds.
	if _, err := client.EvictArtifact(ctx, &nodev1.EvictArtifactRequest{
		Artifact: &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_STATEFUL, Workload: "w", Ref: ref},
		Remote:   false,
	}); err != nil {
		t.Fatalf("idempotent stateful evict of absent ref should succeed: %v", err)
	}
	if statefulDirExists(root, ref) {
		t.Fatalf("stateful/%s should not exist", ref)
	}
}

// TestEvictSnapshotGroupMemberRemovesDisk proves per-member EvictSnapshot over
// bufconn removes each member bundle dir (group/<set>/<member>) from disk, so
// evicting every member of a two-member set clears the whole set. Against the
// pre-#38 code this FAILS: a group/<set>/<member> ref fell into the session path.
func TestEvictSnapshotGroupMemberRemovesDisk(t *testing.T) {
	client, s, root := newEvictTestServer(t)
	ctx := context.Background()

	set := "k3s-cluster__set1"
	seedGroupMember(t, s, root, set, "a", "grp-k3s-1")
	seedGroupMember(t, s, root, set, "b", "grp-k3s-1")

	for _, m := range []string{"a", "b"} {
		ref := "group/" + set + "/" + m
		if _, err := client.EvictSnapshot(ctx, &nodev1.EvictSnapshotRequest{SnapshotRef: ref}); err != nil {
			t.Fatalf("EvictSnapshot(%s): %v", ref, err)
		}
		if groupMemberDirExists(root, set, m) {
			t.Fatalf("group/%s/%s should be GONE from disk after EvictSnapshot (pre-#38 misroute leaves it)", set, m)
		}
	}
	// The whole set dir is empty of members now.
	if groupMemberDirExists(root, set, "a") || groupMemberDirExists(root, set, "b") {
		t.Fatalf("both members of set %s should be gone", set)
	}
}

// TestEvictGroupMemberInUseRefused proves a per-member evict is refused
// FAILED_PRECONDITION while a live member relit from that (set, member) is
// attached, and the member bundle SURVIVES.
func TestEvictGroupMemberInUseRefused(t *testing.T) {
	client, s, root := newEvictTestServer(t)
	ctx := context.Background()

	set := "k3s-cluster__set2"
	inst := "grp-k3s-2"
	seedGroupMember(t, s, root, set, "entry", inst)
	// A live member VM matching (group_instance_id, member_name): a relit member.
	s.groupMembers.add(&groupMemberEntry{vmID: "live-member-1", groupInstanceID: inst, memberName: "entry", ip: net.ParseIP("10.0.0.5")})

	ref := "group/" + set + "/entry"
	_, err := client.EvictSnapshot(ctx, &nodev1.EvictSnapshotRequest{SnapshotRef: ref})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("in-use group member evict: want FAILED_PRECONDITION, got %v", err)
	}
	if !groupMemberDirExists(root, set, "entry") {
		t.Fatalf("group/%s/entry must SURVIVE a refused in-use evict", set)
	}
}

// TestEvictGroupMemberInUseRefusedRefFirstNoGid proves a live member relit from a
// ref is protected (FAILED_PRECONDITION, bundle survives) via the ref-first match
// EVEN when its banked-bundle entry lost its group_instance_id to a pre-sidecar
// boot scan (gid=""). This closes the F3 window: without the ref-first match, the
// (gid, member) fallback would find no gid to match and the live member's bundle
// would be wrongly evictable.
func TestEvictGroupMemberInUseRefusedRefFirstNoGid(t *testing.T) {
	client, s, root := newEvictTestServer(t)
	ctx := context.Background()

	set := "k3s-cluster__set3"
	ref := "group/" + set + "/entry"
	writeBundleFiles(t, filepath.Join(root, "group", set, "entry"), map[string]string{"snapfile": "snap", "memfile": "mem"})
	// Bundle entry seeded WITHOUT a group_instance_id, as a pre-sidecar boot scan
	// would (ReconcileGroupBundlesFromDisk reads back "" for a member with no
	// sidecar).
	s.groupBundles.add(groupBundleEntry{setID: set, memberName: "entry", groupInstanceID: "", snapshotRef: ref, sizeBytes: 5120})
	// A live member relit from this ref carries the snapshotRef (set by the relight
	// plumbing) but its groupInstanceID need not match the empty bundle entry.
	s.groupMembers.add(&groupMemberEntry{vmID: "live-relit-1", groupInstanceID: "grp-k3s-3", memberName: "entry", snapshotRef: ref, ip: net.ParseIP("10.0.0.9")})

	_, err := client.EvictSnapshot(ctx, &nodev1.EvictSnapshotRequest{SnapshotRef: ref})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("ref-first in-use guard: want FAILED_PRECONDITION, got %v", err)
	}
	if !groupMemberDirExists(root, set, "entry") {
		t.Fatalf("group/%s/entry must SURVIVE: a live member relit from the ref holds it", set)
	}
}

// TestEvictSnapshotGroupMemberIdempotentUnknownRef proves an EvictSnapshot for a
// group-shaped ref that is NOT in the banked inventory returns success without
// touching disk (nothing to remove, no live member could hold it).
func TestEvictSnapshotGroupMemberIdempotentUnknownRef(t *testing.T) {
	client, _, _ := newEvictTestServer(t)
	ctx := context.Background()

	// This ref is group-shaped but never banked, so it is not in groupBundles; it
	// falls through to the idempotent session path (no such dir) and succeeds.
	if _, err := client.EvictSnapshot(ctx, &nodev1.EvictSnapshotRequest{SnapshotRef: "group/unknown-set/x"}); err != nil {
		t.Fatalf("EvictSnapshot of an unbanked group ref should be idempotent success: %v", err)
	}
}

// seedGroupMember writes a real member bundle dir under group/<set>/<member> and
// registers it in the banked-bundle inventory keyed by its snapshot_ref, with the
// group_instance_id the in-use guard matches live members against.
func seedGroupMember(t *testing.T, s *Server, root, setID, member, groupInstanceID string) {
	t.Helper()
	writeBundleFiles(t, filepath.Join(root, "group", setID, member), map[string]string{"snapfile": "snap", "memfile": "mem"})
	s.groupBundles.add(groupBundleEntry{
		setID:           setID,
		memberName:      member,
		groupInstanceID: groupInstanceID,
		snapshotRef:     "group/" + setID + "/" + member,
		sizeBytes:       5120,
	})
}
