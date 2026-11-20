package review

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"strings"

	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
	"github.com/jomcgi/homelab/projects/factory/connector/provenance"
)

type RunRecorder interface {
	RecordReview(context.Context, provenance.ReviewerRun) error
}

type GoalSessionCreator interface {
	CreateSession(context.Context, kagent.AgentRef, string) (*kagent.Session, error)
}

// prepareGoalDispatch is only legal before any send could have executed, or
// after a definitive refusal. Recompute every receipt input before each send.
// An unused session from earlier settings must never pin a changed receipt.
func (e *Engine) prepareGoalDispatch(ctx context.Context, b *Binding, in *inputs, base, prefix string, key []byte, d *GoalTask, msg *kagent.Message) error {
	msg.MessageID = prefix
	var revision string
	var record provenance.ReviewerRun
	if e.cfg.Runs != nil {
		var err error
		record, err = reviewProvenance(b, in, msg, base)
		if err != nil {
			return err
		}
		revision, err = e.cfg.Sessions.PromptRevision(ctx, b.Agent, b.Runtime.SystemPrompt)
		if err != nil {
			return err
		}
		data, _ := json.Marshal(record)
		mac := hmac.New(sha256.New, key)
		mac.Write(data)
		mac.Write([]byte{0})
		mac.Write([]byte(revision))
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
		if revision != "" && s.PreparedRevision != revision {
			if s.PreparedRevision != "" {
				next := sha256.Sum256([]byte(d.Nonce + "\x00" + s.ID))
				d.Nonce = hex.EncodeToString(next[:16])
				if err := e.cfg.GoalTasks.SaveGoalTask(*d); err != nil {
					return err
				}
			}
			return errors.New("goal session pinned a different revision")
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
	requestID := msg.MessageID
	key := "review-session/" + msg.MessageID
	if msg.ContextID == "" {
		cursor, err := e.cfg.GoalTasks.LoadGoalTask(key)
		if err != nil {
			return err
		}
		if cursor != nil {
			requestID = cursor.MessageID
		}
	}
	msg.ContextID, err = kagent.PinPromptSession(ctx, e.cfg.Sessions, b.Agent, requestID, msg.ContextID, b.Runtime.SystemPrompt, e.cfg.GoalSessions.CreateSession)
	if errors.Is(err, kagent.ErrCreatedRevisionMismatch) {
		next := sha256.Sum256([]byte(requestID))
		if saveErr := e.cfg.GoalTasks.SaveGoalTask(GoalTask{Key: key, MessageID: hex.EncodeToString(next[:]), CreatedAt: e.cfg.Now().UTC()}); saveErr != nil {
			return saveErr
		}
	}
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
