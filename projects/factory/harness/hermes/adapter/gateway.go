package adapter

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"syscall"
	"time"
)

// DefaultCommand starts the gateway from the pinned Hermes image.
var DefaultCommand = []string{"/opt/hermes/.venv/bin/hermes", "gateway"}

// passEnv names the variables the gateway inherits. Everything else is
// dropped, so a messaging token or HERMES_YOLO_MODE in the Actor environment
// cannot enable a platform or skip approvals.
var passEnv = map[string]bool{
	"PATH": true, "HOME": true, "LANG": true, "LC_ALL": true, "TZ": true, "TMPDIR": true,
	// Substrate's egress gateway trust bundle (kagent actor_template.go).
	"SSL_CERT_FILE": true, "SSL_CERT_DIR": true, "REQUESTS_CA_BUNDLE": true, "AWS_CA_BUNDLE": true,
	"NODE_EXTRA_CA_CERTS": true, "CURL_CA_BUNDLE": true, "GIT_SSL_CAINFO": true,
	// The Hermes image's own runtime settings.
	"PYTHONPATH": true, "PYTHONUNBUFFERED": true, "PYTHONDONTWRITEBYTECODE": true, "PYTHONPYCACHEPREFIX": true,
	"PLAYWRIGHT_BROWSERS_PATH": true, "XDG_RUNTIME_DIR": true,
	"HERMES_PYTHON": true, "HERMES_RUNTIME_DIR": true, "HERMES_SITE": true, "HERMES_WEB_DIST": true,
	"HERMES_TUI_DIR": true, "HERMES_WRITE_SAFE_ROOT": true,
}

// Gateway is one `hermes gateway` process with only the API server enabled.
type Gateway struct {
	Command []string
	Home    string
	Port    int
	Key     string
	cmd     *exec.Cmd
	exited  chan struct{}
	exitErr error
}

// NewKey returns a random API server key. Hermes refuses keys shorter than 16
// characters.
func NewKey() (string, error) {
	b := make([]byte, 32)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return hex.EncodeToString(b), nil
}

// Environment returns the gateway's environment from the adapter's own.
func (g *Gateway) Environment(parent []string) []string {
	env := make([]string, 0, len(parent)+6)
	for _, kv := range parent {
		name, _, _ := strings.Cut(kv, "=")
		if passEnv[name] || strings.HasPrefix(name, "OTEL_") {
			env = append(env, kv)
		}
	}
	return append(env,
		"HERMES_HOME="+g.Home,
		"API_SERVER_ENABLED=true",
		"API_SERVER_HOST=127.0.0.1",
		"API_SERVER_PORT="+strconv.Itoa(g.Port),
		"API_SERVER_KEY="+g.Key,
	)
}

// URL is the API server's loopback base URL.
func (g *Gateway) URL() string {
	return "http://127.0.0.1:" + strconv.Itoa(g.Port)
}

// Start runs the gateway. Done reports its exit.
func (g *Gateway) Start() error {
	if len(g.Command) == 0 {
		return fmt.Errorf("hermes: gateway command is empty")
	}
	cmd := exec.Command(g.Command[0], g.Command[1:]...)
	cmd.Env = g.Environment(os.Environ())
	cmd.Dir = g.Home
	cmd.Stdout = os.Stderr
	cmd.Stderr = os.Stderr
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if err := cmd.Start(); err != nil {
		return fmt.Errorf("hermes: start gateway: %w", err)
	}
	g.cmd = cmd
	g.exited = make(chan struct{})
	go func() {
		g.exitErr = cmd.Wait()
		close(g.exited)
	}()
	return nil
}

// Exited is closed when the gateway process ends.
func (g *Gateway) Exited() <-chan struct{} { return g.exited }

// ExitErr is the gateway's exit status. Read it only after Exited is closed.
func (g *Gateway) ExitErr() error { return g.exitErr }

// Stop sends SIGTERM to the gateway's process group, then SIGKILL after grace.
func (g *Gateway) Stop(grace time.Duration) {
	if g.cmd == nil || g.cmd.Process == nil {
		return
	}
	pgid := -g.cmd.Process.Pid
	_ = syscall.Kill(pgid, syscall.SIGTERM)
	select {
	case <-g.exited:
	case <-time.After(grace):
		_ = syscall.Kill(pgid, syscall.SIGKILL)
		<-g.exited
	}
}

// Healthy reports whether the API server answers GET /health.
func (g *Gateway) Healthy(ctx context.Context) bool {
	ctx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, g.URL()+"/health", nil)
	if err != nil {
		return false
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return false
	}
	resp.Body.Close()
	return resp.StatusCode == http.StatusOK
}

// WaitHealthy polls Healthy until it passes, the gateway exits or ctx ends.
func (g *Gateway) WaitHealthy(ctx context.Context) error {
	tick := time.NewTicker(250 * time.Millisecond)
	defer tick.Stop()
	for {
		if g.Healthy(ctx) {
			return nil
		}
		select {
		case <-g.exited:
			return fmt.Errorf("hermes: gateway exited before it was healthy: %v", g.exitErr)
		case <-ctx.Done():
			return fmt.Errorf("hermes: gateway not healthy: %w", ctx.Err())
		case <-tick.C:
		}
	}
}
