package config

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestDetectCPUVendorFromFixture(t *testing.T) {
	tests := []struct {
		name     string
		cpuinfo  string
		wantVend string
	}{
		{
			name:     "intel",
			cpuinfo:  "processor\t: 0\nvendor_id\t: GenuineIntel\ncpu family\t: 6\n",
			wantVend: "intel",
		},
		{
			name:     "amd",
			cpuinfo:  "processor\t: 0\nvendor_id\t: AuthenticAMD\ncpu family\t: 23\n",
			wantVend: "amd",
		},
		{
			name:     "unrecognised vendor lowercased and sanitised",
			cpuinfo:  "processor\t: 0\nvendor_id\t: Some Other_Vendor!\n",
			wantVend: "someothervendor",
		},
		{
			name:     "missing vendor_id line",
			cpuinfo:  "processor\t: 0\ncpu family\t: 6\n",
			wantVend: "",
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			dir := t.TempDir()
			path := filepath.Join(dir, "cpuinfo")
			if err := os.WriteFile(path, []byte(tt.cpuinfo), 0o600); err != nil {
				t.Fatal(err)
			}
			got := detectCPUVendorFrom(path)
			if got != tt.wantVend {
				t.Errorf("detectCPUVendorFrom(%q) = %q, want %q", tt.name, got, tt.wantVend)
			}
		})
	}
}

func TestDetectCPUVendorMissingFile(t *testing.T) {
	if got := detectCPUVendorFrom(filepath.Join(t.TempDir(), "does-not-exist")); got != "" {
		t.Errorf("detectCPUVendorFrom(missing) = %q, want empty", got)
	}
}

// TestDefaultCPUTemplatePerVendor unit-tests defaultCPUTemplate directly: a
// known vendor resolves its conservative fleet default; an unknown/empty
// vendor resolves "" (a template can never be more specific than an unknown
// vendor).
func TestDefaultCPUTemplatePerVendor(t *testing.T) {
	cases := []struct {
		vendor string
		want   string
	}{
		{"amd", "amd-default"},
		{"intel", "t2-conservative"},
		{"", ""},
		{"unknownvendor", ""},
	}
	for _, tc := range cases {
		if got := defaultCPUTemplate(tc.vendor); got != tc.want {
			t.Errorf("defaultCPUTemplate(%q) = %q, want %q", tc.vendor, got, tc.want)
		}
	}
}

func TestLoadCpuVendorEnvOverride(t *testing.T) {
	t.Setenv("EMBERVM_NODED_CPU_VENDOR", "intel")
	c, err := Load()
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if c.CpuVendor != "intel" {
		t.Errorf("CpuVendor = %q, want intel (env override)", c.CpuVendor)
	}
}

// TestLoadCpuTemplateDefaultsPerVendor proves CpuTemplate resolves to the
// conservative per-vendor default (PR-E) when EMBERVM_NODED_CPU_TEMPLATE is
// unset and CpuVendor is known, for both fleet vendors.
func TestLoadCpuTemplateDefaultsPerVendor(t *testing.T) {
	cases := []struct {
		vendor       string
		wantTemplate string
	}{
		{"amd", "amd-default"},
		{"intel", "t2-conservative"},
	}
	for _, tc := range cases {
		t.Run(tc.vendor, func(t *testing.T) {
			t.Setenv("EMBERVM_NODED_CPU_VENDOR", tc.vendor)
			c, err := Load()
			if err != nil {
				t.Fatalf("Load: %v", err)
			}
			if c.CpuTemplate != tc.wantTemplate {
				t.Errorf("CpuTemplate = %q, want %q (default for %s)", c.CpuTemplate, tc.wantTemplate, tc.vendor)
			}
		})
	}
}

