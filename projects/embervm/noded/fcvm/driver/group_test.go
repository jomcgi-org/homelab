package driver

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/jomcgi/homelab/projects/embervm/noded/substrate"
)

// TestGroupNetworkRecordRoundTrip asserts a written record is rescanned back
// verbatim, a removed record disappears, and a fresh node (no dir) scans to nil.
func TestGroupNetworkRecordRoundTrip(t *testing.T) {
	d := testDriver(t)

	// Fresh node: no group_networks dir yet, scan yields nil.
	if got := d.ScanGroupNetworks(); got != nil {
		t.Errorf("fresh-node scan = %v, want nil", got)
	}

	recA := substrate.GroupNetworkRecord{
		GroupInstanceID: "grp-A",
		BridgeName:      "emgAAAAAA",
		SubnetCIDR:      "10.101.1.0/24",
		GatewayIP:       "10.101.1.1",
		CreatedAtUnixMs: 1700000000000,
	}
	recB := substrate.GroupNetworkRecord{
		GroupInstanceID: "grp-B",
		BridgeName:      "emgBBBBBB",
		SubnetCIDR:      "10.101.2.0/24",
		GatewayIP:       "10.101.2.1",
		CreatedAtUnixMs: 1700000001000,
	}
	if err := d.WriteGroupNetworkRecord(recA); err != nil {
		t.Fatalf("WriteGroupNetworkRecord(A): %v", err)
	}
	if err := d.WriteGroupNetworkRecord(recB); err != nil {
		t.Fatalf("WriteGroupNetworkRecord(B): %v", err)
	}

	got := d.ScanGroupNetworks()
	byID := map[string]substrate.GroupNetworkRecord{}
	for _, r := range got {
		byID[r.GroupInstanceID] = r
	}
	if len(byID) != 2 {
		t.Fatalf("scan returned %d records, want 2: %v", len(byID), got)
	}
	if byID["grp-A"] != recA {
		t.Errorf("grp-A round-trip mismatch:\n got %+v\nwant %+v", byID["grp-A"], recA)
	}
	if byID["grp-B"] != recB {
		t.Errorf("grp-B round-trip mismatch:\n got %+v\nwant %+v", byID["grp-B"], recB)
	}

	// Removing grp-A leaves only grp-B.
	if err := d.RemoveGroupNetworkRecord("grp-A"); err != nil {
		t.Fatalf("RemoveGroupNetworkRecord(A): %v", err)
	}
	got = d.ScanGroupNetworks()
	if len(got) != 1 || got[0].GroupInstanceID != "grp-B" {
		t.Errorf("after removing A, scan = %v", got)
	}
	// Removing again is idempotent.
	if err := d.RemoveGroupNetworkRecord("grp-A"); err != nil {
		t.Errorf("idempotent remove: %v", err)
	}
}

// TestScanGroupNetworksSkipsCorrupt asserts a record dir with no or malformed
// config.json is skipped, and a body missing the id recovers it from the dir name.
func TestScanGroupNetworksSkipsCorrupt(t *testing.T) {
	d := testDriver(t)
	root := d.GroupNetworksDir()

	// A dir with a malformed config.json is skipped.
	corrupt := filepath.Join(root, "grp-corrupt")
	if err := os.MkdirAll(corrupt, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(corrupt, "config.json"), []byte("{not json"), 0o600); err != nil {
		t.Fatal(err)
	}
	// A dir with NO config.json is skipped.
	if err := os.MkdirAll(filepath.Join(root, "grp-empty"), 0o700); err != nil {
		t.Fatal(err)
	}
	// A valid record whose body OMITS the id: recovered from the dir name.
	noID := filepath.Join(root, "grp-noid")
	if err := os.MkdirAll(noID, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(noID, "config.json"), []byte(`{"subnetCidr":"10.101.9.0/24"}`), 0o600); err != nil {
		t.Fatal(err)
	}

	got := d.ScanGroupNetworks()
	if len(got) != 1 {
		t.Fatalf("scan = %v, want exactly the recovered grp-noid", got)
	}
	if got[0].GroupInstanceID != "grp-noid" || got[0].SubnetCIDR != "10.101.9.0/24" {
		t.Errorf("id recovery from dir name failed: %+v", got[0])
	}
}
