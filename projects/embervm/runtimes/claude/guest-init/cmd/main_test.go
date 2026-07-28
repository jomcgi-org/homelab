//go:build linux

package main

import (
	"encoding/base64"
	"errors"
	"log/slog"
	"os"
	"testing"

	"golang.org/x/sys/unix"
)

func TestValueFromCmdline(t *testing.T) {
	for _, tc := range []struct {
		name, cmdline, key, want string
	}{
		{"present", "console=ttyS0 ember.volume_dev=/dev/vdb", volumeDevCmdlineKey, "/dev/vdb"},
		{"absent", "console=ttyS0 init=/init", volumeDevCmdlineKey, ""},
		{"empty value", "ember.volume_dev=", volumeDevCmdlineKey, ""},
		{"duplicate last wins", "ember.volume_dev=/dev/vdb ember.volume_dev=/dev/vdc", volumeDevCmdlineKey, "/dev/vdc"},
		{"not a prefix match", "other.ember.volume_dev=/dev/vdb", volumeDevCmdlineKey, ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := valueFromCmdline(tc.cmdline, tc.key); got != tc.want {
				t.Fatalf("valueFromCmdline(%q, %q) = %q, want %q", tc.cmdline, tc.key, got, tc.want)
			}
		})
	}
}

func TestIsValidEnvKeyName(t *testing.T) {
	for _, tc := range []struct {
		key  string
		want bool
	}{
		{"EMBER_CLAUDE_WORKSPACE", true}, {"A1", true}, {"", false}, {"bad-key", false}, {"bad.key", false},
	} {
		if got := isValidEnvKeyName(tc.key); got != tc.want {
			t.Errorf("isValidEnvKeyName(%q) = %v, want %v", tc.key, got, tc.want)
		}
	}
}

func TestLoopbackUpFlags(t *testing.T) {
	for _, tc := range []struct {
		name string
		cur  uint16
		want uint16
	}{
		{"from down", 0, unix.IFF_UP},
		{"already up", unix.IFF_UP, unix.IFF_UP},
		{"preserves loopback", unix.IFF_LOOPBACK, unix.IFF_LOOPBACK | unix.IFF_UP},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := loopbackUpFlags(tc.cur); got != tc.want {
				t.Fatalf("loopbackUpFlags(%#x) = %#x, want %#x", tc.cur, got, tc.want)
			}
		})
	}
}

func TestSetDefaultEnv(t *testing.T) {
	logger := slog.New(slog.NewTextHandler(os.Stderr, nil))
	keys := []string{"PATH", "HOME", "PYTHONUNBUFFERED", "TERM", "EMBER_CLAUDE_WORKSPACE"}
	previous := make(map[string]string, len(keys))
	wasSet := make(map[string]bool, len(keys))
	for _, key := range keys {
		previous[key], wasSet[key] = os.LookupEnv(key)
		os.Unsetenv(key)
	}
	t.Cleanup(func() {
		for _, key := range keys {
			if wasSet[key] {
				_ = os.Setenv(key, previous[key])
			} else {
				_ = os.Unsetenv(key)
			}
		}
	})
	setDefaultEnv(logger)
	want := map[string]string{
		"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": "/home/runtime", "PYTHONUNBUFFERED": "1",
		"TERM": "dumb", "EMBER_CLAUDE_WORKSPACE": "/workspace",
	}
	for key, value := range want {
		if got := os.Getenv(key); got != value {
			t.Errorf("%s = %q, want %q", key, got, value)
		}
	}
}

func TestSetMmdsEnv(t *testing.T) {
	logger := slog.New(slog.NewTextHandler(os.Stderr, nil))
	validName := base64.RawURLEncoding.EncodeToString([]byte("Test User"))
	validEmail := base64.RawURLEncoding.EncodeToString([]byte("test@example.invalid"))
	path := t.TempDir() + "/cmdline"
	cmdline := "ember.env.EMBER_GIT_USER_NAME=" + validName + " " +
		"ember.env.EMBER_GIT_USER_EMAIL=" + validEmail + " " +
		"ember.env.bad-key=Zm9v ember.env.BROKEN=%%%"
	if err := os.WriteFile(path, []byte(cmdline), 0o600); err != nil {
		t.Fatal(err)
	}
	withCmdlinePath(t, path)
	for _, key := range []string{"EMBER_GIT_USER_NAME", "EMBER_GIT_USER_EMAIL", "BROKEN"} {
		previous, wasSet := os.LookupEnv(key)
		_ = os.Unsetenv(key)
		t.Cleanup(func() {
			if wasSet {
				_ = os.Setenv(key, previous)
			} else {
				_ = os.Unsetenv(key)
			}
		})
	}
	setMmdsEnv(logger)
	if got := os.Getenv("EMBER_GIT_USER_NAME"); got != "Test User" {
		t.Errorf("EMBER_GIT_USER_NAME = %q", got)
	}
	if got := os.Getenv("EMBER_GIT_USER_EMAIL"); got != "test@example.invalid" {
		t.Errorf("EMBER_GIT_USER_EMAIL = %q", got)
	}
	if got := os.Getenv("BROKEN"); got != "" {
		t.Errorf("malformed token set BROKEN = %q", got)
	}
}

