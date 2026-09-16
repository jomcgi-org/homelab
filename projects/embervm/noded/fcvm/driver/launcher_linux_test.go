//go:build linux

package driver

import (
	"context"
	"errors"
	"log/slog"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"syscall"
	"testing"
	"time"
)

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
	released := 0
	var output lockedBuffer
	launcher := &ExecLauncher{
		Logger:        slog.New(slog.NewJSONHandler(&output, nil)),
		Bin:           os.Args[0],
		JailerBin:     wrapper,
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
	if slices.Contains(execProc.cmd.Args, "--new-pid-ns") {
		t.Fatal("jailed launch unexpectedly enabled a PID namespace")
	}
	jailDir := execProc.Jail().Dir
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
	if len(records) != len(want) {
		t.Fatalf("Firecracker records = %d, want %d: %s", len(records), len(want), output.Bytes())
	}
	for _, record := range records {
		message, _ := record["msg"].(string)
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
	if len(records) != 4 {
		t.Fatalf("records = %d, want 4 after readiness cleanup: %s", len(records), output.Bytes())
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

func TestExecLauncherCgroupFailureCleansJailAndFallsBack(t *testing.T) {
	t.Setenv("EMBER_JAILER_HELPER", "1")
	t.Setenv("EMBER_JAILER_TEST_BINARY", os.Args[0])
	tmp := t.TempDir()
	wrapper := filepath.Join(tmp, "firecracker-helper")
	script := "#!/bin/sh\nexec \"$EMBER_JAILER_TEST_BINARY\" -test.run=TestExecLauncherHelperProcess -- \"$@\"\n"
	if err := os.WriteFile(wrapper, []byte(script), 0o700); err != nil {
		t.Fatal(err)
	}
	released := 0
	launcher := &ExecLauncher{
		Bin:           wrapper,
		JailerBin:     wrapper,
		JailerEnabled: true,
		ReadyTimeout:  2 * time.Second,
		allocateJailUID: func() (int, func(), error) {
			return os.Getuid(), func() { released++ }, nil
		},
		createCgroup: func(string, int64) (*vmCgroup, error) {
			return nil, errors.New("injected cgroup failure")
		},
	}
	socket := filepath.Join(tmp, "bundle", "api.sock")
	if err := os.MkdirAll(filepath.Dir(socket), 0o755); err != nil {
		t.Fatal(err)
	}
	process, err := launcher.Launch(context.Background(), LaunchSpec{VMID: "vm-fallback", SocketPath: socket, Workload: "test", Phase: guestPhaseVM, MemMib: 256})
	if err != nil {
		t.Fatalf("Launch fallback: %v", err)
	}
	if process.(*execProcess).Jail() != nil {
		t.Fatal("cgroup failure fallback retained a jail")
	}
	failedJail := filepath.Join(filepath.Dir(socket), "jailer", filepath.Base(wrapper), "vm-fallback")
	if _, err := os.Stat(failedJail); !os.IsNotExist(err) {
		t.Fatalf("failed jail setup remains after fallback: %v", err)
	}
	if err := process.Kill(); err != nil {
		t.Fatalf("Kill: %v", err)
	}
	if released != 1 {
		t.Fatalf("uid release count = %d, want 1", released)
	}
}

func TestExecLauncherHelperProcess(t *testing.T) {
	if os.Getenv("EMBER_JAILER_HELPER") != "1" {
		return
	}
	_, _ = os.Stdout.Write([]byte("stdout line\nstdout tail"))
	_, _ = os.Stderr.Write([]byte("stderr line\nstderr tail"))
	if os.Getenv("EMBER_JAILER_NO_SOCKET") == "1" {
		select {}
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
	select {}
}

func argumentValue(args []string, name string) string {
	for i := len(args) - 2; i >= 0; i-- {
		if args[i] == name {
			return args[i+1]
		}
	}
	return ""
}
