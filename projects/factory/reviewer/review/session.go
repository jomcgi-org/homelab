package review

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"log/slog"
	"regexp"
	"slices"

	"github.com/jomcgi/homelab/projects/factory/connector/github"
	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
)

// Sessions is the part of kagent's SessionService and AgentService the
// reviewer reads before it continues a recorded session.
type Sessions interface {
	GetSession(ctx context.Context, id string) (*kagent.Session, error)
	// AgentRevision is the Agent's latest successful revision, which kagent
	// pins a new session to.
	AgentRevision(ctx context.Context, agent kagent.AgentRef) (string, error)
}

// A review's task opens a fresh kagent session, or resumes the one that
// reviewed the PR's previous head.
const (
	PathFresh   = "fresh"
	PathResumed = "resumed"
)

// maxSessionBytes bounds the task bytes a session may already hold for the
// next head's review to resume it. Task bytes are the prompts, replies, tool
// calls and tool results kagent keeps on a session's tasks, at roughly four
// bytes a token: 512 KiB is about 130k tokens. A late full review of #8062
// held about 300 KB, so one more review past the bound could approach the
// model's context window. Codex then compacts the conversation, and its
// summary replaces both the cached prefix and the detail a continuation
// relies on.
const maxSessionBytes = 512 << 10

// maxSessionReviews bounds the reviews one session holds, and so the task
// IDs its State records.
const maxSessionReviews = 32

