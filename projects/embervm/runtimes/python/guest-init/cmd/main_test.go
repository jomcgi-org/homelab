package main

import (
	"encoding/base64"
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

// TestSetHandlerDiskEnv drives the D-R3.11.2 seam: a /proc/cmdline carrying
// ember.handler_disk= + ember.handler_zip_bytes= exports EMBER_HANDLER_ZIP and
// EMBER_HANDLER_ZIP_BYTES for the shim; a cmdline without the disk token leaves both
// unset so task/session/relight boots are unaffected.
func TestSetHandlerDiskEnv(t *testing.T) {
	logger := slog.New(slog.NewTextHandler(os.Stderr, nil))

	t.Run("tokens present export both envs", func(t *testing.T) {
		path := filepath.Join(t.TempDir(), "cmdline")
		if err := os.WriteFile(path, []byte("init=/init ember.serving_port=8080 ember.handler_disk=/dev/vdb ember.handler_zip_bytes=4096\n"), 0o600); err != nil {
			t.Fatal(err)
		}
		os.Unsetenv(handlerZipEnv)
		os.Unsetenv(handlerZipBytesEnv)
		withCmdlinePath(t, path)
		setHandlerDiskEnv(logger)
		if got := os.Getenv(handlerZipEnv); got != "/dev/vdb" {
			t.Fatalf("%s = %q, want /dev/vdb", handlerZipEnv, got)
		}
		if got := os.Getenv(handlerZipBytesEnv); got != "4096" {
			t.Fatalf("%s = %q, want 4096", handlerZipBytesEnv, got)
		}
	})

	t.Run("disk token absent leaves envs unset", func(t *testing.T) {
		path := filepath.Join(t.TempDir(), "cmdline")
		if err := os.WriteFile(path, []byte("init=/init ember.serving_port=8080\n"), 0o600); err != nil {
			t.Fatal(err)
		}
		os.Unsetenv(handlerZipEnv)
		os.Unsetenv(handlerZipBytesEnv)
		withCmdlinePath(t, path)
		setHandlerDiskEnv(logger)
		if _, set := os.LookupEnv(handlerZipEnv); set {
			t.Fatalf("%s set, want unset (non-zip-serving boot)", handlerZipEnv)
		}
	})
}

// TestMmdsEnvFromCmdline covers the R4 D-R4.PR-7.1 boot-arg decoder: multiple
// ember.env.<KEY>= tokens, base64url decoding, an invalid key skipped, a
// malformed base64 value skipped, an absent prefix ignored, and duplicate keys
// (last wins, matching valueFromCmdline's convention).
func TestMmdsEnvFromCmdline(t *testing.T) {
	pw := base64.RawURLEncoding.EncodeToString([]byte("hunter2"))
	user := base64.RawURLEncoding.EncodeToString([]byte("app"))

	for _, tc := range []struct {
		name    string
		cmdline string
		want    map[string]string
	}{
		{
			"multiple keys",
			"console=ttyS0 ember.env.POSTGRES_PASSWORD=" + pw + " ember.env.POSTGRES_USER=" + user,
			map[string]string{"POSTGRES_PASSWORD": "hunter2", "POSTGRES_USER": "app"},
		},
		{"absent (non-stateful boot)", "console=ttyS0 init=/init", map[string]string{}},
		{"invalid key skipped", "ember.env.bad-key=" + pw, map[string]string{}},
		{"malformed base64 skipped", "ember.env.POSTGRES_PASSWORD=not!valid!base64", map[string]string{}},
		{
			"duplicate key: last wins",
			"ember.env.KEY=" + base64.RawURLEncoding.EncodeToString([]byte("first")) + " ember.env.KEY=" + base64.RawURLEncoding.EncodeToString([]byte("second")),
			map[string]string{"KEY": "second"},
		},
		{"empty value skipped", "ember.env.POSTGRES_PASSWORD=", map[string]string{}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got := mmdsEnvFromCmdline(tc.cmdline)
			if len(got) != len(tc.want) {
				t.Fatalf("mmdsEnvFromCmdline(%q) = %v, want %v", tc.cmdline, got, tc.want)
			}
			for k, v := range tc.want {
				if got[k] != v {
					t.Fatalf("mmdsEnvFromCmdline(%q)[%q] = %q, want %q", tc.cmdline, k, got[k], v)
				}
			}
		})
	}
}

// TestSetMmdsEnv drives the full seam: a /proc/cmdline fixture carrying
// ember.env.* tokens sets each decoded value as a process env var; an absent
// token or missing cmdline file is a no-op (covers the RELIGHT / non-stateful
// boot case, which never carries these tokens).
func TestSetMmdsEnv(t *testing.T) {
	logger := slog.New(slog.NewTextHandler(os.Stderr, nil))

	t.Run("tokens present set env vars", func(t *testing.T) {
		path := filepath.Join(t.TempDir(), "cmdline")
		pw := base64.RawURLEncoding.EncodeToString([]byte("hunter2"))
		content := "init=/init ember.env.POSTGRES_PASSWORD=" + pw + "\n"
		if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
			t.Fatal(err)
		}
		os.Unsetenv("POSTGRES_PASSWORD")
		t.Cleanup(func() { os.Unsetenv("POSTGRES_PASSWORD") })
		withCmdlinePath(t, path)
		setMmdsEnv(logger)
		if got := os.Getenv("POSTGRES_PASSWORD"); got != "hunter2" {
			t.Fatalf("POSTGRES_PASSWORD = %q, want %q", got, "hunter2")
		}
	})

	t.Run("tokens absent leaves env unset (relight/non-stateful boot)", func(t *testing.T) {
		path := filepath.Join(t.TempDir(), "cmdline")
		if err := os.WriteFile(path, []byte("init=/init console=ttyS0\n"), 0o600); err != nil {
			t.Fatal(err)
		}
		os.Unsetenv("POSTGRES_PASSWORD")
		withCmdlinePath(t, path)
		setMmdsEnv(logger)
		if _, set := os.LookupEnv("POSTGRES_PASSWORD"); set {
			t.Fatalf("POSTGRES_PASSWORD set, want unset (no mmds_env tokens)")
		}
	})

	t.Run("missing cmdline file is a no-op", func(t *testing.T) {
		os.Unsetenv("POSTGRES_PASSWORD")
		withCmdlinePath(t, filepath.Join(t.TempDir(), "does-not-exist"))
		setMmdsEnv(logger)
		if _, set := os.LookupEnv("POSTGRES_PASSWORD"); set {
			t.Fatalf("POSTGRES_PASSWORD set from a missing cmdline, want unset")
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
