package review

import (
	"context"
	"errors"
	"log/slog"
	"strconv"
	"strings"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/github"
	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
)

// SessionCleaner is the part of kagent's SessionService the reviewer uses to
// name the sessions its reviews run in and to delete the ones no review will
// resume. Each session keeps a Substrate snapshot (about 340 MB) until it is
// deleted, and kagent expires idle sessions only after its idle TTL (HOME-136).
type SessionCleaner interface {
	ListSessions(ctx context.Context, agent kagent.AgentRef, pageToken string) ([]*kagent.Session, string, error)
	DeleteSession(ctx context.Context, id string) (*kagent.Session, error)
	NameSession(ctx context.Context, id, name string) (*kagent.Session, error)
}

const (
	// cleanupEvery spaces the session sweeps of one binding.
	cleanupEvery = 10 * time.Minute
	// cleanupGrace protects a new session: its first turn may still be
	// starting, or its name not yet written.
	cleanupGrace = 15 * time.Minute
	// unnamedIdle is how long a session with no name (opened before the
	// reviewer named its sessions, or whose name write failed) must go
	// without a task change before it is deleted.
	unnamedIdle = 12 * time.Hour
	// maxSessionPages bounds one sweep's listing. A sweep that cannot list
	// every session deletes none, since an unlisted session may be the
	// newest of its PR.
	maxSessionPages = 50
)

// Why the sweep deleted a session. The values bound the metric's reason
// label.
const (
	cleanupClosed     = "closed"     // its PR is no longer open into the base branch
	cleanupSuperseded = "superseded" // a newer session of the same PR exists
	cleanupIdle       = "idle"       // unnamed, and idle for unnamedIdle
)

// SessionName is the display name of the sessions that review PR number of
// repo ("owner/name").
func SessionName(repo string, number int) string {
	return "factory-review/" + repo + "/" + strconv.Itoa(number)
}

// sessionPR is the PR a session name of repo refers to.
func sessionPR(name, repo string) (int, bool) {
	rest, ok := strings.CutPrefix(name, "factory-review/"+repo+"/")
	if !ok {
		return 0, false
	}
	n, err := strconv.Atoi(rest)
	return n, err == nil && n > 0 && strconv.Itoa(n) == rest
}

// nameSession names the session a review task runs in after its PR, so the
// sweep knows which PR it belongs to. A failed write leaves the session
// unnamed, and the sweep then deletes it only after unnamedIdle.
func (e *Engine) nameSession(ctx context.Context, b *Binding, pr github.PullRequest, contextID string, l *slog.Logger) {
	if e.cfg.Cleaner == nil || !contextIDPattern.MatchString(contextID) {
		return
	}
	if _, err := e.cfg.Cleaner.NameSession(ctx, contextID, SessionName(b.repo(), pr.Number)); err != nil {
		e.kagentError(ctx, l, b, "name_session", err)
	}
}

// sweepSessions starts a sweep of the binding's review sessions when
// cleanupEvery has passed since the last one and none still runs. It needs
// the complete listing of the binding's open PRs. Deletes run inline in
// kagent and take seconds each, so the sweep runs beside the poll loop.
func (e *Engine) sweepSessions(ctx context.Context, b *Binding, prs []github.PullRequest, complete bool, l *slog.Logger) {
	if e.cfg.Cleaner == nil || !complete {
		return
	}
	now := e.cfg.Now()
	e.mu.Lock()
	if e.sweeping[b.Name] || now.Sub(e.swept[b.Name]) < cleanupEvery {
		e.mu.Unlock()
		return
	}
	e.sweeping[b.Name], e.swept[b.Name] = true, now
	e.mu.Unlock()
	open := map[int]bool{}
	for _, pr := range prs {
		open[pr.Number] = true
	}
	e.wg.Add(1)
	go func() {
		defer e.wg.Done()
		defer func() {
			e.mu.Lock()
			e.sweeping[b.Name] = false
			e.mu.Unlock()
		}()
		e.sweep(ctx, b, open, l)
	}()
}

