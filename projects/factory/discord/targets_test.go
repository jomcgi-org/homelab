package discord

import (
	"context"
	"strings"
	"testing"
)

func TestActionTargetsParse(t *testing.T) {
	if e, tg, err := ParseReact(`{"emoji":["🔥"],"target":"m2"}`); err != nil || e[0] != "🔥" || tg != "m2" {
		t.Errorf("ParseReact = %v %q %v", e, tg, err)
	}
	if n, tg, err := ParseThread(`{"name":"x","target":"replied"}`); err != nil || n != "x" || tg != "replied" {
		t.Errorf("ParseThread = %q %q %v", n, tg, err)
	}
	if tg, err := ParsePin(`{"target":"m1"}`); err != nil || tg != "m1" {
		t.Errorf("ParsePin = %q %v", tg, err)
	}
	if tg, err := ParseReply(`{"target":"replied"}`); err != nil || tg != "replied" {
		t.Errorf("ParseReply = %q %v", tg, err)
	}
	for _, spec := range []string{`{}`, `{"target":""}`, `{"target":"a b"}`, `{"target":"` + strings.Repeat("x", 33) + `"}`, `{"to":"m1"}`} {
		if _, err := ParseReply(spec); err == nil {
			t.Errorf("ParseReply(%s) accepted", spec)
		}
	}
}

// targetBridge answers with answer; the channel holds dave's message 50
// (m1) and Joe's tag 60 replies to dave's message 40.
func targetBridge(t *testing.T, answer string) (*actingReplies, *fakeAgent) {
	t.Helper()
	agent := &fakeAgent{answer: answer}
	b, _, _ := ingestBridge(agent)
	b.History = &channelHistory{msgs: []HistoryMessage{said("50", "dave", "I'm moving to Leith", 1)}}
	rep := &actingReplies{}
	b.Replies = rep
	m := tagged("60", "answer this")
	m.Type = 19
	m.Referenced = &Referenced{ID: "40", Author: User{ID: "9", Username: "dave"}, Content: "when is the pub?"}
	b.Turn(context.Background(), m)
	return rep, agent
}

func TestContextMessagesCarryIDs(t *testing.T) {
	_, agent := targetBridge(t, "ok")
	p := lastPrompt(agent)
	if lines := sinceLines(t, p); len(lines) != 1 || lines[0].ID != "m1" {
		t.Errorf("lines = %+v", lines)
	}
	if !strings.Contains(p, `"id":"replied"`) || strings.Contains(p, `"50"`) {
		t.Errorf("prompt = %s", p)
	}
}

func TestActionsTargetMessagesInTheTurn(t *testing.T) {
	answer := "Thursday.\n```reply\n{\"target\":\"replied\"}\n```\n" +
		"```react\n{\"emoji\":[\"🔥\"],\"target\":\"m1\"}\n```\n```pin\n{\"target\":\"m1\"}\n```"
	rep, _ := targetBridge(t, answer)
	if len(rep.sent) != 1 || rep.sent[0].MessageReference.MessageID != "40" ||
		rep.sent[0].AllowedMentions.RepliedUser || len(rep.sent[0].AllowedMentions.Parse) != 0 {
		t.Fatalf("sent %+v", rep.sent)
	}
	if got := strings.Join(rep.actions, ","); got != "react "+channel+"/50 🔥,pin "+channel+"/50" {
		t.Errorf("actions = %s", got)
	}
}

func TestATargetNotInTheTurnIsIgnored(t *testing.T) {
	// A raw ID, a label with no line and "replied" are all checked; 777 is
	// a real message elsewhere that the model must not reach.
	answer := "ok\n```reply\n{\"target\":\"777\"}\n```\n" +
		"```react\n{\"emoji\":[\"🔥\"],\"target\":\"m9\"}\n```\n```thread\n{\"name\":\"t\",\"target\":\"777\"}\n```"
	rep, _ := targetBridge(t, answer)
	if got := strings.Join(rep.actions, ","); got != "thread "+channel+"/60 t,react "+channel+"/60 🔥" {
		t.Errorf("actions = %s, want the tagged message", got)
	}
	if len(rep.sent) != 1 || rep.sent[0].MessageReference != nil {
		t.Errorf("sent %+v, want the reply in the thread", rep.sent)
	}
}

func TestPinWithABadTargetPinsTheAnswerAndPinWithoutOneToo(t *testing.T) {
	rep, _ := targetBridge(t, "ok\n```pin\n{\"target\":\"m7\"}\n```")
	if got := strings.Join(rep.actions, ","); got != "pin "+channel+"/r1" {
		t.Errorf("bad target: actions = %s", got)
	}
	rep, _ = targetBridge(t, "ok\n```pin\n{}\n```")
	if got := strings.Join(rep.actions, ","); got != "pin "+channel+"/r1" {
		t.Errorf("no target: actions = %s", got)
	}
}

func TestReplyToRepliedNeedsAReply(t *testing.T) {
	// Without a replied-to message, "replied" is not in the turn.
	if _, ok := (Targets{TargetTagged: "1"}).Resolve(TargetReplied, "1"); ok {
		t.Error("replied resolved with no replied-to message")
	}
	if id, ok := turnTargets(Message{ID: "1"}, []ChannelLine{{ID: "m1"}}).Resolve("m1", "1"); ok || id != "1" {
		t.Errorf("an ingest-buffer line with no message ID resolved to %q", id)
	}
}
