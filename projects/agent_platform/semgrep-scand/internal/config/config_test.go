package config

import (
	"testing"
	"time"
)

// TestLoadDefaults verifies the documented defaults apply when no SEMGREP_SCAND_*
// vars are set (Arch falls back to the build GOARCH, which is non-empty).
func TestLoadDefaults(t *testing.T) {
	// Ensure a clean slate for the values with defaults we assert on.
	for _, k := range []string{
		"SEMGREP_SCAND_LISTEN_ADDR", "SEMGREP_SCAND_MAX_CONCURRENT",
		"SEMGREP_SCAND_GUEST_MEM_MIB", "SEMGREP_SCAND_GUEST_VCPUS",
		"SEMGREP_SCAND_GUEST_OOM_SCORE_ADJ", "SEMGREP_SCAND_PROVISIONER",
		"SEMGREP_SCAND_SCAN_TIMEOUT", "SEMGREP_SCAND_BOOT_READY_TIMEOUT",
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
	if c.MaxConcurrent != 4 {
		t.Errorf("MaxConcurrent = %d, want 4", c.MaxConcurrent)
	}
	if c.GuestMemMib != 2048 {
		t.Errorf("GuestMemMib = %d, want 2048", c.GuestMemMib)
	}
	if c.GuestVCPUs != 4 {
		t.Errorf("GuestVCPUs = %d, want 4", c.GuestVCPUs)
	}
	if c.GuestOomScoreAdj != 1000 {
		t.Errorf("GuestOomScoreAdj = %d, want 1000", c.GuestOomScoreAdj)
	}
	if c.Provisioner != "copy" {
		t.Errorf("Provisioner = %q, want copy", c.Provisioner)
	}
	if c.ScanTimeout != 60*time.Second {
		t.Errorf("ScanTimeout = %s, want 60s", c.ScanTimeout)
	}
	if c.BootReadyTimeout != 30*time.Second {
		t.Errorf("BootReadyTimeout = %s, want 30s", c.BootReadyTimeout)
	}
	if c.Arch == "" {
		t.Error("Arch should fall back to runtime.GOARCH, got empty")
	}
}

// TestLoadOverrides verifies env vars override the defaults, including the
// duration fields.
func TestLoadOverrides(t *testing.T) {
	t.Setenv("SEMGREP_SCAND_LISTEN_ADDR", ":9000")
	t.Setenv("SEMGREP_SCAND_MAX_CONCURRENT", "2")
	t.Setenv("SEMGREP_SCAND_GUEST_MEM_MIB", "4096")
	t.Setenv("SEMGREP_SCAND_BASE_ROOTFS", "/img/semgrep.ext4")
	t.Setenv("SEMGREP_SCAND_NODE", "node-4")
	t.Setenv("SEMGREP_SCAND_ARCH", "arm64")
	t.Setenv("SEMGREP_SCAND_SCAN_TIMEOUT", "90s")
	t.Setenv("SEMGREP_SCAND_BOOT_READY_TIMEOUT", "15s")

	c, err := Load()
	if err != nil {
		t.Fatalf("Load() error = %v", err)
	}
	if c.ListenAddr != ":9000" {
		t.Errorf("ListenAddr = %q, want :9000", c.ListenAddr)
	}
	if c.MaxConcurrent != 2 {
		t.Errorf("MaxConcurrent = %d, want 2", c.MaxConcurrent)
	}
	if c.GuestMemMib != 4096 {
		t.Errorf("GuestMemMib = %d, want 4096", c.GuestMemMib)
	}
	if c.BaseRootfsPath != "/img/semgrep.ext4" {
		t.Errorf("BaseRootfsPath = %q, want /img/semgrep.ext4", c.BaseRootfsPath)
	}
	if c.Node != "node-4" {
		t.Errorf("Node = %q, want node-4", c.Node)
	}
	if c.Arch != "arm64" {
		t.Errorf("Arch = %q, want arm64", c.Arch)
	}
	if c.ScanTimeout != 90*time.Second {
		t.Errorf("ScanTimeout = %s, want 90s", c.ScanTimeout)
	}
	if c.BootReadyTimeout != 15*time.Second {
		t.Errorf("BootReadyTimeout = %s, want 15s", c.BootReadyTimeout)
	}
}

// TestLoadRejectsMalformedDuration verifies a present-but-invalid duration is a
// load error rather than a silent fallback.
func TestLoadRejectsMalformedDuration(t *testing.T) {
	t.Setenv("SEMGREP_SCAND_SCAN_TIMEOUT", "not-a-duration")
	if _, err := Load(); err == nil {
		t.Fatal("Load() error = nil, want a parse error for a malformed duration")
	}
}