// sweep deletes the binding's sessions that no review will resume:
//   - every session of a PR that is no longer open (closed);
//   - every session of an open PR but its newest (superseded), since a
//     later head resumes only the session the last review recorded, and
//     that is the PR's newest unless every later attempt failed;
//   - an unnamed session idle for unnamedIdle (idle).
//
// It never deletes a session younger than cleanupGrace, one named for
// another repository, or one with a task that still executes; kagent also
// refuses to delete a session during a turn.
func (e *Engine) sweep(ctx context.Context, b *Binding, open map[int]bool, l *slog.Logger) {
	now := e.cfg.Now()
	var all []*kagent.Session
	token := ""
	for page := 0; ; page++ {
		if page == maxSessionPages {
			l.WarnContext(ctx, "too many review sessions to list; deleting none this sweep", "listed", len(all))
			return
		}
		ss, next, err := e.cfg.Cleaner.ListSessions(ctx, b.Agent, token)
		if err != nil {
			e.kagentError(ctx, l, b, "list_sessions", err)
			return
		}
		all = append(all, ss...)
		if next == "" {
			break
		}
		token = next
	}
	type entry struct {
		s  *kagent.Session
		pr int
	}
	var live []entry
	newest := map[int]*kagent.Session{}
	for _, s := range all {
		if s == nil || s.Gone() || !contextIDPattern.MatchString(s.ID) ||
			(s.AgentNamespace != "" && (s.AgentNamespace != b.Agent.Namespace || s.AgentName != b.Agent.Name)) {
			continue
		}
		pr := 0
		if s.Name != "" {
			n, ok := sessionPR(s.Name, b.repo())
			if !ok {
				continue // Named by someone else, or for another binding's repository.
			}
			pr = n
			if cur := newest[n]; cur == nil || s.CreatedAt.After(cur.CreatedAt) ||
				(s.CreatedAt.Equal(cur.CreatedAt) && s.ID > cur.ID) {
				newest[n] = s
			}
		}
		live = append(live, entry{s, pr})
	}
	deleted := map[string]int{}
	busy, failed := 0, 0
	for _, en := range live {
		if ctx.Err() != nil {
			return
		}
		s := en.s
		if s.CreatedAt.IsZero() || now.Sub(s.CreatedAt) < cleanupGrace {
			continue
		}
		var reason string
		switch {
		case en.pr == 0:
			reason = cleanupIdle
		case !open[en.pr]:
			reason = cleanupClosed
		case newest[en.pr] != s:
			reason = cleanupSuperseded
		default:
			continue
		}
		last, running, ok := e.sessionActivity(ctx, b, s, l)
		switch {
		case !ok:
			failed++
			continue
		case running:
			busy++
			continue
		case reason == cleanupIdle && now.Sub(last) < unnamedIdle:
			continue
		}
		if _, err := e.cfg.Cleaner.DeleteSession(ctx, s.ID); err != nil && !errors.Is(err, kagent.ErrSessionNotFound) {
			e.kagentError(ctx, l, b, "delete_session", err)
			failed++
			continue
		}
		deleted[reason]++
		e.m.SessionsDeleted.WithLabelValues(b.Name, reason).Inc()
		l.InfoContext(ctx, "deleted a review session", "context_id", s.ID, "reason", reason, "pr", en.pr)
	}
	l.InfoContext(ctx, "review sessions swept", "listed", len(all), "closed", deleted[cleanupClosed],
		"superseded", deleted[cleanupSuperseded], "idle", deleted[cleanupIdle], "busy", busy, "failed", failed)
}

// sessionActivity reads a session's tasks: when one last changed (the
// session's creation when none reports a time), and whether one still
// executes. ok is false when the listing failed or was incomplete.
func (e *Engine) sessionActivity(ctx context.Context, b *Binding, s *kagent.Session, l *slog.Logger) (last time.Time, running, ok bool) {
	page, err := e.tasks.ListTasks(ctx, b.Agent, s.ID, "", "")
	if err != nil {
		if errors.Is(err, kagent.ErrTaskNotFound) {
			return s.CreatedAt, false, true
		}
		e.kagentError(ctx, l, b, "list_tasks", err)
		return time.Time{}, false, false
	}
	if page == nil || page.NextPageToken != "" || page.TotalSize != len(page.Tasks) {
		return time.Time{}, false, false
	}
	last = s.CreatedAt
	for _, t := range page.Tasks {
		if t == nil {
			continue
		}
		if !kagent.Quiescent(t.Status.State) {
			return last, true, true
		}
		at, err := time.Parse(time.RFC3339Nano, t.Status.Timestamp)
		if err != nil {
			// Unknown activity: treat it as now, so an idle check keeps it.
			at = e.cfg.Now()
		}
		if at.After(last) {
			last = at
		}
	}
	return last, false, true
}
