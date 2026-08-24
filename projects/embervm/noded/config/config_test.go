package config

import (
	"os"
	"path/filepath"
	"strings"
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

func TestLoadSilenceTimeoutSeconds(t *testing.T) {
	const key = "EMBERVM_NODED_SILENCE_TIMEOUT_SECONDS"
	for _, tc := range []struct {
		name    string
		value   *string
		want    int
		wantErr bool
	}{
		{name: "unset disables", want: 0},
		{name: "zero disables", value: stringPtr("0"), want: 0},
		{name: "armed", value: stringPtr("21600"), want: 21600},
		{name: "garbage", value: stringPtr("six-hours"), wantErr: true},
		{name: "negative", value: stringPtr("-1"), wantErr: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if tc.value == nil {
				if err := os.Unsetenv(key); err != nil {
					t.Fatal(err)
				}
			} else {
				t.Setenv(key, *tc.value)
			}
			c, err := Load()
			if tc.wantErr {
				if err == nil || !strings.Contains(err.Error(), key) {
					t.Fatalf("Load error = %v, want an invalid %s error", err, key)
				}
				return
			}
			if err != nil {
				t.Fatalf("Load: %v", err)
			}
			if c.SilenceTimeoutSeconds != tc.want {
				t.Errorf("SilenceTimeoutSeconds = %d, want %d", c.SilenceTimeoutSeconds, tc.want)
			}
		})
	}
}

func TestLoadWarmRestoreWithVolumeEnv(t *testing.T) {
	const key = "EMBERVM_NODED_WARM_RESTORE_WITH_VOLUME"
	for _, tc := range []struct {
		name  string
		value *string
		want  bool
	}{
		{name: "unset", want: false},
		{name: "true", value: stringPtr("true"), want: true},
		{name: "false", value: stringPtr("false"), want: false},
		{name: "garbage", value: stringPtr("garbage"), want: false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if tc.value == nil {
				if err := os.Unsetenv(key); err != nil {
					t.Fatal(err)
				}
			} else {
				t.Setenv(key, *tc.value)
			}
			c, err := Load()
			if err != nil {
				t.Fatalf("Load: %v", err)
			}
			if c.WarmRestoreWithVolume != tc.want {
				t.Errorf("WarmRestoreWithVolume = %v, want %v", c.WarmRestoreWithVolume, tc.want)
			}
		})
	}
}

func TestLoadStoreCredentialsBothOrNeither(t *testing.T) {
	t.Run("missing access key", func(t *testing.T) {
		t.Setenv("EMBERVM_NODED_STORE_SECRET_ACCESS_KEY", "secret")
		if _, err := Load(); err == nil || !strings.Contains(err.Error(), "EMBERVM_NODED_STORE_ACCESS_KEY_ID") {
			t.Fatalf("Load error = %v, want missing access key", err)
		}
	})
	t.Run("missing secret key", func(t *testing.T) {
		t.Setenv("EMBERVM_NODED_STORE_ACCESS_KEY_ID", "embervm")
		if _, err := Load(); err == nil || !strings.Contains(err.Error(), "EMBERVM_NODED_STORE_SECRET_ACCESS_KEY") {
			t.Fatalf("Load error = %v, want missing secret key", err)
		}
	})
	t.Run("both configured", func(t *testing.T) {
		t.Setenv("EMBERVM_NODED_STORE_ACCESS_KEY_ID", "embervm")
		t.Setenv("EMBERVM_NODED_STORE_SECRET_ACCESS_KEY", "secret")
		cfg, err := Load()
		if err != nil {
			t.Fatal(err)
		}
		if cfg.StoreAccessKeyID != "embervm" || cfg.StoreSecretAccessKey != "secret" {
			t.Fatal("store credentials were not parsed")
		}
	})
}

func stringPtr(value string) *string {
	return &value
}