// TestLoadCpuTemplateEnvOverride proves EMBERVM_NODED_CPU_TEMPLATE overrides
// the per-vendor default.
func TestLoadCpuTemplateEnvOverride(t *testing.T) {
	t.Setenv("EMBERVM_NODED_CPU_VENDOR", "intel")
	t.Setenv("EMBERVM_NODED_CPU_TEMPLATE", "t2s-custom")
	c, err := Load()
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if c.CpuTemplate != "t2s-custom" {
		t.Errorf("CpuTemplate = %q, want t2s-custom (env override)", c.CpuTemplate)
	}
}

func TestLoadDefaults(t *testing.T) {
	// t.Setenv clears everything else via a clean slate per key; unset the ones
	// that would leak from the runner by setting them empty.
	for _, k := range []string{
		"EMBERVM_NODED_LISTEN_ADDR", "EMBERVM_NODED_HEALTH_ADDR", "EMBERVM_NODED_NODE",
		"NODE_NAME", "EMBERVM_NODED_ARCH", "EMBERVM_NODED_CPU_VENDOR", "EMBERVM_NODED_BEARER_TOKEN",
		"EMBERVM_NODED_MAX_LIVE_VMS", "EMBERVM_NODED_IMAGES", "EMBERVM_NODED_IMAGES_FILE",
		"EMBERVM_NODED_BOOT_READY_TIMEOUT", "EMBERVM_NODED_RESTORE_READY_TIMEOUT",
		"EMBERVM_NODED_DRAIN_TIMEOUT",
	} {
		t.Setenv(k, "")
	}

	c, err := Load()
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if c.ListenAddr != ":9090" {
		t.Errorf("ListenAddr = %q, want :9090", c.ListenAddr)
	}
	if c.HealthAddr != ":8080" {
		t.Errorf("HealthAddr = %q, want :8080", c.HealthAddr)
	}
	if c.MaxLiveVMs != 8 {
		t.Errorf("MaxLiveVMs = %d, want 8", c.MaxLiveVMs)
	}
	if c.BinPath != "/opt/fc/firecracker" {
		t.Errorf("BinPath = %q, want /opt/fc/firecracker", c.BinPath)
	}
	if c.KernelImagePath != "/opt/fc/vmlinux.container" {
		t.Errorf("KernelImagePath = %q, want /opt/fc/vmlinux.container", c.KernelImagePath)
	}
	if c.GuestOomScoreAdj != 1000 {
		t.Errorf("GuestOomScoreAdj = %d, want 1000", c.GuestOomScoreAdj)
	}
	if c.BootReadyTimeout != 60*time.Second {
		t.Errorf("BootReadyTimeout = %v, want 60s", c.BootReadyTimeout)
	}
	if c.RestoreReadyTimeout != 2*time.Second {
		t.Errorf("RestoreReadyTimeout = %v, want 2s", c.RestoreReadyTimeout)
	}
	if c.Arch == "" {
		t.Error("Arch should default to runtime.GOARCH, got empty")
	}
	if len(c.Images) != 0 {
		t.Errorf("Images = %v, want empty", c.Images)
	}
	if c.ServingBridge != "embervm-serv0" {
		t.Errorf("ServingBridge = %q, want embervm-serv0", c.ServingBridge)
	}
	// Default serving CIDR is in the 172.16/12 space, NOT the 10.0.0.0/8 pod range.
	if c.ServingSubnetCIDR != "172.31.0.0/24" {
		t.Errorf("ServingSubnetCIDR = %q, want 172.31.0.0/24", c.ServingSubnetCIDR)
	}
	if c.ServingProbeInterval != 5*time.Second {
		t.Errorf("ServingProbeInterval = %v, want 5s", c.ServingProbeInterval)
	}
	if c.ServingUnhealthyThreshold != 3 {
		t.Errorf("ServingUnhealthyThreshold = %d, want 3", c.ServingUnhealthyThreshold)
	}
	// Default composite supernet is 10.101.0.0/16 (distinct from the serving space).
	if c.CompositeSupernet != "10.101.0.0/16" {
		t.Errorf("CompositeSupernet = %q, want 10.101.0.0/16", c.CompositeSupernet)
	}
	if c.GroupProbeInterval != 5*time.Second {
		t.Errorf("GroupProbeInterval = %v, want 5s", c.GroupProbeInterval)
	}
	if c.GroupUnhealthyThreshold != 3 {
		t.Errorf("GroupUnhealthyThreshold = %d, want 3", c.GroupUnhealthyThreshold)
	}
	if c.RequireBlessing {
		t.Error("RequireBlessing should default false so a rollout can land the control-plane side first")
	}
}

