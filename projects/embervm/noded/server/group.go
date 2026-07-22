package server

import (
	"context"
	"os"
	"sort"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	nodev1 "github.com/jomcgi/homelab/projects/embervm/proto/embervm/node/v1"

	"github.com/jomcgi/homelab/projects/embervm/noded/substrate"
)

// CreateGroupNetwork stands up the per-group bridge for group_instance_id on the
// control-plane-assigned /24, installs the inter-group isolation posture (the
// composite<->composite and composite->serving forward DROPs, in the dedicated
// embervm_group table so they never collide with serving_dnat/forward), writes
// the durable on-disk record, and returns (bridge_name, gateway_ip). It is
// IDEMPOTENT per group_instance_id (D-R3.11.4): a re-issue for the same group with
// the same cidr returns the existing bridge without disturbing it, so the control
// plane can safely re-issue before a relight or an adoption-time rebind. A cidr
// that is not a /24 within the supernet, or that overlaps a DIFFERENT group's /24,
// is refused FAILED_PRECONDITION per the proto.
func (s *Server) CreateGroupNetwork(ctx context.Context, req *nodev1.CreateGroupNetworkRequest) (*nodev1.CreateGroupNetworkResponse, error) {
	if s.groupNet == nil {
		return nil, status.Error(codes.Unimplemented, "noded: group networking not configured")
	}
	if s.isDraining() {
		return nil, status.Error(codes.Unavailable, "noded: draining")
	}
	groupInstanceID := req.GetGroupInstanceId()
	if groupInstanceID == "" {
		return nil, status.Error(codes.InvalidArgument, "noded: group_instance_id required")
	}
	cidr := req.GetCidr()
	if cidr == "" {
		return nil, status.Error(codes.InvalidArgument, "noded: cidr required")
	}

	bridge, gatewayIP, err := s.groupNet.CreateGroupNetwork(ctx, groupInstanceID, cidr)
	if err != nil {
		// A validation/overlap failure is a client-side precondition (bad or
		// colliding cidr), mapped to FAILED_PRECONDITION per the proto.
		return nil, status.Errorf(codes.FailedPrecondition, "noded: create group network %q: %v", groupInstanceID, err)
	}

	// Persist the durable on-disk record. The bridge dies with the pod; this record
	// is what a restart rescans (and what the control plane re-issues against). A
	// write failure tears the just-created bridge back down so we never advertise a
	// group whose durable truth was not recorded (a rescan would then not rebuild
	// it, an inconsistency worse than failing the create).
	if s.groupRecords != nil {
		rec := substrate.GroupNetworkRecord{
			GroupInstanceID: groupInstanceID,
			BridgeName:      bridge,
			SubnetCIDR:      cidr,
			GatewayIP:       gatewayIP,
			CreatedAtUnixMs: time.Now().UnixMilli(),
		}
		if werr := s.groupRecords.WriteGroupNetworkRecord(rec); werr != nil {
			_ = s.groupNet.DeleteGroupNetwork(ctx, groupInstanceID)
			return nil, status.Errorf(codes.Internal, "noded: persist group network record for %q: %v", groupInstanceID, werr)
		}
	}

	s.signalChange()
	return &nodev1.CreateGroupNetworkResponse{
		BridgeName: bridge,
		GatewayIp:  gatewayIP,
	}, nil
}

// DeleteGroupNetwork tears the group bridge, its nftables rules, and its on-disk
// record down. It REFUSES FAILED_PRECONDITION when any live member VM is still
// attached to the group (deleting the bridge would yank a live member's NIC); the
// control plane must stop every member first. It is otherwise idempotent: deleting
// an unknown group returns OK (the desired end-state already holds).
func (s *Server) DeleteGroupNetwork(ctx context.Context, req *nodev1.DeleteGroupNetworkRequest) (*nodev1.DeleteGroupNetworkResponse, error) {
	if s.groupNet == nil {
		return nil, status.Error(codes.Unimplemented, "noded: group networking not configured")
	}
	groupInstanceID := req.GetGroupInstanceId()
	if groupInstanceID == "" {
		return nil, status.Error(codes.InvalidArgument, "noded: group_instance_id required")
	}

	// Idempotent no-op on an unknown group (never held or already deleted).
	if !s.groupNet.Has(groupInstanceID) {
		// Still remove any stray record so a half-deleted group cannot be re-adopted.
		if s.groupRecords != nil {
			_ = s.groupRecords.RemoveGroupNetworkRecord(groupInstanceID)
		}
		return &nodev1.DeleteGroupNetworkResponse{}, nil
	}

	// Attached-member refusal: a live member on this group's bridge blocks the
	// delete. The registry is filled by Task 5's StartGroupMember; in Task 4 it is
	// empty, so this never fires yet, but the guard is wired now so Task 5's members
	// are protected the moment they exist.
	if s.groupMembers.hasMembers(groupInstanceID) {
		return nil, status.Errorf(codes.FailedPrecondition, "noded: group %q has %d live member(s) still attached; stop them before deleting the network", groupInstanceID, s.groupMembers.memberCount(groupInstanceID))
	}

	if err := s.groupNet.DeleteGroupNetwork(ctx, groupInstanceID); err != nil {
		return nil, status.Errorf(codes.Internal, "noded: delete group network %q: %v", groupInstanceID, err)
	}
	// Remove the durable record LAST: the bridge is gone, so a rescan must not
	// re-seed this group. Best-effort (the network is already torn down; a stale
	// record only re-adopts a dead group, which the control plane reconciles).
	if s.groupRecords != nil {
		if rerr := s.groupRecords.RemoveGroupNetworkRecord(groupInstanceID); rerr != nil {
			s.logger.Warn("noded: remove group network record", "group", groupInstanceID, "err", rerr)
		}
	}
	s.signalChange()
	return &nodev1.DeleteGroupNetworkResponse{}, nil
}

