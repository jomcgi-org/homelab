package main

import (
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// TestStateBootstrapped pins the first-boot marker. iggy-server writes the root
// user as the FIRST entry of <system.path>/state/log, so the file is absent or
// zero-length before the bootstrap and non-empty after. The zero-length case is
// the one that matters: the server creates and truncates the state log before it
// creates the root user, so "exists" alone would report a half-bootstrapped
// volume as done and skip the root-password requirement forever.
func TestStateBootstrapped(t *testing.T) {
	t.Run("absent state log: not bootstrapped", func(t *testing.T) {
		got, err := stateBootstrapped(t.TempDir())
		if err != nil {
			t.Fatal(err)
		}
		if got {
			t.Fatal("stateBootstrapped() = true on an empty volume, want false")
		}
	})

	t.Run("zero-length state log: not bootstrapped", func(t *testing.T) {
		systemPath := t.TempDir()
		writeStateLog(t, systemPath, "")
		got, err := stateBootstrapped(systemPath)
		if err != nil {
			t.Fatal(err)
		}
		if got {
			t.Fatal("stateBootstrapped() = true on a 0-byte state log, want false")
		}
	})

	t.Run("non-empty state log: bootstrapped", func(t *testing.T) {
		systemPath := t.TempDir()
		writeStateLog(t, systemPath, "one persisted state entry")
		got, err := stateBootstrapped(systemPath)
		if err != nil {
			t.Fatal(err)
		}
		if !got {
			t.Fatal("stateBootstrapped() = false on a non-empty state log, want true")
		}
	})
}

// TestRequireRootPassword covers the gate that turns an unusable datastore into a
// named wiring error. Upstream does not fail without IGGY_ROOT_PASSWORD: it
// autogenerates one and prints it once, which on a stateful volume is
// unrecoverable the moment that boot's log rotates.
func TestRequireRootPassword(t *testing.T) {
	t.Run("first boot without a password is refused", func(t *testing.T) {
		t.Setenv(rootPasswordEnv, "")
		os.Unsetenv(rootPasswordEnv)
		err := requireRootPassword(false)
		if err == nil {
			t.Fatal("requireRootPassword(false) = nil with no password, want an error")
		}
		if !strings.Contains(err.Error(), rootPasswordEnv) {
			t.Fatalf("error %q does not name %s", err, rootPasswordEnv)
		}
	})

	t.Run("first boot with a password is allowed", func(t *testing.T) {
		t.Setenv(rootPasswordEnv, "hunter2")
		if err := requireRootPassword(false); err != nil {
			t.Fatalf("requireRootPassword(false) = %v with a password set, want nil", err)
		}
	})

	t.Run("already bootstrapped volume needs no password", func(t *testing.T) {
		t.Setenv(rootPasswordEnv, "")
		os.Unsetenv(rootPasswordEnv)
		if err := requireRootPassword(true); err != nil {
			t.Fatalf("requireRootPassword(true) = %v on a bootstrapped volume, want nil", err)
		}
	})

	// The gate is only as good as the probe that feeds it: a volume whose state
	// log exists but is empty (a first boot interrupted before the root user was
	// persisted) must still be treated as a first boot.
	t.Run("interrupted first boot still requires a password", func(t *testing.T) {
		systemPath := t.TempDir()
		writeStateLog(t, systemPath, "")
		bootstrapped, err := stateBootstrapped(systemPath)
		if err != nil {
			t.Fatal(err)
		}
		t.Setenv(rootPasswordEnv, "")
		os.Unsetenv(rootPasswordEnv)
		if err := requireRootPassword(bootstrapped); err == nil {
			t.Fatal("an interrupted first boot was allowed through with no password")
		}
	})
}

// TestIggyChildEnv pins the one env var that cannot be a static default: the data
// root is only knowable once the volume mount path is read off the kernel command
// line. An operator value delivered through mmds_env must still win.
func TestIggyChildEnv(t *testing.T) {
	t.Run("defaults to the data dir on the mounted volume", func(t *testing.T) {
		t.Setenv(systemPathEnv, "")
		os.Unsetenv(systemPathEnv)
		want := systemPathEnv + "=" + filepath.Join("/data", iggyDataDirName)
		if !containsEnv(iggyChildEnv("/data"), want) {
			t.Fatalf("iggyChildEnv(/data) does not contain %q", want)
		}
	})

	t.Run("an mmds_env override is not clobbered", func(t *testing.T) {
		t.Setenv(systemPathEnv, "/data/elsewhere")
		env := iggyChildEnv("/data")
		if containsEnv(env, systemPathEnv+"="+filepath.Join("/data", iggyDataDirName)) {
			t.Fatalf("iggyChildEnv(/data) appended a default over an existing %s", systemPathEnv)
		}
		if !containsEnv(env, systemPathEnv+"=/data/elsewhere") {
			t.Fatalf("iggyChildEnv(/data) dropped the existing %s", systemPathEnv)
		}
	})
}

// TestSetDefaultEnv covers the knobs that make iggy-server usable in this guest:
// binding the tap NIC (the compiled default is loopback, which would fail the
// workload's TCP health probe forever) and keeping rotating log files off the
// volume. Existing values must never be overwritten, since setMmdsEnv runs after
// this and an operator override has to win.
func TestSetDefaultEnv(t *testing.T) {
	logger := slog.New(slog.NewTextHandler(os.Stderr, nil))

	t.Run("binds every interface and disables the unrouted transports", func(t *testing.T) {
		for _, k := range []string{"IGGY_TCP_ADDRESS", "IGGY_QUIC_ENABLED", "IGGY_HTTP_ENABLED", "IGGY_SYSTEM_LOGGING_FILE_ENABLED", "IGGY_ROOT_USERNAME"} {
			t.Setenv(k, "")
			os.Unsetenv(k)
		}
		setDefaultEnv(logger)
		for k, want := range map[string]string{
			"IGGY_TCP_ADDRESS":                 "0.0.0.0:" + iggyTCPPortString,
			"IGGY_QUIC_ENABLED":                "false",
			"IGGY_HTTP_ENABLED":                "false",
			"IGGY_SYSTEM_LOGGING_FILE_ENABLED": "false",
			"IGGY_ROOT_USERNAME":               defaultRootUsername,
		} {
			if got := os.Getenv(k); got != want {
				t.Fatalf("%s = %q, want %q", k, got, want)
			}
		}
	})

	t.Run("an existing value wins", func(t *testing.T) {
		t.Setenv("IGGY_TCP_ADDRESS", "0.0.0.0:9999")
		setDefaultEnv(logger)
		if got := os.Getenv("IGGY_TCP_ADDRESS"); got != "0.0.0.0:9999" {
			t.Fatalf("IGGY_TCP_ADDRESS = %q, want the pre-set 0.0.0.0:9999", got)
		}
	})

	// The root PASSWORD must have no default: a baked one would be a shared
	// credential across every deployment of this image, and an empty one would let
	// upstream autogenerate an unrecoverable password. requireRootPassword is what
	// turns its absence into an error.
	t.Run("no default root password is ever baked in", func(t *testing.T) {
		t.Setenv(rootPasswordEnv, "")
		os.Unsetenv(rootPasswordEnv)
		setDefaultEnv(logger)
		if v, set := os.LookupEnv(rootPasswordEnv); set {
			t.Fatalf("setDefaultEnv baked in %s = %q, want it left unset", rootPasswordEnv, v)
		}
	})
}

// writeStateLog creates <systemPath>/state/log with the given contents, standing
// in for what iggy-server writes on its first bootstrap.
func writeStateLog(t *testing.T, systemPath, contents string) {
	t.Helper()
	path := stateLogPath(systemPath)
	if err := os.MkdirAll(filepath.Dir(path), 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(contents), 0o600); err != nil {
		t.Fatal(err)
	}
}

// containsEnv reports whether env holds the exact KEY=VALUE entry.
func containsEnv(env []string, want string) bool {
	for _, e := range env {
		if e == want {
			return true
		}
	}
	return false
}