func TestLoadAdmissionConfig(t *testing.T) {
	t.Setenv("EMBERVM_NODED_ADMISSION_MODEL", "reserved")
	t.Setenv("EMBERVM_NODED_VM_OVERHEAD_MIB", "64")
	c, err := Load()
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if c.AdmissionModel != "reserved" || c.VMOverheadMib != 64 {
		t.Fatalf("admission config = (%q, %d), want (reserved, 64)", c.AdmissionModel, c.VMOverheadMib)
	}
	t.Setenv("EMBERVM_NODED_ADMISSION_MODEL", "invalid")
	if _, err := Load(); err == nil {
		t.Fatal("Load accepted an unknown admission model")
	}
}

func TestLoadActivatorAddress(t *testing.T) {
	t.Setenv("EMBERVM_NODED_ACTIVATOR_ADDR", "127.0.0.1:18081")
	c, err := Load()
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if c.ActivatorAddr != "127.0.0.1:18081" || c.ActivatorPort != 18081 {
		t.Errorf("activator endpoint = %q:%d, want 127.0.0.1:18081", c.ActivatorAddr, c.ActivatorPort)
	}
}

func TestLoadStatefulActivatorPortRange(t *testing.T) {
	t.Setenv("EMBERVM_NODED_STATEFUL_ACTIVATOR_PORT_RANGE", "15400-15409")
	c, err := Load()
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if c.StatefulActivatorPortRange != [2]uint32{15400, 15409} {
		t.Errorf("StatefulActivatorPortRange = %v, want [15400 15409]", c.StatefulActivatorPortRange)
	}
}

func TestLoadStatefulActivatorPortRangeDisabled(t *testing.T) {
	t.Setenv("EMBERVM_NODED_STATEFUL_ACTIVATOR_PORT_RANGE", "0")
	c, err := Load()
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if c.StatefulActivatorPortRange != [2]uint32{} {
		t.Errorf("StatefulActivatorPortRange = %v, want disabled", c.StatefulActivatorPortRange)
	}
}

func TestLoadStatefulActivatorPortRangeEmptyDisables(t *testing.T) {
	t.Setenv("EMBERVM_NODED_STATEFUL_ACTIVATOR_PORT_RANGE", "")
	c, err := Load()
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if c.StatefulActivatorPortRange != [2]uint32{} {
		t.Errorf("StatefulActivatorPortRange = %v, want disabled", c.StatefulActivatorPortRange)
	}
}

func TestLoadGroupActivatorPortRange(t *testing.T) {
	t.Setenv("EMBERVM_NODED_GROUP_ACTIVATOR_PORT_RANGE", "15410-15419")
	c, err := Load()
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if c.GroupActivatorPortRange != [2]uint32{15410, 15419} {
		t.Errorf("GroupActivatorPortRange = %v, want [15410 15419]", c.GroupActivatorPortRange)
	}
}

func TestLoadGroupActivatorPortRangeDisabled(t *testing.T) {
	t.Setenv("EMBERVM_NODED_GROUP_ACTIVATOR_PORT_RANGE", "0")
	c, err := Load()
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if c.GroupActivatorPortRange != [2]uint32{} {
		t.Errorf("GroupActivatorPortRange = %v, want disabled", c.GroupActivatorPortRange)
	}
}

