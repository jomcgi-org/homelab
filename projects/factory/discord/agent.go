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

// ReasonContentPolicy is the failure reason of a turn that the model
// provider's safety filter refused. The Hermes adapter puts it in the failed
// task's status message (kagent fork, HOME-909).
const ReasonContentPolicy = "content_policy_blocked"

// TurnFailure is a turn that ended in a state other than completed. Message
// is the task's status message: the harness wrote it, not the model.
type TurnFailure struct {
	State   string
	Message string
}

func (e *TurnFailure) Error() string {
	if e.Message != "" {
		return fmt.Sprintf("%s: %s: %.300s", ErrTurnFailed, e.State, e.Message)
	}
	return fmt.Sprintf("%s: %s", ErrTurnFailed, e.State)
}

func (e *TurnFailure) Unwrap() error { return ErrTurnFailed }

// ContentBlocked reports whether err is a turn that the model provider's
// safety filter refused.
func ContentBlocked(err error) bool {
	var f *TurnFailure
	return errors.As(err, &f) && f.State == kagent.StateFailed && strings.Contains(f.Message, ReasonContentPolicy)
}

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
	return awaitAnswer(ctx, k.A2A, k.Agent, task, poll)
}

// awaitAnswer polls task until it ends and returns its answer.
func awaitAnswer(ctx context.Context, a *kagent.A2A, agent kagent.AgentRef, task *kagent.Task, poll time.Duration) (string, error) {
	for !kagent.Terminal(task.Status.State) {
		if err := sleep(ctx, poll); err != nil {
			return "", err
		}
		next, err := a.GetTask(ctx, agent, task.ID)
		if err != nil {
			if ctx.Err() != nil {
				return "", err
			}
			continue // A transient read error; the next poll tries again.
		}
		task = next
	}
	if task.Status.State != kagent.StateCompleted {
		f := &TurnFailure{State: task.Status.State}
		if task.Status.Message != nil {
			f.Message = partsText(task.Status.Message.Parts)
		}
		return "", f
	}
	return AnswerText(task), nil
}

// deleteSession runs after the turn on its own deadline, so a cancelled
// turn still cleans up.
func (k *Kagent) deleteSession(id string) {
	if err := deleteSession(k.Sessions, id); err != nil && k.Log != nil {
		k.Log.Warn("kagent session not deleted", "session", id, "error", err)
	}
}

// deleteSession deletes a kagent session on its own deadline. kagent
// refuses while a turn settles, so it retries.
func deleteSession(d SessionDeleter, id string) error {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	for {
		_, err := d.DeleteSession(ctx, id)
		if err == nil || errors.Is(err, kagent.ErrSessionNotFound) {
			return nil
		}
		if sleep(ctx, 5*time.Second) != nil {
			return err
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
