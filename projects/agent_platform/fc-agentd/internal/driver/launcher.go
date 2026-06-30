package driver

import (
	"context"
	"fmt"
	"log/slog"
	"net"
	"os"
	"os/exec"
	"strconv"
	"time"
)

// ExecLauncher launches real Firecracker processes. It is the production
// Launcher; tests inject a fake. fc-agentd owns FC process supervision (crash
// cleanup, orphan reaping), mirroring E2B's patterns.
type ExecLauncher struct {
	// Bin is the firecracker binary (/opt/kata/bin/firecracker on node-4).
	Bin string
	// ReadyTimeout bounds how long Launch waits for the API socket to accept
	// connections before giving up.
	ReadyTimeout time.Duration
	// OOMScoreAdj, when > 0, is written to the launched Firecracker process's
	// /proc/<pid>/oom_score_adj so the guest is the kernel's first OOM victim
	// under memory pressure, never the daemon (the guests share the daemon's
	// cgroup because they are child processes, not pods). Best-effort: a write
	// failure is logged, not fatal.
	OOMScoreAdj int
}

type execProcess struct {
	cmd *exec.Cmd
}

func (p *execProcess) Kill() error {
	if p.cmd.Process == nil {
		return nil
	}
	killErr := p.cmd.Process.Kill()
	// Always reap the child, even when Kill reports it already exited (a crashed
	// or panicked VM): without a Wait the dead process lingers as a zombie. Wait
	// is the sole reaper, so it is safe to call once here.
	_ = p.cmd.Wait()
	return killErr
}

func (p *execProcess) Wait() error { return p.cmd.Wait() }

// Launch starts firecracker with its API socket at socketPath and blocks until
// the socket is connectable (or the timeout/context fires).
func (l *ExecLauncher) Launch(ctx context.Context, vmID, socketPath string) (Process, error) {
	if l.Bin == "" {
		return nil, fmt.Errorf("driver: ExecLauncher.Bin is empty")
	}
	_ = os.Remove(socketPath)

	cmd := exec.Command(l.Bin, "--api-sock", socketPath, "--id", vmID)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		return nil, fmt.Errorf("driver: start firecracker: %w", err)
	}
	proc := &execProcess{cmd: cmd}

	if l.OOMScoreAdj > 0 {
		if err := setOOMScoreAdj(cmd.Process.Pid, l.OOMScoreAdj); err != nil {
			// Best-effort hardening; the cap still bounds memory if this fails.
			slog.Warn("driver: set firecracker oom_score_adj",
				"vm", vmID, "pid", cmd.Process.Pid, "score", l.OOMScoreAdj, "err", err)
		}
	}

	timeout := l.ReadyTimeout
	if timeout <= 0 {
		timeout = 10 * time.Second
	}
	if err := waitForSocket(ctx, socketPath, timeout); err != nil {
		_ = proc.Kill()
		return nil, err
	}
	return proc, nil
}

// setOOMScoreAdj writes score to /proc/<pid>/oom_score_adj, biasing the kernel
// OOM killer to pick this process first. Valid range is -1000..1000; the daemon
// must have permission (fc-agentd runs privileged on node-4).
func setOOMScoreAdj(pid, score int) error {
	path := fmt.Sprintf("/proc/%d/oom_score_adj", pid)
	return os.WriteFile(path, []byte(strconv.Itoa(score)), 0o644)
}

func waitForSocket(ctx context.Context, socketPath string, timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	for {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		conn, err := net.DialTimeout("unix", socketPath, 200*time.Millisecond)
		if err == nil {
			_ = conn.Close()
			return nil
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("driver: firecracker API socket %q not ready after %s", socketPath, timeout)
		}
		time.Sleep(20 * time.Millisecond)
	}
}
