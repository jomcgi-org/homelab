//go:build linux

package driver

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

func TestExecProcessAlreadyExitedBeforeKill(t *testing.T) {
	for _, panicMarker := range []bool{false, true} {
		t.Run(fmt.Sprint(panicMarker), func(t *testing.T) {
			cmd := exec.Command(os.Args[0], "-test.run=^TestExecOutputHelperProcess$")
			cmd.Env = append(os.Environ(), "EMBER_OUTPUT_HELPER=1")
			if err := cmd.Start(); err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() { _ = cmd.Process.Kill(); _ = cmd.Wait() })
			// Observe death without reaping: Release can arrive before its watcher
			// has cached Wait, including while cmd.Wait drains stdout/stderr.
			var info unix.Siginfo
			if err := unix.Waitid(unix.P_PID, cmd.Process.Pid, &info, unix.WEXITED|unix.WNOWAIT, nil); err != nil {
				t.Fatal(err)
			}
			path := filepath.Join(t.TempDir(), "serial.log")
			serial := "normal exit"
			want := ExitUnclassified
			if panicMarker {
				serial = "Kernel panic - not syncing"
				want = ExitGuestKernelPanic
			}
			if err := os.WriteFile(path, []byte(serial), 0o600); err != nil {
				t.Fatal(err)
			}
			var output lockedBuffer
			var got ExitReason
			p := &execProcess{
				cmd: cmd, serialPath: path,
				logger: slog.New(slog.NewJSONHandler(&output, nil)),
				onExit: func(reason ExitReason) { got = reason },
			}
			if err := p.Kill(); err != nil {
				t.Fatal(err)
			}
			if got != want {
				t.Fatalf("reason = %q, want %q for already exited child", got, want)
			}
			records := decodeLogRecords(t, output.Bytes())
			if len(records) != 1 || records[0]["exit_code"] != float64(0) {
				t.Fatalf("exit event = %v", records)
			}
		})
	}
}

func TestSetUnshareMountNSPreservesOtherCloneFlags(t *testing.T) {
	cmd := exec.Command("true")
	cmd.SysProcAttr = &syscall.SysProcAttr{Cloneflags: syscall.CLONE_NEWUSER}
	setUnshareMountNS(cmd)
	if cmd.SysProcAttr.Cloneflags != syscall.CLONE_NEWUSER {
		t.Fatalf("clone flags changed to %#x", cmd.SysProcAttr.Cloneflags)
	}
	if cmd.SysProcAttr.Unshareflags&syscall.CLONE_NEWNS == 0 {
		t.Fatalf("unshare flags %#x do not contain CLONE_NEWNS", cmd.SysProcAttr.Unshareflags)
	}
}

