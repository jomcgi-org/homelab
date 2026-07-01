// Package handler implements the shim.Handler for the goosecracker agent guest.
// It bridges the fc-invoke HTTP-over-vsock substrate (ADR 030) to a single cold
// goose run: decode an AgentRequest, optionally clone a git mirror into the
// workspace, build the goose argv via harness.GooseCommand, run it with the
// caller-supplied model env while streaming each output line to a progress URL,
// and return the captured output as an AgentResult.
//
// Scope covers the cold path (recipe + task -> goose -> result) and stateful
// resume (ADR 026 Phase 2): when the caller ships a prior goose sessions.db the
// handler hydrates it, runs `goose run --resume`, and exports the updated db back
// so the next reply can resume again. Artifact publish is still deferred; the
// Runner and SessionStore seams leave room to slot it in without reshaping the
// contract.
package handler

import (
	"bytes"
	"context"
	"encoding/base64"
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

// AgentRequest is the JSON body of an /invoke call. It carries everything a goose
// run needs: the recipe + task, the model provider/base-url env the guest cannot
// hardcode, an optional progress URL to stream to, an optional git mirror/ref to
// seed the workspace, and (for ADR 026 Phase 2 resume) the prior goose sessions.db
// plus a Resume flag. Session is the goose --name; on resume it selects which
// session to replay.
type AgentRequest struct {
	Recipe      string            `json:"recipe"`      // goose recipe name (e.g. "agent")
	Task        string            `json:"task"`        // task description fed to the recipe
	Session     string            `json:"session"`     // goose --name (selects the session to resume)
	Env         map[string]string `json:"env"`         // model provider/base-url/tier env to inject
	ProgressURL string            `json:"progressUrl"` // optional: POST progress lines here mid-run
	GitMirror   string            `json:"gitMirror"`   // optional: clone this mirror into the workspace
	GitRef      string            `json:"gitRef"`      // optional: checkout ref after clone
	Resume      bool              `json:"resume"`      // resume the prior session instead of a cold recipe run
	SessionDb   string            `json:"sessionDb"`   // optional: base64 prior sessions.db to hydrate before resume
}

// AgentResult is the JSON body of a successful /invoke response. Status is "ok"
// when goose ran to completion and "error" when the clone or goose run failed;
// both are returned at HTTP 200, because a run that ran but failed is data, not
// a transport error. Only an undecodable request body yields a handler error
// (which the shim maps to 502). SessionDb carries the updated goose session back
// (base64) so the orchestrator can persist it for the next resume.
type AgentResult struct {
	Status    string `json:"status"`              // "ok" | "error"
	Result    string `json:"result,omitempty"`    // goose output captured from the run
	Error     string `json:"error,omitempty"`     // failure detail when Status == "error"
	SessionDb string `json:"sessionDb,omitempty"` // base64 updated sessions.db to persist
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

// SessionStore hydrates and exports goose's SQLite session db for stateful resume
// (ADR 026 Phase 2). The production impl writes/reads the guest filesystem and
// folds goose's WAL into the db before export; tests inject a fake. It is optional
// (via WithSessionStore): without one the handler runs the cold path only.
type SessionStore interface {
	// Hydrate writes the prior sessions.db bytes into the guest so
	// `goose run --resume` finds the earlier conversation.
	Hydrate(ctx context.Context, data []byte) error
	// Export returns the current sessions.db bytes (WAL folded in), or nil when no
	// session exists (nothing to persist).
	Export(ctx context.Context) ([]byte, error)
}

// Option configures the handler built by New.
type Option func(*config)

type config struct {
	store SessionStore
}

// WithSessionStore installs the store used to hydrate/export goose's sessions.db
// for resume (ADR 026 Phase 2). Without it the handler runs the cold path only.
func WithSessionStore(s SessionStore) Option {
	return func(c *config) { c.store = s }
}

// New returns a shim.Handler for a goose run. It decodes an AgentRequest, clones
// the git mirror (when set) into Workspace, hydrates a prior goose session for
// resume (when a store is configured and the request carries one), builds the
// goose command via harness.GooseCommand, and runs it through runner while
// streaming each output line to the request's progress URL. On success it exports
// the updated sessions.db back in the result. The result is marshalled as an
// AgentResult at HTTP 200; only an undecodable body returns an error.
func New(runner Runner, opts ...Option) shim.Handler {
	cfg := &config{}
	for _, o := range opts {
		o(cfg)
	}
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

		// Hydrate the prior goose session for resume (ADR 026 Phase 2). On any
		// failure fall back to a cold run (resume=false): a corrupt or unreadable db
		// must never fail the run, per the ADR's always-fall-back-to-cold rule.
		resume := req.Resume
		if cfg.store != nil && req.SessionDb != "" {
			if raw, err := base64.StdEncoding.DecodeString(req.SessionDb); err != nil {
				slog.Warn("handler: session db not decodable; cold run", "err", err)
				resume = false
			} else if err := cfg.store.Hydrate(ctx, raw); err != nil {
				slog.Warn("handler: session hydrate failed; cold run", "err", err)
				resume = false
			}
		}

		argv := harness.GooseCommand(harness.Config{
			Recipe:      req.Recipe,
			Task:        req.Task,
			SessionName: req.Session,
			Resume:      resume,
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
			return jsonResult(result)
		}

		// Export the updated session so the next reply can resume it. Best-effort:
		// an export failure does not fail a run that already succeeded.
		if cfg.store != nil {
			if db, err := cfg.store.Export(ctx); err != nil {
				slog.Warn("handler: session export failed", "err", err)
			} else if len(db) > 0 {
				result.SessionDb = base64.StdEncoding.EncodeToString(db)
			}
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
