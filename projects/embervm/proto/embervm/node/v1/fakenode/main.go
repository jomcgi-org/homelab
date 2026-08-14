// Command fakenode is a minimal in-memory NodeService gRPC server used only by
// the cross-language round-trip test (//projects/embervm/control:node_roundtrip_test).
// It proves the Go-generated server stubs and the Elixir-generated client stubs
// interoperate on the wire, including the server-streaming WatchNode RPC. No
// Firecracker, no real VM lifecycle: every handler returns deterministic,
// request-derived data so the Elixir client can assert genuine round-trip
// fidelity (echoed fields prove the request crossed the wire intact).
//
// It listens on 127.0.0.1 on an OS-chosen ephemeral port and prints "PORT=<n>"
// to stdout so the test harness can read the port and connect. It serves until
// the process is killed (the test owns its lifetime via an OS port/pipe).
package main

import (
	"context"
	"flag"
	"fmt"
	"net"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"

	nodev1 "github.com/jomcgi/homelab/projects/embervm/proto/embervm/node/v1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// watchNodeHeartbeats is how many NodeStatus messages WatchNode streams before
// completing. The test asserts it receives exactly this many, in order.
const watchNodeHeartbeats = 3

type fakeServer struct {
	nodev1.UnimplementedNodeServiceServer
	// store is the in-memory object-store stand-in for the continuity verbs (R6):
	// ExportArtifact records a key, a repeat export short-circuits skipped, and
	// EvictArtifact removes it, so the round-trip test proves the verbs cross the
	// wire and observe each other's effect within one test's fresh server.
	mu             sync.Mutex
	store          map[string]bool
	vmState        map[string]bool
	vmWorkload     map[string]string
	suppressPrimed bool
	dropStatus     bool
	closeStream    bool
	streamActive   bool
}

// storeKey mirrors the Fork-3 layout <kind>/<workload>/<ref> so the fake's
// idempotency keying matches the real store's.
func storeKey(a *nodev1.ArtifactRef) string {
	return fmt.Sprintf("%d/%s/%s", a.GetKind(), a.GetWorkload(), a.GetRef())
}

func (*fakeServer) BuildBase(_ context.Context, req *nodev1.BuildBaseRequest) (*nodev1.BuildBaseResponse, error) {
	// Echo image_ref into snapshot_ref so the client can prove the request field
	// arrived, and reflect the requested memory shape back.
	return &nodev1.BuildBaseResponse{
		SnapshotRef:   "snap:" + req.GetImageRef(),
		ImageDigest:   "sha256:" + req.GetWorkloadRevision(),
		BaseSizeBytes: uint64(req.GetResources().GetMemMib()) * 1024 * 1024,
		Arch:          "amd64",
		AlreadyBuilt:  false,
	}, nil
}

func (s *fakeServer) Prime(_ context.Context, req *nodev1.PrimeRequest) (*nodev1.PrimeResponse, error) {
	vmID := "vm:" + req.GetLineageId() + ":" + req.GetVolumeMount()
	s.mu.Lock()
	s.vmState[vmID] = true
	s.vmWorkload[vmID] = req.GetTrace().GetWorkload()
	s.mu.Unlock()
	return &nodev1.PrimeResponse{VmId: vmID}, nil
}

func (*fakeServer) Assign(_ context.Context, req *nodev1.AssignRequest) (*nodev1.AssignResponse, error) {
	// Echo the guest request body and path back so the client can assert the full
	// HTTP-semantics payload survived the round trip.
	return &nodev1.AssignResponse{
		Response: &nodev1.GuestResponse{
			StatusCode: 200,
			Headers:    map[string]string{"x-echo-path": req.GetRequest().GetPath()},
			Body:       req.GetRequest().GetBody(),
		},
		Usage: &nodev1.UsageStats{CpuMs: 1, PeakRssMib: 2, WallMs: 3},
	}, nil
}

func (s *fakeServer) Destroy(_ context.Context, req *nodev1.DestroyRequest) (*nodev1.DestroyResponse, error) {
	s.mu.Lock()
	delete(s.vmState, req.GetVmId())
	delete(s.vmWorkload, req.GetVmId())
	s.mu.Unlock()
	return &nodev1.DestroyResponse{}, nil
}

// SessionAssign echoes the request body and path back like Assign, but proves
// deliver-without-destroy by echoing the session_id it was handed into a
// response header the client asserts on (the VM "survives", so the session id
// is still meaningful). suspect is left false (a clean round trip).
func (*fakeServer) SessionAssign(_ context.Context, req *nodev1.SessionAssignRequest) (*nodev1.SessionAssignResponse, error) {
	return &nodev1.SessionAssignResponse{
		Response: &nodev1.GuestResponse{
			StatusCode: 200,
			Headers: map[string]string{
				"x-echo-path":  req.GetRequest().GetPath(),
				"x-session-id": req.GetSessionId(),
			},
			Body: req.GetRequest().GetBody(),
		},
		Usage:   &nodev1.UsageStats{CpuMs: 4, PeakRssMib: 5, WallMs: 6},
		Suspect: false,
	}, nil
}

// Bank derives a snapshot_ref from the session_id so the client can prove the
// request field crossed the wire, and returns a fixed size.
func (*fakeServer) Bank(_ context.Context, req *nodev1.BankRequest) (*nodev1.BankResponse, error) {
	return &nodev1.BankResponse{
		SnapshotRef: "sessions/" + req.GetSessionId(),
		SizeBytes:   2048,
	}, nil
}

// Relight derives a vm_id from the snapshot_ref so the client can prove the
// restore request crossed the wire intact.
func (*fakeServer) Relight(_ context.Context, req *nodev1.RelightRequest) (*nodev1.RelightResponse, error) {
	return &nodev1.RelightResponse{VmId: "vm:" + req.GetSnapshotRef()}, nil
}

// EvictSnapshot is idempotent and returns an empty response for any ref.
func (*fakeServer) EvictSnapshot(_ context.Context, _ *nodev1.EvictSnapshotRequest) (*nodev1.EvictSnapshotResponse, error) {
	return &nodev1.EvictSnapshotResponse{}, nil
}

// StartServing derives vm_id and ip from whichever source ref was set (fresh or
// relight) so the client can prove both oneof branches cross the wire, and
// echoes the requested port back.
func (*fakeServer) StartServing(_ context.Context, req *nodev1.StartServingRequest) (*nodev1.StartServingResponse, error) {
	ref := req.GetFresh().GetServingImageRef()
	if ref == "" {
		ref = req.GetRelight().GetSnapshotRef()
	}
	return &nodev1.StartServingResponse{
		VmId: "vm:" + ref,
		Ip:   "10.99.0.1",
		Port: req.GetPort(),
	}, nil
}

// StopServing returns a snapshot_ref/size_bytes for BANK, and zero values for
// DESTROY, so the client can assert both mode branches.
func (*fakeServer) StopServing(_ context.Context, req *nodev1.StopServingRequest) (*nodev1.StopServingResponse, error) {
	if req.GetMode() == nodev1.StopServingMode_STOP_SERVING_MODE_BANK {
		return &nodev1.StopServingResponse{
			SnapshotRef: "serving/" + req.GetVmId(),
			SizeBytes:   3072,
		}, nil
	}
	return &nodev1.StopServingResponse{}, nil
}

// StartStateful derives its response from mode so the client can assert every
// stateful boot path crosses the wire. RELIGHT scripts the pairing outcome off
// the relight_snapshot_ref content ("mismatch" -> generation_mismatch cold-boot
// fallback, "noledger" -> ledger_unreadable, otherwise a warm relight), which is
// how the round-trip test exercises each pairing branch against a stateless fake.
// FRESH/COLD cold-boot from boot_image_ref and report a bumped generation.
//
// blessed_generation and volume_device (R7) are recorded on the fake's response
// so the client can assert both crossed the wire: the returned generation
// mirrors blessed_generation when the caller set one (0 falls back to the
// scripted legacy self-bump values below), and volume_device is echoed back
// verbatim in vm_id's suffix so an empty value (legacy vol.img lane) and a set
// one (Longhorn device path) are both observable.
func (*fakeServer) StartStateful(_ context.Context, req *nodev1.StartStatefulRequest) (*nodev1.StartStatefulResponse, error) {
	vmIDSuffix := ""
	if dev := req.GetVolumeDevice(); dev != "" {
		vmIDSuffix = "@" + dev
	}
	if req.GetMode() == nodev1.StartStatefulMode_START_STATEFUL_MODE_RELIGHT {
		ref := req.GetRelightSnapshotRef()
		switch {
		case strings.Contains(ref, "mismatch"):
			return &nodev1.StartStatefulResponse{
				VmId: "vm:" + ref + vmIDSuffix, Ip: "10.99.0.3", Port: req.GetPort(),
				Generation: 8, WasRelight: false, ColdBootReason: "generation_mismatch",
			}, nil
		case strings.Contains(ref, "noledger"):
			return &nodev1.StartStatefulResponse{
				VmId: "vm:" + ref + vmIDSuffix, Ip: "10.99.0.3", Port: req.GetPort(),
				Generation: 8, WasRelight: false, ColdBootReason: "ledger_unreadable",
			}, nil
		default:
			gen := uint64(7)
			if bg := req.GetBlessedGeneration(); bg != 0 {
				gen = bg
			}
			return &nodev1.StartStatefulResponse{
				VmId: "vm:" + ref + vmIDSuffix, Ip: "10.99.0.3", Port: req.GetPort(),
				Generation: gen, WasRelight: true, ColdBootReason: "",
			}, nil
		}
	}
	// FRESH or COLD: cold boot from the boot image ref. The blessed generation
	// wins when the caller supplied one (R7); 0 falls back to the legacy
	// self-bump value of 1, matching pre-blessing daemon behavior.
	gen := uint64(1)
	if bg := req.GetBlessedGeneration(); bg != 0 {
		gen = bg
	}
	return &nodev1.StartStatefulResponse{
		VmId: "vm:" + req.GetBootImageRef() + vmIDSuffix, Ip: "10.99.0.3", Port: req.GetPort(),
		Generation: gen, WasRelight: false, ColdBootReason: "",
	}, nil
}

// StopStateful returns a bundle ref, stamped generation, and size for BANK, a
// checkpoint token plus the paused generation for CHECKPOINT, and zero values for
// DESTROY, so the client can assert every mode branch.
func (*fakeServer) StopStateful(_ context.Context, req *nodev1.StopStatefulRequest) (*nodev1.StopStatefulResponse, error) {
	switch req.GetMode() {
	case nodev1.StopStatefulMode_STOP_STATEFUL_MODE_BANK:
		return &nodev1.StopStatefulResponse{
			SnapshotRef: "stateful/" + req.GetVmId(),
			Generation:  7,
			SizeBytes:   16384,
		}, nil
	case nodev1.StopStatefulMode_STOP_STATEFUL_MODE_CHECKPOINT:
		return &nodev1.StopStatefulResponse{
			CheckpointToken: "ckpt:" + req.GetVmId(),
			Generation:      7,
		}, nil
	default:
		return &nodev1.StopStatefulResponse{}, nil
	}
}

// ResolveStateful returns the published bundle for COMMIT (deriving the ref from
// the token so the client proves it crossed the wire) and an empty response for
// ABORT, so the client can assert both resolve branches (ADR embervm/008).
func (*fakeServer) ResolveStateful(_ context.Context, req *nodev1.ResolveStatefulRequest) (*nodev1.ResolveStatefulResponse, error) {
	if req.GetMode() == nodev1.ResolveMode_RESOLVE_MODE_COMMIT {
		return &nodev1.ResolveStatefulResponse{
			SnapshotRef: "stateful/" + req.GetCheckpointToken(),
			Generation:  8,
			SizeBytes:   16384,
		}, nil
	}
	return &nodev1.ResolveStatefulResponse{}, nil
}

// DeleteVolume is idempotent and echoes the lineage for round-trip coverage.
func (*fakeServer) DeleteVolume(_ context.Context, req *nodev1.DeleteVolumeRequest) (*nodev1.DeleteVolumeResponse, error) {
	return &nodev1.DeleteVolumeResponse{LineageId: req.GetLineageId()}, nil
}

func (*fakeServer) ArchiveVolume(_ context.Context, _ *nodev1.ArchiveVolumeRequest) (*nodev1.ArchiveVolumeResponse, error) {
	return &nodev1.ArchiveVolumeResponse{}, nil
}

func (*fakeServer) RetireVolume(_ context.Context, _ *nodev1.RetireVolumeRequest) (*nodev1.RetireVolumeResponse, error) {
	return &nodev1.RetireVolumeResponse{}, nil
}

// CreateGroupNetwork derives the bridge_name and gateway_ip from the request so
// the client can prove the fields crossed the wire, and scripts the CIDR-overlap
// refusal off the cidr content ("overlap" -> FAILED_PRECONDITION), which is how
// the round-trip test exercises the create-refusal branch against a stateless fake.
func (*fakeServer) CreateGroupNetwork(_ context.Context, req *nodev1.CreateGroupNetworkRequest) (*nodev1.CreateGroupNetworkResponse, error) {
	if strings.Contains(req.GetCidr(), "overlap") {
		return nil, status.Errorf(codes.FailedPrecondition, "cidr %q overlaps an existing group bridge", req.GetCidr())
	}
	return &nodev1.CreateGroupNetworkResponse{
		BridgeName: "br-" + req.GetGroupInstanceId(),
		GatewayIp:  "10.100.0.1",
	}, nil
}

// DeleteGroupNetwork is idempotent and returns an empty response for any group.
func (*fakeServer) DeleteGroupNetwork(_ context.Context, _ *nodev1.DeleteGroupNetworkRequest) (*nodev1.DeleteGroupNetworkResponse, error) {
	return &nodev1.DeleteGroupNetworkResponse{}, nil
}

// StartGroupMember derives its response from mode so the client can assert every
// member boot path crosses the wire. RELIGHT scripts the clock-resync outcome off
// the snapshot_ref content ("clockfail" -> FAILED_PRECONDITION with a clock-delta
// detail, otherwise a verified warm relight), which is how the round-trip test
// exercises the resync-failure branch against a stateless fake. FRESH cold-boots
// from source and echoes the pinned ip.
func (*fakeServer) StartGroupMember(_ context.Context, req *nodev1.StartGroupMemberRequest) (*nodev1.StartGroupMemberResponse, error) {
	if req.GetMode() == nodev1.StartGroupMemberMode_START_GROUP_MEMBER_MODE_RELIGHT {
		ref := req.GetSnapshotRef()
		if strings.Contains(ref, "clockfail") {
			return nil, status.Errorf(codes.FailedPrecondition, "clock resync delta exceeds 1s for %q", ref)
		}
		return &nodev1.StartGroupMemberResponse{
			VmId: "vm:" + ref, Ip: req.GetIp(), WasRelight: true,
		}, nil
	}
	// FRESH: cold boot from the source, echo the pinned ip. The R6 request fields
	// derive response content so the client can assert they crossed the wire:
	// ready_budget_seconds folds into vm_id ("@<n>s") and a non-zero
	// entry_guest_port yields the daemon-side endpoint projection fields.
	resp := &nodev1.StartGroupMemberResponse{
		VmId: "vm:" + req.GetSource(), Ip: req.GetIp(), WasRelight: false,
	}
	if b := req.GetReadyBudgetSeconds(); b > 0 {
		resp.VmId = fmt.Sprintf("%s@%ds", resp.VmId, b)
	}
	if p := req.GetEntryGuestPort(); p > 0 {
		resp.EndpointIp = "fakenode-pod"
		resp.EndpointPort = 30000 + p
	}
	return resp, nil
}

// StopGroupMember returns a per-member bundle ref under the caller-supplied set
// dir and a size for BANK, and zero values for DESTROY, so the client can assert
// both mode branches and that set_id/member_name crossed the wire.
func (*fakeServer) StopGroupMember(_ context.Context, req *nodev1.StopGroupMemberRequest) (*nodev1.StopGroupMemberResponse, error) {
	if req.GetMode() == nodev1.StopGroupMemberMode_STOP_GROUP_MEMBER_MODE_BANK {
		return &nodev1.StopGroupMemberResponse{
			SnapshotRef: "group/" + req.GetSetId() + "/" + req.GetMemberName(),
			SizeBytes:   5120,
		}, nil
	}
	return &nodev1.StopGroupMemberResponse{}, nil
}

// ExportArtifact records the artifact key in the in-memory store and reports the
// bytes moved (derived from the ref length so the client proves the ref crossed),
// short-circuiting skipped=true on a repeat export of the same key. It echoes the
// volume generation for a VOLUME artifact so the pairing fact crosses the wire.
func (s *fakeServer) ExportArtifact(_ context.Context, req *nodev1.ExportArtifactRequest) (*nodev1.ExportArtifactResponse, error) {
	art := req.GetArtifact()
	if art.GetRef() == "" && art.GetKind() != nodev1.ArtifactKind_ARTIFACT_KIND_VOLUME {
		return nil, status.Errorf(codes.FailedPrecondition, "artifact ref required for kind %v", art.GetKind())
	}
	key := storeKey(art)
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.store[key] {
		return &nodev1.ExportArtifactResponse{BytesMoved: 0, Skipped: true, Generation: volGen(art)}, nil
	}
	s.store[key] = true
	return &nodev1.ExportArtifactResponse{
		BytesMoved: uint64(len(art.GetWorkload()) + len(art.GetRef())),
		Skipped:    false,
		Generation: volGen(art),
	}, nil
}

// RestoreArtifact reports a successful restore for a key present in the store,
// echoing the volume generation for a VOLUME. FAILED_PRECONDITION when the store
// has no copy (a corrupt or absent restore surfaces loudly, never silently), and
// also when vendor is set but does not match the vendor the artifact was
// exported under (R7, standing decision 1: restore fails closed on a vendor
// mismatch, the fake mirrors the arch-mismatch behavior noded already has).
func (s *fakeServer) RestoreArtifact(_ context.Context, req *nodev1.RestoreArtifactRequest) (*nodev1.RestoreArtifactResponse, error) {
	art := req.GetArtifact()
	key := storeKey(art)
	s.mu.Lock()
	defer s.mu.Unlock()
	if !s.store[key] {
		return nil, status.Errorf(codes.FailedPrecondition, "artifact %q absent from store", key)
	}
	if v := req.GetVendor(); v != "" && strings.Contains(art.GetRef(), "vendor-mismatch") {
		return nil, status.Errorf(codes.FailedPrecondition, "artifact %q vendor mismatch for %q", key, v)
	}
	return &nodev1.RestoreArtifactResponse{
		BytesMoved: uint64(len(art.GetWorkload()) + len(art.GetRef())),
		Skipped:    false,
		Generation: volGen(art),
	}, nil
}

// EvictArtifact deletes the key from the store (remote=true) and is idempotent on
// an already-absent artifact. remote=false is accepted as the typed local-evict
// alias and returns OK without touching the store map.
func (s *fakeServer) EvictArtifact(_ context.Context, req *nodev1.EvictArtifactRequest) (*nodev1.EvictArtifactResponse, error) {
	art := req.GetArtifact()
	key := storeKey(art)
	s.mu.Lock()
	defer s.mu.Unlock()
	present := s.store[key]
	if req.GetRemote() {
		delete(s.store, key)
	}
	if !present {
		return &nodev1.EvictArtifactResponse{BytesFreed: 0}, nil
	}
	return &nodev1.EvictArtifactResponse{BytesFreed: uint64(len(art.GetWorkload()) + len(art.GetRef()))}, nil
}

// volGen scripts a fixed volume generation for a VOLUME artifact so the client
// can assert the generation echo crosses the wire; 0 for every other kind.
func volGen(a *nodev1.ArtifactRef) uint64 {
	if a.GetKind() == nodev1.ArtifactKind_ARTIFACT_KIND_VOLUME {
		return 5
	}
	return 0
}

func (s *fakeServer) GetNodeStatus(_ context.Context, req *nodev1.GetNodeStatusRequest) (*nodev1.NodeStatus, error) {
	return &nodev1.NodeStatus{
		NodeId:     req.GetNodeId(),
		LiveVms:    1,
		MaxLiveVms: 10,
		// Continuity facts (R6): a fixed drain deadline and store reachability so
		// the client can assert the new node-level fields round-trip. The bundle,
		// volume, and set exported flags below prove the per-artifact fields cross.
		DrainDeadlineUnixMs: 1_700_000_009_000,
		StoreReachable:      true,
		// Distribution facts (R7): fixed vendor and template hash so the client can
		// assert the new node-level fields round-trip.
		CpuVendor:        "amd",
		NodeTemplateHash: "tmpl-abc123",
		// Session facts (R2): deterministic, so the client can assert the new
		// repeated/scalar status fields round-trip.
		SessionVms: []*nodev1.SessionVm{
			{VmId: "vm-s1", SessionId: "s-sess1", Workload: "sandbox-session"},
		},
		SessionSnapshots: []*nodev1.SessionSnapshot{
			{
				SnapshotRef:     "sessions/s-sess2",
				SessionId:       "s-sess2",
				Workload:        "sandbox-session",
				SizeBytes:       4096,
				CreatedAtUnixMs: 1_700_000_000_000,
			},
		},
		SessionVolumes: []*nodev1.SessionVolume{
			{Workload: "sandbox-session", LineageId: "s-sess3", SizeBytes: 1024, AllocatedBytes: 512},
		},
		SnapshotDiskFreeBytes: 9_000_000_000,
		SnapshotDiskUsedBytes: 1_000_000_000,
		// Serving facts (R3): deterministic, so the client can assert the new
		// repeated/scalar status fields round-trip.
		ServingVms: []*nodev1.ServingVm{
			{
				VmId:            "vm-srv1",
				Workload:        "sandbox-serving",
				Ip:              "10.99.0.2",
				Port:            8080,
				Healthy:         true,
				LastProbeUnixMs: 1_700_000_001_000,
				// origin (ADR embervm/018): this VM was woken by the brick
				// activator, so the client can assert the adoption discriminator
				// round-trips (UNSPECIFIED on the other VMs proves the default).
				Origin: nodev1.InstanceOrigin_INSTANCE_ORIGIN_ACTIVATOR,
			},
		},
		ServingSnapshots: []*nodev1.ServingSnapshot{
			{
				SnapshotRef:     "serving/s-srv2",
				Workload:        "sandbox-serving",
				SizeBytes:       8192,
				CreatedAtUnixMs: 1_700_000_002_000,
			},
		},
		ServingSubnetCidr: "10.99.0.0/24",
		// Stateful facts (R4): deterministic, so the client can assert the new
		// repeated stateful status fields (VMs with generations, the one banked
		// bundle, and the durable volume) round-trip.
		StatefulVms: []*nodev1.StatefulVm{
			{
				VmId:            "vm-st1",
				Workload:        "scratch-postgres",
				Ip:              "10.99.0.3",
				Port:            5432,
				Healthy:         true,
				Generation:      5,
				LastProbeUnixMs: 1_700_000_003_000,
				// origin (ADR embervm/018 Phase 2): a node-woken stateful VM the
				// control plane adopts (fenced-writer adoption trusts its generation).
				Origin: nodev1.InstanceOrigin_INSTANCE_ORIGIN_ACTIVATOR,
			},
			// A second VM PAUSED awaiting a resolve (ADR embervm/008), so the
			// client can assert the checkpoint_pending inventory fields adoption
			// reads round-trip.
			{
				VmId:              "vm-st2",
				Workload:          "demo-postgres",
				Ip:                "10.99.0.4",
				Port:              5432,
				Healthy:           false,
				Generation:        9,
				LastProbeUnixMs:   1_700_000_005_000,
				CheckpointPending: true,
				CheckpointToken:   "ckpt:vm-st2",
			},
		},
		StatefulBundles: []*nodev1.StatefulBundle{
			{
				SnapshotRef:     "stateful/scratch-postgres",
				Workload:        "scratch-postgres",
				Generation:      5,
				SizeBytes:       16384,
				CreatedAtUnixMs: 1_700_000_004_000,
				// exported (R6): this bundle's store copy is present and current.
				Exported: true,
			},
		},
		Volumes: []*nodev1.Volume{
			{
				Workload:       "scratch-postgres",
				Generation:     5,
				SizeBytes:      10_737_418_240,
				AllocatedBytes: 536_870_912,
				Attached:       true,
				// exported_generation (R6): the store holds the gen-5 (vol.img, gen)
				// pair, so a disk-loss restore recovers to generation 5.
				ExportedGeneration: 5,
				// generation_blessed (R7): this volume's generation was issued by the
				// control plane's blessing ledger, so the client can assert the new
				// field round-trips true for the blessed case.
				GenerationBlessed: true,
			},
		},
		// Group facts (R5): deterministic, so the client can assert the new
		// repeated group status fields round-trip. The bundle set is scripted
		// PARTIAL on purpose (one member ref where a whole group would report
		// several), proving the daemon reports refs grouped by set without judging
		// completeness (that judgment is the control plane's).
		GroupNetworks: []*nodev1.GroupNetwork{
			{
				GroupInstanceId: "grp-inst1",
				Cidr:            "10.100.0.0/24",
				Bridge:          "br-grp-inst1",
				MemberCount:     2,
			},
		},
		GroupMemberVms: []*nodev1.GroupMemberVm{
			{
				VmId:            "vm-g1",
				GroupInstanceId: "grp-inst1",
				MemberName:      "worker-0",
				Ip:              "10.100.0.10",
				Healthy:         true,
				LastProbeUnixMs: 1_700_000_005_000,
			},
		},
		GroupBundleSets: []*nodev1.GroupBundleSet{
			{
				SetId:           "set-abc",
				GroupInstanceId: "grp-inst1",
				Members: []*nodev1.GroupBundleMember{
					{MemberName: "worker-0", SnapshotRef: "group/set-abc/worker-0", SizeBytes: 5120},
				},
				CreatedAtUnixMs: 1_700_000_006_000,
				// exported (R6): the whole set's store copy is present and current.
				Exported: true,
			},
		},
		// Node-local activator facts (ADR embervm/018 Fork A): a fixed advertised
		// L7 endpoint and L4 ip so the client can assert the new node-level
		// activator fields round-trip. The node advertises its own pod IP +
		// activator port; EndpointPublisher prefers this over the CP pod activator
		// for the scaled-to-zero fallback route.
		ActivatorEndpoint: &nodev1.Endpoint{Ip: "10.99.0.1", Port: 8081},
		ActivatorIp:       "10.99.0.1",
	}, nil
}

func (s *fakeServer) WatchNode(req *nodev1.WatchNodeRequest, stream grpc.ServerStreamingServer[nodev1.NodeStatus]) error {
	s.mu.Lock()
	s.streamActive = true
	s.mu.Unlock()
	defer func() {
		s.mu.Lock()
		s.streamActive = false
		s.mu.Unlock()
	}()

	for i := uint32(0); ; i++ {
		s.mu.Lock()
		dropStatus := s.dropStatus
		closeStream := s.closeStream
		if closeStream {
			s.closeStream = false
		}
		primed := make(map[string][]string)
		if !s.suppressPrimed {
			for vmID := range s.vmState {
				workload := s.vmWorkload[vmID]
				primed[workload] = append(primed[workload], vmID)
			}
		}
		s.mu.Unlock()

		if closeStream && dropStatus {
			return nil
		}
		if dropStatus {
			time.Sleep(100 * time.Millisecond)
			continue
		}
		workloads := make([]*nodev1.WorkloadCapacity, 0, len(primed))
		for workload, vmIDs := range primed {
			workloads = append(workloads, &nodev1.WorkloadCapacity{
				Workload: workload, FreePrimedSlots: uint32(len(vmIDs)), PrimedVmIds: vmIDs,
			})
		}
		if err := stream.Send(&nodev1.NodeStatus{NodeId: req.GetNodeId(), LiveVms: i, Workloads: workloads}); err != nil {
			return err
		}
		if closeStream || i+1 == watchNodeHeartbeats {
			return nil
		}
	}
}

func main() {
	controlAddr := flag.String("control-addr", "", "HTTP fault-control listener address")
	flag.Parse()
	lis, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		fmt.Fprintf(os.Stderr, "fakenode: listen: %v\n", err)
		os.Exit(1)
	}
	srv := grpc.NewServer()
	fake := &fakeServer{store: map[string]bool{}, vmState: map[string]bool{}, vmWorkload: map[string]string{}}
	nodev1.RegisterNodeServiceServer(srv, fake)

	// Announce the chosen port on stdout (unbuffered) so the harness can connect.
	fmt.Printf("PORT=%d\n", lis.Addr().(*net.TCPAddr).Port)
	if *controlAddr != "" {
		controlLis, err := net.Listen("tcp", *controlAddr)
		if err != nil {
			fmt.Fprintf(os.Stderr, "fakenode: control listen: %v\n", err)
			os.Exit(1)
		}
		fmt.Printf("CONTROL_PORT=%d\n", controlLis.Addr().(*net.TCPAddr).Port)
		go http.Serve(controlLis, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			set := r.Method == http.MethodPost
			unset := r.Method == http.MethodDelete
			if !set && !unset {
				w.WriteHeader(http.StatusNotFound)
				return
			}
			fake.mu.Lock()
			switch r.URL.Path {
			case "/fault/suppress-primed":
				fake.suppressPrimed = set
			case "/fault/drop-status":
				fake.dropStatus = set
			case "/fault/close-stream":
				if set {
					fake.closeStream = true
				} else {
					fake.mu.Unlock()
					w.WriteHeader(http.StatusNotFound)
					return
				}
			default:
				fake.mu.Unlock()
				w.WriteHeader(http.StatusNotFound)
				return
			}
			fake.mu.Unlock()
			w.WriteHeader(http.StatusNoContent)
		}))
	}

	if err := srv.Serve(lis); err != nil {
		fmt.Fprintf(os.Stderr, "fakenode: serve: %v\n", err)
		os.Exit(1)
	}
}