// TestLoadRequireBlessingOverride proves EMBERVM_NODED_REQUIRE_BLESSING is
// parsed as a bool (R7, ADR embervm/011): the chart flips this true in the
// same version the control plane starts blessing.
func TestLoadRequireBlessingOverride(t *testing.T) {
	t.Setenv("EMBERVM_NODED_REQUIRE_BLESSING", "true")
	c, err := Load()
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if !c.RequireBlessing {
		t.Error("RequireBlessing should be true when EMBERVM_NODED_REQUIRE_BLESSING=true")
	}
}

func TestLoadCompositeSupernetOverride(t *testing.T) {
	t.Setenv("EMBERVM_NODED_COMPOSITE_SUPERNET", "10.150.0.0/16")
	t.Setenv("EMBERVM_NODED_GROUP_PROBE_INTERVAL", "7s")
	t.Setenv("EMBERVM_NODED_GROUP_UNHEALTHY_THRESHOLD", "4")
	c, err := Load()
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if c.CompositeSupernet != "10.150.0.0/16" {
		t.Errorf("CompositeSupernet = %q", c.CompositeSupernet)
	}
	if c.GroupProbeInterval != 7*time.Second {
		t.Errorf("GroupProbeInterval = %v", c.GroupProbeInterval)
	}
	if c.GroupUnhealthyThreshold != 4 {
		t.Errorf("GroupUnhealthyThreshold = %d", c.GroupUnhealthyThreshold)
	}
}

func TestLoadServingOverrides(t *testing.T) {
	t.Setenv("EMBERVM_NODED_SERVING_BRIDGE", "br-test")
	t.Setenv("EMBERVM_NODED_SERVING_SUBNET_CIDR", "172.20.5.0/24")
	t.Setenv("EMBERVM_NODED_SERVING_PROBE_INTERVAL", "10s")
	t.Setenv("EMBERVM_NODED_SERVING_UNHEALTHY_THRESHOLD", "5")
	c, err := Load()
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if c.ServingBridge != "br-test" {
		t.Errorf("ServingBridge = %q", c.ServingBridge)
	}
	if c.ServingSubnetCIDR != "172.20.5.0/24" {
		t.Errorf("ServingSubnetCIDR = %q", c.ServingSubnetCIDR)
	}
	if c.ServingProbeInterval != 10*time.Second {
		t.Errorf("ServingProbeInterval = %v", c.ServingProbeInterval)
	}
	if c.ServingUnhealthyThreshold != 5 {
		t.Errorf("ServingUnhealthyThreshold = %d", c.ServingUnhealthyThreshold)
	}
}

// TestLoadImagesAlwaysEmpty proves the artifact-decoupling Phase 2 retirement of
// the EMBERVM_NODED_IMAGES env parse: even when the (now-ignored) env is set, the
// resolved Images table is empty. Workload identity is PUSHED over SyncRegistry,
// not parsed here.
func TestLoadImagesAlwaysEmpty(t *testing.T) {
	t.Setenv("EMBERVM_NODED_IMAGES", `{"ghcr.io/x/echo:1":{"rootfsPath":"/x"}}`)
	t.Setenv("EMBERVM_NODED_IMAGES_FILE", "")
	c, err := Load()
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if len(c.Images) != 0 {
		t.Errorf("Images = %v, want empty (env parse retired)", c.Images)
	}
}

