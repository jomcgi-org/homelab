// Package handler implements the shim.Handler for the goosecracker agent guest.
// It bridges the fc-invoke HTTP-over-vsock substrate (ADR 030) to a single cold
// goose run: decode an AgentRequest, optionally clone a git mirror into the
// workspace, build the goose argv via harness.GooseCommand, run it with the
// caller-supplied model env while streaming each output line to a progress URL,
// and return the captured output as an AgentResult.
//
// Scope is the cold e2e path (recipe + task -> goose -> result). Session resume,
// artifact publish, and secret-swap are deliberately deferred; the Runner seam
// and the AgentRequest fields (Session, GitRef, ...) leave room to slot them in
// without reshaping the contract.
package handler

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"time"

	"github.com/jomcgi/homelab/projects/firecracker/goosecracker/guest-init/internal/harness"
	"github.com/jomcgi/homelab/projects/firecracker/substrate/shim"
)

// Workspace is the guest directory goose runs in and a git mirror is cloned into.
// It is the single source of truth shared by the handler (the clone destination)
// and the production Runner (goose's working directory), so the two never
// disagree about where the workspace lives.
const Workspace = "/workspace"

// AgentRequest is the JSON body of an /invoke call. It carries everything a cold
// goose run needs: the recipe + task, the model provider/base-url env the guest
// cannot hardcode, an optional progress URL to stream to, and an optional git
// mirror/ref to seed the workspace. Session is an opaque id used only for goose
// session naming today (resume is deferred).
type AgentRequest struct {
	Recipe      string            `json:"recipe"`      // goose recipe name (e.g. "agent")
	Task        string            `json:"task"`        // task description fed to the recipe
	Session     string            `json:"session"`     // opaque session id (goose --name; resume deferred)
	Env         map[string]string `json:"env"`         // model provider/base-url/tier env to inject
	ProgressURL string            `json:"progressUrl"` // optional: POST progress lines here mid-run
	GitMirror   string            `json:"gitMirror"`   // optional: clone this mirror into the workspace
	GitRef      string            `json:"gitRef"`      // optional: checkout ref after clone
}

// AgentResult is the JSON body of a successful /invoke response. Status is "ok"
// when goose ran to completion and "error" when the clone or goose run failed;
// both are returned at HTTP 200, because a run that ran but failed is data, not
// a transport error. Only an undecodable request body yields a handler error
// (which the shim maps to 502).
type AgentResult struct {
	Status string `json:"status"`           // "ok" | "error"
	Result string `json:"result,omitempty"` // goose output captured from the run
	Error  string `json:"error,omitempty"`  // failure detail when Status == "error"
}

// Runner is the seam the handler uses to run goose and (optionally) clone a git
// mirror. The production implementation shells goose and git via os/exec; tests
// inject a fake so the handler is exercised with no goose/git binary present.
type Runner interface {
	// Run executes argv with env overlaid on the process environment, invoking
	// onLine for each output line as it is produced, and returns the full
	// captured output. A non-nil error means goose ran but exited non-zero.
	Run(ctx context.Context, argv []string, env map[string]string, onLine func(string)) (string, error)
	// Clone replicates the repository at mirror into dest and checks out ref.
	Clone(ctx context.Context, mirror, ref, dest string) error
}

// New returns a shim.Handler for a single cold goose run. It decodes an
// AgentRequest, clones the git mirror (when set) into Workspace, builds the goose
// command via harness.GooseCommand, and runs it through runner while streaming
// each output line to the request's progress URL. The result is marshalled as an
// AgentResult at HTTP 200; only an undecodable body returns an error.
func New(runner Runner) shim.Handler {
	return func(ctx context.Context, r *shim.Request) (*shim.Response, error) {
		var req AgentRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			return nil, fmt.Errorf("handler: decode agent request: %w", err)
		}

		// Seed the workspace from the git mirror before goose starts, so the recipe
		// operates on a checked-out tree. A clone failure is a run failure, not a
		// transport error, so it is reported as an error result at 200.
		if req.GitMirror != "" {
			if err := runner.Clone(ctx, req.GitMirror, req.GitRef, Workspace); err != nil {
				return jsonResult(AgentResult{Status: "error", Error: fmt.Sprintf("clone %s: %v", req.GitMirror, err)})
			}
		}

		argv := harness.GooseCommand(harness.Config{
			Recipe:      req.Recipe,
			Task:        req.Task,
			SessionName: req.Session,
		})
		if len(argv) == 0 {
			return jsonResult(AgentResult{Status: "error", Error: "no recipe or task supplied"})
		}

		poster := newProgressPoster(req.ProgressURL)
		onLine := func(line string) {
			slog.Info("goose", "line", line)
			poster.post(line)
		}

		out, runErr := runner.Run(ctx, argv, req.Env, onLine)
		result := AgentResult{Status: "ok", Result: out}
		if runErr != nil {
			result.Status = "error"
			result.Error = runErr.Error()
		}
		return jsonResult(result)
	}
}

// jsonResult marshals res into a 200 shim.Response. A marshal failure (which
// should be impossible for AgentResult) is the only non-decode handler error.
func jsonResult(res AgentResult) (*shim.Response, error) {
	body, err := json.Marshal(res)
	if err != nil {
		return nil, fmt.Errorf("handler: marshal result: %w", err)
	}
	return &shim.Response{Status: 200, Body: body}, nil
}

// progressPoster POSTs goose output lines to the tier's progress URL as they are
// produced. It is best-effort and fire-and-forget: a nil poster, an empty URL,
// or any HTTP error is silently ignored so a slow or broken progress endpoint can
// never stall or fail the goose run. Batching/async delivery is a later
// optimization; the cold path posts one line at a time.
type progressPoster struct {
	url    string
	client *http.Client
}

func newProgressPoster(url string) *progressPoster {
	return &progressPoster{url: url, client: &http.Client{Timeout: 5 * time.Second}}
}

func (p *progressPoster) post(line string) {
	if p == nil || p.url == "" {
		return
	}
	payload, err := json.Marshal(map[string]string{"chunk": line})
	if err != nil {
		return
	}
	resp, err := p.client.Post(p.url, "application/json", bytes.NewReader(payload))
	if err != nil {
		return
	}
	_ = resp.Body.Close()
}