func TestExecLauncherJailerLifecycle(t *testing.T) {
	t.Setenv("EMBER_JAILER_HELPER", "1")
	t.Setenv("EMBER_JAILER_TEST_BINARY", os.Args[0])
	tmp := t.TempDir()
	wrapper := filepath.Join(tmp, "jailer-helper")
	script := "#!/bin/sh\nexec \"$EMBER_JAILER_TEST_BINARY\" -test.run=TestExecLauncherHelperProcess -- \"$@\"\n"
	if err := os.WriteFile(wrapper, []byte(script), 0o700); err != nil {
		t.Fatal(err)
	}
	cgroupDir := filepath.Join(tmp, "cgroup", "vm-test")
	if err := os.MkdirAll(filepath.Join(cgroupDir, "firecracker", "vm-test"), 0o755); err != nil {
		t.Fatal(err)
	}
	// The helper joins its leaf the way the jailer does, so the launcher's
	// membership check sees the VMM in its cgroup.
	t.Setenv("EMBER_JAILER_CGROUP_PROCS", filepath.Join(cgroupDir, "cgroup.procs"))
	released := 0
	var output lockedBuffer
	var modes launchModeCounters
	launcher := &ExecLauncher{
		Logger:        slog.New(slog.NewJSONHandler(&output, nil)),
		Bin:           os.Args[0],
		JailerBin:     wrapper,
		Env:           os.Environ(),
		JailerEnabled: true,
		ReadyTimeout:  2 * time.Second,
		allocateJailUID: func() (int, func(), error) {
			return os.Getuid(), func() { released++ }, nil
		},
		createCgroup: func(vmID string, limitBytes int64) (*vmCgroup, error) {
			if vmID != "vm-test" || limitBytes != memoryLimitBytes(256) {
				t.Fatalf("cgroup request vm=%q limit=%d", vmID, limitBytes)
			}
			return &vmCgroup{dir: cgroupDir, parentArg: "test-parent/vm-test"}, nil
		},
		// CLONE_NEWPID needs CAP_SYS_ADMIN; the namespace itself is covered by
		// TestExecLauncherJailerLifecycleInPIDNamespace.
		jailerPIDNamespace: func(*exec.Cmd) {},
		launchModes:        &modes,
	}
	socket := filepath.Join(tmp, "bundle", "api.sock")
	if err := os.MkdirAll(filepath.Dir(socket), 0o755); err != nil {
		t.Fatal(err)
	}
	process, err := launcher.Launch(context.Background(), LaunchSpec{VMID: "vm-test", SocketPath: socket, Workload: "test", Phase: guestPhaseVM, MemMib: 256})
	if err != nil {
		t.Fatalf("Launch: %v", err)
	}
	execProc := process.(*execProcess)
	if execProc.Jail() == nil {
		t.Fatal("jailed launch returned no jail mapper")
	}
	// noded creates the PID namespace itself; the jailer's --new-pid-ns would
	// fork the VMM away from the process noded waits on and kills.
	if slices.Contains(execProc.cmd.Args, "--new-pid-ns") {
		t.Fatal("jailed launch asked the jailer to fork into a PID namespace")
	}
	if modes.jailed.Load() != 1 || modes.directExec.Load() != 0 || modes.refused.Load() != 0 {
		t.Fatalf("launch modes jailed=%d direct=%d refused=%d, want one jailed",
			modes.jailed.Load(), modes.directExec.Load(), modes.refused.Load())
	}
	jailDir := execProc.Jail().Dir
	// cgroupfs control files are virtual and vanish with the leaf; drop the
	// plain-file fixture so cleanup's rmdir sees what it would on cgroupfs.
	if err := os.Remove(filepath.Join(cgroupDir, "cgroup.procs")); err != nil {
		t.Fatal(err)
	}
	if err := process.Kill(); err != nil {
		t.Fatalf("Kill: %v", err)
	}
	if _, err := os.Stat(jailDir); !os.IsNotExist(err) {
		t.Fatalf("jail dir survived process cleanup: %v", err)
	}
	if _, err := os.Stat(cgroupDir); !os.IsNotExist(err) {
		t.Fatalf("cgroup tree survived process cleanup: %v", err)
	}
	if released != 1 {
		t.Fatalf("uid release count = %d, want 1", released)
	}
	records := decodeLogRecords(t, output.Bytes())
	want := map[string]string{
		"stdout line": "stdout",
		"stdout tail": "stdout",
		"stderr line": "stderr",
		"stderr tail": "stderr",
	}
	if len(records) != len(want)+1 {
		t.Fatalf("records = %d, want %d including exit event: %s", len(records), len(want)+1, output.Bytes())
	}
	for _, record := range records {
		message, _ := record["msg"].(string)
		if message == "driver: vmm exited" {
			if record["vm"] != "vm-test" || record["workload"] != "test" || record["reason"] != "host_requested" {
				t.Errorf("unexpected exit event: %#v", record)
			}
			continue
		}
		assertTaggedRecord(t, record, message, "firecracker", "test", "vm")
		if got := record["stream"]; got != want[message] {
			t.Errorf("record stream = %#v, want %q: %#v", got, want[message], record)
		}
	}
}

func TestExecProcessWaitFlushesBothStreams(t *testing.T) {
	var output lockedBuffer
	logger := slog.New(slog.NewJSONHandler(&output, nil))
	stdout := newTagWriter(logger.With("stream", "stdout"), "firecracker", "wait-workload", "vm")
	stderr := newTagWriter(logger.With("stream", "stderr"), "firecracker", "wait-workload", "vm")
	cmd := exec.Command(os.Args[0], "-test.run=^TestExecOutputHelperProcess$")
	cmd.Env = append(os.Environ(), "EMBER_OUTPUT_HELPER=1")
	cmd.Stdout = stdout
	cmd.Stderr = stderr
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	proc := &execProcess{cmd: cmd, stdout: stdout, stderr: stderr}
	if err := proc.Wait(); err != nil {
		t.Fatalf("Wait: %v", err)
	}
	records := decodeLogRecords(t, output.Bytes())
	if len(records) != 4 {
		t.Fatalf("records = %d, want 4: %s", len(records), output.Bytes())
	}
}

