package review

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"strings"

	"github.com/jomcgi/homelab/projects/factory/connector/github"
	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
	"github.com/jomcgi/homelab/projects/factory/connector/provenance"
)

type RunRecorder interface {
	RecordReview(context.Context, provenance.ReviewerRun) error
}

type ReviewerTaskPins interface {
	BindReviewTask(context.Context, provenance.ReviewerTask) error
	ReviewTaskMain(context.Context, provenance.ReviewerTask) (string, error)
}

func reviewTask(b *Binding, session, task string) provenance.ReviewerTask {
	return provenance.ReviewerTask{Repository: b.repo(), Agent: agentName(b.Agent), Session: session, Task: task}
}

func (e *Engine) bindReviewTask(ctx context.Context, b *Binding, messageID string, t *kagent.Task, log *slog.Logger) bool {
	if e.cfg.TaskPins == nil || t == nil || t.ID == "" || t.ContextID == "" {
		return false
	}
	binding := reviewTask(b, t.ContextID, t.ID)
	binding.MessageID = messageID
	if err := e.cfg.TaskPins.BindReviewTask(ctx, binding); err != nil {
		log.WarnContext(ctx, "review analysis pin unavailable", "task", t.ID, "error", err)
		return false
	}
	return true
}

// The check run supplies the task ID. A resumed or recovered task must also
// hold the exact dispatch message before it can bind the stored receipt.
func recordedReviewMessage(b *Binding, pr github.PullRequest, runID int64, rec recorded, t *kagent.Task) string {
	base := AttemptMessageID(b.repo(), pr.Number, pr.HeadSHA, runID, rec.Attempt)
	suffix := ""
	if rec.QuotaRetry > 0 {
		suffix = fmt.Sprintf("/quota-%d", rec.QuotaRetry)
	}
	fresh, resumed := base+suffix, base+"/resume"+suffix
	hasFresh, hasResumed := t.HasMessage(fresh), t.HasMessage(resumed)
	if hasFresh == hasResumed {
		return ""
	}
	if hasResumed {
		return resumed
	}
	return fresh
}

// ResolveAnalysisMain checks the live native task before returning its stored
// analysis commit. Older review receipts yield an unavailable pin.
func (e *Engine) ResolveAnalysisMain(ctx context.Context, namespace, session string) (string, error) {
	if e.cfg.TaskPins == nil || e.cfg.Sessions == nil {
		return "", sql.ErrNoRows
	}
	s, err := e.cfg.Sessions.GetSession(ctx, session)
	if errors.Is(err, kagent.ErrSessionNotFound) {
		return "", sql.ErrNoRows
	}
	if err != nil {
		return "", err
	}
	if s == nil || s.ID != session || s.ContextID != session || s.AgentNamespace != namespace || s.Gone() || s.Crashed() {
		return "", sql.ErrNoRows
	}
	for i := range e.cfg.Bindings {
		b := &e.cfg.Bindings[i]
		if b.Agent.Namespace != namespace || b.Agent.Name != s.AgentName {
			continue
		}
		t, err := e.workingReviewTask(ctx, b, session)
		if err != nil {
			return "", err
		}
		main, err := e.cfg.TaskPins.ReviewTaskMain(ctx, reviewTask(b, session, t))
		if err != nil {
			return "", err
		}
		current, err := e.workingReviewTask(ctx, b, session)
		if err != nil {
			return "", err
		}
		if current != t {
			return "", sql.ErrNoRows
		}
		return main, nil
	}
	return "", sql.ErrNoRows
}

func (e *Engine) workingReviewTask(ctx context.Context, b *Binding, session string) (string, error) {
	page, err := e.tasks.ListTasks(ctx, b.Agent, session, kagent.StateWorking, "")
	if err != nil {
		return "", err
	}
	if page == nil || page.TotalSize != 1 || len(page.Tasks) != 1 || page.NextPageToken != "" {
		return "", sql.ErrNoRows
	}
	t := page.Tasks[0]
	if t == nil || t.ID == "" || t.ContextID != session || t.Status.State != kagent.StateWorking {
		return "", sql.ErrNoRows
	}
	return t.ID, nil
}

type GoalSessionCreator interface {
	CreateSession(context.Context, kagent.AgentRef, string) (*kagent.Session, error)
}

