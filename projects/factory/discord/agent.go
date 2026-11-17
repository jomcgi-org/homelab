package discord

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
)

// Agent answers one turn.
type Agent interface {
	Turn(ctx context.Context, messageID, prompt string) (string, error)
}

// SessionDeleter deletes a finished turn's kagent session.
type SessionDeleter interface {
	DeleteSession(ctx context.Context, id string) (*kagent.Session, error)
}

// Kagent runs each turn on one fixed kagent Agent in a fresh session, then
// deletes the session: continuity comes from Hindsight recall, so sessions
// stay short and a stuck one never stops the bot (HOME-162 "Short
// sessions"). Message text never chooses the Agent.
type Kagent struct {
	A2A      *kagent.A2A
	Sessions SessionDeleter
	Agent    kagent.AgentRef
	Poll     time.Duration
	Timeout  time.Duration
	Log      *slog.Logger
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
	if task.ContextID != "" && k.Sessions != nil {
		defer k.deleteSession(task.ContextID)
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

// deleteSession runs after the turn on its own deadline, so a cancelled
// turn still cleans up. kagent refuses while the turn settles, so it retries.
func (k *Kagent) deleteSession(id string) {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	for {
		_, err := k.Sessions.DeleteSession(ctx, id)
		if err == nil || errors.Is(err, kagent.ErrSessionNotFound) {
			return
		}
		if sleep(ctx, 5*time.Second) != nil {
			if k.Log != nil {
				k.Log.Warn("kagent session not deleted", "session", id, "error", err)
			}
			return
		}
	}
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
