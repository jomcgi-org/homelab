//go:build linux

package driver

import (
	"context"
	"errors"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"sync/atomic"
	"syscall"
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

// jailerHarness drives ExecLauncher through the helper-process stand-in for the
// jailer: a wrapper script execs this test binary, which binds the API socket
// inside the jail root exactly where the real jailer's Firecracker would.
type jailerHarness struct {
	launcher  *ExecLauncher
	socket    string
	cgroupDir string
	released  *atomic.Int32
	output    *lockedBuffer
	modes     *launchModeCounters
}

func newJailerHarness(t *testing.T, env map[string]string) *jailerHarness {
	t.Helper()
	t.Setenv("EMBER_JAILER_HELPER", "1")
	t.Setenv("EMBER_JAILER_TEST_BINARY", os.Args[0])
	for k, v := range env {
		t.Setenv(k, v)
	}
	// The helper binds the API socket deep inside the jail root, so keep the
	// base short enough for sun_path whatever the test's name.
	tmp := shortTempDir(t)
	wrapper := filepath.Join(tmp, "jailer-helper")
	script := "#!/bin/sh\nexec \"$EMBER_JAILER_TEST_BINARY\" -test.run=TestExecLauncherHelperProcess -- \"$@\"\n"
	if err := os.WriteFile(wrapper, []byte(script), 0o700); err != nil {
		t.Fatal(err)
	}
	cgroupDir := filepath.Join(tmp, "cgroup", "vm-test")
	if err := os.MkdirAll(cgroupDir, 0o755); err != nil {
		t.Fatal(err)
	}
	socket := filepath.Join(tmp, "bundle", "api.sock")
	if err := os.MkdirAll(filepath.Dir(socket), 0o755); err != nil {
		t.Fatal(err)
	}
	h := &jailerHarness{
		socket: socket, cgroupDir: cgroupDir,
		released: &atomic.Int32{}, output: &lockedBuffer{}, modes: &launchModeCounters{},
	}
	h.launcher = &ExecLauncher{
		Logger:        slog.New(slog.NewJSONHandler(h.output, nil)),
		Bin:           os.Args[0],
		JailerBin:     wrapper,
		Env:           os.Environ(),
		JailerEnabled: true,
		ReadyTimeout:  2 * time.Second,
		allocateJailUID: func() (int, func(), error) {
			return os.Getuid(), func() { h.released.Add(1) }, nil
		},
		createCgroup: func(string, int64) (*vmCgroup, error) {
			return &vmCgroup{dir: cgroupDir, parentArg: "test-parent/vm-test"}, nil
		},
		jailerPIDNamespace: func(*exec.Cmd) {},
		launchModes:        h.modes,
	}
	return h
}

func (h *jailerHarness) launch() (Process, error) {
	return h.launcher.Launch(context.Background(), LaunchSpec{VMID: "vm-test", SocketPath: h.socket, Workload: "test", Phase: guestPhaseVM, MemMib: 256})
}

func (h *jailerHarness) jailDir() string {
	return filepath.Join(filepath.Dir(h.socket), "jailer", filepath.Base(h.launcher.Bin), "vm-test")
}

// TestExecLauncherRefusesJailerOutsideLeafCgroup pins J5's post-start check: the
// jailer skips its cgroup join silently when the leaf is missing, so a VMM that
// is not in its leaf is killed and the launch refused.
func TestExecLauncherRefusesJailerOutsideLeafCgroup(t *testing.T) {
	h := newJailerHarness(t, nil)
	if err := os.WriteFile(filepath.Join(h.cgroupDir, "cgroup.procs"), []byte("1\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	process, err := h.launch()
	if process != nil {
		_ = process.Kill()
		t.Fatal("an uncontained VMM was returned as launched")
	}
	if !errors.Is(err, ErrJailerContainment) || !strings.Contains(err.Error(), "not in VM cgroup") {
		t.Fatalf("Launch error = %v, want a containment refusal", err)
	}
	if h.released.Load() != 1 {
		t.Fatalf("uid release count = %d, want 1", h.released.Load())
	}
	if _, err := os.Stat(h.jailDir()); !os.IsNotExist(err) {
		t.Fatalf("jail survived the refusal: %v", err)
	}
	if h.modes.refused.Load() != 1 || h.modes.jailed.Load() != 0 {
		t.Fatalf("launch modes jailed=%d refused=%d, want one refusal", h.modes.jailed.Load(), h.modes.refused.Load())
	}
}

// TestExecLauncherEarlyExitFailsFast pins J4 layer 3: a VMM that exits before
// binding its API socket fails the launch at once with its exit status, instead
// of after the full readiness timeout.
func TestExecLauncherEarlyExitFailsFast(t *testing.T) {
	t.Setenv("EMBER_JAILER_HELPER", "1")
	t.Setenv("EMBER_JAILER_TEST_BINARY", os.Args[0])
	t.Setenv("EMBER_JAILER_EXIT_IMMEDIATELY", "1")
	tmp := t.TempDir()
	wrapper := filepath.Join(tmp, "firecracker-helper")
	script := "#!/bin/sh\nexec \"$EMBER_JAILER_TEST_BINARY\" -test.run=TestExecLauncherHelperProcess -- \"$@\"\n"
	if err := os.WriteFile(wrapper, []byte(script), 0o700); err != nil {
		t.Fatal(err)
	}
	var modes launchModeCounters
	launcher := &ExecLauncher{
		Bin:          wrapper,
		Env:          os.Environ(),
		Logger:       slog.New(slog.NewJSONHandler(&lockedBuffer{}, nil)),
		ReadyTimeout: 30 * time.Second,
		launchModes:  &modes,
	}
	start := time.Now()
	_, err := launcher.Launch(context.Background(), LaunchSpec{
		VMID: "early-exit", SocketPath: filepath.Join(tmp, "api.sock"),
		Workload: "early-workload", Phase: guestPhaseInit,
	})
	if elapsed := time.Since(start); elapsed > 10*time.Second {
		t.Fatalf("Launch took %s for a VMM that exited at once", elapsed)
	}
	if !errors.Is(err, errVMMExitedBeforeSocket) || !strings.Contains(err.Error(), "exit status 3") {
		t.Fatalf("Launch error = %v, want the early exit with its status", err)
	}
	if errors.Is(err, ErrJailerContainment) {
		t.Fatalf("a direct-exec early exit was reported as a jailer refusal: %v", err)
	}
	if modes.directExec.Load() != 0 || modes.refused.Load() != 0 {
		t.Fatalf("failed direct launch counted: direct=%d refused=%d", modes.directExec.Load(), modes.refused.Load())
	}
}

func TestExecLauncherJailedEarlyExitRefusesFast(t *testing.T) {
	h := newJailerHarness(t, map[string]string{"EMBER_JAILER_EXIT_IMMEDIATELY": "1"})
	h.launcher.ReadyTimeout = 30 * time.Second
	start := time.Now()
	process, err := h.launch()
	if elapsed := time.Since(start); elapsed > 10*time.Second {
		t.Fatalf("Launch took %s for a jailer that exited at once", elapsed)
	}
	if process != nil {
		t.Fatal("a jailer that exited still returned a process")
	}
	if !errors.Is(err, ErrJailerContainment) || !errors.Is(err, errVMMExitedBeforeSocket) {
		t.Fatalf("Launch error = %v, want a containment refusal for the early exit", err)
	}
	if h.released.Load() != 1 {
		t.Fatalf("uid release count = %d, want 1", h.released.Load())
	}
	if _, err := os.Stat(h.jailDir()); !os.IsNotExist(err) {
		t.Fatalf("jail survived the early exit: %v", err)
	}
	if h.modes.refused.Load() != 1 {
		t.Fatalf("refused = %d, want 1", h.modes.refused.Load())
	}
}

func TestSetNewPIDNSPreservesOtherCloneFlags(t *testing.T) {
	cmd := exec.Command("true")
	cmd.SysProcAttr = &syscall.SysProcAttr{Cloneflags: syscall.CLONE_NEWUSER, Unshareflags: syscall.CLONE_NEWNS}
	setNewPIDNS(cmd)
	if cmd.SysProcAttr.Cloneflags != syscall.CLONE_NEWUSER|syscall.CLONE_NEWPID {
		t.Fatalf("clone flags = %#x, want CLONE_NEWUSER|CLONE_NEWPID", cmd.SysProcAttr.Cloneflags)
	}
	if cmd.SysProcAttr.Unshareflags != syscall.CLONE_NEWNS {
		t.Fatalf("unshare flags changed to %#x", cmd.SysProcAttr.Unshareflags)
	}
	bare := exec.Command("true")
	setNewPIDNS(bare)
	if bare.SysProcAttr == nil || bare.SysProcAttr.Cloneflags != syscall.CLONE_NEWPID {
		t.Fatalf("bare command did not get CLONE_NEWPID: %#v", bare.SysProcAttr)
	}
}

// TestJailedLaunchRequestsPIDNamespace pins J8 without privilege: the jailer is
// spawned into a new PID namespace by noded itself, never via --new-pid-ns.
func TestJailedLaunchRequestsPIDNamespace(t *testing.T) {
	h := newJailerHarness(t, nil)
	t.Setenv("EMBER_JAILER_CGROUP_PROCS", filepath.Join(h.cgroupDir, "cgroup.procs"))
	h.launcher.Env = os.Environ()
	var requested uintptr
	var args []string
	h.launcher.jailerPIDNamespace = func(cmd *exec.Cmd) {
		setNewPIDNS(cmd)
		requested = cmd.SysProcAttr.Cloneflags
		args = append([]string(nil), cmd.Args...)
		// Neutralise the flag so the unprivileged runner can start the helper.
		cmd.SysProcAttr.Cloneflags &^= syscall.CLONE_NEWPID
	}
	process, err := h.launch()
	if err != nil {
		t.Fatalf("Launch: %v", err)
	}
	_ = os.Remove(filepath.Join(h.cgroupDir, "cgroup.procs"))
	if err := process.Kill(); err != nil {
		t.Fatalf("Kill: %v", err)
	}
	if requested&syscall.CLONE_NEWPID == 0 {
		t.Fatalf("jailer clone flags %#x lack CLONE_NEWPID", requested)
	}
	if slices.Contains(args, "--new-pid-ns") || slices.Contains(args, "--daemonize") {
		t.Fatalf("jailer args %q ask the jailer to fork the VMM", args)
	}
}

// TestExecLauncherJailerSpawnFailureRefuses pins the fail-closed spawn: when the
// jailer cannot be started in its namespace, nothing is retried without it.
func TestExecLauncherJailerSpawnFailureRefuses(t *testing.T) {
	marker := filepath.Join(t.TempDir(), "started")
	h := newJailerHarness(t, map[string]string{"EMBER_JAILER_MARKER": marker})
	h.launcher.Env = os.Environ()
	h.launcher.jailerPIDNamespace = func(cmd *exec.Cmd) { cmd.Path = filepath.Join(h.cgroupDir, "missing-jailer") }
	process, err := h.launch()
	if process != nil {
		_ = process.Kill()
		t.Fatal("a failed jailer spawn returned a process")
	}
	if !errors.Is(err, ErrJailerContainment) || !errors.Is(err, syscall.ENOENT) {
		t.Fatalf("Launch error = %v, want a containment refusal for the spawn", err)
	}
	if h.released.Load() != 1 {
		t.Fatalf("uid release count = %d, want 1", h.released.Load())
	}
	if _, err := os.Stat(h.jailDir()); !os.IsNotExist(err) {
		t.Fatalf("jail survived the failed spawn: %v", err)
	}
	if _, err := os.Stat(h.cgroupDir); !os.IsNotExist(err) {
		t.Fatalf("cgroup survived the failed spawn: %v", err)
	}
	if _, err := os.Stat(marker); !os.IsNotExist(err) {
		t.Fatalf("something ran after the failed spawn: %v", err)
	}
	if h.modes.refused.Load() != 1 {
		t.Fatalf("refused = %d, want 1", h.modes.refused.Load())
	}
}

// TestExecLauncherPIDNamespaceDeniedFailsClosed is the unprivileged twin of the
// real-namespace test: without CAP_SYS_ADMIN the clone is refused, and so is the
// launch.
func TestExecLauncherPIDNamespaceDeniedFailsClosed(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("root can create PID namespaces; TestExecLauncherJailerLifecycleInPIDNamespace covers it")
	}
	marker := filepath.Join(t.TempDir(), "started")
	h := newJailerHarness(t, map[string]string{"EMBER_JAILER_MARKER": marker})
	h.launcher.Env = os.Environ()
	h.launcher.jailerPIDNamespace = nil // production setNewPIDNS
	process, err := h.launch()
	if process != nil {
		_ = process.Kill()
		t.Skip("this runner can create PID namespaces without root")
	}
	if !errors.Is(err, ErrJailerContainment) || !errors.Is(err, syscall.EPERM) {
		t.Fatalf("Launch error = %v, want a containment refusal with EPERM", err)
	}
	if h.released.Load() != 1 {
		t.Fatalf("uid release count = %d, want 1", h.released.Load())
	}
	if _, err := os.Stat(marker); !os.IsNotExist(err) {
		t.Fatalf("an un-namespaced jailer ran: %v", err)
	}
}

// TestExecLauncherJailerLifecycleInPIDNamespace runs the real CLONE_NEWPID path:
// the VMM is init of its own PID namespace, and Kill and Wait still reap it.
func TestExecLauncherJailerLifecycleInPIDNamespace(t *testing.T) {
	if os.Geteuid() != 0 {
		t.Skip("CLONE_NEWPID requires CAP_SYS_ADMIN")
	}
	pid1 := filepath.Join(t.TempDir(), "pid1")
	h := newJailerHarness(t, map[string]string{"EMBER_JAILER_PID1_MARKER": pid1})
	t.Setenv("EMBER_JAILER_CGROUP_PROCS", filepath.Join(h.cgroupDir, "cgroup.procs"))
	h.launcher.Env = os.Environ()
	h.launcher.jailerPIDNamespace = nil // production setNewPIDNS
	process, err := h.launch()
	if errors.Is(err, syscall.EPERM) {
		t.Skipf("PID namespaces unavailable here: %v", err)
	}
	if err != nil {
		t.Fatalf("Launch: %v", err)
	}
	execProc := process.(*execProcess)
	if got := process.Pid(); got != execProc.cmd.Process.Pid {
		t.Fatalf("Pid() = %d, want the spawned VMM %d", got, execProc.cmd.Process.Pid)
	}
	if got := strings.TrimSpace(string(mustRead(t, pid1))); got != "1" {
		t.Fatalf("VMM saw itself as pid %q, want 1 in its own namespace", got)
	}
	if got := lastNSpid(t, process.Pid()); got != "1" {
		t.Fatalf("NSpid of the VMM ends in %q, want 1", got)
	}
	if h.released.Load() != 0 {
		t.Fatal("uid released while the VMM lives")
	}
	jailDir := execProc.Jail().Dir
	_ = os.Remove(filepath.Join(h.cgroupDir, "cgroup.procs"))
	done := make(chan error, 1)
	go func() { done <- process.Kill() }()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("Kill: %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("Kill hung on a VMM that is init of its own PID namespace")
	}
	var info unix.Siginfo
	if err := unix.Waitid(unix.P_PID, execProc.cmd.Process.Pid, &info, unix.WEXITED|unix.WNOHANG, nil); !errors.Is(err, unix.ECHILD) {
		t.Fatalf("VMM left unreaped: waitid = %v", err)
	}
	if _, err := os.Stat(jailDir); !os.IsNotExist(err) {
		t.Fatalf("jail survived Kill: %v", err)
	}
	if _, err := os.Stat(h.cgroupDir); !os.IsNotExist(err) {
		t.Fatalf("cgroup survived Kill: %v", err)
	}
	if h.released.Load() != 1 {
		t.Fatalf("uid release count = %d, want 1", h.released.Load())
	}
	var exit map[string]any
	for _, record := range decodeLogRecords(t, h.output.Bytes()) {
		if record["msg"] == "driver: vmm exited" {
			exit = record
		}
	}
	if exit == nil || exit["reason"] != "host_requested" || exit["signal"] != "killed" {
		t.Fatalf("exit record = %#v, want host_requested by SIGKILL", exit)
	}
}

// TestExecProcessSpontaneousExitIsReaped pins J14 at the process level: a VMM
// that exits on its own is reaped by the launch watcher, which reports Exited
// only after its jail, cgroup and uid are released, and a later Kill is clean.
func TestExecProcessSpontaneousExitIsReaped(t *testing.T) {
	h := newJailerHarness(t, map[string]string{"EMBER_JAILER_EXIT_AFTER_READY": "1"})
	t.Setenv("EMBER_JAILER_CGROUP_PROCS", filepath.Join(h.cgroupDir, "cgroup.procs"))
	h.launcher.Env = os.Environ()
	process, err := h.launch()
	if err != nil {
		t.Fatalf("Launch: %v", err)
	}
	_ = os.Remove(filepath.Join(h.cgroupDir, "cgroup.procs"))
	execProc := process.(*execProcess)
	deadline := time.Now().Add(10 * time.Second)
	for !execProc.Exited() {
		if time.Now().After(deadline) {
			t.Fatal("watcher never reaped a VMM that exited on its own")
		}
		time.Sleep(10 * time.Millisecond)
	}
	if h.released.Load() != 1 {
		t.Fatalf("uid release count = %d, want 1 once reaped", h.released.Load())
	}
	if _, err := os.Stat(h.cgroupDir); !os.IsNotExist(err) {
		t.Fatalf("cgroup survived the reap: %v", err)
	}
	if err := process.Kill(); err != nil {
		t.Fatalf("Kill after a spontaneous exit: %v", err)
	}
	if h.released.Load() != 1 {
		t.Fatalf("uid released twice: %d", h.released.Load())
	}
}

// TestExecLauncherDirectLaunchCountsDirectExec pins the legacy-bundle rule: a
// bundle with no jail resource metadata names no rootfs the jail could stage,
// so it can only run direct. A disarmed launcher execs it like every guest and
// counts it as direct_exec; an armed launcher refuses it with
// ErrJailerContainment, counts and logs the refusal, and never execs anything.
func TestExecLauncherDirectLaunchCountsDirectExec(t *testing.T) {
	t.Setenv("EMBER_JAILER_HELPER", "1")
	t.Setenv("EMBER_JAILER_TEST_BINARY", os.Args[0])
	tmp := t.TempDir()
	wrapper := filepath.Join(tmp, "firecracker-helper")
	script := "#!/bin/sh\nexec \"$EMBER_JAILER_TEST_BINARY\" -test.run=TestExecLauncherHelperProcess -- \"$@\"\n"
	if err := os.WriteFile(wrapper, []byte(script), 0o700); err != nil {
		t.Fatal(err)
	}
	launch := func(t *testing.T, armed bool, modes *launchModeCounters) (Process, string, error) {
		t.Helper()
		dir := t.TempDir()
		marker := filepath.Join(dir, "exec-marker")
		t.Setenv("EMBER_JAILER_MARKER", marker)
		launcher := &ExecLauncher{
			Bin:           wrapper,
			Env:           os.Environ(),
			Logger:        slog.New(slog.NewJSONHandler(&lockedBuffer{}, nil)),
			JailerEnabled: armed,
			ReadyTimeout:  2 * time.Second,
			launchModes:   modes,
		}
		process, err := launcher.Launch(context.Background(), LaunchSpec{
			VMID: "legacy", SocketPath: filepath.Join(dir, "api.sock"),
			Workload: "legacy-workload", Phase: guestPhaseVM, DirectExec: true,
		})
		return process, marker, err
	}

	t.Run("disarmed", func(t *testing.T) {
		var modes launchModeCounters
		process, marker, err := launch(t, false, &modes)
		if err != nil {
			t.Fatalf("Launch: %v", err)
		}
		if err := process.Kill(); err != nil {
			t.Fatalf("Kill: %v", err)
		}
		if _, err := os.Stat(marker); err != nil {
			t.Fatalf("disarmed direct launch never execed the VMM: %v", err)
		}
		if modes.directExec.Load() != 1 || modes.jailed.Load() != 0 || modes.refused.Load() != 0 {
			t.Fatalf("launch modes jailed=%d direct=%d refused=%d, want one direct exec",
				modes.jailed.Load(), modes.directExec.Load(), modes.refused.Load())
		}
	})

	t.Run("armed", func(t *testing.T) {
		var logs lockedBuffer
		previous := slog.Default()
		slog.SetDefault(slog.New(slog.NewJSONHandler(&logs, nil)))
		t.Cleanup(func() { slog.SetDefault(previous) })
		var modes launchModeCounters
		process, marker, err := launch(t, true, &modes)
		if process != nil || !errors.Is(err, ErrJailerContainment) || !strings.Contains(err.Error(), "legacy bundle without jail resource metadata") {
			t.Fatalf("armed launch of a legacy bundle = (%v, %v), want an ErrJailerContainment refusal naming the legacy bundle", process, err)
		}
		if _, err := os.Stat(marker); !os.IsNotExist(err) {
			t.Fatalf("armed launcher execed a legacy bundle it refused: %v", err)
		}
		if modes.refused.Load() != 1 || modes.directExec.Load() != 0 || modes.jailed.Load() != 0 {
			t.Fatalf("launch modes jailed=%d direct=%d refused=%d, want one refusal",
				modes.jailed.Load(), modes.directExec.Load(), modes.refused.Load())
		}
		logged := false
		for _, record := range decodeLogRecords(t, logs.Bytes()) {
			if record["msg"] == "driver: refusing jailed launch" && record["vm"] == "legacy" {
				logged = true
			}
		}
		if !logged {
			t.Fatalf("refusal was not logged: %s", logs.Bytes())
		}
	})
}

func mustRead(t *testing.T, path string) []byte {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

// lastNSpid returns the innermost pid of a process: the last NSpid field.
func lastNSpid(t *testing.T, pid int) string {
	t.Helper()
	for _, line := range strings.Split(string(mustRead(t, "/proc/"+strconv.Itoa(pid)+"/status")), "\n") {
		if fields := strings.Fields(line); len(fields) >= 2 && fields[0] == "NSpid:" {
			return fields[len(fields)-1]
		}
	}
	t.Fatalf("no NSpid line for pid %d", pid)
	return ""
}
