package review

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/github"
	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
)

// ownership indexes unfinished checks independently of the open PR listing.
// Binding retains the original runtime identity if configuration changes.
// The journal lives on a kept PVC; only one reviewer process may write it.
type ownership struct {
	Binding Binding
	PR      github.PullRequest
	Run     github.CheckRun
	Pending *pendingSend
}

// The exact message is durable before Send: task histories can identify the
// original execution even if saving the returned task ID fails. Its receipt
// may expire, so an unresolved send must never be replayed.
type pendingSend struct {
	Message    kagent.Message
	Attempt    int
	Sent       time.Time
	QuotaRetry int
	Issues     string
	Semgrep    string
}

func (e *Engine) loadOwnership() {
	e.owned = map[headKey]ownership{}
	if e.cfg.StatePath == "" {
		return
	}
	data, err := os.ReadFile(e.cfg.StatePath)
	if os.IsNotExist(err) {
		if e.cfg.DeferStateCreation {
			// The first release ships the reader and PVC only. A later
			// activation creates the journal; rolling its pin back here
			// keeps reading and writing that existing ownership instead.
			e.log.Info("ownership journal creation deferred; using memory until activation")
			e.cfg.StatePath = ""
			return
		}
		// Verify the volume is writable before admitting any work.
		e.loadErr = e.saveOwnershipLocked()
		return
	}
	var saved struct {
		Version int
		Reviews []ownership
	}
	if err == nil {
		err = json.Unmarshal(data, &saved)
	}
	if err == nil && saved.Version != 1 && saved.Version != 2 {
		err = fmt.Errorf("unsupported ownership journal version %d", saved.Version)
	}
	if err != nil {
		e.loadErr = fmt.Errorf("read reviewer ownership: %w", err)
		return
	}
	for _, o := range saved.Reviews {
		key := headKey{o.Binding.Name, o.PR.HeadSHA}
		if _, duplicate := e.owned[key]; duplicate || o.Run.ID <= 0 || o.PR.Number <= 0 ||
			o.PR.HeadSHA == "" || o.Run.HeadSHA != o.PR.HeadSHA || o.Binding.Name == "" ||
			o.Binding.Owner == "" || o.Binding.Repo == "" || o.Binding.BaseBranch == "" ||
			o.Binding.Agent.Name == "" || o.Binding.Agent.Namespace == "" {
			e.loadErr = fmt.Errorf("invalid review ownership entry")
			return
		}
		if p := o.Pending; p != nil {
			id := AttemptMessageID(o.Binding.repo(), o.PR.Number, o.PR.HeadSHA, o.Run.ID, p.Attempt)
			if p.Message.ContextID != "" {
				id += "/resume"
			}
			if p.QuotaRetry > 0 {
				id += fmt.Sprintf("/quota-%d", p.QuotaRetry)
			}
			if p.Attempt < 1 || p.QuotaRetry < 0 || p.Sent.IsZero() || p.Message.MessageID != id {
				e.loadErr = fmt.Errorf("invalid pending review send")
				return
			}
		}
		e.owned[key] = o
	}
	e.m.InFlight.Set(float64(len(e.owned)))
}

// saveOwnershipLocked atomically replaces and fsyncs the journal and its
// directory. Failure keeps ownership in memory and fences new admissions.
func (e *Engine) saveOwnershipLocked() (err error) {
	defer func() { e.stateErr = err }()
	if e.cfg.StatePath == "" {
		return nil
	}
	var saved struct {
		Version int
		Reviews []ownership
	}
	// Older readers must not silently drop an unresolved send on rollback.
	saved.Version = 2
	for _, o := range e.owned {
		saved.Reviews = append(saved.Reviews, o)
	}
	data, err := json.Marshal(saved)
	if err != nil {
		return err
	}
	dir, err := os.Open(filepath.Dir(e.cfg.StatePath))
	if err != nil {
		return err
	}
	defer dir.Close()
	f, err := os.CreateTemp(filepath.Dir(e.cfg.StatePath), ".ownership-*")
	if err != nil {
		return err
	}
	defer os.Remove(f.Name())
	if _, err = f.Write(data); err == nil {
		err = f.Sync()
	}
	if closeErr := f.Close(); err == nil {
		err = closeErr
	}
	if err == nil {
		err = os.Rename(f.Name(), e.cfg.StatePath)
	}
	if err == nil {
		err = dir.Sync()
	}
	return err
}