// contextIDPattern is the shape of a context ID the reviewer records and
// resumes: kagent's are UUIDs.
var contextIDPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`)

// SessionRef is the kagent session that reviewed a head, kept in that
// review's State for the next head. Only the service writes it: the context
// ID comes from kagent's task, the rest from the binding, the PR listing and
// the service's own counts.
type SessionRef struct {
	// FilteredComments records that the session began with the CI comment filter in effect.
	FilteredComments bool   `json:"filteredComments,omitempty"`
	ContextID        string `json:"contextId"`
	// Binding and Agent ("namespace/name") own the session; PR and Head are
	// the pull request and head it last reviewed.
	Binding string `json:"binding"`
	Agent   string `json:"agent"`
	PR      int    `json:"pr"`
	Head    string `json:"head"`
	// Path is how the review of Head began: fresh or resumed.
	Path string `json:"path"`
	// Reviews counts the heads the session reviewed, Head included; Bytes
	// is the task bytes it holds across them.
	Reviews int   `json:"reviews"`
	Bytes   int64 `json:"bytes"`
	// Tasks are the IDs of those reviews' tasks, oldest first: every task
	// the session may hold for Bytes to count all of its turns.
	Tasks []string `json:"tasks,omitempty"`
	// Rules is the digest of the rules and repository context the session
	// was last sent, or empty when unknown.
	Rules string `json:"rules,omitempty"`
}

// Why a review's task resumed or started fresh. The values bound the
// sessions metric's reason label.
const (
	reasonResumed       = "resumed"
	reasonNone          = "none"     // no session recorded for an earlier head
	reasonRetry         = "retry"    // an earlier attempt on this head ended without a report
	reasonDisabled      = "disabled" // the service has no SessionService client
	reasonCommentFilter = "comment_filter"
	reasonForeign       = "foreign"     // another binding, agent, PR or head
	reasonSize          = "size"        // the session holds maxSessionBytes or more, or maxSessionReviews reviews
	reasonUnaccounted   = "unaccounted" // the session's tasks are not exactly the recorded reviews' tasks
	reasonUnsettled     = "unsettled"   // an unrecorded turn in the session may still run
	reasonMissing       = "missing"     // kagent has no such session for this caller, or cannot recover it
	reasonDeleted       = "deleted"     // the session is being or was deleted
	reasonCrashed       = "crashed"     // the session's runtime crashed
	reasonFailed        = "failed"      // the session's runtime failed
	reasonRevision      = "revision"    // the session runs another Agent revision than new sessions get
	reasonUnreadable    = "unreadable"  // reading the session, the Agent or the session's tasks failed
	reasonRefused       = "refused"     // kagent refused the continuation
)

// sessionPlan is how an attempt's task starts: in the recorded session
// (resume) or a fresh one, why, and the digest of the rules its prompt
// carries or relies on.
type sessionPlan struct {
	resume *SessionRef
	reason string
	rules  string
}

func (p sessionPlan) path() string {
	if p.resume != nil {
		return PathResumed
	}
	return PathFresh
}

func agentName(a kagent.AgentRef) string { return a.Namespace + "/" + a.Name }

// resumable is the recorded session the review of pr's head may resume, or
// nil and the reason it starts fresh. Only the first attempt on a later head
// of the same PR resumes, into a session of the same binding and agent that
// holds less than maxSessionBytes and maxSessionReviews, that kagent reports
// usable and pinned to the Agent's current revision, and whose tasks are
// exactly the ones the recorded reviews ran. A turn no completed review
// accounts for (an interrupted or superseded review, or a review that ended
// without a report) leaves a task the State does not list, so Bytes would
// undercount the session. refused says kagent already refused this
// attempt's continuation.
func (e *Engine) resumable(ctx context.Context, b *Binding, pr github.PullRequest, in *inputs, attempt int, refused bool, l *slog.Logger) (*SessionRef, string) {
	ref := in.prior.Session
	switch {
	case ref == nil || in.priorHead == "" || in.priorHead == pr.HeadSHA || in.prior.Incomplete:
		return nil, reasonNone
	case e.cfg.Sessions == nil:
		return nil, reasonDisabled
	case !contextIDPattern.MatchString(ref.ContextID) || ref.Binding != b.Name || ref.Agent != agentName(b.Agent) ||
		ref.PR != pr.Number || ref.Head != in.priorHead:
		return nil, reasonForeign
	case !ref.FilteredComments:
		return nil, reasonCommentFilter
	case ref.Bytes >= maxSessionBytes || len(ref.Tasks) >= maxSessionReviews:
		return nil, reasonSize
	case len(ref.Tasks) != ref.Reviews:
		return nil, reasonUnaccounted
	}
	s, err := e.cfg.Sessions.GetSession(ctx, ref.ContextID)
	switch {
	case errors.Is(err, kagent.ErrSessionNotFound) || errors.Is(err, kagent.ErrSessionUnrecoverable):
		return nil, reasonMissing
	case err != nil || s == nil:
		e.kagentError(ctx, l, b, "get_session", err)
		return nil, reasonUnreadable
	case s.Gone():
		return nil, reasonDeleted
	case s.Crashed():
		return nil, reasonCrashed
	case s.State == kagent.RuntimeFailed:
		return nil, reasonFailed
	case (s.ContextID != "" && s.ContextID != ref.ContextID) ||
		(s.AgentNamespace != "" && (s.AgentNamespace != b.Agent.Namespace || s.AgentName != b.Agent.Name)):
		return nil, reasonForeign
	}
	// kagent runs every turn of a session on the revision it pinned, so a
	// model or prompt change reaches only new sessions.
	rev, err := e.cfg.Sessions.AgentRevision(ctx, b.Agent)
	if err != nil {
		e.kagentError(ctx, l, b, "get_agent", err)
		return nil, reasonUnreadable
	}
	// A running turn may be on a later page. Inspect the complete listing
	// before deciding whether a retry or fallback can start.
	page := &kagent.TaskPage{}
	seen := map[string]bool{}
	for token := ""; ; {
		part, err := e.tasks.ListTasks(ctx, b.Agent, ref.ContextID, "", token)
		if err != nil || part == nil {
			e.kagentError(ctx, l, b, "list_tasks", err)
			return nil, reasonUnreadable
		}
		page.Tasks = append(page.Tasks, part.Tasks...)
		page.TotalSize = part.TotalSize
		token = part.NextPageToken
		if token == "" {
			break
		}
		if seen[token] {
			return nil, reasonUnreadable
		}
		seen[token] = true
	}
	if page.TotalSize != len(page.Tasks) {
		return nil, reasonUnreadable
	}
	matched := accounted(page, ref)
	if !matched {
		l.InfoContext(ctx, "the recorded session holds turns its reviews do not account for", "context_id", ref.ContextID,
			"recorded_tasks", len(ref.Tasks), "session_tasks", page.TotalSize)
		if !e.stopUnrecorded(ctx, b, page, ref, l) {
			return nil, reasonUnsettled
		}
	}
	switch {
	case attempt > 1:
		return nil, reasonRetry
	case refused:
		return nil, reasonRefused
	}
	if rev == "" || s.PreparedRevision != rev {
		l.InfoContext(ctx, "the recorded session runs another agent revision", "context_id", ref.ContextID,
			"session_revision", s.PreparedRevision, "agent_revision", rev)
		return nil, reasonRevision
	}
	if !matched {
		return nil, reasonUnaccounted
	}
	return ref, reasonResumed
}

// errUnsettledTurn holds a review back while a session turn no review
// recorded may still run; the attempt builds its message again after
// Reissue, which retries the cancellation.
var errUnsettledTurn = errors.New("a session turn no review recorded is still running")

// stopUnrecorded cancels the session's listed turns that no review recorded
// and that still execute, such as a continuation kagent accepted before a
// restart lost its task ID. It reports whether each is confirmed stopped, so
// a fresh review never runs beside one.
func (e *Engine) stopUnrecorded(ctx context.Context, b *Binding, page *kagent.TaskPage, ref *SessionRef, l *slog.Logger) bool {
	if page == nil {
		return false
	}
	stopped := true
	for _, t := range page.Tasks {
		if t == nil || slices.Contains(ref.Tasks, t.ID) || kagent.Quiescent(t.Status.State) {
			continue
		}
		l.InfoContext(ctx, "cancelling a session turn no review recorded", "context_id", ref.ContextID, "task", t.ID)
		if _, err := e.tasks.CancelTask(ctx, b.Agent, t.ID); err != nil && !errors.Is(err, kagent.ErrTaskNotFound) {
			e.kagentError(ctx, l, b, "cancel", err) // Possibly finished meanwhile: confirm below.
		}
		got, err := e.tasks.GetTask(ctx, b.Agent, t.ID)
		switch {
		case errors.Is(err, kagent.ErrTaskNotFound):
		case err != nil:
			e.kagentError(ctx, l, b, "get_task", err)
			stopped = false
		case got == nil || !kagent.Quiescent(got.Status.State):
			stopped = false
		}
	}
	return stopped
}

// accounted reports whether a complete listing of the session's tasks holds
// exactly the tasks ref records.
func accounted(page *kagent.TaskPage, ref *SessionRef) bool {
	if page == nil || page.NextPageToken != "" || page.TotalSize != len(page.Tasks) || len(page.Tasks) != len(ref.Tasks) {
		return false
	}
	want := map[string]bool{}
	for _, id := range ref.Tasks {
		want[id] = true
	}
	for _, t := range page.Tasks {
		if t == nil || !want[t.ID] || (t.ContextID != "" && t.ContextID != ref.ContextID) {
			return false
		}
		delete(want, t.ID)
	}
	return len(want) == 0
}

// rulesDigest identifies the rules and repository context a prompt carries
// or tells the agent to read: each file's whole text, clipped or omitted
// parts included. It leaves out the base commit: main moves with every
// merge, and a resumed session needs the rules again only when their text
// changed. It is empty, so the rules are always sent, when any text that may
// apply went unread: a file too large to read, or rule files past
// MaxRuleFiles or changed paths no rule could be looked up for.
func rulesDigest(r *Rules) string {
	if r == nil || r.Unchecked > 0 {
		return ""
	}
	for _, f := range append(slices.Clone(r.Files), r.Context...) {
		if f.Digest == "" {
			return ""
		}
	}
	h := sha256.New()
	_ = json.NewEncoder(h).Encode(struct {
		Base           string
		Files, Context []RuleFile
		Fallback       bool
		Unchecked      int
		Notes          []string
	}{r.Base, r.Files, r.Context, r.Fallback, r.Unchecked, r.Notes})
	return hex.EncodeToString(h.Sum(nil)[:16])
}

// sessionAfter is the session a completed review leaves for the next head:
// the one its task ran in, counted as resumed when the previous head's
// review recorded that same session.
func sessionAfter(b *Binding, pr github.PullRequest, in *inputs, t *kagent.Task) *SessionRef {
	if !contextIDPattern.MatchString(t.ContextID) {
		return nil
	}
	ref := &SessionRef{
		// A recovered task may have been dispatched before the filter was deployed.
		FilteredComments: in.plan.reason != "",
		ContextID:        t.ContextID, Binding: b.Name, Agent: agentName(b.Agent), PR: pr.Number, Head: pr.HeadSHA,
		Path: PathFresh, Reviews: 1, Bytes: taskBytes(t), Tasks: []string{t.ID}, Rules: in.plan.rules,
	}
	if prev := in.prior.Session; prev != nil && prev.ContextID == t.ContextID && in.priorHead != pr.HeadSHA {
		ref.FilteredComments = prev.FilteredComments
		ref.Path, ref.Reviews, ref.Bytes = PathResumed, prev.Reviews+1, prev.Bytes+ref.Bytes
		ref.Tasks = append(slices.Clone(prev.Tasks), t.ID)
		if in.plan.resume == nil {
			// A restart lost the attempt's prompt, so whether it carried the
			// rules is unknown: the next continuation sends them again.
			ref.Rules = ""
		}
	}
	return ref
}

// withSession records ref in the State a completed check run's text holds.
func withSession(text string, ref *SessionRef) string {
	st, ok := LoadState(text)
	if !ok || ref == nil || st.Incomplete {
		return text
	}
	st.Session = ref
	updated := encodeState(st)
	if saved, _ := LoadState(updated); saved.Incomplete {
		return text // Optional session reuse cannot displace complete findings.
	}
	return updated
}

// taskBytes is the text and data kagent keeps on a task: its prompt,
// replies, tool calls and tool results. It approximates the context the
// turn added to its session; kagent's own history may be longer than what it
// returns, so it is a lower bound.
func taskBytes(t *kagent.Task) int64 {
	var n int64
	add := func(parts []kagent.Part) {
		for _, p := range parts {
			n += int64(len(p.Text))
			if p.Data != nil {
				if b, err := json.Marshal(p.Data); err == nil {
					n += int64(len(b))
				}
			}
		}
	}
	for _, m := range t.History {
		if m != nil {
			add(m.Parts)
		}
	}
	for _, a := range t.Artifacts {
		if a != nil {
			add(a.Parts)
		}
	}
	return n
}

// Usage is a task's token usage: Input counts every input token, Cached
// the part of it read from the provider's prompt cache, and Output
// includes reasoning tokens.
type Usage struct {
	Input, Cached, Output int64
}

// usageExtension is kagent's task-lifetime token usage, stamped on the
// status update that ends an execution. kagent v1.0.1 emits it only from its
// ADK runtimes; its Codex harness reports usage in OpenTelemetry spans and
// metrics alone, so a reviewer task carries none today. The per-call
// kagent.dev/a2a/usage key holds one model call's usage, never a task
// total, so it is not read.
const usageExtension = "https://kagent.dev/extensions/usage/v1"

// TaskUsage is the usage kagent reported for a task, if any.
func TaskUsage(t *kagent.Task) (Usage, bool) {
	metas := []map[string]any{t.Metadata}
	if t.Status.Message != nil {
		metas = append(metas, t.Status.Message.Metadata)
	}
	for _, md := range metas {
		raw, ok := md[usageExtension].(map[string]any)
		if !ok {
			continue
		}
		u := Usage{
			Input:  tokens(raw["inputTokens"]),
			Cached: tokens(raw["cachedInputTokens"]),
			Output: tokens(raw["outputTokens"]) + tokens(raw["reasoningTokens"]),
		}
		if u.Input == 0 && u.Output == 0 {
			continue
		}
		u.Cached = min(u.Cached, u.Input)
		return u, true
	}
	return Usage{}, false
}

func tokens(v any) int64 {
	switch n := v.(type) {
	case float64:
		if n > 0 {
			return int64(n)
		}
	case int64:
		return max(n, 0)
	case int:
		return int64(max(n, 0))
	}
	return 0
}

// observeSession counts a completed review by the path its task took and
// logs the task's size and any usage kagent reported, so fresh and resumed
// reviews can be compared.
func (e *Engine) observeSession(ctx context.Context, b *Binding, t *kagent.Task, ref *SessionRef, l *slog.Logger) {
	path, n := PathFresh, taskBytes(t)
	attrs := []any{"task", t.ID, "context_id", t.ContextID, "task_bytes", n}
	if ref != nil {
		path = ref.Path
		attrs = append(attrs, "session_reviews", ref.Reviews, "session_bytes", ref.Bytes)
	}
	e.m.ReviewTaskBytes.WithLabelValues(b.Name, path).Observe(float64(n))
	reported := "unreported"
	if u, ok := TaskUsage(t); ok {
		reported = "reported"
		e.m.ReviewTokens.WithLabelValues(b.Name, path, "input_uncached").Add(float64(u.Input - u.Cached))
		e.m.ReviewTokens.WithLabelValues(b.Name, path, "input_cached").Add(float64(u.Cached))
		e.m.ReviewTokens.WithLabelValues(b.Name, path, "output").Add(float64(u.Output))
		attrs = append(attrs, "input_uncached", u.Input-u.Cached, "input_cached", u.Cached, "output", u.Output)
	}
	e.m.ReviewUsage.WithLabelValues(b.Name, path, reported).Inc()
	l.InfoContext(ctx, "review usage", append([]any{"path", path, "usage", reported}, attrs...)...)
}
