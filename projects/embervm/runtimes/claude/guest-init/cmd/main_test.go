package main

import (
	"log/slog"
	"os"
	"path/filepath"
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

func TestMountWorkspaceVolumeDeviceAndTmpfsPathsNeverFail(t *testing.T) {
	logger := slog.New(slog.NewTextHandler(os.Stderr, nil))
	for _, tc := range []struct {
		name, cmdline string
	}{
		{"device path", "init=/init ember.workspace_dev=/dev/does-not-exist\n"},
		{"tmpfs path", "init=/init console=ttyS0\n"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "cmdline")
			if err := os.WriteFile(path, []byte(tc.cmdline), 0o600); err != nil {
				t.Fatal(err)
			}
			withCmdlinePath(t, path)
			mountWorkspaceVolume(logger)
		})
	}
}

func withCmdlinePath(t *testing.T, path string) {
	t.Helper()
	previous := procCmdlinePath
	procCmdlinePath = path
	t.Cleanup(func() { procCmdlinePath = previous })
}