func (e *Engine) remember(b *Binding, pr github.PullRequest, run *github.CheckRun) bool {
	e.mu.Lock()
	defer e.mu.Unlock()
	key := headKey{b.Name, pr.HeadSHA}
	o := e.owned[key]
	if o.Run.ID != run.ID {
		o.Pending = nil
	}
	o.Binding, o.PR, o.Run = *b, pr, *run
	e.owned[key] = o
	e.m.InFlight.Set(float64(e.slotsLocked()))
	return e.saveOwnershipLocked() == nil
}

func (e *Engine) checkpoint(b *Binding, run *github.CheckRun) bool {
	e.mu.Lock()
	defer e.mu.Unlock()
	key := headKey{b.Name, run.HeadSHA}
	o, ok := e.owned[key]
	if !ok || o.Run.ID != run.ID {
		return false
	}
	o.Run = *run
	if RecordedTask(run.Summary).ID != "" {
		o.Pending = nil
	}
	e.owned[key] = o
	if err := e.saveOwnershipLocked(); err != nil {
		e.log.Error("cannot persist review ownership; new work is fenced", "error", err)
		return false
	}
	return true
}

func (e *Engine) pendingSend(b *Binding, run *github.CheckRun) *pendingSend {
	e.mu.Lock()
	defer e.mu.Unlock()
	o := e.owned[headKey{b.Name, run.HeadSHA}]
	if o.Run.ID != run.ID || o.Pending == nil {
		return nil
	}
	p := *o.Pending
	return &p
}

func (e *Engine) prepareSend(b *Binding, run *github.CheckRun, msg kagent.Message, rec recorded) (fresh, ready bool) {
	e.mu.Lock()
	defer e.mu.Unlock()
	key := headKey{b.Name, run.HeadSHA}
	o := e.owned[key]
	if o.Run.ID != run.ID {
		return false, false
	}
	fresh = o.Pending == nil
	if !fresh {
		return false, false
	}
	o.Pending = &pendingSend{Message: msg, Attempt: rec.Attempt, Sent: e.cfg.Now(), QuotaRetry: rec.QuotaRetry, Issues: rec.Issues, Semgrep: rec.Semgrep}
	e.owned[key] = o
	if err := e.saveOwnershipLocked(); err != nil {
		e.log.Error("cannot persist review send intent", "error", err)
		return fresh, false
	}
	return fresh, true
}

// Only a definite rejection of the first send can clear its intent without
// a task ID. A later rejection says nothing about earlier ambiguous sends.
func (e *Engine) clearSend(b *Binding, run *github.CheckRun) bool {
	e.mu.Lock()
	defer e.mu.Unlock()
	key := headKey{b.Name, run.HeadSHA}
	o := e.owned[key]
	if o.Run.ID != run.ID {
		return false
	}
	o.Pending = nil
	e.owned[key] = o
	return e.saveOwnershipLocked() == nil
}

// Cancellation cannot interpret an unknown task ID as quiescence. Only a
// positive history match can recover it; even an empty listing leaves the
// original execution owned. Never resend after its creation receipt may
// have disappeared while the actor still runs.
func (e *Engine) resolveSend(ctx context.Context, b *Binding, run *github.CheckRun, rec *recorded, l *slog.Logger) bool {
	p := e.pendingSend(b, run)
	if rec.ID != "" || p == nil {
		return true
	}
	t, err := e.findSentTask(ctx, b.Agent, p.Message)
	if err != nil || t == nil {
		if err != nil {
			e.kagentError(ctx, l, b, "resolve_send", err)
		}
		rec.Attempt, rec.Sent = p.Attempt, p.Sent
		e.recordProgress(ctx, b, run, nil, *rec,
			"The original dispatch is unresolved. Its execution still owns a review slot; only a matching task history can recover it. No replacement will be sent.", l)
		return false
	}
	rec.Attempt, rec.ID, rec.Sent = p.Attempt, t.ID, p.Sent
	rec.Issues, rec.Semgrep = p.Issues, p.Semgrep
	return e.recordProgress(ctx, b, run, nil, *rec, "", l)
}

