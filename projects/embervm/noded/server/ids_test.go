package server

import (
	"strings"
	"testing"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func TestSafeSegment(t *testing.T) {
	for _, ok := range []string{"sess-1", "lineage_9", "a.b", "sha256:abc", "node-4", "pod uid"} {
		if !safeSegment(ok) {
			t.Errorf("safeSegment(%q) = false, want true", ok)
		}
	}
	for _, bad := range []string{"", ".", "..", "a/b", "../x", "/abs", "a\\b", "nul\x00"} {
		if safeSegment(bad) {
			t.Errorf("safeSegment(%q) = true, want false", bad)
		}
	}
}

func TestRequireIDsSkipsEmptyAndNamesTheField(t *testing.T) {
	if err := requireIDs("workload", "", "lineage_id", "lin-1"); err != nil {
		t.Fatalf("empty and safe values must pass: %v", err)
	}
	err := requireIDs("workload", "wl", "lineage_id", "../../bases")
	if status.Code(err) != codes.InvalidArgument {
		t.Fatalf("code = %v, want InvalidArgument", status.Code(err))
	}
	if msg := status.Convert(err).Message(); !strings.Contains(msg, "lineage_id") {
		t.Fatalf("message %q does not name the field", msg)
	}
}

func TestRequireSnapshotRefAcceptsGroupShapeOnly(t *testing.T) {
	for _, ok := range []string{"", "sess-snap", "group/set-1/leader"} {
		if err := requireSnapshotRef("snapshot_ref", ok); err != nil {
			t.Errorf("requireSnapshotRef(%q) = %v, want accepted", ok, err)
		}
	}
	for _, bad := range []string{"..", "../..", "group/../x", "group/set", "group/set/a/b", "other/set/leader", "set/leader", "group//leader"} {
		if err := requireSnapshotRef("snapshot_ref", bad); status.Code(err) != codes.InvalidArgument {
			t.Errorf("requireSnapshotRef(%q) = %v, want InvalidArgument", bad, err)
		}
	}
}
