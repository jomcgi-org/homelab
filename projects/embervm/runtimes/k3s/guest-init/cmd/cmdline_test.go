package main

import (
	"encoding/base64"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

// discardLogger returns a logger that drops all output, for tests that only care
// about the env side effects of a helper, not its logs.
func discardLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

func TestMmdsEnvFromCmdline(t *testing.T) {
	enc := func(s string) string { return base64.RawURLEncoding.EncodeToString([]byte(s)) }
	cmdline := "console=ttyS0 " +
		"ember.env.EMBER_GROUP_ROLE=" + enc("server") + " " +
		"ember.env.EMBER_GROUP_SECRET=" + enc("s3cr3t") + " " +
		"ember.env.EMBER_PEER_SERVER=" + enc("10.101.0.10") + " " +
		"ember.env.BAD-KEY=" + enc("dropped") + " " + // invalid key, skipped
		"ember.env.EMBER_BADB64=%%%notb64%%% " + // bad base64, skipped
		"ember.serving_port=6443"
	got := mmdsEnvFromCmdline(cmdline)
	want := map[string]string{
		"EMBER_GROUP_ROLE":   "server",
		"EMBER_GROUP_SECRET": "s3cr3t",
		"EMBER_PEER_SERVER":  "10.101.0.10",
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("mmds env = %v, want %v", got, want)
	}
}

func TestValueFromCmdline(t *testing.T) {
	cmdline := "a=1 ember.serving_port=6443 b ember.serving_port=10250"
	// last occurrence wins, matching the kernel
	if got := valueFromCmdline(cmdline, servingPortCmdlineKey); got != "10250" {
		t.Fatalf("serving port = %q, want 10250 (last wins)", got)
	}
	if got := valueFromCmdline(cmdline, "missing"); got != "" {
		t.Fatalf("missing key should be empty, got %q", got)
	}
}

func TestSetMmdsEnvFromFixtureFile(t *testing.T) {
	enc := func(s string) string { return base64.RawURLEncoding.EncodeToString([]byte(s)) }
	dir := t.TempDir()
	f := filepath.Join(dir, "cmdline")
	if err := os.WriteFile(f, []byte("ember.env.EMBER_GROUP_ROLE="+enc("agent")), 0o600); err != nil {
		t.Fatal(err)
	}
	// Redirect the package's cmdline path at the fixture and restore after.
	orig := procCmdlinePath
	procCmdlinePath = f
	defer func() { procCmdlinePath = orig }()
	t.Setenv("EMBER_GROUP_ROLE", "") // ensure a clean slate

	setMmdsEnv(discardLogger())
	if got := os.Getenv("EMBER_GROUP_ROLE"); got != "agent" {
		t.Fatalf("EMBER_GROUP_ROLE = %q, want agent", got)
	}
}

func TestStatefulVolumeFromCmdline(t *testing.T) {
	dir := t.TempDir()
	orig := procCmdlinePath
	defer func() { procCmdlinePath = orig }()

	// Base build: no volume boot-arg -> ("", "").
	base := filepath.Join(dir, "base")
	if err := os.WriteFile(base, []byte("console=ttyS0"), 0o600); err != nil {
		t.Fatal(err)
	}
	procCmdlinePath = base
	if dev, mp := statefulVolumeFromCmdline(discardLogger()); dev != "" || mp != "" {
		t.Fatalf("base build should yield empty, got dev=%q mount=%q", dev, mp)
	}

	// Stateful cold boot: both tokens present.
	cold := filepath.Join(dir, "cold")
	if err := os.WriteFile(cold, []byte("ember.volume_dev=/dev/vdc ember.volume_mount=/var/lib/rancher/k3s"), 0o600); err != nil {
		t.Fatal(err)
	}
	procCmdlinePath = cold
	dev, mp := statefulVolumeFromCmdline(discardLogger())
	if dev != "/dev/vdc" || mp != "/var/lib/rancher/k3s" {
		t.Fatalf("cold boot: got dev=%q mount=%q, want /dev/vdc + /var/lib/rancher/k3s", dev, mp)
	}

	// Malformed: dev without mount defaults to the k3s data dir.
	mal := filepath.Join(dir, "mal")
	if err := os.WriteFile(mal, []byte("ember.volume_dev=/dev/vdc"), 0o600); err != nil {
		t.Fatal(err)
	}
	procCmdlinePath = mal
	if dev, mp := statefulVolumeFromCmdline(discardLogger()); dev != "/dev/vdc" || mp != k3sDataDir {
		t.Fatalf("malformed: got dev=%q mount=%q, want /dev/vdc + %s", dev, mp, k3sDataDir)
	}
}

func TestClassifyBoot(t *testing.T) {
	cases := []struct {
		name string
		dev  string
		env  map[string]string
		want bootClass
	}{
		{
			name: "no volume and no facts is a base build",
			dev:  "",
			env:  map[string]string{},
			want: bootBaseBuild,
		},
		{
			name: "a volume boot-arg is the stateful lane",
			dev:  "/dev/vdc",
			env:  map[string]string{},
			want: bootStateful,
		},
		{
			// The regression this fix exists for: an R5 composite member carries
			// EMBER_GROUP_* facts but NO volume (warmth-only), and MUST run k3s. The
			// old volume-only discriminator classified it as a base build, so k3s
			// never started and the member's health-gate never passed.
			name: "composite member (facts, no volume) runs k3s, not a base build",
			dev:  "",
			env: map[string]string{
				"EMBER_GROUP_MEMBER": "server",
				"EMBER_GROUP_ROLE":   "server",
				"EMBER_GROUP_IP":     "10.101.0.10",
				"EMBER_GROUP_SECRET": "s3cr3t",
			},
			want: bootComposite,
		},
		{
			name: "an agent member is also composite",
			dev:  "",
			env: map[string]string{
				"EMBER_GROUP_MEMBER": "agent-0",
				"EMBER_GROUP_ROLE":   "agent",
			},
			want: bootComposite,
		},
		{
			// A volume takes precedence: the stateful lane is volume-backed even if
			// group facts were somehow also present (they are not, in practice).
			name: "a volume wins over facts (stateful, not composite)",
			dev:  "/dev/vdc",
			env:  map[string]string{"EMBER_GROUP_MEMBER": "server"},
			want: bootStateful,
		},
		{
			// A composite member with an empty role still classifies composite: the
			// member-name marker (not the role) is the discriminator, because the CR
			// role can be empty and k3sArgv then defaults to server.
			name: "member name present but role empty is still composite",
			dev:  "",
			env:  map[string]string{"EMBER_GROUP_MEMBER": "server", "EMBER_GROUP_ROLE": ""},
			want: bootComposite,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := classifyBoot(tc.dev, envFunc(tc.env)); got != tc.want {
				t.Fatalf("classifyBoot(%q, %v) = %d, want %d", tc.dev, tc.env, got, tc.want)
			}
		})
	}
}

func TestIsValidEnvKeyName(t *testing.T) {
	cases := map[string]bool{
		"EMBER_GROUP_ROLE":   true,
		"EMBER_PEER_AGENT_0": true,
		"lower_ok":           true,
		"":                   false,
		"has-dash":           false,
		"has space":          false,
		"has.dot":            false,
	}
	for k, want := range cases {
		if got := isValidEnvKeyName(k); got != want {
			t.Fatalf("isValidEnvKeyName(%q) = %v, want %v", k, got, want)
		}
	}
}
