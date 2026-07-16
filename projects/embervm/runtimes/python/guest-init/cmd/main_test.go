package main

import (
	"log/slog"
	"os"
	"path/filepath"
	"testing"
)

// TestServingPortFromCmdline covers extracting the ember.serving_port= value from
// a kernel command line: present, absent, empty-value, and duplicate (last wins).
func TestServingPortFromCmdline(t *testing.T) {
	for _, tc := range []struct {
		name    string
		cmdline string
		want    string
	}{
		{"present", "console=ttyS0 init=/init ip=10.0.0.2::10.0.0.1:255.255.255.0::eth0:off ember.serving_port=8080", "8080"},
		{"absent (task/session boot)", "console=ttyS0 init=/init", ""},
		{"empty value", "console=ttyS0 ember.serving_port=", ""},
		{"first token", "ember.serving_port=9000 console=ttyS0", "9000"},
		{"duplicate last wins", "ember.serving_port=1 ember.serving_port=2", "2"},
		{"not a prefix match", "other.ember.serving_port=8080", ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := servingPortFromCmdline(tc.cmdline); got != tc.want {
				t.Fatalf("servingPortFromCmdline(%q) = %q, want %q", tc.cmdline, got, tc.want)
			}
		})
	}
}

// TestSetServingPortEnv drives the full seam: a /proc/cmdline fixture carrying the
// token exports EMBER_SERVING_PORT for the shim; a cmdline without it (or a missing
// file) leaves the env unset so the vsock task/session boot is unaffected.
func TestSetServingPortEnv(t *testing.T) {
	logger := slog.New(slog.NewTextHandler(os.Stderr, nil))

	t.Run("token present exports env", func(t *testing.T) {
		path := filepath.Join(t.TempDir(), "cmdline")
		if err := os.WriteFile(path, []byte("init=/init ember.serving_port=8080\n"), 0o600); err != nil {
			t.Fatal(err)
		}
		t.Setenv(servingPortEnv, "")
		os.Unsetenv(servingPortEnv)
		withCmdlinePath(t, path)
		setServingPortEnv(logger)
		if got := os.Getenv(servingPortEnv); got != "8080" {
			t.Fatalf("%s = %q, want %q", servingPortEnv, got, "8080")
		}
	})

	t.Run("token absent leaves env unset", func(t *testing.T) {
		path := filepath.Join(t.TempDir(), "cmdline")
		if err := os.WriteFile(path, []byte("init=/init console=ttyS0\n"), 0o600); err != nil {
			t.Fatal(err)
		}
		os.Unsetenv(servingPortEnv)
		withCmdlinePath(t, path)
		setServingPortEnv(logger)
		if _, set := os.LookupEnv(servingPortEnv); set {
			t.Fatalf("%s set, want unset (vsock boot)", servingPortEnv)
		}
	})

	t.Run("missing cmdline file is a no-op", func(t *testing.T) {
		os.Unsetenv(servingPortEnv)
		withCmdlinePath(t, filepath.Join(t.TempDir(), "does-not-exist"))
		setServingPortEnv(logger)
		if _, set := os.LookupEnv(servingPortEnv); set {
			t.Fatalf("%s set from a missing cmdline, want unset", servingPortEnv)
		}
	})
}

// withCmdlinePath points procCmdlinePath at a fixture for one subtest and restores
// it after (the package global is the only injectable seam).
func withCmdlinePath(t *testing.T, path string) {
	t.Helper()
	prev := procCmdlinePath
	procCmdlinePath = path
	t.Cleanup(func() { procCmdlinePath = prev })
}