func TestExecLauncherReadinessFailureFlushesFinalOutput(t *testing.T) {
	t.Setenv("EMBER_JAILER_HELPER", "1")
	t.Setenv("EMBER_JAILER_TEST_BINARY", os.Args[0])
	t.Setenv("EMBER_JAILER_NO_SOCKET", "1")
	tmp := t.TempDir()
	wrapper := filepath.Join(tmp, "firecracker-helper")
	script := "#!/bin/sh\nexec \"$EMBER_JAILER_TEST_BINARY\" -test.run=TestExecLauncherHelperProcess -- \"$@\"\n"
	if err := os.WriteFile(wrapper, []byte(script), 0o700); err != nil {
		t.Fatal(err)
	}
	var output lockedBuffer
	launcher := &ExecLauncher{
		Bin:          wrapper,
		Env:          os.Environ(),
		Logger:       slog.New(slog.NewJSONHandler(&output, nil)),
		ReadyTimeout: 500 * time.Millisecond,
	}
	_, err := launcher.Launch(context.Background(), LaunchSpec{
		VMID: "not-ready", SocketPath: filepath.Join(tmp, "api.sock"),
		Workload: "readiness-workload", Phase: guestPhaseInit,
	})
	if err == nil {
		t.Fatal("Launch should fail when the API socket never appears")
	}
	records := decodeLogRecords(t, output.Bytes())
	if len(records) != 5 {
		t.Fatalf("records = %d, want 5 including exit event after readiness cleanup: %s", len(records), output.Bytes())
	}
}

func TestExecOutputHelperProcess(t *testing.T) {
	if os.Getenv("EMBER_OUTPUT_HELPER") != "1" {
		return
	}
	_, _ = os.Stdout.Write([]byte("stdout line\nstdout tail"))
	_, _ = os.Stderr.Write([]byte("stderr line\nstderr tail"))
	os.Exit(0)
}

// TestExecLauncherCgroupFailureRefusesLaunch pins J5: a jailed launch whose VM
// cgroup cannot be created is refused and cleaned up, never run unjailed.
func TestExecLauncherCgroupFailureRefusesLaunch(t *testing.T) {
	t.Setenv("EMBER_JAILER_HELPER", "1")
	t.Setenv("EMBER_JAILER_TEST_BINARY", os.Args[0])
	tmp := t.TempDir()
	marker := filepath.Join(tmp, "started")
	t.Setenv("EMBER_JAILER_MARKER", marker)
	wrapper := filepath.Join(tmp, "firecracker-helper")
	script := "#!/bin/sh\nexec \"$EMBER_JAILER_TEST_BINARY\" -test.run=TestExecLauncherHelperProcess -- \"$@\"\n"
	if err := os.WriteFile(wrapper, []byte(script), 0o700); err != nil {
		t.Fatal(err)
	}
	released := 0
	injected := errors.New("injected cgroup failure")
	var modes launchModeCounters
	launcher := &ExecLauncher{
		Bin:           wrapper,
		JailerBin:     wrapper,
		Env:           os.Environ(),
		JailerEnabled: true,
		ReadyTimeout:  2 * time.Second,
		allocateJailUID: func() (int, func(), error) {
			return os.Getuid(), func() { released++ }, nil
		},
		createCgroup: func(string, int64) (*vmCgroup, error) {
			return nil, injected
		},
		jailerPIDNamespace: func(*exec.Cmd) {},
		launchModes:        &modes,
	}
	socket := filepath.Join(tmp, "bundle", "api.sock")
	if err := os.MkdirAll(filepath.Dir(socket), 0o755); err != nil {
		t.Fatal(err)
	}
	process, err := launcher.Launch(context.Background(), LaunchSpec{VMID: "vm-fallback", SocketPath: socket, Workload: "test", Phase: guestPhaseVM, MemMib: 256})
	if process != nil {
		_ = process.Kill()
		t.Fatal("cgroup failure still returned a running process")
	}
	if !errors.Is(err, ErrJailerContainment) || !errors.Is(err, injected) {
		t.Fatalf("Launch error = %v, want ErrJailerContainment wrapping the cgroup failure", err)
	}
	failedJail := filepath.Join(filepath.Dir(socket), "jailer", filepath.Base(wrapper), "vm-fallback")
	if _, err := os.Stat(failedJail); !os.IsNotExist(err) {
		t.Fatalf("failed jail setup remains after refusal: %v", err)
	}
	if _, err := os.Lstat(socket); !os.IsNotExist(err) {
		t.Fatalf("API socket alias remains after refusal: %v", err)
	}
	if _, err := os.Stat(marker); !os.IsNotExist(err) {
		t.Fatalf("a refused launch still executed a VMM: %v", err)
	}
	if released != 1 {
		t.Fatalf("uid release count = %d, want 1", released)
	}
	if modes.refused.Load() != 1 || modes.jailed.Load() != 0 || modes.directExec.Load() != 0 {
		t.Fatalf("launch modes jailed=%d direct=%d refused=%d, want one refusal",
			modes.jailed.Load(), modes.directExec.Load(), modes.refused.Load())
	}
}

