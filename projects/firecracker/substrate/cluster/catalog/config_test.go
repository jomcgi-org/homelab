package catalog

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// TestLoadDefaultsNoWorkloads verifies the documented defaults apply when all
// FC_INVOKE_* vars are cleared: ListenAddr ":8080", Arch falls back to
// runtime.GOARCH (non-empty), Workloads is an empty map, no error.
func TestLoadDefaultsNoWorkloads(t *testing.T) {
	for _, k := range []string{
		"FC_INVOKE_LISTEN_ADDR", "FC_INVOKE_NODE",
		"FC_INVOKE_ARCH", "FC_INVOKE_SNAPSHOT_ROOT",
		"FC_INVOKE_WORKLOADS", "FC_INVOKE_WORKLOADS_FILE",
		"FC_INVOKE_FIRECRACKER_BIN", "FC_INVOKE_KERNEL_IMAGE",
		"FC_INVOKE_KERNEL_BOOT_ARGS", "FC_INVOKE_HARNESS_INIT",
		"FC_INVOKE_CANONICAL_VSOCK_DIR", "FC_INVOKE_GUEST_OOM_SCORE_ADJ",
		"FC_INVOKE_BOOT_READY_TIMEOUT", "NODE_NAME",
	} {
		t.Setenv(k, "")
	}

	c, err := Load()
	if err != nil {
		t.Fatalf("Load() error = %v", err)
	}
	if c.ListenAddr != ":8080" {
		t.Errorf("ListenAddr = %q, want :8080", c.ListenAddr)
	}
	if c.Arch == "" {
		t.Error("Arch should fall back to runtime.GOARCH, got empty string")
	}
	if len(c.Workloads) != 0 {
		t.Errorf("Workloads = %v, want empty map", c.Workloads)
	}
	if c.BinPath != "/opt/fc/firecracker" {
		t.Errorf("BinPath = %q, want /opt/fc/firecracker", c.BinPath)
	}
	if c.KernelImagePath != "/opt/fc/vmlinux.container" {
		t.Errorf("KernelImagePath = %q, want the baked-in /opt/fc/vmlinux.container default", c.KernelImagePath)
	}
	if c.HarnessInit != "/usr/local/bin/fc-shim-init" {
		t.Errorf("HarnessInit = %q, want /usr/local/bin/fc-shim-init", c.HarnessInit)
	}
	if c.CanonicalVsockDir != "/disks/nvme-02/fc-invoke-vsock" {
		t.Errorf("CanonicalVsockDir = %q, want /disks/nvme-02/fc-invoke-vsock", c.CanonicalVsockDir)
	}
	if c.GuestOomScoreAdj != 1000 {
		t.Errorf("GuestOomScoreAdj = %d, want 1000", c.GuestOomScoreAdj)
	}
	if c.BootReadyTimeout != 60*time.Second {
		t.Errorf("BootReadyTimeout = %s, want 60s", c.BootReadyTimeout)
	}
}

// TestLoadWorkloadsInlineJSON verifies that a JSON object set in
// FC_INVOKE_WORKLOADS is parsed and per-workload defaults applied correctly.
// The "semgrep" workload omits optional fields; the "artifact" workload sets
// all optional knobs.
func TestLoadWorkloadsInlineJSON(t *testing.T) {
	t.Setenv("FC_INVOKE_WORKLOADS_FILE", "")
	t.Setenv("FC_INVOKE_WORKLOADS", `{
		"semgrep": {
			"image": "semgrep-guest",
			"rootfsPath": "/disks/nvme-02/images/semgrep-guest.ext4",
			"egressEnabled": false,
			"warmBase": true
		},
		"artifact": {
			"image": "artifact-guest",
			"egressEnabled": true,
			"egressSecrets": ["openrouter"],
			"requestTimeout": "180s",
			"sessioned": true
		}
	}`)

	c, err := Load()
	if err != nil {
		t.Fatalf("Load() error = %v", err)
	}

	// Verify semgrep workload defaults.
	sg, ok := c.Workloads["semgrep"]
	if !ok {
		t.Fatal("semgrep workload not found in parsed table")
	}
	if sg.ReadyPath != "/shim/ready" {
		t.Errorf("semgrep ReadyPath = %q, want /shim/ready", sg.ReadyPath)
	}
	if sg.Concurrency != 4 {
		t.Errorf("semgrep Concurrency = %d, want 4 (default)", sg.Concurrency)
	}
	if sg.VCPUs != 2 {
		t.Errorf("semgrep VCPUs = %d, want 2 (default)", sg.VCPUs)
	}
	if sg.MemMib != 2048 {
		t.Errorf("semgrep MemMib = %d, want 2048 (default)", sg.MemMib)
	}
	if sg.RequestTimeout != 90*time.Second {
		t.Errorf("semgrep RequestTimeout = %s, want 90s (default)", sg.RequestTimeout)
	}
	if !sg.WarmBase {
		t.Error("semgrep WarmBase = false, want true")
	}
	if sg.RootfsPath != "/disks/nvme-02/images/semgrep-guest.ext4" {
		t.Errorf("semgrep RootfsPath = %q, want /disks/nvme-02/images/semgrep-guest.ext4", sg.RootfsPath)
	}

	// Verify artifact workload explicit values.
	art, ok := c.Workloads["artifact"]
	if !ok {
		t.Fatal("artifact workload not found in parsed table")
	}
	if art.RequestTimeout != 180*time.Second {
		t.Errorf("artifact RequestTimeout = %s, want 180s", art.RequestTimeout)
	}
	if !art.Sessioned {
		t.Error("artifact Sessioned = false, want true")
	}
	if !art.EgressEnabled {
		t.Error("artifact EgressEnabled = false, want true")
	}
	if len(art.EgressSecrets) != 1 || art.EgressSecrets[0] != "openrouter" {
		t.Errorf("artifact EgressSecrets = %v, want [openrouter]", art.EgressSecrets)
	}
}