func TestLoadGroupActivatorPortRangeEmptyDisables(t *testing.T) {
	t.Setenv("EMBERVM_NODED_GROUP_ACTIVATOR_PORT_RANGE", "")
	c, err := Load()
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if c.GroupActivatorPortRange != [2]uint32{} {
		t.Errorf("GroupActivatorPortRange = %v, want disabled", c.GroupActivatorPortRange)
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
		"EMBERVM_NODED_DRAIN_TIMEOUT", "EMBERVM_NODED_DIFF_BANKING", "EMBERVM_NODED_DIFF_BANKING_WORKLOADS",
		"EMBERVM_NODED_WARMTH_HEARTBEAT_INTERVAL", "EMBERVM_NODED_WARMTH_STALE_AFTER",
		"EMBERVM_NODED_REAP_UNCLAIMED_WARMTH",
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
	if c.DiffBanking {
		t.Error("DiffBanking should default false")
	}
	if c.WarmthHeartbeatInterval != 30*time.Second {
		t.Errorf("WarmthHeartbeatInterval = %s, want 30s", c.WarmthHeartbeatInterval)
	}
	if c.WarmthStaleAfter != 10*time.Minute {
		t.Errorf("WarmthStaleAfter = %s, want 10m", c.WarmthStaleAfter)
	}
	if c.ReapUnclaimedWarmth {
		t.Error("ReapUnclaimedWarmth should default false")
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
	if c.StoreEncrypt {
		t.Error("StoreEncrypt should default false for the two-phase rollout")
	}
	if c.RequireRestoreCapability {
		t.Error("RequireRestoreCapability should default false for the two-phase rollout")
	}
}

func TestLoadDiffBankingOverride(t *testing.T) {
	t.Setenv("EMBERVM_NODED_DIFF_BANKING", "true")
	t.Setenv("EMBERVM_NODED_DIFF_BANKING_WORKLOADS", "sandbox-session,another-session")
	c, err := Load()
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if !c.DiffBanking {
		t.Error("DiffBanking should be true when EMBERVM_NODED_DIFF_BANKING=true")
	}
	if got, want := strings.Join(c.DiffBankingWorkloads, ","), "sandbox-session,another-session"; got != want {
		t.Errorf("DiffBankingWorkloads = %q, want %q", got, want)
	}
}

func TestLoadArtifactEncryptionOverrides(t *testing.T) {
	t.Setenv("EMBERVM_NODED_STORE_ENCRYPT", "true")
	t.Setenv("EMBERVM_NODED_REQUIRE_RESTORE_CAPABILITY", "true")
	c, err := Load()
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if !c.StoreEncrypt {
		t.Error("StoreEncrypt should parse EMBERVM_NODED_STORE_ENCRYPT=true")
	}
	if !c.RequireRestoreCapability {
		t.Error("RequireRestoreCapability should parse EMBERVM_NODED_REQUIRE_RESTORE_CAPABILITY=true")
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

// TestLoadWarmthRootDerivation proves the per-instance warmth root (brick-capacity):
// a brick (SizeClass + PodUID both set) nests warmth under SnapshotRoot/i/<short-uid>
// (the SHORT segment, kept under SUN_LEN); every other case keeps warmth flat at
// SnapshotRoot so the legacy DaemonSet repaths nothing.
func TestLoadWarmthRootDerivation(t *testing.T) {
	root := "/scratch/embervm-noded/snapshots"
	// A realistic k8s pod UID (RFC 4122 v4); the segment is its first 10 hex chars
	// with hyphens stripped: "a1b2c3d4e5".
	const uid = "a1b2c3d4-e5f6-4788-9abc-def012345678"
	cases := []struct {
		name      string
		snapshot  string
		sizeClass string
		podUID    string
		want      string
	}{
		{"brick nests per short uid", root, "8gi", uid, root + "/i/a1b2c3d4e5"},
		{"legacy DS stays flat (no size class)", root, "", uid, root},
		{"size class but no pod_uid stays flat", root, "8gi", "", root},
		{"no snapshot root yields empty", "", "8gi", uid, ""},
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

// TestInstanceSegmentDeterministicAndDistinct proves the per-instance segment is
// deterministic (same UID -> same segment, so a restarted daemon reattaches to
// its own warmth) and distinct across co-located UIDs (no clobber).
func TestInstanceSegmentDeterministicAndDistinct(t *testing.T) {
	const a = "a1b2c3d4-e5f6-4788-9abc-def012345678"
	const b = "f9e8d7c6-b5a4-4300-8fed-cba987654321"
	if instanceSegment(a) != instanceSegment(a) {
		t.Error("instanceSegment not deterministic")
	}
	if instanceSegment(a) == instanceSegment(b) {
		t.Errorf("distinct UIDs collided: %q", instanceSegment(a))
	}
	if got := instanceSegment(a); got != "a1b2c3d4e5" {
		t.Errorf("instanceSegment(%q) = %q, want a1b2c3d4e5", a, got)
	}
	// A UID shorter than the cap is used whole (no panic, no padding).
	if got := instanceSegment("short"); got != "short" {
		t.Errorf("instanceSegment(short) = %q, want short", got)
	}
}

// TestWorstCaseSocketPathUnderSunLen is the regression guard for the bug this PR
// fixes: on a brick, the LONGEST firecracker unix socket path (a per-op
// thread-<16hex> bundle dir under the per-instance warmth root, holding
// restore.sock) MUST stay under the 108-byte sockaddr_un SUN_LEN limit, or every
// VM operation fails with "path must be shorter than SUN_LEN". We reconstruct the
// exact worst-case path the driver builds and assert it is comfortably under the
// limit.
func TestWorstCaseSocketPathUnderSunLen(t *testing.T) {
	// The real production NVMe scratch snapshot root (the longest root in the
	// fleet), a realistic pod UID, and the longest per-op thread id (thread- +
	// 16 hex chars = 8 random bytes) with the longest socket basename.
	const snapshotRoot = "/var/lib/embervm/scratch/embervm-noded/snapshots"
	const podUID = "a1b2c3d4-e5f6-4788-9abc-def012345678"
	const threadDir = "thread-0123456789abcdef" // driver: "thread-" + 16 hex
	const longestSock = "restore.sock"          // > api.sock, vsock.sock

	t.Setenv("EMBERVM_NODED_SNAPSHOT_ROOT", snapshotRoot)
	t.Setenv("EMBERVM_NODED_SIZE_CLASS", "16gi")
	t.Setenv("EMBERVM_POD_UID", podUID)
	c, err := Load()
	if err != nil {
		t.Fatalf("Load: %v", err)
	}

	worst := filepath.Join(c.WarmthRoot, threadDir, longestSock)
	// SUN_LEN is 108 on Linux; a NUL terminator eats one byte, so the usable path
	// is <= 107. We assert a hard margin (< 100) so a future root/threadid tweak
	// cannot silently creep back over the cliff.
	if len(worst) >= 108 {
		t.Fatalf("worst-case socket path OVERFLOWS SUN_LEN: %d bytes: %q", len(worst), worst)
	}
	if len(worst) >= 100 {
		t.Errorf("worst-case socket path %d bytes (want < 100 for margin): %q", len(worst), worst)
	}
	t.Logf("worst-case brick socket path is %d bytes: %q", len(worst), worst)
}

// TestPruneStaleInstanceWarmth proves startup GC uses checked liveness claims,
// never a foreign path pattern alone, to decide which brick warmth to reap.
func TestPruneStaleInstanceWarmth(t *testing.T) {
	const ownUID = "a1b2c3d4-e5f6-4788-9abc-def012345678"
	ownSeg := instanceSegment(ownUID)
	now := time.Date(2026, 8, 15, 12, 0, 0, 0, time.UTC)

	setup := func(t *testing.T) (string, Config) {
		t.Helper()
		root := t.TempDir()
		c := Config{
			SnapshotRoot:     root,
			SizeClass:        "8gi",
			PodUID:           ownUID,
			WarmthStaleAfter: 10 * time.Minute,
		}
		return root, c
	}
	mkdirSegment := func(t *testing.T, root, segment string) string {
		t.Helper()
		path := filepath.Join(root, InstanceWarmthSubdir, segment)
		if err := os.MkdirAll(path, 0o750); err != nil {
			t.Fatal(err)
		}
		return path
	}
	writeAlive := func(t *testing.T, segmentPath string, heartbeat time.Time) {
		t.Helper()
		path := filepath.Join(segmentPath, ".alive")
		if err := os.WriteFile(path, []byte("foreign-pod-uid\n"+heartbeat.Format(time.RFC3339)+"\n"), 0o600); err != nil {
			t.Fatal(err)
		}
		if err := os.Chtimes(path, heartbeat, heartbeat); err != nil {
			t.Fatal(err)
		}
	}

	t.Run("fresh sibling is live", func(t *testing.T) {
		root, c := setup(t)
		segment := "49c0376d0900"
		segmentPath := mkdirSegment(t, root, segment)
		writeAlive(t, segmentPath, now.Add(-time.Minute))

		result := PruneStaleInstanceWarmth(c, now, func(seg string, err error) {
			t.Errorf("unexpected removeErr(%q, %v)", seg, err)
		})
		if len(result.SkippedLive) != 1 || result.SkippedLive[0] != segment {
			t.Errorf("SkippedLive = %v, want [%s]", result.SkippedLive, segment)
		}
		if len(result.Removed) != 0 {
			t.Errorf("Removed = %v, want none", result.Removed)
		}
		if _, err := os.Stat(segmentPath); err != nil {
			t.Errorf("fresh sibling warmth must survive: %v", err)
		}
	})

	t.Run("stale sibling is removed", func(t *testing.T) {
		root, c := setup(t)
		segment := "deadbeef0000"
		segmentPath := mkdirSegment(t, root, segment)
		writeAlive(t, segmentPath, now.Add(-11*time.Minute))

		result := PruneStaleInstanceWarmth(c, now, nil)
		if len(result.Removed) != 1 || result.Removed[0] != segment {
			t.Errorf("Removed = %v, want [%s]", result.Removed, segment)
		}
		if _, err := os.Stat(segmentPath); !os.IsNotExist(err) {
			t.Errorf("stale sibling warmth should be gone, stat err = %v", err)
		}
	})

	t.Run("unclaimed sibling is skipped by default", func(t *testing.T) {
		root, c := setup(t)
		segment := "cafef00d1111"
		segmentPath := mkdirSegment(t, root, segment)

		result := PruneStaleInstanceWarmth(c, now, nil)
		if len(result.SkippedUnclaimed) != 1 || result.SkippedUnclaimed[0] != segment {
			t.Errorf("SkippedUnclaimed = %v, want [%s]", result.SkippedUnclaimed, segment)
		}
		if _, err := os.Stat(segmentPath); err != nil {
			t.Errorf("unclaimed warmth must survive while guard is off: %v", err)
		}
	})

	t.Run("unclaimed sibling is removed when enabled", func(t *testing.T) {
		root, c := setup(t)
		c.ReapUnclaimedWarmth = true
		segment := "cafef00d1111"
		segmentPath := mkdirSegment(t, root, segment)

		result := PruneStaleInstanceWarmth(c, now, nil)
		if len(result.Removed) != 1 || result.Removed[0] != segment {
			t.Errorf("Removed = %v, want [%s]", result.Removed, segment)
		}
		if _, err := os.Stat(segmentPath); !os.IsNotExist(err) {
			t.Errorf("enabled guard should reap unclaimed warmth, stat err = %v", err)
		}
	})

	t.Run("own stale claim and shared bases are never touched", func(t *testing.T) {
		root, c := setup(t)
		ownPath := mkdirSegment(t, root, ownSeg)
		writeAlive(t, ownPath, now.Add(-24*time.Hour))
		basesDir := filepath.Join(root, "bases", "somekey")
		if err := os.MkdirAll(basesDir, 0o750); err != nil {
			t.Fatal(err)
		}

		result := PruneStaleInstanceWarmth(c, now, nil)
		if len(result.Removed) != 0 {
			t.Errorf("Removed = %v, want none", result.Removed)
		}
		if _, err := os.Stat(ownPath); err != nil {
			t.Errorf("own segment must survive GC: %v", err)
		}
		if _, err := os.Stat(basesDir); err != nil {
			t.Errorf("bases/ must never be touched by GC: %v", err)
		}
	})

	t.Run("legacy DS is a no-op", func(t *testing.T) {
		root := t.TempDir()
		// Even if an i/ dir somehow exists, a non-brick (empty SizeClass) never sweeps.
		if err := os.MkdirAll(filepath.Join(root, InstanceWarmthSubdir, "deadbeef0000"), 0o750); err != nil {
			t.Fatal(err)
		}
		c := Config{SnapshotRoot: root, SizeClass: "", PodUID: ownUID}
		if result := PruneStaleInstanceWarmth(c, now, nil); len(result.Removed) != 0 {
			t.Errorf("legacy DS GC removed %v, want none", result.Removed)
		}
		if _, err := os.Stat(filepath.Join(root, InstanceWarmthSubdir, "deadbeef0000")); err != nil {
			t.Errorf("non-brick must not touch i/: %v", err)
		}
	})

	t.Run("missing i/ dir is not an error", func(t *testing.T) {
		_, c := setup(t)
		result := PruneStaleInstanceWarmth(c, now, func(seg string, err error) {
			t.Errorf("unexpected removeErr(%q, %v) for missing dir", seg, err)
		})
		if len(result.Removed) != 0 || len(result.SkippedLive) != 0 || len(result.SkippedUnclaimed) != 0 {
			t.Errorf("result = %+v, want empty for missing i/", result)
		}
	})
}

func TestWriteInstanceHeartbeat(t *testing.T) {
	root := t.TempDir()
	c := Config{
		SnapshotRoot: root,
		SizeClass:    "8gi",
		PodUID:       "a1b2c3d4-e5f6-4788-9abc-def012345678",
	}
	first := time.Date(2026, 8, 15, 12, 0, 0, 0, time.UTC)
	second := first.Add(30 * time.Second)
	path := filepath.Join(root, InstanceWarmthSubdir, instanceSegment(c.PodUID), ".alive")

	if err := WriteInstanceHeartbeat(c, first); err != nil {
		t.Fatal(err)
	}
	firstInfo, err := os.Stat(path)
	if err != nil {
		t.Fatalf("heartbeat was not created: %v", err)
	}
	contents, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	wantContents := c.PodUID + "\n" + first.Format(time.RFC3339) + "\n"
	if string(contents) != wantContents {
		t.Errorf("heartbeat contents = %q, want %q", contents, wantContents)
	}

	if err := WriteInstanceHeartbeat(c, second); err != nil {
		t.Fatal(err)
	}
	secondInfo, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if !secondInfo.ModTime().After(firstInfo.ModTime()) {
		t.Errorf("second mtime = %s, want after %s", secondInfo.ModTime(), firstInfo.ModTime())
	}
}

func TestLoadRejectsWarmthStaleAfterAtOrBelowHeartbeatInterval(t *testing.T) {
	t.Setenv("EMBERVM_NODED_WARMTH_HEARTBEAT_INTERVAL", "30s")
	t.Setenv("EMBERVM_NODED_WARMTH_STALE_AFTER", "30s")
	if _, err := Load(); err == nil {
		t.Fatal("Load should reject warmth staleAfter <= heartbeat interval")
	}
}

func TestLoadWarmthHeartbeatConfig(t *testing.T) {
	t.Setenv("EMBERVM_NODED_WARMTH_HEARTBEAT_INTERVAL", "45s")
	t.Setenv("EMBERVM_NODED_WARMTH_STALE_AFTER", "15m")
	t.Setenv("EMBERVM_NODED_REAP_UNCLAIMED_WARMTH", "1")
	c, err := Load()
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if c.WarmthHeartbeatInterval != 45*time.Second {
		t.Errorf("WarmthHeartbeatInterval = %s, want 45s", c.WarmthHeartbeatInterval)
	}
	if c.WarmthStaleAfter != 15*time.Minute {
		t.Errorf("WarmthStaleAfter = %s, want 15m", c.WarmthStaleAfter)
	}
	if !c.ReapUnclaimedWarmth {
		t.Error("ReapUnclaimedWarmth = false, want true for exact value 1")
	}

	t.Setenv("EMBERVM_NODED_REAP_UNCLAIMED_WARMTH", "true")
	c, err = Load()
	if err != nil {
		t.Fatalf("Load with non-1 transition guard: %v", err)
	}
	if c.ReapUnclaimedWarmth {
		t.Error("ReapUnclaimedWarmth = true for value true, want exact-value-1 parsing")
	}
}
