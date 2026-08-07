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

// Git identity must reach EVERY adapter, not just claude. The EMBER_GIT_* pair
// only becomes git config via ClaudeProcess._configure_git, which runs on the
// claude spawn path alone, so a codex (luna/terra/sol) or pi (qwen) session had
// no identity and every commit failed with "no configured author identity".
// git reads GIT_AUTHOR_*/GIT_COMMITTER_* directly with no config file, so these
// hold for all adapters and do not depend on $HOME/.gitconfig surviving the HOME
// bind-mount from the session volume.
func TestSetDefaultEnvSetsGitIdentityForEveryAdapter(t *testing.T) {
	logger := slog.New(slog.NewTextHandler(os.Stderr, nil))
	keys := []string{
		"EMBER_GIT_USER_NAME", "EMBER_GIT_USER_EMAIL",
		"GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
		"GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL",
		"GH_TOKEN",
	}
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

	const name, email = "EmberAgent", "agent@jomcgi.dev"
	// gh refuses to issue any request while it believes it has no credentials,
	// so it never reaches the sidecar that would credential it. The value is
	// inert and discarded on the way out; only its PRESENCE matters.
	if got := os.Getenv("GH_TOKEN"); got == "" {
		t.Error("GH_TOKEN must be set to a login-gate dummy, or gh makes no request at all")
	}
	want := map[string]string{
		"EMBER_GIT_USER_NAME": name, "EMBER_GIT_USER_EMAIL": email,
		"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
		"GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email,
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
	originalDeviceAvailable := deviceAvailableFn
	originalMounted := mountedFstypeFn
	t.Cleanup(func() {
		statFn = originalStat
		readFileFn = originalRead
		writeFileFn = originalWrite
		deviceAvailableFn = originalDeviceAvailable
		mountedFstypeFn = originalMounted
	})
	mountFn = func(string, string, string, uintptr, string) error { return nil }
	mkdirAllFn = func(string, os.FileMode) error { return nil }
	chownFn = func(string, int, int) error { return nil }
	chmodFn = func(string, os.FileMode) error { return nil }
	// No trust record on the writable HOME yet, so the seed path runs.
	statFn = func(string) (os.FileInfo, error) { return nil, os.ErrNotExist }
	readFileFn = func(string) ([]byte, error) { return []byte("{}"), nil }
	writeFileFn = func(string, []byte, os.FileMode) error { return nil }
	mountedFstypeFn = func(string) (string, bool) { return "", false }
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
			deviceAvailableFn = func(string) bool { return tc.wantErr }
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

func TestEnsureWorkspaceVolumeIsIdempotentAndSeedsIdentically(t *testing.T) {
	logger := slog.New(slog.NewTextHandler(os.Stderr, nil))
	path := t.TempDir() + "/cmdline"
	if err := os.WriteFile(path, []byte("ember.volume_dev=/dev/vdb ember.volume_mount=/session\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	withCmdlinePath(t, path)

	originalMount, originalVolume := mountFn, mountVolumeDeviceFn
	originalMkdir, originalChown, originalChmod := mkdirAllFn, chownFn, chmodFn
	originalStat, originalRead, originalWrite := statFn, readFileFn, writeFileFn
	originalDeviceAvailable, originalMounted := deviceAvailableFn, mountedFstypeFn
	t.Cleanup(func() {
		mountFn, mountVolumeDeviceFn = originalMount, originalVolume
		mkdirAllFn, chownFn, chmodFn = originalMkdir, originalChown, originalChmod
		statFn, readFileFn, writeFileFn = originalStat, originalRead, originalWrite
		deviceAvailableFn, mountedFstypeFn = originalDeviceAvailable, originalMounted
	})

	mounts := 0
	mounted := false
	bindTargets := []string{}
	mountFn = func(source, target string, _ string, flags uintptr, _ string) error {
		mounts++
		if flags&unix.MS_BIND != 0 {
			bindTargets = append(bindTargets, source+" -> "+target)
		}
		return nil
	}
	mountVolumeDeviceFn = func(*slog.Logger, string, string) error {
		mounts++
		mounted = true
		return nil
	}
	mkdirAllFn = func(string, os.FileMode) error { return nil }
	chownFn = func(string, int, int) error { return nil }
	chmodFn = func(string, os.FileMode) error { return nil }
	statFn = func(string) (os.FileInfo, error) { return nil, os.ErrNotExist }
	readFileFn = func(string) ([]byte, error) { return []byte("{}"), nil }
	trustWrites := 0
	writeFileFn = func(path string, _ []byte, _ os.FileMode) error {
		if path == runtimeHomePath+"/.claude.json" {
			trustWrites++
		}
		return nil
	}
	deviceAvailableFn = func(string) bool { return true }
	mountedFstypeFn = func(string) (string, bool) { return "ext4", mounted }

	if err := ensureWorkspaceVolume(logger); err != nil {
		t.Fatal(err)
	}
	firstMounts := mounts
	if firstMounts == 0 {
		t.Fatal("ensureWorkspaceVolume() did not mount the device")
	}
	if len(bindTargets) != 2 || trustWrites != 1 {
		t.Fatalf("binds = %v, trust writes = %d, want two binds and one trust seed", bindTargets, trustWrites)
	}
	if err := ensureWorkspaceVolume(logger); err != nil {
		t.Fatal(err)
	}
	if mounts != firstMounts {
		t.Fatalf("second ensureWorkspaceVolume() mounted %d additional times", mounts-firstMounts)
	}
}

func TestEnsureWorkspaceVolumeUsesTmpfsWithoutDevice(t *testing.T) {
	logger := slog.New(slog.NewTextHandler(os.Stderr, nil))
	path := t.TempDir() + "/cmdline"
	if err := os.WriteFile(path, []byte("init=/init\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	withCmdlinePath(t, path)
	originalMount, originalVolume := mountFn, mountVolumeDeviceFn
	originalMkdir, originalChown, originalChmod := mkdirAllFn, chownFn, chmodFn
	originalStat, originalRead, originalWrite := statFn, readFileFn, writeFileFn
	originalDeviceAvailable, originalMounted := deviceAvailableFn, mountedFstypeFn
	t.Cleanup(func() {
		mountFn, mountVolumeDeviceFn = originalMount, originalVolume
		mkdirAllFn, chownFn, chmodFn = originalMkdir, originalChown, originalChmod
		statFn, readFileFn, writeFileFn = originalStat, originalRead, originalWrite
		deviceAvailableFn, mountedFstypeFn = originalDeviceAvailable, originalMounted
	})

	deviceMounts := 0
	tmpfsMounts := 0
	mountFn = func(source, _ string, _ string, _ uintptr, _ string) error {
		if source == "tmpfs" {
			tmpfsMounts++
		}
		return nil
	}
	mountVolumeDeviceFn = func(*slog.Logger, string, string) error {
		deviceMounts++
		return nil
	}
	mkdirAllFn = func(string, os.FileMode) error { return nil }
	chownFn = func(string, int, int) error { return nil }
	chmodFn = func(string, os.FileMode) error { return nil }
	statFn = func(string) (os.FileInfo, error) { return nil, os.ErrNotExist }
	readFileFn = func(string) ([]byte, error) { return []byte("{}"), nil }
	writeFileFn = func(string, []byte, os.FileMode) error { return nil }
	deviceAvailableFn = func(string) bool { return false }
	mountedFstypeFn = func(string) (string, bool) { return "", false }

	if err := ensureWorkspaceVolume(logger); err != nil {
		t.Fatal(err)
	}
	if tmpfsMounts != 1 || deviceMounts != 0 {
		t.Fatalf("mounts = tmpfs:%d device:%d, want tmpfs:1 device:0", tmpfsMounts, deviceMounts)
	}
}

func TestEnsureWorkspaceVolumePlaceholderDeviceIgnoredWithoutCmdlineArg(t *testing.T) {
	logger := slog.New(slog.NewTextHandler(os.Stderr, nil))
	path := t.TempDir() + "/cmdline"
	// No ember.volume_dev argument: base-build case
	if err := os.WriteFile(path, []byte("init=/init\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	withCmdlinePath(t, path)

	originalMount, originalVolume := mountFn, mountVolumeDeviceFn
	originalMkdir, originalChown, originalChmod := mkdirAllFn, chownFn, chmodFn
	originalStat, originalRead, originalWrite := statFn, readFileFn, writeFileFn
	originalDeviceAvailable, originalMounted := deviceAvailableFn, mountedFstypeFn
	t.Cleanup(func() {
		mountFn, mountVolumeDeviceFn = originalMount, originalVolume
		mkdirAllFn, chownFn, chmodFn = originalMkdir, originalChown, originalChmod
		statFn, readFileFn, writeFileFn = originalStat, originalRead, originalWrite
		deviceAvailableFn, mountedFstypeFn = originalDeviceAvailable, originalMounted
	})

	deviceMounts := 0
	tmpfsMounts := 0
	mountFn = func(source, _ string, _ string, _ uintptr, _ string) error {
		if source == "tmpfs" {
			tmpfsMounts++
		}
		return nil
	}
	mountVolumeDeviceFn = func(*slog.Logger, string, string) error {
		deviceMounts++
		return nil
	}
	mkdirAllFn = func(string, os.FileMode) error { return nil }
	chownFn = func(string, int, int) error { return nil }
	chmodFn = func(string, os.FileMode) error { return nil }
	statFn = func(string) (os.FileInfo, error) { return nil, os.ErrNotExist }
	readFileFn = func(string) ([]byte, error) { return []byte("{}"), nil }
	writeFileFn = func(string, []byte, os.FileMode) error { return nil }
	// Device node EXISTS but is NOT requested on cmdline
	deviceAvailableFn = func(path string) bool { return path == "/dev/vdb" }
	mountedFstypeFn = func(string) (string, bool) { return "", false }

	// No explicit device, so should use tmpfs
	if err := ensureWorkspaceVolume(logger); err != nil {
		t.Fatal(err)
	}
	if tmpfsMounts != 1 || deviceMounts != 0 {
		t.Fatalf("mounts = tmpfs:%d device:%d, want tmpfs:1 device:0 (base build must NOT mount placeholder)", tmpfsMounts, deviceMounts)
	}
}

func TestEnsureWorkspaceVolumeWithExplicitDeviceMountsWithoutCmdlineArg(t *testing.T) {
	logger := slog.New(slog.NewTextHandler(os.Stderr, nil))
	path := t.TempDir() + "/cmdline"
	// No device on cmdline: this is a restored guest
	if err := os.WriteFile(path, []byte("init=/init\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	withCmdlinePath(t, path)

	originalMount, originalVolume := mountFn, mountVolumeDeviceFn
	originalMkdir, originalChown, originalChmod := mkdirAllFn, chownFn, chmodFn
	originalStat, originalRead, originalWrite := statFn, readFileFn, writeFileFn
	originalDeviceAvailable, originalMounted := deviceAvailableFn, mountedFstypeFn
	t.Cleanup(func() {
		mountFn, mountVolumeDeviceFn = originalMount, originalVolume
		mkdirAllFn, chownFn, chmodFn = originalMkdir, originalChown, originalChmod
		statFn, readFileFn, writeFileFn = originalStat, originalRead, originalWrite
		deviceAvailableFn, mountedFstypeFn = originalDeviceAvailable, originalMounted
	})

	deviceMounts := 0
	tmpfsMounts := 0
	mountedDev := ""
	mountFn = func(source, _ string, _ string, _ uintptr, _ string) error {
		if source == "tmpfs" {
			tmpfsMounts++
		}
		return nil
	}
	mountVolumeDeviceFn = func(_ *slog.Logger, dev, _ string) error {
		deviceMounts++
		mountedDev = dev
		return nil
	}
	mkdirAllFn = func(string, os.FileMode) error { return nil }
	chownFn = func(string, int, int) error { return nil }
	chmodFn = func(string, os.FileMode) error { return nil }
	statFn = func(string) (os.FileInfo, error) { return nil, os.ErrNotExist }
	readFileFn = func(string) ([]byte, error) { return []byte("{}"), nil }
	writeFileFn = func(string, []byte, os.FileMode) error { return nil }
	deviceAvailableFn = func(path string) bool { return path == "/dev/vdb" }
	mountedFstypeFn = func(string) (string, bool) { return "", false }

	// Explicit device, so should mount it despite cmdline having nothing
	if err := ensureWorkspaceVolumeWithDevice(logger, "/dev/vdb"); err != nil {
		t.Fatal(err)
	}
	if tmpfsMounts != 0 || deviceMounts != 1 || mountedDev != "/dev/vdb" {
		t.Fatalf("mounts = tmpfs:%d device:%d (dev=%q), want tmpfs:0 device:1 (dev=/dev/vdb) (restored guest must mount explicit device)", tmpfsMounts, deviceMounts, mountedDev)
	}
}

func TestEnsureWorkspaceVolumeMountDecisions(t *testing.T) {
	logger := slog.New(slog.NewTextHandler(os.Stderr, nil))
	path := t.TempDir() + "/cmdline"
	if err := os.WriteFile(path, []byte("init=/init\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	withCmdlinePath(t, path)

	originalMount, originalUnmount := mountFn, unmountFn
	originalVolume, originalMkdir := mountVolumeDeviceFn, mkdirAllFn
	originalChown, originalChmod := chownFn, chmodFn
	originalStat, originalRead, originalWrite := statFn, readFileFn, writeFileFn
	originalAvailable, originalFstype := deviceAvailableFn, mountedFstypeFn
	t.Cleanup(func() {
		mountFn, unmountFn = originalMount, originalUnmount
		mountVolumeDeviceFn, mkdirAllFn = originalVolume, originalMkdir
		chownFn, chmodFn = originalChown, originalChmod
		statFn, readFileFn, writeFileFn = originalStat, originalRead, originalWrite
		deviceAvailableFn, mountedFstypeFn = originalAvailable, originalFstype
	})
	mkdirAllFn = func(string, os.FileMode) error { return nil }
	chownFn = func(string, int, int) error { return nil }
	chmodFn = func(string, os.FileMode) error { return nil }
	statFn = func(string) (os.FileInfo, error) { return nil, os.ErrNotExist }
	readFileFn = func(string) ([]byte, error) { return []byte("{}"), nil }
	writeFileFn = func(string, []byte, os.FileMode) error { return nil }
	deviceAvailableFn = func(string) bool { return true }

	for _, tc := range []struct {
		name, fstype, explicit string
		wantUnmounts           []string
		wantDeviceMounts       int
		wantTrustWrites        int
	}{
		{name: "takeover", fstype: "tmpfs", explicit: "/dev/vdb", wantUnmounts: []string{workspaceMountPath, runtimeHomePath, defaultSessionRoot}, wantDeviceMounts: 1, wantTrustWrites: 1},
		{name: "already on device", fstype: "ext4", explicit: "/dev/vdb", wantDeviceMounts: 0},
		{name: "base build unchanged", fstype: "tmpfs", wantDeviceMounts: 0},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var unmounts []string
			deviceMounts := 0
			var binds []string
			trustWrites := 0
			mountedFstypeFn = func(string) (string, bool) { return tc.fstype, true }
			unmountFn = func(path string, flags int) error {
				if flags != unix.MNT_DETACH {
					t.Fatalf("unmount flags = %d, want MNT_DETACH", flags)
				}
				unmounts = append(unmounts, path)
				return nil
			}
			mountFn = func(source, target, _ string, flags uintptr, _ string) error {
				if flags&unix.MS_BIND != 0 {
					binds = append(binds, source+" -> "+target)
				}
				return nil
			}
			mountVolumeDeviceFn = func(*slog.Logger, string, string) error {
				deviceMounts++
				return nil
			}
			writeFileFn = func(string, []byte, os.FileMode) error {
				trustWrites++
				return nil
			}
			if err := ensureWorkspaceVolumeWithDevice(logger, tc.explicit); err != nil {
				t.Fatal(err)
			}
			if len(unmounts) != len(tc.wantUnmounts) {
				t.Fatalf("unmounts = %v, want %v", unmounts, tc.wantUnmounts)
			}
			for i := range tc.wantUnmounts {
				if unmounts[i] != tc.wantUnmounts[i] {
					t.Fatalf("unmounts = %v, want %v", unmounts, tc.wantUnmounts)
				}
			}
			if deviceMounts != tc.wantDeviceMounts {
				t.Fatalf("device mounts = %d, want %d", deviceMounts, tc.wantDeviceMounts)
			}
			if trustWrites != tc.wantTrustWrites {
				t.Fatalf("trust writes = %d, want %d", trustWrites, tc.wantTrustWrites)
			}
			wantBinds := 0
			if tc.wantDeviceMounts != 0 {
				wantBinds = 2
			}
			if len(binds) != wantBinds {
				t.Fatalf("binds = %v, want %d", binds, wantBinds)
			}
		})
	}
}

func TestMountedFstypeParsesMountinfo(t *testing.T) {
	original := mountInfoFn
	t.Cleanup(func() { mountInfoFn = original })
	mountInfoFn = func(string) ([]byte, error) {
		return []byte("36 25 0:32 / /session\\040data rw,relatime - tmpfs tmpfs rw,size=2g\n" +
			"37 25 8:17 / /session rw,relatime shared:1 - ext4 /dev/vdb rw\n"), nil
	}
	if got, ok := mountedFstype("/session"); !ok || got != "ext4" {
		t.Fatalf("mountedFstype(/session) = %q, %v, want ext4, true", got, ok)
	}
	if got, ok := mountedFstype("/session data"); !ok || got != "tmpfs" {
		t.Fatalf("mountedFstype(/session data) = %q, %v, want tmpfs, true", got, ok)
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
