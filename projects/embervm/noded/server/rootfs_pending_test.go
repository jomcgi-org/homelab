package server

import (
	"context"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/embervm/noded/config"
	nodev1 "github.com/jomcgi/homelab/projects/embervm/proto/embervm/node/v1"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func pendingRootName() string {
	return "rootfs-runtime-claude-sha256-" + strings.Repeat("a", 64) + "-size-4G-format-b2.ext4"
}

func TestRootfsPending(t *testing.T) {
	now := time.Now()
	for _, tc := range []struct {
		name                           string
		age                            time.Duration
		marker, present, enabled, want bool
	}{
		{"fresh", time.Minute, true, false, true, true},
		{"bound", 1800 * time.Second, true, false, true, true},
		{"stale", 1801 * time.Second, true, false, true, false},
		{"future", -time.Minute, true, false, true, false},
		{"absent", 0, false, false, true, false},
		{"env unset", 0, true, false, false, false},
		{"published", 0, true, true, true, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), pendingRootName())
			if tc.marker {
				if err := os.WriteFile(path+".pending", nil, 0600); err != nil {
					t.Fatal(err)
				}
				stamp := now.Add(-tc.age)
				if err := os.Chtimes(path+".pending", stamp, stamp); err != nil {
					t.Fatal(err)
				}
			}
			if tc.present {
				writeExt4Rootfs(t, filepath.Dir(path), filepath.Base(path), testRootfsUUIDA)
			}
			s := &Server{}
			if tc.enabled {
				s.cfg.RootfsPendingMaxAge = 1800 * time.Second
			}
			if got := s.rootfsPending(path, now); got != tc.want {
				t.Fatalf("rootfsPending=%v, want %v", got, tc.want)
			}
		})
	}
}

func TestBuildBaseRootfsPendingBothLanes(t *testing.T) {
	for _, zipLane := range []bool{false, true} {
		for _, mode := range []string{"fresh", "stale", "absent", "env unset", "published", "invalid"} {
			t.Run(fmtLane(zipLane)+"/"+mode, func(t *testing.T) {
				dir := t.TempDir()
				path := filepath.Join(dir, pendingRootName())
				if mode != "absent" {
					if err := os.WriteFile(path+".pending", nil, 0600); err != nil {
						t.Fatal(err)
					}
					if mode == "stale" {
						old := time.Now().Add(-time.Hour)
						if err := os.Chtimes(path+".pending", old, old); err != nil {
							t.Fatal(err)
						}
					}
				}
				if mode == "published" {
					writeExt4Rootfs(t, dir, pendingRootName(), testRootfsUUIDA)
				}
				if mode == "invalid" {
					if err := os.WriteFile(path, make([]byte, ext4HeaderSize), 0600); err != nil {
						t.Fatal(err)
					}
				}
				snapshotRoot := t.TempDir()
				build := &fakeDriver{snapshotRoot: snapshotRoot}
				s := New(Options{
					Config: config.Config{Arch: "amd64", Node: "node-4", SnapshotRoot: snapshotRoot,
						BootReadyTimeout: time.Second, Images: map[string]config.Image{"img:1": {RootfsPath: path}}},
					Driver: &fakeDriver{}, Transport: &fakeTransport{},
					NewBuildDriver: func(BuildDriverSpec) BuildDriver { return build },
					Logger:         slog.New(slog.NewTextHandler(io.Discard, nil)),
				})
				if mode != "env unset" {
					s.cfg.RootfsPendingMaxAge = 1800 * time.Second
				}
				req := &nodev1.BuildBaseRequest{Trace: &nodev1.Trace{Workload: "echo"}, ImageRef: "img:1",
					WorkloadRevision: "r1", ReadyPath: "/shim/ready", Resources: &nodev1.ResourceSpec{Vcpus: 1, MemMib: 128}}
				if zipLane {
					req.Source = &nodev1.BuildBaseRequest_Zip{Zip: &nodev1.ZipSource{
						RuntimeImageRef: "img:1", ArchiveUrl: "http://unused.invalid/archive.zip", ArchiveSha256: "archive-sha"}}
					// Stop at archive validation after the UUID gate without a network fetch.
				}
				_, err := s.BuildBase(context.Background(), req)
				want := codes.FailedPrecondition
				if mode == "fresh" {
					want = codes.Aborted
				}
				if mode == "published" {
					if !zipLane && err != nil {
						t.Fatal(err)
					}
					if zipLane && strings.Contains(err.Error(), "rootfs") {
						t.Fatalf("published root failed UUID gate: %v", err)
					}
					return
				}
				if status.Code(err) != want {
					t.Fatalf("BuildBase=%v, want %v", err, want)
				}
				bases := s.bases.snapshot()
				if mode == "fresh" {
					if len(bases) != 0 || s.nodeStatus().GetBuildError() != "" {
						t.Fatalf("pending build recorded failure: %+v", bases)
					}
				} else if len(bases) != 1 || bases[0].state != nodev1.BaseBuildState_BASE_BUILD_STATE_FAILED {
					t.Fatalf("missing/invalid root did not failBuild: %+v", bases)
				}
			})
		}
	}
}

func fmtLane(zip bool) string {
	if zip {
		return "zip"
	}
	return "image"
}
