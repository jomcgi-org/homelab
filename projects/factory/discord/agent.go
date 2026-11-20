package discord

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
)

// Agent answers one turn.
type Agent interface {
	Turn(ctx context.Context, messageID, prompt string) (string, error)
}

// Kagent runs each turn on one fixed Agent in a fresh session.
type Kagent struct {
	A2A     *kagent.A2A
	Agent   kagent.AgentRef
	Poll    time.Duration
	Timeout time.Duration
}

// ErrTurnFailed is a turn that ended in a state other than completed.
var ErrTurnFailed = errors.New("discord: agent turn did not complete")

// Turn sends prompt and waits for the answer. The Discord message ID makes
// the A2A message ID, so a resend after an ambiguous outcome is the same
// message to kagent.
func (k *Kagent) Turn(ctx context.Context, messageID, prompt string) (string, error) {
	timeout := k.Timeout
	if timeout <= 0 {
		timeout = 3 * time.Minute
	}
	poll := k.Poll
	if poll <= 0 {
		poll = 2 * time.Second
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	msg := kagent.Message{MessageID: "discord-" + messageID, Parts: []kagent.Part{{Text: prompt}}}
	task, err := k.A2A.Send(ctx, k.Agent, msg)
	if errors.Is(err, kagent.ErrAmbiguous) {
		// kagent answers a repeated task-opening message with its task.
		task, err = k.A2A.Send(ctx, k.Agent, msg)
	}
	if err != nil {
		return "", err
	}
	for !kagent.Terminal(task.Status.State) {
		if err := sleep(ctx, poll); err != nil {
			return "", err
		}
		next, err := k.A2A.GetTask(ctx, k.Agent, task.ID)
		if err != nil {
			if ctx.Err() != nil {
				return "", err
			}
			continue // A transient read error; the next poll tries again.
		}
		task = next
	}
	if task.Status.State != kagent.StateCompleted {
		return "", fmt.Errorf("%w: %s", ErrTurnFailed, task.Status.State)
	}
	return AnswerText(task), nil
}

// AnswerText is the agent's final answer: its status message, else its last
// agent message, else its text artifacts.
func AnswerText(t *kagent.Task) string {
	if t.Status.Message != nil {
		if s := partsText(t.Status.Message.Parts); s != "" {
			return s
		}
	}
	for i := len(t.History) - 1; i >= 0; i-- {
		if m := t.History[i]; m != nil && m.Role != "ROLE_USER" {
			if s := partsText(m.Parts); s != "" {
				return s
			}
		}
	}
	var b strings.Builder
	for _, a := range t.Artifacts {
		if a != nil {
			b.WriteString(partsText(a.Parts))
		}
	}
	return strings.TrimSpace(b.String())
}

func partsText(parts []kagent.Part) string {
	var b strings.Builder
	for _, p := range parts {
		b.WriteString(p.Text)
	}
	return strings.TrimSpace(b.String())
}
