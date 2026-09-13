package server

import (
	"context"
	"testing"

	cachev3 "github.com/envoyproxy/go-control-plane/pkg/cache/v3"
	resourcev3 "github.com/envoyproxy/go-control-plane/pkg/resource/v3"

	"github.com/jomcgi/homelab/projects/embervm/xds/snapshot"
)

func desired(version string) *snapshot.Desired {
	return &snapshot.Desired{
		Version:  version,
		Clusters: []snapshot.Cluster{{Name: "c1", Endpoints: []snapshot.Endpoint{{IP: "10.0.0.1", Port: 80}}}},
		Routes:   []snapshot.Route{{Host: "h", Cluster: "c1"}},
	}
}

func TestStore_versionMonotonicity(t *testing.T) {
	ctx := context.Background()
	s := NewStore()

	if err := s.Apply(ctx, "node-a", desired("0000000002")); err != nil {
		t.Fatalf("first apply: %v", err)
	}
	if v, ok := s.CurrentVersion("node-a"); !ok || v != "0000000002" {
		t.Fatalf("current version = %q,%v", v, ok)
	}

	// A lower version is rejected as a conflict, leaving the served version intact.
	err := s.Apply(ctx, "node-a", desired("0000000001"))
	if err == nil || !IsVersionConflict(err) {
		t.Fatalf("want version conflict for lower version, got %v", err)
	}
	// An equal version is also rejected (strictly greater required).
	if err := s.Apply(ctx, "node-a", desired("0000000002")); err == nil || !IsVersionConflict(err) {
		t.Fatalf("want version conflict for equal version, got %v", err)
	}
	if v, _ := s.CurrentVersion("node-a"); v != "0000000002" {
		t.Fatalf("served version changed to %q after rejected pushes", v)
	}

	// A strictly greater version converges.
	if err := s.Apply(ctx, "node-a", desired("0000000003")); err != nil {
		t.Fatalf("higher version apply: %v", err)
	}
	if v, _ := s.CurrentVersion("node-a"); v != "0000000003" {
		t.Fatalf("served version = %q, want 0000000003", v)
	}
}

func TestStore_perNodeVersionsAreIndependent(t *testing.T) {
	ctx := context.Background()
	s := NewStore()
	if err := s.Apply(ctx, "node-a", desired("0000000005")); err != nil {
		t.Fatalf("node-a apply: %v", err)
	}
	// A different node starting at a lower version is not blocked by node-a.
	if err := s.Apply(ctx, "node-b", desired("0000000001")); err != nil {
		t.Fatalf("node-b apply should be independent: %v", err)
	}
}

func TestStore_nodeAndEdgeSnapshotsRemainIndependent(t *testing.T) {
	ctx := context.Background()
	s := NewStore()
	if err := s.Apply(ctx, "node-4", desired("0000000002")); err != nil {
		t.Fatalf("node snapshot: %v", err)
	}
	edge := &snapshot.Desired{
		Version: "0000000001",
		Clusters: []snapshot.Cluster{{
			Name:      "serve|ping",
			Endpoints: []snapshot.Endpoint{{IP: "10.42.4.8", Port: 30011}},
		}},
		Routes: []snapshot.Route{{Host: "ping.embervm.internal", Cluster: "serve|ping"}},
	}
	if err := s.Apply(ctx, "embervm-serving-edge", edge); err != nil {
		t.Fatalf("edge snapshot: %v", err)
	}

	// Both node ids retain independent cache entries. This is a unit-level cache
	// assertion only; Envoy retaining last-ACKed resources through control-plane
	// deletion is exercised by the deployment drill, not simulated here.
	for node, cluster := range map[string]string{
		"node-4":               "c1",
		"embervm-serving-edge": "serve|ping",
	} {
		raw, err := s.Cache().GetSnapshot(node)
		if err != nil {
			t.Fatalf("cached snapshot for %s: %v", node, err)
		}
		got := raw.(*cachev3.Snapshot).GetResources(resourcev3.ClusterType)
		if _, ok := got[cluster]; !ok {
			t.Fatalf("snapshot %s lost cluster %s: %v", node, cluster, got)
		}
	}
}

func TestStore_rejectsInconsistentSnapshot(t *testing.T) {
	// A route to an undefined cluster fails validation in Build before it can be
	// served, so Apply returns an error (not a conflict) and nothing is served.
	ctx := context.Background()
	s := NewStore()
	bad := &snapshot.Desired{
		Version: "1",
		Routes:  []snapshot.Route{{Host: "h", Cluster: "ghost"}},
	}
	if err := s.Apply(ctx, "node-a", bad); err == nil || IsVersionConflict(err) {
		t.Fatalf("want validation error, got %v", err)
	}
	if _, ok := s.CurrentVersion("node-a"); ok {
		t.Fatal("nothing should be served after a rejected apply")
	}
}