// prepareGoalDispatch is only legal before any send could have executed, or
// after a definitive refusal. Recompute every receipt input before each send.
// An unused session from earlier settings must never pin a changed receipt.
func (e *Engine) prepareGoalDispatch(ctx context.Context, b *Binding, in *inputs, base, prefix string, key []byte, d *GoalTask, msg *kagent.Message) error {
	msg.MessageID = prefix
	var record provenance.ReviewerRun
	if e.cfg.Runs != nil {
		var err error
		record, err = reviewProvenance(b, in, msg, base)
		if err != nil {
			return err
		}
		data, _ := json.Marshal(record)
		mac := hmac.New(sha256.New, key)
		mac.Write(data)
		msg.MessageID += "/inputs/" + hex.EncodeToString(mac.Sum(nil)[:16])
	}
	// A completed dispatch may have had its session deleted. A persisted
	// nonce gives each new dispatch a fresh, retry-stable session request.
	msg.MessageID += "/dispatch/" + d.Nonce
	if d.MessageID != msg.MessageID {
		d.MessageID, d.ContextID = msg.MessageID, ""
	}
	if err := e.cfg.GoalTasks.SaveGoalTask(*d); err != nil {
		return err
	}
	if e.cfg.Runs != nil {
		record.MessageID = msg.MessageID
		if err := e.cfg.Runs.RecordReview(ctx, record); err != nil {
			return err
		}
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	if d.ContextID == "" {
		sum := sha256.Sum256([]byte(d.MessageID))
		s, err := e.cfg.GoalSessions.CreateSession(ctx, b.Agent, hex.EncodeToString(sum[:]))
		if err != nil {
			return err
		}
		if s == nil || !contextIDPattern.MatchString(s.ContextID) || s.ID != s.ContextID ||
			s.AgentNamespace != b.Agent.Namespace || s.AgentName != b.Agent.Name || s.Gone() || s.Crashed() {
			return errors.New("goal session unavailable")
		}
		d.ContextID = s.ContextID
		if err := e.cfg.GoalTasks.SaveGoalTask(*d); err != nil {
			return err
		}
	}
	msg.ContextID = d.ContextID
	return ctx.Err()
}

var (
	errGoalDispatchUncertain = errors.New("goal dispatch acceptance is uncertain")
	errGoalTaskNotVisible    = errors.New("goal task is not visible")
)

// An issued dispatch must never recover through Send: it could start fresh
// under changed settings. Its isolated session contains only this goal's turn.
// An empty or unreadable session is uncertainty, not permission to send again.
func (e *Engine) recoverGoalDispatch(ctx context.Context, b *Binding, d *GoalTask) (*kagent.Task, error) {
	if d.ContextID == "" {
		// Legacy indexes without a session cannot be safely retried.
		return nil, errGoalDispatchUncertain
	}
	p, err := e.tasks.ListTasks(ctx, b.Agent, d.ContextID, "", "")
	if err != nil {
		return nil, err
	}
	if p != nil && p.NextPageToken == "" && p.TotalSize == 0 && len(p.Tasks) == 0 {
		return nil, errGoalTaskNotVisible
	}
	if p == nil || p.NextPageToken != "" || p.TotalSize != 1 || len(p.Tasks) != 1 ||
		p.Tasks[0] == nil || p.Tasks[0].ID == "" || p.Tasks[0].ContextID != d.ContextID {
		return nil, errGoalDispatchUncertain
	}
	t, err := e.tasks.GetTask(ctx, b.Agent, p.Tasks[0].ID)
	if err != nil {
		return nil, err
	}
	if t == nil || t.ID != p.Tasks[0].ID || t.ContextID != d.ContextID || !t.HasMessage(d.MessageID) {
		return nil, errGoalDispatchUncertain
	}
	return t, nil
}

func (e *Engine) recordProvenance(ctx context.Context, b *Binding, in *inputs, msg *kagent.Message, base string) error {
	// Tests may omit the external store. Production wires it unconditionally.
	if e.cfg.Runs == nil {
		return nil
	}
	record, err := reviewProvenance(b, in, msg, base)
	if err != nil {
		return err
	}
	return e.cfg.Runs.RecordReview(ctx, record)
}

func reviewProvenance(b *Binding, in *inputs, msg *kagent.Message, base string) (provenance.ReviewerRun, error) {
	if err := b.Runtime.Validate(); err != nil {
		return provenance.ReviewerRun{}, err
	}
	class, key := "unclassified", ""
	if in.issues != nil && in.issues.Issue != nil {
		key = in.issues.Issue.Key
		class = in.issues.taskClass
		if class == "" {
			return provenance.ReviewerRun{}, errors.New("review task class unavailable")
		}
	}
	var text strings.Builder
	for _, p := range msg.Parts {
		text.WriteString(p.Text)
	}
	record := provenance.New(*b.Runtime, base, text.String(), class)
	if err := record.Validate(); err != nil {
		return provenance.ReviewerRun{}, err
	}
	return provenance.ReviewerRun{
		MessageID: msg.MessageID, Repository: b.repo(), Agent: agentName(b.Agent), IssueKey: key, Record: record,
	}, nil
}