// TestLoadRegistryCacheDerivedFromNvmeRoot proves the registry cache path is
// derived alongside SnapshotRoot: SnapshotRoot is <nvmeRoot>/embervm-noded/
// snapshots, so the cache lands at <nvmeRoot>/embervm-noded/registry.json.
func TestLoadRegistryCacheDerivedFromNvmeRoot(t *testing.T) {
	t.Setenv("EMBERVM_NODED_REGISTRY_CACHE", "")
	t.Setenv("EMBERVM_NODED_SNAPSHOT_ROOT", "/disks/nvme-02/embervm-noded/snapshots")
	c, err := Load()
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if want := "/disks/nvme-02/embervm-noded/registry.json"; c.RegistryCachePath != want {
		t.Errorf("RegistryCachePath = %q, want %q", c.RegistryCachePath, want)
	}
}

// TestLoadRegistryCacheOverride proves EMBERVM_NODED_REGISTRY_CACHE wins over the
// derived path (the test/out-of-tree override).
func TestLoadRegistryCacheOverride(t *testing.T) {
	t.Setenv("EMBERVM_NODED_REGISTRY_CACHE", "/tmp/custom/registry.json")
	t.Setenv("EMBERVM_NODED_SNAPSHOT_ROOT", "/disks/nvme-02/embervm-noded/snapshots")
	c, err := Load()
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if c.RegistryCachePath != "/tmp/custom/registry.json" {
		t.Errorf("RegistryCachePath = %q, want the override", c.RegistryCachePath)
	}
}

// TestLoadRegistryCacheDisabledWithoutNvmeRoot proves that with neither the
// override nor a SnapshotRoot set, persistence is disabled (empty path).
func TestLoadRegistryCacheDisabledWithoutNvmeRoot(t *testing.T) {
	t.Setenv("EMBERVM_NODED_REGISTRY_CACHE", "")
	t.Setenv("EMBERVM_NODED_SNAPSHOT_ROOT", "")
	c, err := Load()
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if c.RegistryCachePath != "" {
		t.Errorf("RegistryCachePath = %q, want empty (no NVMe root)", c.RegistryCachePath)
	}
}

func TestLoadRejectsBadDuration(t *testing.T) {
	t.Setenv("EMBERVM_NODED_DRAIN_TIMEOUT", "not-a-duration")
	if _, err := Load(); err == nil {
		t.Error("Load should reject a malformed duration")
	}
}

// TestLoadWarmthRootDerivation proves the per-instance warmth root (brick-capacity
// PR-2.5): a brick (SizeClass + PodUID both set) nests warmth under
// SnapshotRoot/instances/<pod_uid>; every other case keeps warmth flat at
// SnapshotRoot so the legacy DaemonSet repaths nothing.
func TestLoadWarmthRootDerivation(t *testing.T) {
	root := "/scratch/embervm-noded/snapshots"
	cases := []struct {
		name      string
		snapshot  string
		sizeClass string
		podUID    string
		want      string
	}{
		{"brick nests per pod_uid", root, "8gi", "pod-123", root + "/instances/pod-123"},
		{"legacy DS stays flat (no size class)", root, "", "pod-123", root},
		{"size class but no pod_uid stays flat", root, "8gi", "", root},
		{"no snapshot root yields empty", "", "8gi", "pod-123", ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv("EMBERVM_NODED_SNAPSHOT_ROOT", tc.snapshot)
			t.Setenv("EMBERVM_NODED_SIZE_CLASS", tc.sizeClass)
			t.Setenv("EMBERVM_POD_UID", tc.podUID)
			c, err := Load()
			if err != nil {
				t.Fatalf("Load: %v", err)
			}
			if c.WarmthRoot != tc.want {
				t.Errorf("WarmthRoot = %q, want %q", c.WarmthRoot, tc.want)
			}
			// Bases stay node-shared on SnapshotRoot regardless.
			if c.SnapshotRoot != tc.snapshot {
				t.Errorf("SnapshotRoot = %q, want %q (bases must not move)", c.SnapshotRoot, tc.snapshot)
			}
		})
	}
}
