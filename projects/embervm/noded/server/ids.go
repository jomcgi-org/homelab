package server

import (
	"strings"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// Control-plane identifiers that become path segments under the warmth root
// (snapshot refs, session and lineage ids, group ids, member and set names,
// workload names, artifact refs). The daemon joined them into paths unchecked,
// so an id of ".." reached os.RemoveAll in EvictSnapshot and DeleteGroupNetwork
// with the whole scratch tree as its target. The control plane is a bearer-
// authenticated peer, not an adversary, but the bearer is one Secret shared by
// every brick and the control plane, and a bug in either side is one bad string
// away from wiping a node. Every handler that takes such an id validates it
// here first and answers InvalidArgument otherwise; the driver's path helpers
// apply the same rule as a second line.

// safeSegment reports whether v is usable as exactly one path segment: not
// empty, not "." or "..", and free of separators and NUL.
func safeSegment(v string) bool {
	return v != "" && v != "." && v != ".." && !strings.ContainsAny(v, "/\\\x00")
}

// requireIDs validates each (field, value) pair as one path segment. Empty
// values pass: whether a field is required stays with the handler's own check,
// which carries the handler-specific message.
func requireIDs(pairs ...string) error {
	for i := 0; i+1 < len(pairs); i += 2 {
		field, v := pairs[i], pairs[i+1]
		if v == "" {
			continue
		}
		if !safeSegment(v) {
			return status.Errorf(codes.InvalidArgument, "noded: %s %q is not a valid identifier", field, v)
		}
	}
	return nil
}

// requireSnapshotRef validates a snapshot_ref: one segment for a session,
// serving or stateful bundle, or exactly group/<set_id>/<member_name> for a
// banked composite member.
func requireSnapshotRef(field, ref string) error {
	if ref == "" || safeSegment(ref) {
		return nil
	}
	parts := strings.Split(ref, "/")
	if len(parts) == 3 && parts[0] == "group" && safeSegment(parts[1]) && safeSegment(parts[2]) {
		return nil
	}
	return status.Errorf(codes.InvalidArgument, "noded: %s %q is not a valid snapshot ref", field, ref)
}