func TestMountWorkspaceVolumePathsAreIsolated(t *testing.T) {
	logger := slog.New(slog.NewTextHandler(os.Stderr, nil))
	originalMount := mountFn
	originalVolume := mountVolumeDeviceFn
	originalMkdir := mkdirAllFn
	originalChown := chownFn
	originalChmod := chmodFn
	t.Cleanup(func() {
		mountFn = originalMount
		mountVolumeDeviceFn = originalVolume
		mkdirAllFn = originalMkdir
		chownFn = originalChown
		chmodFn = originalChmod
	})
	originalStat := statFn
	originalRead := readFileFn
	originalWrite := writeFileFn
	t.Cleanup(func() {
		statFn = originalStat
		readFileFn = originalRead
		writeFileFn = originalWrite
	})
	mountFn = func(string, string, string, uintptr, string) error { return nil }
	mkdirAllFn = func(string, os.FileMode) error { return nil }
	chownFn = func(string, int, int) error { return nil }
	chmodFn = func(string, os.FileMode) error { return nil }
	// No trust record on the writable HOME yet, so the seed path runs.
	statFn = func(string) (os.FileInfo, error) { return nil, os.ErrNotExist }
	readFileFn = func(string) ([]byte, error) { return []byte("{}"), nil }
	writeFileFn = func(string, []byte, os.FileMode) error { return nil }
	for _, tc := range []struct {
		name, cmdline string
		wantErr       bool
	}{
		{"device path fails closed", "init=/init ember.volume_dev=/dev/does-not-exist\n", true},
		{"tmpfs path", "init=/init console=ttyS0\n", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			path := t.TempDir() + "/cmdline"
			if err := os.WriteFile(path, []byte(tc.cmdline), 0o600); err != nil {
				t.Fatal(err)
			}
			withCmdlinePath(t, path)
			mountVolumeDeviceFn = func(*slog.Logger, string, string) error {
				return errors.New("device unavailable")
			}
			err := mountWorkspaceVolume(logger)
			if (err != nil) != tc.wantErr {
				t.Fatalf("mountWorkspaceVolume() error = %v, wantErr %v", err, tc.wantErr)
			}
		})
	}
}

func withCmdlinePath(t *testing.T, path string) {
	t.Helper()
	previous := procCmdlinePath
	procCmdlinePath = path
	t.Cleanup(func() { procCmdlinePath = previous })
}

// TestSeedTrustRecordDoesNotClobber covers the branch that matters once a session
// has a real disk: the CLI's own writes to ~/.claude.json (onboarding state) must
// survive a cold boot, so the image's baked master seeds ONLY into an empty HOME.
func TestSeedTrustRecordDoesNotClobber(t *testing.T) {
	logger := slog.New(slog.NewTextHandler(os.Stderr, nil))
	originalStat, originalRead, originalWrite, originalChown := statFn, readFileFn, writeFileFn, chownFn
	t.Cleanup(func() {
		statFn, readFileFn, writeFileFn, chownFn = originalStat, originalRead, originalWrite, originalChown
	})
	chownFn = func(string, int, int) error { return nil }
	readFileFn = func(string) ([]byte, error) { return []byte(`{"projects":{}}`), nil }

	t.Run("absent seeds", func(t *testing.T) {
		wrote := ""
		statFn = func(string) (os.FileInfo, error) { return nil, os.ErrNotExist }
		writeFileFn = func(path string, _ []byte, _ os.FileMode) error { wrote = path; return nil }
		if err := seedTrustRecord(logger); err != nil {
			t.Fatalf("seedTrustRecord() = %v", err)
		}
		if wrote != runtimeHomePath+"/.claude.json" {
			t.Errorf("seeded %q, want the record in HOME", wrote)
		}
	})

	t.Run("present is left alone", func(t *testing.T) {
		statFn = func(string) (os.FileInfo, error) { return nil, nil }
		writeFileFn = func(string, []byte, os.FileMode) error {
			t.Fatal("overwrote an existing trust record; the CLI's own state would be rolled back")
			return nil
		}
		if err := seedTrustRecord(logger); err != nil {
			t.Fatalf("seedTrustRecord() = %v", err)
		}
	})
}

// TestSetDefaultEnvOverridesInherited pins the two things that made every turn
// 503 in the cluster while CI was green.
//
// The kernel hands PID 1 its own HOME (/), and the previous if-unset guard left
// it there, so git tried to write //.gitconfig on the read-only rootfs. And the
// egress auth variables lived only in apko.yaml, which is OCI image config that a
// raw Firecracker boot never reads, so the CLI booted with no base URL and no
// placeholder to swap.
func TestSetDefaultEnvOverridesInherited(t *testing.T) {
	logger := slog.New(slog.NewTextHandler(os.Stderr, nil))
	// Exactly what a kernel gives init: a HOME that is wrong for this guest.
	t.Setenv("HOME", "/")

	setDefaultEnv(logger)

	if got := os.Getenv("HOME"); got != "/home/runtime" {
		t.Errorf("HOME = %q, want /home/runtime; an inherited HOME must not win", got)
	}
	// Only guest-init can deliver these; apko.yaml alone is a silent no-op.
	if got := os.Getenv("ANTHROPIC_BASE_URL"); got != "http://api.anthropic.com" {
		t.Errorf("ANTHROPIC_BASE_URL = %q, want the cleartext egress lane", got)
	}
	if got := os.Getenv("CLAUDE_CODE_OAUTH_TOKEN"); got == "" {
		t.Error("CLAUDE_CODE_OAUTH_TOKEN is empty; the sidecar has no placeholder to swap")
	}
}