// Scan every state and page for positive evidence. Partial, malformed and
// empty reads never authorize a new send or release capacity. The timeout
// and cursor check bound scans even when the runtime listing is unstable.
func (e *Engine) findSentTask(ctx context.Context, agent kagent.AgentRef, msg kagent.Message) (*kagent.Task, error) {
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	token := ""
	seen := map[string]bool{}
	for {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		page, err := e.tasks.ListTasks(ctx, agent, msg.ContextID, "", token)
		if err != nil {
			return nil, err
		}
		if page == nil {
			return nil, fmt.Errorf("missing task listing")
		}
		for _, candidate := range page.Tasks {
			if candidate == nil || candidate.ID == "" || candidate.ContextID == "" {
				continue
			}
			t, err := e.tasks.GetTask(ctx, agent, candidate.ID)
			if err != nil {
				continue // Another listed task may positively identify the send.
			}
			if t == nil || t.ID != candidate.ID || t.ContextID != candidate.ContextID ||
				(msg.ContextID != "" && t.ContextID != msg.ContextID) || len(t.History) == 0 {
				continue
			}
			first := t.History[0]
			if first != nil && first.Role == "ROLE_USER" && first.MessageID == msg.MessageID &&
				first.TaskID == t.ID && first.ContextID == t.ContextID {
				return t, nil
			}
		}
		token = page.NextPageToken
		if token == "" {
			return nil, nil
		}
		if seen[token] {
			return nil, fmt.Errorf("repeated task page token")
		}
		seen[token] = true
	}
}

func (e *Engine) forget(b *Binding, run *github.CheckRun) {
	e.mu.Lock()
	defer e.mu.Unlock()
	key := headKey{b.Name, run.HeadSHA}
	o, ok := e.owned[key]
	if !ok || o.Run.ID != run.ID {
		return
	}
	delete(e.owned, key)
	if err := e.saveOwnershipLocked(); err != nil {
		e.owned[key] = o
		e.log.Error("cannot release review ownership", "error", err)
	}
	e.m.InFlight.Set(float64(e.slotsLocked()))
}

func (e *Engine) slotsLocked() int {
	n := len(e.owned)
	for key := range e.inflight {
		if _, ok := e.owned[key]; !ok {
			n++
		}
	}
	return n
}

// Recover every owned check before admitting new heads, including closed
// PRs, superseded heads and work above a newly lowered concurrency limit.
// A failed PR read retains the slot and retries next tick.
func (e *Engine) recoverOwnership(ctx context.Context) bool {
	e.mu.Lock()
	if e.loadErr != nil {
		e.log.Error("cannot load review ownership; new work is fenced", "error", e.loadErr)
		e.mu.Unlock()
		return false
	}
	if e.stateErr != nil && e.saveOwnershipLocked() != nil {
		e.mu.Unlock()
		return false
	}
	var saved []ownership
	for key, o := range e.owned {
		if _, running := e.inflight[key]; !running {
			saved = append(saved, o)
		}
	}
	e.mu.Unlock()
	for _, o := range saved {
		if ctx.Err() != nil {
			return false
		}
		b := o.Binding
		var cause error
		found := false
		for _, current := range e.cfg.Bindings {
			if current.Name == b.Name && current.repo() == b.repo() && current.Agent == b.Agent && current.BaseBranch == b.BaseBranch {
				b, found = current, true
				break
			}
		}
		if !found {
			cause = errClosed
		} else if rec := RecordedTask(o.Run.Summary); rec.Cancel != "closed" && rec.Cancel != "superseded" && rec.Cancel != "duplicate" {
			pr, err := e.gh.PullRequest(ctx, b.repo(), o.PR.Number)
			if err != nil {
				e.ghError(ctx, e.prLog(&b, o.PR), &b, "recover_pull_request", err)
				continue
			}
			// Labels may have changed while the service was stopped. Never
			// resume with the journal's gate inputs. Keep the owned head's
			// identity for cancellation if the PR moved while we were down.
			head := o.PR.HeadSHA
			o.PR = *pr
			o.PR.HeadSHA = head
			if pr.State != "open" || pr.BaseRef != b.BaseBranch {
				cause = errClosed
			} else if pr.HeadSHA != head {
				cause = errSuperseded
			}
		}
		rctx := ctx
		if cause != nil {
			var cancel context.CancelCauseFunc
			rctx, cancel = context.WithCancelCause(ctx)
			cancel(cause)
		}
		e.begin(rctx, &b, o.PR, &o.Run)
	}
	return true
}
