package mandoline

import (
	"bytes"
	"context"
	"errors"
	"os"
	"os/exec"
	"sync"
	"syscall"
	"time"
)

func capture(parent context.Context, program, root string, env, args []string) captured {
	ctx, stop := context.WithTimeout(parent, 45*time.Second)
	defer stop()
	cmd := exec.CommandContext(ctx, program, args...)
	cmd.Dir, cmd.Env = root, env
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true, Pdeathsig: syscall.SIGKILL}
	cmd.Cancel = func() error {
		err := syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
		if errors.Is(err, syscall.ESRCH) {
			return os.ErrProcessDone
		}
		return err
	}
	cmd.WaitDelay = time.Second
	budget := captureBudget{left: outputLimit, stop: stop}
	stdout := boundedStream{budget: &budget}
	stderr := boundedStream{budget: &budget}
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	err := cmd.Run()
	if cmd.Process != nil {
		_ = cmd.Cancel() // Stop any child remaining after the analyzer exits.
	}
	if ctx.Err() != nil {
		err = ctx.Err()
	}
	return captured{stdout.buf.Bytes(), stderr.buf.Bytes(), err, budget.truncated}
}

type captureBudget struct {
	mu        sync.Mutex
	left      int
	stop      context.CancelFunc
	truncated bool
}

type boundedStream struct {
	budget *captureBudget
	buf    bytes.Buffer
}

func (w *boundedStream) Write(p []byte) (int, error) {
	w.budget.mu.Lock()
	defer w.budget.mu.Unlock()
	n := min(len(p), w.budget.left)
	w.buf.Write(p[:n])
	w.budget.left -= n
	if n < len(p) {
		w.budget.truncated = true
		w.budget.stop()
	}
	return len(p), nil
}