// ReconcileGroupNetworksFromDisk scans the group_networks/ record dir (via the
// driver's ScanGroupNetworks) and re-seeds the group-network manager so a
// restarted daemon reports what group networks the durable records describe, and
// EnsureNetwork rebuilds the isolation table over them. The BRIDGES themselves do
// NOT survive a restart (they died in the prior pod's netns, D-R3.11.4), so the
// control plane re-issues an idempotent CreateGroupNetwork to rebuild each bridge
// (Task 7 sequences this); this rescan is purely the durable-truth re-seed that
// makes NodeStatus.group_networks reflect the surviving records. A malformed
// record is skipped and logged.
func (s *Server) ReconcileGroupNetworksFromDisk() {
	if s.groupNet == nil || s.groupRecords == nil {
		return
	}
	root := s.groupRecords.GroupNetworksDir()
	if root != "" {
		if err := os.MkdirAll(root, 0o700); err != nil {
			s.logger.Warn("noded: create group networks dir", "root", root, "err", err)
		} else if err := os.Chmod(root, 0o700); err != nil {
			s.logger.Warn("noded: chmod group networks dir 0700", "root", root, "err", err)
		}
	}
	records := s.groupRecords.ScanGroupNetworks()
	seeded := 0
	for _, rec := range records {
		if err := s.groupNet.AdoptGroupNetwork(rec.GroupInstanceID, rec.SubnetCIDR, rec.CreatedAtUnixMs); err != nil {
			s.logger.Warn("noded: adopt group network record", "group", rec.GroupInstanceID, "err", err)
			continue
		}
		seeded++
	}
	if seeded > 0 {
		s.logger.Info("noded: reconciled existing group networks", "count", seeded)
		s.signalChange()
	}
}

// groupNetworksStatus projects the held group-network inventory into
// NodeStatus.group_networks, stamping each with the live member_count (0 until
// Task 5 fills the member registry). Reported so a restarted control plane
// reconciles group networks from node truth and the DeleteGroupNetwork backstop
// has a member count to read.
func (s *Server) groupNetworksStatus() []*nodev1.GroupNetwork {
	if s.groupNet == nil {
		return nil
	}
	nets := s.groupNet.List()
	out := make([]*nodev1.GroupNetwork, 0, len(nets))
	for _, n := range nets {
		out = append(out, &nodev1.GroupNetwork{
			GroupInstanceId: n.GroupInstanceID,
			Cidr:            n.CIDR,
			Bridge:          n.Bridge,
			MemberCount:     uint32(s.groupMembers.memberCount(n.GroupInstanceID)),
		})
	}
	return out
}

