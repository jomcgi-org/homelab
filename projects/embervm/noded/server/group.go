package server

import (
	"context"
	"os"
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

// groupMemberVmsStatus projects the live group-member registry into
// NodeStatus.group_member_vms. Empty in Task 4 (no live members yet); Task 5's
// StartGroupMember fills the registry and this reports each member's group
// identity and TCP-connect health verdict.
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