// TestLoadWorkloadsFileTakesPrecedence verifies that FC_INVOKE_WORKLOADS_FILE
// wins when both FC_INVOKE_WORKLOADS_FILE and FC_INVOKE_WORKLOADS are set.
func TestLoadWorkloadsFileTakesPrecedence(t *testing.T) {
	dir := t.TempDir()
	filePath := filepath.Join(dir, "workloads.json")
	if err := os.WriteFile(filePath, []byte(`{"fromfile": {"image": "file-image"}}`), 0o600); err != nil {
		t.Fatalf("WriteFile: %v", err)
	}

	t.Setenv("FC_INVOKE_WORKLOADS_FILE", filePath)
	t.Setenv("FC_INVOKE_WORKLOADS", `{"frominline": {"image": "inline-image"}}`)

	c, err := Load()
	if err != nil {
		t.Fatalf("Load() error = %v", err)
	}
	if _, ok := c.Workloads["fromfile"]; !ok {
		t.Error("expected 'fromfile' workload from file source, not found")
	}
	if _, ok := c.Workloads["frominline"]; ok {
		t.Error("unexpected 'frominline' workload: FC_INVOKE_WORKLOADS_FILE should take precedence")
	}
}

// TestLoadMalformedWorkloadsError verifies that invalid JSON in the workloads
// source returns a non-nil error.
func TestLoadMalformedWorkloadsError(t *testing.T) {
	t.Setenv("FC_INVOKE_WORKLOADS_FILE", "")
	t.Setenv("FC_INVOKE_WORKLOADS", `{not valid json}`)

	if _, err := Load(); err == nil {
		t.Fatal("Load() error = nil, want a parse error for malformed JSON")
	}
}

// TestLoadMalformedDurationError verifies that an unparseable requestTimeout
// string returns an error that names the offending workload.
func TestLoadMalformedDurationError(t *testing.T) {
	t.Setenv("FC_INVOKE_WORKLOADS_FILE", "")
	t.Setenv("FC_INVOKE_WORKLOADS", `{"badtimeout": {"image": "x", "requestTimeout": "notaduration"}}`)

	_, err := Load()
	if err == nil {
		t.Fatal("Load() error = nil, want duration parse error")
	}
	if !strings.Contains(err.Error(), "badtimeout") {
		t.Errorf("error %q does not mention workload name 'badtimeout'", err.Error())
	}
}

// TestLoadGlobalOverrides verifies that FC_INVOKE_LISTEN_ADDR, FC_INVOKE_NODE,
// and FC_INVOKE_ARCH all override their respective defaults.
func TestLoadGlobalOverrides(t *testing.T) {
	t.Setenv("FC_INVOKE_LISTEN_ADDR", ":9090")
	t.Setenv("FC_INVOKE_NODE", "node-4")
	t.Setenv("FC_INVOKE_ARCH", "arm64")
	t.Setenv("FC_INVOKE_WORKLOADS", "")
	t.Setenv("FC_INVOKE_WORKLOADS_FILE", "")

	c, err := Load()
	if err != nil {
		t.Fatalf("Load() error = %v", err)
	}
	if c.ListenAddr != ":9090" {
		t.Errorf("ListenAddr = %q, want :9090", c.ListenAddr)
	}
	if c.Node != "node-4" {
		t.Errorf("Node = %q, want node-4", c.Node)
	}
	if c.Arch != "arm64" {
		t.Errorf("Arch = %q, want arm64", c.Arch)
	}
}
