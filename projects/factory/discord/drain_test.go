package discord

import (
	"context"
	"testing"
	"time"
)

// gatedAgent answers a turn only after release closes.
type gatedAgent struct {
	fakeAgent
	started chan struct{}
	release chan struct{}
}

func (g *gatedAgent) Turn(ctx context.Context, id, prompt string) (string, error) {
	g.started <- struct{}{}
	select {
	case <-g.release:
	case <-ctx.Done():
		return "", ctx.Err()
	}
	return g.fakeAgent.Turn(ctx, id, prompt)
}

// A drain takes no new turn, waits for the in-flight turn and its reply,
// then reports that all ended.
func TestDrainFinishesTheInFlightTurnAndTakesNoNewOne(t *testing.T) {
	agent := &gatedAgent{fakeAgent: fakeAgent{answer: "done"}, started: make(chan struct{}, 4), release: make(chan struct{})}
	b, _, rep := newBridge(&agent.fakeAgent)
	b.Agent = agent
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	b.Handle(ctx, tagged("1", "hi"))
	<-agent.started
	drained := make(chan bool)
	go func() { drained <- b.Drain(5 * time.Second) }()
	waitFor(t, b.Draining)
	b.Handle(ctx, tagged("2", "a new tag"))
	select {
	case <-drained:
		t.Fatal("the drain ended before the in-flight turn")
	case <-time.After(100 * time.Millisecond):
	}
	close(agent.release)
	if !<-drained {
		t.Fatal("the drain timed out")
	}
	cancel()
	b.Wait()
	if n := len(agent.prompts); n != 1 {
		t.Errorf("turns = %d, want 1: the drain took a new tag", n)
	}
	if len(rep.sent) != 1 {
		t.Errorf("replies = %d, want the in-flight turn's reply", len(rep.sent))
	}
}

// A turn that outlasts the grace ends the drain with false; the caller then
// cancels it.
func TestDrainStopsWaitingAfterTheGrace(t *testing.T) {
	agent := &gatedAgent{started: make(chan struct{}, 4), release: make(chan struct{})}
	b, _, _ := newBridge(&agent.fakeAgent)
	b.Agent = agent
	ctx, cancel := context.WithCancel(context.Background())
	b.Handle(ctx, tagged("1", "hi"))
	<-agent.started
	if b.Drain(100 * time.Millisecond) {
		t.Error("the drain reported done with a turn in flight")
	}
	cancel()
	b.Wait()
}

// A message that only tags Bosun recalls with the text it replies to, or
// skips the recall, and still answers.
func TestAnEmptyTagRecallsTheReplyOrNothing(t *testing.T) {
	agent := &fakeAgent{answer: "ok"}
	b, mem, rep := newBridge(agent)
	only := tagged("1", "")
	only.Content = "<@" + bot + ">  "
	b.Turn(context.Background(), only)
	if len(mem.queries) != 0 {
		t.Errorf("recall queries = %q, want none", mem.queries)
	}
	reply := tagged("2", "")
	reply.Content = "<@" + bot + "> ?!"
	reply.Type = 19
	reply.Referenced = &Referenced{ID: "0", Author: User{ID: "u2", Username: "bea"}, Content: "the boiler is broken"}
	b.Turn(context.Background(), reply)
	if len(mem.queries) != 1 || mem.queries[0] != "the boiler is broken" {
		t.Errorf("recall queries = %q, want the replied-to text", mem.queries)
	}
	if len(agent.prompts) != 2 || len(rep.sent) != 2 {
		t.Errorf("turns = %d, replies = %d, want both answered", len(agent.prompts), len(rep.sent))
	}
	if q := recallQuery("", Message{}, []ChannelLine{{Text: "older words"}, {Text: "newest line"}, {Text: "🙂"}}); q != "newest line" {
		t.Errorf("query from channel lines = %q", q)
	}
}
