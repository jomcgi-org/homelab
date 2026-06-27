package driver

import (
	"context"
	"fmt"
	"net"
	"os"
	"os/exec"
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
}

type execProcess struct {
	cmd *exec.Cmd
}

func (p *execProcess) Kill() error {
	if p.cmd.Process == nil {
		return nil
	}
	if err := p.cmd.Process.Kill(); err != nil {
		return err
	}
	_ = p.cmd.Wait()
	return nil
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
