package discord

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"
)

// factMemory is a fakeMemory that also stores remembered facts.
type factMemory struct {
	fakeMemory
	facts []string // guild/channel/message: content
	err   error
}

func (f *factMemory) RetainFact(_ context.Context, g, c, id string, _ time.Time, content string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.err != nil {
		return f.err
	}
	f.facts = append(f.facts, g+"/"+c+"/"+id+": "+content)
	return nil
}

func rememberBridge(agent *fakeAgent) (*Bridge, *factMemory, *fakeReplies) {
	b, _, rep := newBridge(agent)
	mem := &factMemory{}
	b.Memory = mem
	b.Config.Operator.UserID = joe
	return b, mem, rep
}

func fromJoe(id, text string) Message {
	m := tagged(id, text)
	m.Author = User{ID: joe, Username: "joe"}
	return m
}

func TestRememberFact(t *testing.T) {
	cases := map[string]string{
		"remember the code word is heron":       "the code word is heron",
		"Remember that the code word is heron":  "the code word is heron",
		"REMEMBER: the code word is heron":      "the code word is heron",
		"remember, the code word is heron":      "the code word is heron",
		"please remember that I moved to Leith": "I moved to Leith",
		"Please, remember: bins go out Tuesday": "bins go out Tuesday",
		"remember that: the gate code is 4417":  "the gate code is 4417",
		"remember thatcher was PM":              "thatcher was PM",
	}
	for in, want := range cases {
		if got, ok := RememberFact(in); !ok || got != want {
			t.Errorf("RememberFact(%q) = %q, %v; want %q", in, got, ok, want)
		}
	}
	for _, in := range []string{
		"", "remember", "remember that", "remember:", "what is the code word?",
		"do you remember the code word", "remembered the code word", "remembering is hard",
		"remember when we went to Skye?", "I remember the code word",
	} {
		if got, ok := RememberFact(in); ok {
			t.Errorf("RememberFact(%q) = %q; want no fact", in, got)
		}
	}
}

func TestJoesRememberIsRetainedAsAFactAndAnswered(t *testing.T) {
	agent := &fakeAgent{answer: "Noted."}
	b, mem, rep := rememberBridge(agent)
	b.Turn(context.Background(), fromJoe("7", "remember that the code word is heron"))
	want := guild + "/" + channel + "/7: [kind=remembered] Joe asked Bosun to remember (channel " + channel + ", 2026-11-17T12:00:00Z): the code word is heron"
	if len(mem.facts) != 1 || mem.facts[0] != want {
		t.Fatalf("facts = %q, want %q", mem.facts, want)
	}
	if len(mem.retained) != 1 || !strings.HasPrefix(mem.retained[0], guild+"/"+channel+": joe (") {
		t.Errorf("the exchange retain changed: %v", mem.retained)
	}
	prompt := agent.prompts[0]
	if !strings.HasPrefix(prompt, RememberedNote+"\n\nA Discord user tagged you.") {
		t.Errorf("prompt does not start with the trusted note: %q", prompt)
	}
	var data map[string]any
	if err := json.Unmarshal([]byte(prompt[strings.Index(prompt, "{"):]), &data); err != nil || data["message"] != "remember that the code word is heron" {
		t.Errorf("prompt data = %v (%v)", data, err)
	}
	if len(rep.sent) != 1 {
		t.Errorf("sent %d replies, want 1", len(rep.sent))
	}
}

func TestOnlyJoesRememberIsRetained(t *testing.T) {
	agent := &fakeAgent{answer: "ok"}
	b, mem, _ := rememberBridge(agent)
	b.Config.OperatorOnly = false
	b.Turn(context.Background(), tagged("1", "remember that the code word is heron"))
	notJoe := fromJoe("2", "remember that the code word is heron")
	notJoe.Author.Bot = true
	b.Turn(context.Background(), notJoe)
	b.Turn(context.Background(), fromJoe("3", "what is the code word?"))
	for _, r := range mem.retained {
		if strings.Contains(r, "[kind=remembered]") {
			t.Errorf("an exchange holds a remembered fact: %q", r)
		}
	}
	if len(mem.facts) != 0 {
		t.Errorf("facts = %q, want none", mem.facts)
	}
	for _, p := range agent.prompts {
		if strings.Contains(p, RememberedNote) || strings.Contains(p, NotRememberedNote) {
			t.Errorf("a prompt has the remember note: %q", p)
		}
	}
	b.Config.Operator.UserID = ""
	b.Turn(context.Background(), fromJoe("4", "remember that the code word is heron"))
	if len(mem.facts) != 0 {
		t.Errorf("remembered without an operator ID: %q", mem.facts)
	}
}

func TestAFailedRememberStillAnswers(t *testing.T) {
	agent := &fakeAgent{answer: "Sorry, not saved."}
	b, mem, rep := rememberBridge(agent)
	mem.err = errors.New("hindsight down")
	b.Turn(context.Background(), fromJoe("7", "remember the code word is heron"))
	if len(agent.prompts) != 1 || !strings.HasPrefix(agent.prompts[0], NotRememberedNote+"\n\n") {
		t.Fatalf("prompts = %q", agent.prompts)
	}
	if len(rep.sent) != 1 || len(mem.retained) != 1 {
		t.Errorf("sent %d, retained %v: the turn did not go on", len(rep.sent), mem.retained)
	}
}

// A Memory without RetainFact gets the fact through Retain.
func TestRememberFallsBackToRetain(t *testing.T) {
	b, mem, _ := newBridge(&fakeAgent{answer: "ok"})
	b.Config.Operator.UserID = joe
	b.Turn(context.Background(), fromJoe("7", "remember the code word is heron"))
	if len(mem.retained) != 2 || !strings.HasPrefix(mem.retained[0], guild+"/"+channel+": [kind=remembered] ") {
		t.Errorf("retained = %q", mem.retained)
	}
}