// ReconcileGroupBundlesFromDisk scans the group/ bundle dir (via the driver's
// ScanGroupBundleSets) and re-seeds the banked-group-bundle inventory so a restarted
// daemon reports what banked group warmth survives, GROUPED BY set. A daemon restart
// kills every LIVE member (they died with the pod, the standing single-node
// availability posture); only the on-disk bundle sets survive, and the control plane
// resolves each group to relightable (a complete set) or fresh-bootable from these
// reports (the daemon makes NO completeness judgment). The set dir names only the
// set_id + member, but a member banked after #38 carries a group_instance_id
// sidecar beside its bundle, which this rescan reads back so the boot-scanned
// entry is remotely evictable; a pre-sidecar member reads back "" and the control
// plane rebinds it by adoption (and the reaper SKIPS an empty-binding set).
func (s *Server) ReconcileGroupBundlesFromDisk() {
	if s.groupDriver == nil {
		return
	}
	root := s.groupDriver.GroupSetsDir()
	if root != "" {
		if err := os.MkdirAll(root, 0o700); err != nil {
			s.logger.Warn("noded: create group bundle dir", "root", root, "err", err)
		} else if err := os.Chmod(root, 0o700); err != nil {
			s.logger.Warn("noded: chmod group bundle dir 0700", "root", root, "err", err)
		}
	}
	sets := s.groupDriver.ScanGroupBundleSets()
	seeded := 0
	for _, set := range sets {
		for _, m := range set.Members {
			s.groupBundles.add(groupBundleEntry{
				setID:      set.SetID,
				memberName: m.MemberName,
				// Recover the group_instance_id from the on-disk sidecar (#38 F2), so
				// this boot-scanned member is remotely evictable across a restart.
				// "" for a pre-sidecar member: the reaper SKIPS such a set.
				groupInstanceID: s.readGroupInstanceSidecar(set.SetID, m.MemberName),
				snapshotRef:     m.SnapshotRef,
				sizeBytes:       m.SizeBytes,
				createdAtUnixMs: set.CreatedAtUnixMs,
			})
			seeded++
		}
	}
	if seeded > 0 {
		s.logger.Info("noded: reconciled existing group bundle sets", "sets", len(sets), "members", seeded)
		s.signalChange()
	}
}

// groupBundleSetsStatus projects the banked-group-bundle inventory into
// NodeStatus.group_bundle_sets, grouping the per-member bundles by their set_id. The
// daemon reports refs grouped by the set dir it wrote them under and makes NO
// completeness judgment (whether a set has every member it needs to relight is the
// control plane's to decide).
func (s *Server) groupBundleSetsStatus() []*nodev1.GroupBundleSet {
	if s.groupBundles == nil {
		return nil
	}
	entries := s.groupBundles.snapshot()
	// Group by set_id, preserving each set's group binding + created time.
	type setAgg struct {
		groupInstanceID string
		createdAtUnixMs int64
		members         []*nodev1.GroupBundleMember
	}
	bySet := make(map[string]*setAgg)
	for _, e := range entries {
		agg, ok := bySet[e.setID]
		if !ok {
			agg = &setAgg{}
			bySet[e.setID] = agg
		}
		if e.groupInstanceID != "" {
			agg.groupInstanceID = e.groupInstanceID
		}
		if e.createdAtUnixMs > agg.createdAtUnixMs {
			agg.createdAtUnixMs = e.createdAtUnixMs
		}
		agg.members = append(agg.members, &nodev1.GroupBundleMember{
			MemberName:  e.memberName,
			SnapshotRef: e.snapshotRef,
			SizeBytes:   uint64(e.sizeBytes),
		})
	}
	out := make([]*nodev1.GroupBundleSet, 0, len(bySet))
	for setID, agg := range bySet {
		sort.Slice(agg.members, func(i, j int) bool { return agg.members[i].GetMemberName() < agg.members[j].GetMemberName() })
		out = append(out, &nodev1.GroupBundleSet{
			SetId:           setID,
			GroupInstanceId: agg.groupInstanceID,
			Members:         agg.members,
			CreatedAtUnixMs: agg.createdAtUnixMs,
			// A set is the export unit: `exported` is true only when the WHOLE set's
			// store copy is present (the export queue marks the set prefix on a
			// completed set export). Keyed by the group instance + set_id (Fork 3).
			Exported: s.artifactExported(nodev1.ArtifactKind_ARTIFACT_KIND_GROUP_SET, agg.groupInstanceID, setID),
		})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].GetSetId() < out[j].GetSetId() })
	return out
}

// groupMemberVmsStatus projects the live group-member registry into
// NodeStatus.group_member_vms, reporting each member's group identity and TCP-connect
// health verdict.
func (s *Server) groupMemberVmsStatus() []*nodev1.GroupMemberVm {
	if s.groupMembers == nil {
		return nil
	}
	live := s.groupMembers.snapshot()
	out := make([]*nodev1.GroupMemberVm, 0, len(live))
	for _, e := range live {
		out = append(out, &nodev1.GroupMemberVm{
			VmId:            e.vmID,
			GroupInstanceId: e.groupInstanceID,
			MemberName:      e.memberName,
			Ip:              e.ip,
			Healthy:         e.healthy,
			LastProbeUnixMs: e.lastProbeUnixMs,
		})
	}
	return out
}
