package fcclient

import (
	"context"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"sync"
	"testing"
)

// fakeFC is an in-process Firecracker API stub listening on a unix socket. It
// records every request so tests can assert the controller drives the API in
// the right order with the right bodies.
type fakeFC struct {
	mu       sync.Mutex
	requests []recordedReq
	srv      *http.Server
	failPath string // if set, return 400 for this path
}

type recordedReq struct {
	Method string
	Path   string
	Body   map[string]any
}

func startFakeFC(t *testing.T) (*fakeFC, string) {
	t.Helper()
	dir := t.TempDir()
	sock := filepath.Join(dir, "fc.sock")
	ln, err := net.Listen("unix", sock)
	if err != nil {
		t.Fatalf("listen unix: %v", err)
	}
	f := &fakeFC{}
	mux := http.NewServeMux()
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		if b, _ := io.ReadAll(r.Body); len(b) > 0 {
			_ = json.Unmarshal(b, &body)
		}
		f.mu.Lock()
		f.requests = append(f.requests, recordedReq{Method: r.Method, Path: r.URL.Path, Body: body})
		fail := f.failPath != "" && r.URL.Path == f.failPath
		f.mu.Unlock()
		if fail {
			w.WriteHeader(http.StatusBadRequest)
			_, _ = w.Write([]byte(`{"fault_message":"boom"}`))
			return
		}
		w.WriteHeader(http.StatusNoContent)
	})
	f.srv = &http.Server{Handler: mux}
	go func() { _ = f.srv.Serve(ln) }()
	t.Cleanup(func() { _ = f.srv.Close(); _ = os.Remove(sock) })
	return f, sock
}

func (f *fakeFC) paths() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]string, 0, len(f.requests))
	for _, r := range f.requests {
		out = append(out, r.Method+" "+r.Path)
	}
	return out
}

func TestClientBootSequence(t *testing.T) {
	fake, sock := startFakeFC(t)
	c := New(sock)
	ctx := context.Background()

	if err := c.PutMachineConfig(ctx, MachineConfig{VCPUCount: 1, MemSizeMib: 1024}); err != nil {
		t.Fatalf("PutMachineConfig: %v", err)
	}
	if err := c.PutBootSource(ctx, BootSource{KernelImagePath: "/opt/kata/vmlinux", BootArgs: "console=ttyS0"}); err != nil {
		t.Fatalf("PutBootSource: %v", err)
	}
	if err := c.PutDrive(ctx, Drive{DriveID: "rootfs", PathOnHost: "/dev/mapper/x", IsRootDevice: true}); err != nil {
		t.Fatalf("PutDrive: %v", err)
	}
	if err := c.Start(ctx); err != nil {
		t.Fatalf("Start: %v", err)
	}

	got := fake.paths()
	want := []string{"PUT /machine-config", "PUT /boot-source", "PUT /drives/rootfs", "PUT /actions"}
	if len(got) != len(want) {
		t.Fatalf("paths = %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("path[%d] = %q, want %q", i, got[i], want[i])
		}
	}
}

func TestClientSnapshotLifecycle(t *testing.T) {
	fake, sock := startFakeFC(t)
	c := New(sock)
	ctx := context.Background()

	if err := c.Pause(ctx); err != nil {
		t.Fatalf("Pause: %v", err)
	}
	if err := c.CreateSnapshot(ctx, SnapshotCreate{SnapshotPath: "/snap/snapfile", MemFilePath: "/snap/memfile"}); err != nil {
		t.Fatalf("CreateSnapshot: %v", err)
	}
	if err := c.Resume(ctx); err != nil {
		t.Fatalf("Resume: %v", err)
	}

	fake.mu.Lock()
	defer fake.mu.Unlock()
	// Pause and Resume both PATCH /vm; assert the state values differ.
	var states []string
	var snap recordedReq
	for _, r := range fake.requests {
		if r.Path == "/vm" {
			states = append(states, r.Body["state"].(string))
		}
		if r.Path == "/snapshot/create" {
			snap = r
		}
	}
	if len(states) != 2 || states[0] != "Paused" || states[1] != "Resumed" {
		t.Fatalf("vm states = %v, want [Paused Resumed]", states)
	}
	if snap.Body["snapshot_path"] != "/snap/snapfile" || snap.Body["mem_file_path"] != "/snap/memfile" {
		t.Fatalf("snapshot body = %v", snap.Body)
	}
}

func TestClientLoadSnapshotResume(t *testing.T) {
	fake, sock := startFakeFC(t)
	c := New(sock)
	if err := c.LoadSnapshot(context.Background(), SnapshotLoad{
		SnapshotPath: "/snap/snapfile",
		MemBackend:   &MemBackend{BackendType: "File", BackendPath: "/snap/memfile"},
		ResumeVM:     true,
	}); err != nil {
		t.Fatalf("LoadSnapshot: %v", err)
	}
	fake.mu.Lock()
	defer fake.mu.Unlock()
	r := fake.requests[0]
	if r.Path != "/snapshot/load" || r.Body["resume_vm"] != true {
		t.Fatalf("load body = %+v", r)
	}
	mb, ok := r.Body["mem_backend"].(map[string]any)
	if !ok || mb["backend_type"] != "File" {
		t.Fatalf("mem_backend = %v", r.Body["mem_backend"])
	}
}

func TestClientPatchDrive(t *testing.T) {
	fake, sock := startFakeFC(t)
	c := New(sock)
	drive := Drive{DriveID: "volume", PathOnHost: "/sessions/s1/workspace.img", IsRootDevice: false, IsReadOnly: false}
	if err := c.PatchDrive(context.Background(), "volume", drive); err != nil {
		t.Fatalf("PatchDrive: %v", err)
	}

	fake.mu.Lock()
	defer fake.mu.Unlock()
	if len(fake.requests) != 1 {
		t.Fatalf("requests = %d, want 1", len(fake.requests))
	}
	r := fake.requests[0]
	if r.Method != http.MethodPatch || r.Path != "/drives/volume" {
		t.Fatalf("request = %s %s, want PATCH /drives/volume", r.Method, r.Path)
	}
	if r.Body["drive_id"] != "volume" || r.Body["path_on_host"] != drive.PathOnHost || r.Body["is_read_only"] != false {
		t.Fatalf("drive body = %v", r.Body)
	}
}

func TestClientPropagatesAPIError(t *testing.T) {
	fake, sock := startFakeFC(t)
	fake.failPath = "/actions"
	c := New(sock)
	err := c.Start(context.Background())
	if err == nil {
		t.Fatal("expected error from failing /actions")
	}
	if !contains(err.Error(), "status 400") || !contains(err.Error(), "boom") {
		t.Fatalf("error = %v, want status 400 + fault message", err)
	}
}

func contains(s, sub string) bool {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return false
}
