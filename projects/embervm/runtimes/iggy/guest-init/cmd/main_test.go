package main

import (
	"encoding/base64"
	"log/slog"
	"os"
	"path/filepath"
	"testing"
)

// TestMmdsEnvFromCmdline covers the R4 D-R4.PR-7.1 boot-arg decoder shared with
// the postgres and python runtime guest-inits: base64url decoding, an invalid key
// skipped, a malformed base64 value skipped, an absent prefix ignored, and
// duplicate keys (last wins). This is the seam that delivers IGGY_ROOT_PASSWORD
// to first boot.
func TestMmdsEnvFromCmdline(t *testing.T) {
	pw := base64.RawURLEncoding.EncodeToString([]byte("hunter2"))

	for _, tc := range []struct {
		name    string
		cmdline string
		want    map[string]string
	}{
		{
			"iggy root password",
			"console=ttyS0 ember.env.IGGY_ROOT_PASSWORD=" + pw,
			map[string]string{"IGGY_ROOT_PASSWORD": "hunter2"},
		},
		{"absent (base build boot)", "console=ttyS0 init=/init", map[string]string{}},
		{"invalid key skipped", "ember.env.bad-key=" + pw, map[string]string{}},
		{"malformed base64 skipped", "ember.env.IGGY_ROOT_PASSWORD=not!valid!base64", map[string]string{}},
		{"empty value skipped", "ember.env.IGGY_ROOT_PASSWORD=", map[string]string{}},
		{
			"duplicate key: last wins",
			"ember.env.KEY=" + base64.RawURLEncoding.EncodeToString([]byte("first")) + " ember.env.KEY=" + base64.RawURLEncoding.EncodeToString([]byte("second")),
			map[string]string{"KEY": "second"},
		},
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

// TestSetMmdsEnv drives the full seam: a /proc/cmdline fixture carrying the
// IGGY_ROOT_PASSWORD token sets it in the process env; an absent token or missing
// cmdline is a no-op (the base build / relight case, which never carries these).
func TestSetMmdsEnv(t *testing.T) {
	logger := slog.New(slog.NewTextHandler(os.Stderr, nil))

	t.Run("password token present sets env", func(t *testing.T) {
		path := filepath.Join(t.TempDir(), "cmdline")
		pw := base64.RawURLEncoding.EncodeToString([]byte("hunter2"))
		if err := os.WriteFile(path, []byte("init=/init ember.env.IGGY_ROOT_PASSWORD="+pw+"\n"), 0o600); err != nil {
			t.Fatal(err)
		}
		t.Setenv(rootPasswordEnv, "")
		os.Unsetenv(rootPasswordEnv)
		withCmdlinePath(t, path)
		setMmdsEnv(logger)
		if got := os.Getenv(rootPasswordEnv); got != "hunter2" {
			t.Fatalf("%s = %q, want %q", rootPasswordEnv, got, "hunter2")
		}
	})

	t.Run("absent leaves env unset (base build / relight)", func(t *testing.T) {
		path := filepath.Join(t.TempDir(), "cmdline")
		if err := os.WriteFile(path, []byte("init=/init console=ttyS0\n"), 0o600); err != nil {
			t.Fatal(err)
		}
		t.Setenv(rootPasswordEnv, "")
		os.Unsetenv(rootPasswordEnv)
		withCmdlinePath(t, path)
		setMmdsEnv(logger)
		if _, set := os.LookupEnv(rootPasswordEnv); set {
			t.Fatalf("%s set, want unset", rootPasswordEnv)
		}
	})
}

// TestStatefulVolumeFromCmdline covers the boot-class discriminator: the presence
// of ember.volume_dev distinguishes a stateful cold boot (mount + launch
// iggy-server) from a base build (vsock ready only, no volume/server).
func TestStatefulVolumeFromCmdline(t *testing.T) {
	logger := slog.New(slog.NewTextHandler(os.Stderr, nil))

	t.Run("stateful cold boot returns dev + mount", func(t *testing.T) {
		path := filepath.Join(t.TempDir(), "cmdline")
		if err := os.WriteFile(path, []byte("init=/init ember.volume_dev=/dev/vdc ember.volume_mount=/data\n"), 0o600); err != nil {
			t.Fatal(err)
		}
		withCmdlinePath(t, path)
		dev, mount := statefulVolumeFromCmdline(logger)
		if dev != "/dev/vdc" || mount != "/data" {
			t.Fatalf("statefulVolumeFromCmdline() = (%q, %q), want (/dev/vdc, /data)", dev, mount)
		}
	})

	t.Run("volume without mount path falls back to the CR default", func(t *testing.T) {
		path := filepath.Join(t.TempDir(), "cmdline")
		if err := os.WriteFile(path, []byte("init=/init ember.volume_dev=/dev/vdc\n"), 0o600); err != nil {
			t.Fatal(err)
		}
		withCmdlinePath(t, path)
		dev, mount := statefulVolumeFromCmdline(logger)
		if dev != "/dev/vdc" || mount != defaultVolumeMountPath {
			t.Fatalf("statefulVolumeFromCmdline() = (%q, %q), want (/dev/vdc, %q)", dev, mount, defaultVolumeMountPath)
		}
	})

	t.Run("base build returns empty (no volume)", func(t *testing.T) {
		path := filepath.Join(t.TempDir(), "cmdline")
		if err := os.WriteFile(path, []byte("init=/init console=ttyS0\n"), 0o600); err != nil {
			t.Fatal(err)
		}
		withCmdlinePath(t, path)
		dev, mount := statefulVolumeFromCmdline(logger)
		if dev != "" || mount != "" {
			t.Fatalf("statefulVolumeFromCmdline() = (%q, %q), want empty (base build)", dev, mount)
		}
	})

	t.Run("missing cmdline returns empty (host build)", func(t *testing.T) {
		withCmdlinePath(t, filepath.Join(t.TempDir(), "does-not-exist"))
		dev, mount := statefulVolumeFromCmdline(logger)
		if dev != "" || mount != "" {
			t.Fatalf("statefulVolumeFromCmdline() = (%q, %q), want empty", dev, mount)
		}
	})
}

// withCmdlinePath points procCmdlinePath at a fixture for one subtest and
// restores it after (the package global is the only injectable seam).
func withCmdlinePath(t *testing.T, path string) {
	t.Helper()
	prev := procCmdlinePath
	procCmdlinePath = path
	t.Cleanup(func() { procCmdlinePath = prev })
}