func TestExecLauncherHelperProcess(t *testing.T) {
	if os.Getenv("EMBER_JAILER_HELPER") != "1" {
		return
	}
	if marker := os.Getenv("EMBER_JAILER_MARKER"); marker != "" {
		_ = os.WriteFile(marker, nil, 0o600)
	}
	if marker := os.Getenv("EMBER_JAILER_PID1_MARKER"); marker != "" {
		_ = os.WriteFile(marker, []byte(strconv.Itoa(os.Getpid())), 0o600)
	}
	if procs := os.Getenv("EMBER_JAILER_CGROUP_PROCS"); procs != "" {
		// Join the leaf as the jailer does. A cgroupfs write is translated into
		// the reader's PID namespace; a plain file is not, so write the pid as
		// noded sees it: the first NSpid field of this process's status.
		if err := os.WriteFile(procs, []byte(hostVisiblePid()+"\n"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	_, _ = os.Stdout.Write([]byte("stdout line\nstdout tail"))
	_, _ = os.Stderr.Write([]byte("stderr line\nstderr tail"))
	if os.Getenv("EMBER_JAILER_EXIT_IMMEDIATELY") == "1" {
		os.Exit(3)
	}
	if os.Getenv("EMBER_JAILER_NO_SOCKET") == "1" {
		for {
			time.Sleep(time.Hour)
		}
	}
	args := os.Args
	base := argumentValue(args, "--chroot-base-dir")
	vmID := argumentValue(args, "--id")
	execFile := argumentValue(args, "--exec-file")
	apiSocket := argumentValue(args, "--api-sock")
	if base != "" {
		apiSocket = filepath.Join(base, filepath.Base(execFile), vmID, "root", apiSocket)
	}
	if err := os.MkdirAll(filepath.Dir(apiSocket), 0o755); err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("unix", apiSocket)
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	if os.Getenv("EMBER_JAILER_EXIT_AFTER_READY") == "1" {
		// A VMM that dies on its own after serving its API socket.
		time.Sleep(300 * time.Millisecond)
		os.Exit(0)
	}
	for {
		time.Sleep(time.Hour)
	}
}

// hostVisiblePid is this process's pid in the PID namespace /proc belongs to:
// the first NSpid field, which equals os.Getpid() outside a nested namespace.
func hostVisiblePid() string {
	b, err := os.ReadFile("/proc/self/status")
	if err == nil {
		for _, line := range strings.Split(string(b), "\n") {
			if fields := strings.Fields(line); len(fields) >= 2 && fields[0] == "NSpid:" {
				return fields[1]
			}
		}
	}
	return strconv.Itoa(os.Getpid())
}

func argumentValue(args []string, name string) string {
	for i := len(args) - 2; i >= 0; i-- {
		if args[i] == name {
			return args[i+1]
		}
	}
	return ""
}
