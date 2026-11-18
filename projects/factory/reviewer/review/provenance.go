package review

import (
	"context"
	"errors"
	"strings"

	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
	"github.com/jomcgi/homelab/projects/factory/connector/provenance"
)

type RunRecorder interface {
	RecordReview(context.Context, provenance.ReviewerRun) error
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

// Recover by the stable goal prefix before trying to record new inputs. An
// existing task keeps its original immutable receipt, even across label edits
// and runtime rollouts. Only a new send needs a new input-bound message ID.
func (e *Engine) existingGoalTask(ctx context.Context, b *Binding, prefix string) (*kagent.Task, string, error) {
	token := ""
	seen := map[string]bool{}
	for page := 0; page < maxSessionPages; page++ {
		p, err := e.tasks.ListTasks(ctx, b.Agent, "", "", token)
		if err != nil {
			return nil, "", err
		}
		if p == nil {
			return nil, "", errors.New("goal task listing unavailable")
		}
		for _, cursor := range p.Tasks {
			if cursor == nil {
				return nil, "", errors.New("invalid goal task cursor")
			}
			t, err := e.tasks.GetTask(ctx, b.Agent, cursor.ID)
			if err != nil || t == nil {
				return nil, "", errors.New("goal task history unavailable")
			}
			for _, m := range t.History {
				if m != nil && (m.MessageID == prefix || strings.HasPrefix(m.MessageID, prefix+"/inputs/")) {
					return t, m.MessageID, nil
				}
			}
		}
		if p.NextPageToken == "" {
			return nil, "", nil
		}
		if seen[p.NextPageToken] {
			break
		}
		token = p.NextPageToken
		seen[token] = true
	}
	return nil, "", errors.New("goal task listing incomplete")
}
