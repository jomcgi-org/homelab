//go:build linux

package main

import (
	"encoding/base64"
	"errors"
	"log/slog"
	"os"
	"testing"
)

func TestValueFromCmdline(t *testing.T) {
	for _, tc := range []struct {
		name, cmdline, key, want string
	}{
		{"present", "console=ttyS0 ember.workspace_dev=/dev/vdb", workspaceDevCmdlineKey, "/dev/vdb"},
		{"absent", "console=ttyS0 init=/init", workspaceDevCmdlineKey, ""},
		{"empty value", "ember.workspace_dev=", workspaceDevCmdlineKey, ""},
		{"duplicate last wins", "ember.workspace_dev=/dev/vdb ember.workspace_dev=/dev/vdc", workspaceDevCmdlineKey, "/dev/vdc"},
		{"not a prefix match", "other.ember.workspace_dev=/dev/vdb", workspaceDevCmdlineKey, ""},
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
	mountFn = func(string, string, string, uintptr, string) error { return nil }
	mkdirAllFn = func(string, os.FileMode) error { return nil }
	chownFn = func(string, int, int) error { return nil }
	chmodFn = func(string, os.FileMode) error { return nil }
	for _, tc := range []struct {
		name, cmdline string
		wantErr       bool
	}{
		{"device path fails closed", "init=/init ember.workspace_dev=/dev/does-not-exist\n", true},
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
