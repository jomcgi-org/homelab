package review

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/github"
)

// StateStore is a fenced writer. A missing store never permits a side effect.
type StateStore interface {
	Load(context.Context) ([]byte, error)
	Save(context.Context, []byte) error
	Check(context.Context) error
}

type projection struct {
	Repo  string
	ID    int64
	Write github.CheckRunWrite
}

type pendingHold struct {
	Repo string
	SHA  string
	Run  *github.CheckRun
}

// A snapshot commits cross-PR observations together. Map keys include the
// repository; no gate or context generation can leak across repositories.
type durableState struct {
	Version      int
	Contexts     map[string]*heldContext
	Pending      []pendingHold
	Projections  map[string]projection
	Groups       map[string]string
	EnqueueAfter map[string]time.Time
}

func prKey(b *Binding, number int) string { return fmt.Sprintf("%s#%d", b.repo(), number) }

func cloneGate(g *Gate) *Gate {
	if g == nil {
		return nil
	}
	data, _ := json.Marshal(g)
	var out Gate
	_ = json.Unmarshal(data, &out)
	return &out
}

func (e *Engine) storageError() error {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.storeErr
}

// A failed commit ends this writer's run. Its uncommitted cache must never
// authorize a later pass, even if the database becomes available again.
func (e *Engine) failStoreLocked(err error) {
	if e.storeErr == nil {
		e.storeErr = fmt.Errorf("reviewer state: %w", err)
		e.log.Error("durable gate state unavailable; stopping writer")
		for _, r := range e.inflight {
			r.cancel(e.storeErr)
		}
	}
}

func (e *Engine) saveLocked(ctx context.Context) error {
	if e.storeErr != nil {
		return e.storeErr
	}
	if e.cfg.Store == nil {
		e.failStoreLocked(errors.New("database writer is required"))
		return e.storeErr
	}
	s := durableState{Version: 1, Contexts: e.contexts, Projections: e.projections, Groups: e.groups, EnqueueAfter: e.enqueueAfter}
	for k, run := range e.pendingHolds {
		s.Pending = append(s.Pending, pendingHold{Repo: k.binding, SHA: k.sha, Run: run})
	}
	data, err := json.Marshal(s)
	if err == nil {
		err = e.cfg.Store.Save(ctx, data)
	}
	if err != nil {
		e.failStoreLocked(err)
	}
	return err
}

func (e *Engine) checkpoint(ctx context.Context) bool {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.saveLocked(ctx) == nil
}

// load imports every configured binding before any review, pass or enqueue.
// A failed or truncated read leaves no import marker and is retried next poll.
func (e *Engine) load(ctx context.Context) bool {
	if e.storageError() != nil {
		return false
	}
	if e.cfg.Store == nil {
		e.checkpoint(ctx)
		return false
	}
	if err := e.cfg.Store.Check(ctx); err != nil {
		e.mu.Lock()
		e.failStoreLocked(err)
		e.mu.Unlock()
		return false
	}
	if e.loaded {
		return true
	}
	data, err := e.cfg.Store.Load(ctx)
	if err != nil {
		e.mu.Lock()
		e.failStoreLocked(err)
		e.mu.Unlock()
		return false
	}
	if data != nil {
		var s durableState
		err := json.Unmarshal(data, &s)
		if err != nil || s.Version != 1 || s.Contexts == nil || s.Projections == nil || s.Groups == nil || s.EnqueueAfter == nil {
			e.mu.Lock()
			e.failStoreLocked(errors.New("invalid durable state"))
			e.mu.Unlock()
			return false
		}
		e.contexts, e.projections, e.groups, e.enqueueAfter = s.Contexts, s.Projections, s.Groups, s.EnqueueAfter
		for _, p := range s.Pending {
			e.pendingHolds[headKey{p.Repo, p.SHA}] = p.Run
		}
		e.loaded = true
		return true
	}
	imported := map[string]*heldContext{}
	for i := range e.cfg.Bindings {
		b := &e.cfg.Bindings[i]
		prs, complete, err := e.gh.ListPullRequests(ctx, b.repo(), b.BaseBranch)
		if err != nil || !complete {
			e.log.WarnContext(ctx, "gate import needs a complete open PR listing", "binding", b.Name)
			return false
		}
		for _, pr := range prs {
			run, err := e.reviewCheck(ctx, b, pr.HeadSHA)
			if err != nil {
				return false
			}
			st := State{}
			if run != nil {
				var ok bool
				st, ok = LoadState(run.Text)
				if !ok && strings.Contains(run.Text, statePrefix) {
					return false
				}
			}
			g := st.Gate
			if g == nil {
				g = &Gate{ObservedLabels: gateLabels(b, pr.Labels)}
			}
			events, err := e.gh.PullRequestLabelEvents(ctx, b.repo(), pr.Number)
			if err != nil {
				e.ghError(ctx, e.prLog(b, pr), b, "import_label_events", err)
				return false
			}
			g.observeLabels(gateLabels(b, pr.Labels), e.cfg.Now())
			g.observeLabelEvents(b, events)
			// LoadState deliberately ignores the retired recovery fields. Do
			// not import their approval baseline as a new reason to hold.
			if !g.Needed() {
				g = &Gate{ObservedLabels: gateLabels(b, pr.Labels)}
			}
			imported[prKey(b, pr.Number)] = &heldContext{Head: pr.HeadSHA, Token: g.ContextToken, Labels: append([]string(nil), g.Labels...), Shared: g.SharedHead, Gate: g}
		}
	}
	e.mu.Lock()
	e.contexts = imported
	err = e.saveLocked(ctx)
	e.loaded = err == nil
	e.mu.Unlock()
	return err == nil
}

