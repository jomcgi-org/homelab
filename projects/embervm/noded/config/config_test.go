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

// TestPruneStaleInstanceWarmth proves the startup GC reaps orphan per-instance
// warmth (other pods' i/<seg> dirs) while never touching our own segment, bases/,
// or anything outside i/; and that it is a no-op for the legacy DaemonSet.
func TestPruneStaleInstanceWarmth(t *testing.T) {
	const ownUID = "a1b2c3d4-e5f6-4788-9abc-def012345678"
	ownSeg := instanceSegment(ownUID)

	t.Run("brick reaps other segments, keeps own and bases", func(t *testing.T) {
		root := t.TempDir()
		instancesDir := filepath.Join(root, InstanceWarmthSubdir)
		mkdir := func(p string) {
			if err := os.MkdirAll(p, 0o750); err != nil {
				t.Fatal(err)
			}
		}
		mkdir(filepath.Join(instancesDir, ownSeg))
		mkdir(filepath.Join(instancesDir, "deadbeef0000")) // orphan
		mkdir(filepath.Join(instancesDir, "cafef00d1111")) // orphan
		basesDir := filepath.Join(root, "bases", "somekey")
		mkdir(basesDir)

		c := Config{SnapshotRoot: root, SizeClass: "8gi", PodUID: ownUID}
		removed := PruneStaleInstanceWarmth(c, func(seg string, err error) {
			t.Errorf("unexpected removeErr(%q, %v)", seg, err)
		})
		if len(removed) != 2 {
			t.Errorf("removed = %v, want 2 orphans", removed)
		}
		if _, err := os.Stat(filepath.Join(instancesDir, ownSeg)); err != nil {
			t.Errorf("own segment must survive GC: %v", err)
		}
		if _, err := os.Stat(basesDir); err != nil {
			t.Errorf("bases/ must NEVER be touched by GC: %v", err)
		}
		if _, err := os.Stat(filepath.Join(instancesDir, "deadbeef0000")); !os.IsNotExist(err) {
			t.Errorf("orphan segment should be gone, stat err = %v", err)
		}
	})

	t.Run("legacy DS is a no-op", func(t *testing.T) {
		root := t.TempDir()
		// Even if an i/ dir somehow exists, a non-brick (empty SizeClass) never sweeps.
		if err := os.MkdirAll(filepath.Join(root, InstanceWarmthSubdir, "deadbeef0000"), 0o750); err != nil {
			t.Fatal(err)
		}
		c := Config{SnapshotRoot: root, SizeClass: "", PodUID: ownUID}
		if removed := PruneStaleInstanceWarmth(c, nil); removed != nil {
			t.Errorf("legacy DS GC removed %v, want nil", removed)
		}
		if _, err := os.Stat(filepath.Join(root, InstanceWarmthSubdir, "deadbeef0000")); err != nil {
			t.Errorf("non-brick must not touch i/: %v", err)
		}
	})

	t.Run("missing i/ dir is not an error", func(t *testing.T) {
		root := t.TempDir()
		c := Config{SnapshotRoot: root, SizeClass: "8gi", PodUID: ownUID}
		if removed := PruneStaleInstanceWarmth(c, func(seg string, err error) {
			t.Errorf("unexpected removeErr(%q, %v) for missing dir", seg, err)
		}); removed != nil {
			t.Errorf("removed = %v, want nil for missing i/", removed)
		}
	})
}