// stateFor takes the gate from Postgres, while review reports and task IDs
// remain on their check runs. New heads inherit sticky facts in retainGate.
func (e *Engine) stateFor(b *Binding, pr github.PullRequest, run *github.CheckRun) State {
	var st State
	if run != nil {
		st, _ = LoadState(run.Text)
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	if m := e.contexts[prKey(b, pr.Number)]; m != nil && m.Head == pr.HeadSHA {
		st.Gate = cloneGate(m.Gate)
	}
	return st
}

func (e *Engine) persistGate(ctx context.Context, b *Binding, pr github.PullRequest, g *Gate) {
	e.mu.Lock()
	defer e.mu.Unlock()
	key := prKey(b, pr.Number)
	m := e.contexts[key]
	if m == nil {
		m = &heldContext{Head: pr.HeadSHA}
		e.contexts[key] = m
	}
	if m.Head == pr.HeadSHA {
		m.Gate = cloneGate(g)
		m.Token, m.Shared, m.Labels = g.ContextToken, g.SharedHead, append([]string(nil), g.Labels...)
	}
	e.saveLocked(ctx)
}

// All siblings in one listing share an atomic commit, including when a
// process dies before the first duplicate check can be posted. Observations
// also cover skipped/draft PRs and heads whose check cannot be read.
func (e *Engine) observeListing(ctx context.Context, b *Binding, prs []github.PullRequest, complete bool, counts map[string]int, labels map[string][]string) bool {
	e.mu.Lock()
	defer e.mu.Unlock()
	for _, pr := range prs {
		if pr.HeadSHA == "" {
			continue
		}
		g := &Gate{ObservedLabels: []string{}}
		if m := e.contexts[prKey(b, pr.Number)]; m != nil && m.Head == pr.HeadSHA && m.Gate != nil {
			g = cloneGate(m.Gate)
		}
		before := g.ApprovalNonce
		g.observeLabels(gateLabels(b, pr.Labels), e.cfg.Now())
		if pr.BaseRef == b.BaseBranch && !pr.Draft && (!complete || counts[pr.HeadSHA] > 1 || e.pendingHolds[headKey{b.repo(), pr.HeadSHA}] != nil) {
			g.SharedHead = true
			for _, label := range labels[pr.HeadSHA] {
				if !slices.Contains(g.Labels, label) {
					g.Labels = append(g.Labels, label)
				}
			}
		}
		e.retainGateLocked(b, pr, g, before != g.ApprovalNonce)
	}
	return e.saveLocked(ctx) == nil
}

func (e *Engine) createCheck(ctx context.Context, b *Binding, w github.CheckRunWrite) (*github.CheckRun, error) {
	return e.project(ctx, projection{Repo: b.repo(), Write: w})
}

func (e *Engine) updateCheck(ctx context.Context, b *Binding, run *github.CheckRun, w github.CheckRunWrite) (*github.CheckRun, error) {
	w.HeadSHA, w.ExternalID = run.HeadSHA, run.ExternalID
	return e.project(ctx, projection{Repo: b.repo(), ID: run.ID, Write: w})
}

func (e *Engine) project(ctx context.Context, p projection) (*github.CheckRun, error) {
	// Detach mutable output (annotation retries, in particular) from the
	// caller before it becomes part of the shared durable snapshot.
	data, err := json.Marshal(p)
	if err != nil {
		return nil, err
	}
	if err := json.Unmarshal(data, &p); err != nil {
		return nil, err
	}
	key := p.Repo + "@" + p.Write.HeadSHA
	e.mu.Lock()
	// ExternalID binds a PR projection to its repository, number and head.
	prefix := "factory-review/" + p.Repo + "/"
	if strings.HasPrefix(p.Write.ExternalID, prefix) && p.Write.Output != nil {
		parts := strings.Split(strings.TrimPrefix(p.Write.ExternalID, prefix), "/")
		if len(parts) == 2 {
			n, err := strconv.Atoi(parts[0])
			if err == nil && n > 0 && parts[1] == p.Write.HeadSHA {
				k := fmt.Sprintf("%s#%d", p.Repo, n)
				st, ok := LoadState(p.Write.Output.Text)
				m := e.contexts[k]
				if m == nil {
					m = &heldContext{Head: p.Write.HeadSHA}
					e.contexts[k] = m
				}
				if ok && m.Head == p.Write.HeadSHA {
					g := cloneGate(st.Gate)
					if g == nil {
						g = &Gate{}
					}
					if m.Gate != nil {
						if p.Write.Conclusion == ConclusionSuccess && !coversGate(g, m.Gate) {
							e.mu.Unlock()
							return nil, errors.New("gate context changed before success projection")
						}
						g.inherit(m.Gate)
						if strings.Contains(p.Write.Output.Summary, duplicateHeadMarker) {
							g.Paths = append([]string(nil), m.Gate.Paths...)
							g.Findings = append([]Finding(nil), m.Gate.Findings...)
							g.ObservedLabels = append([]string{}, m.Gate.ObservedLabels...)
							g.LabelHistory = m.Gate.LabelHistory
						}
					}
					m.Gate, m.Token, m.Shared, m.Labels = g, g.ContextToken, g.SharedHead, append([]string(nil), g.Labels...)
					st.Gate = cloneGate(g)
					p.Write.Output.Text = encodeState(st)
				}
			}
		}
	}
	e.projections[key] = p
	err = e.saveLocked(ctx)
	e.mu.Unlock()
	if err != nil {
		return nil, err
	}
	if err := e.cfg.Store.Check(ctx); err != nil {
		e.mu.Lock()
		e.failStoreLocked(err)
		e.mu.Unlock()
		return nil, err
	}
	var run *github.CheckRun
	if p.ID == 0 {
		run, err = e.gh.CreateCheckRun(ctx, p.Repo, p.Write)
	} else {
		w := p.Write
		w.HeadSHA = ""
		run, err = e.gh.UpdateCheckRun(ctx, p.Repo, p.ID, w)
	}
	if err != nil {
		return nil, err
	}
	e.mu.Lock()
	delete(e.projections, key)
	if p.Write.ExternalID == "" && p.Write.Status == "completed" {
		e.groups[key] = p.Write.Conclusion
	}
	err = e.saveLocked(ctx)
	e.mu.Unlock()
	if err != nil {
		return nil, err
	}
	return run, nil
}

// A worker may finish while a poll records a later hold. Its evaluated
// approval must cover that generation before it can publish success.
func coversGate(evaluated, current *Gate) bool {
	if !current.Needed() {
		return true
	}
	if evaluated.ContextToken != current.ContextToken || evaluated.ApprovalNonce != current.ApprovalNonce || (current.SharedHead && !evaluated.SharedHead) {
		return false
	}
	for _, label := range current.Labels {
		if !slices.Contains(evaluated.Labels, label) {
			return false
		}
	}
	for _, path := range current.Paths {
		if !slices.Contains(evaluated.Paths, path) {
			return false
		}
	}
	for _, finding := range current.Findings {
		if !slices.ContainsFunc(evaluated.Findings, func(f Finding) bool {
			return f.ID == finding.ID }) { return false
		}
	}
	return true
}

// Retry failed hold projections, never a stale pass. Passes and enqueues
// require the current poll's reads and a fresh fenced commit.
func (e *Engine) retryHolds(ctx context.Context) {
	e.mu.Lock()
	var pending []projection
	for _, p := range e.projections {
		if p.Write.Conclusion == ConclusionFailure && p.Write.Output != nil && !strings.Contains(p.Write.Output.Summary, duplicateHeadMarker) {
			pending = append(pending, p)
		}
	}
	e.mu.Unlock()
	for _, p := range pending {
		_, _ = e.project(ctx, p)
	}
}

func (e *Engine) group(key string) string {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.groups[key]
}

func (e *Engine) setGroup(ctx context.Context, key, conclusion string) {
	e.mu.Lock()
	defer e.mu.Unlock()
	if len(e.groups) > maxGroups {
		clear(e.groups)
	}
	e.groups[key] = conclusion
	e.saveLocked(ctx)
}

func (e *Engine) enqueueDue(key string) bool {
	e.mu.Lock()
	defer e.mu.Unlock()
	return !e.cfg.Now().Before(e.enqueueAfter[key])
}

func (e *Engine) backoff(ctx context.Context, key string, until time.Time) bool {
	e.mu.Lock()
	defer e.mu.Unlock()
	if until.IsZero() {
		delete(e.enqueueAfter, key)
	} else {
		e.enqueueAfter[key] = until
	}
	return e.saveLocked(ctx) == nil
}
