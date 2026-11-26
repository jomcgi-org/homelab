package discord

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
)

const (
	joe       = "700"
	opChannel = "350" // The operator channel.
	opThread  = "400" // A thread under it.
	pubThread = "410" // A thread under a public channel.
)

// Text that must never reach an operator prompt.
var leaks = []string{"OTHER-AUTHOR", "BOT-TEXT", "WEBHOOK-TEXT", "FORWARDED-TEXT", "REPLY-PREVIEW", "SYSTEM-TEXT", "MEMORY"}

type fakeChannels struct {
	mu    sync.Mutex
	reads []string
	err   error
}

func (f *fakeChannels) Channel(_ context.Context, id string) (Channel, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.reads = append(f.reads, id)
	if f.err != nil {
		return Channel{}, f.err
	}
	switch id {
	case opThread:
		return Channel{ID: id, Type: 11, ParentID: opChannel}, nil
	case pubThread:
		return Channel{ID: id, Type: 11, ParentID: channel}, nil
	}
	return Channel{ID: id, Type: 0}, nil
}

type threadHistory struct {
	msgs []HistoryMessage
}

func (f *threadHistory) MessagesBefore(_ context.Context, _, _ string) ([]HistoryMessage, error) {
	return f.msgs, nil
}

// fakeOperator is an operator Agent whose sessions outlive a bridge: a
// restarted bridge given the same fakeOperator finds the same sessions.
type fakeOperator struct {
	mu       sync.Mutex
	sessions map[string]string // thread -> context ID
	turns    map[string]int    // context ID -> turns
	prompts  []string
	contexts []string
}

func (f *fakeOperator) Session(_ context.Context, thread string) (OperatorSession, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.sessions == nil {
		f.sessions, f.turns = map[string]string{}, map[string]int{}
	}
	id, ok := f.sessions[thread]
	if !ok {
		id = "ctx-" + thread
		f.sessions[thread] = id
	}
	return OperatorSession{ContextID: id, Fresh: f.turns[id] == 0}, nil
}

func (f *fakeOperator) Turn(_ context.Context, thread, _, prompt string) (string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	id := f.sessions[thread]
	f.turns[id]++
	f.prompts = append(f.prompts, prompt)
	f.contexts = append(f.contexts, id)
	return "noted", nil
}

func (f *fakeOperator) wait(n int) []string {
	deadline := time.Now().Add(5 * time.Second)
	for {
		f.mu.Lock()
		got := append([]string(nil), f.prompts...)
		f.mu.Unlock()
		if len(got) >= n || time.Now().After(deadline) {
			return got
		}
		time.Sleep(10 * time.Millisecond)
	}
}

func newOperatorBridge(op *fakeOperator, history *threadHistory) (*Bridge, *fakeAgent, *fakeMemory, *fakeReplies) {
	agent := &fakeAgent{answer: "public"}
	b, mem, rep := newBridge(agent)
	b.Config.Guilds[guild] = Guild{Ingest: true}
	b.Config.Operator = Operator{Guild: guild, Channels: []string{opChannel}, UserID: joe}
	b.Ingest = &Ingestor{Memory: mem, Log: b.Log}
	b.Operator, b.Channels = op, &fakeChannels{}
	if history != nil {
		b.History = history
	}
	return b, agent, mem, rep
}

// event decodes a gateway MESSAGE_CREATE body, so the test covers the
// fields Discord sends, such as referenced_message and message_snapshots.
func event(t *testing.T, raw string) Message {
	t.Helper()
	var m Message
	if err := json.Unmarshal([]byte(raw), &m); err != nil {
		t.Fatal(err)
	}
	return m
}

func historyEvent(t *testing.T, raw string) HistoryMessage {
	t.Helper()
	var m HistoryMessage
	if err := json.Unmarshal([]byte(raw), &m); err != nil {
		t.Fatal(err)
	}
	return m
}

const (
	stamp = `"timestamp":"2026-11-17T12:00:00Z","guild_id":"200"`
	// A reply whose preview is another author's text.
	replyPreview = `"type":19,"message_reference":{"type":0,"message_id":"1"},"referenced_message":{"id":"1","content":"REPLY-PREVIEW","author":{"id":"800","username":"eve"}}`
	forward      = `"message_reference":{"type":1,"message_id":"2"},"message_snapshots":[{"message":{"content":"FORWARDED-TEXT"}}]`
)

func liveEvents(t *testing.T, ch string) []Message {
	at := func(id, body string) Message {
		return event(t, `{"id":"`+id+`","channel_id":"`+ch+`",`+stamp+`,`+body+`}`)
	}
	return []Message{
		at("11", `"author":{"id":"800","username":"eve"},"content":"OTHER-AUTHOR <@100>","mentions":[{"id":"100"}]`),
		at("12", `"author":{"id":"700","username":"joe","bot":true},"content":"BOT-TEXT"`),
		at("13", `"author":{"id":"700","username":"joe"},"webhook_id":"9","content":"WEBHOOK-TEXT"`),
		at("14", `"author":{"id":"700","username":"joe"},"content":"FORWARDED-TEXT",`+forward),
		at("15", `"author":{"id":"700","username":"joe"},"type":7,"content":"SYSTEM-TEXT"`),
		at("16", `"author":{"id":"100","username":"bosun"},"content":"BOT-TEXT"`),
		at("17", `"author":{"id":"700","username":"joe"},"content":"<@100> plan the move",`+replyPreview+`,"mentions":[{"id":"100"}]`),
	}
}

// In a session thread only Joe's own messages reach the operator Agent. No
// other author, bot, webhook, forward, system message or reply preview does,
// and the thread reaches neither Bosun's memory nor the public Agent.
func TestOperatorTurnsTakeOnlyJoesOwnLiveMessages(t *testing.T) {
	op := &fakeOperator{sessions: map[string]string{opThread: "ctx"}, turns: map[string]int{"ctx": 1}}
	b, agent, mem, rep := newOperatorBridge(op, nil)
	ctx, cancel := context.WithCancel(context.Background())
	for _, m := range liveEvents(t, opThread) {
		b.Handle(ctx, m)
	}
	prompts := op.wait(1)
	time.Sleep(50 * time.Millisecond) // Let any wrongly queued turn run.
	cancel()
	b.Wait()
	if len(prompts) != 1 {
		t.Fatalf("operator turns = %d, want 1: %q", len(prompts), prompts)
	}
	for _, s := range leaks {
		if strings.Contains(prompts[0], s) {
			t.Errorf("prompt holds %s: %s", s, prompts[0])
		}
	}
	var data map[string]any
	if err := json.Unmarshal([]byte(prompts[0][strings.Index(prompts[0], "{"):]), &data); err != nil || data["message"] != "plan the move" {
		t.Fatalf("prompt data = %v (%v)", data, err)
	}
	if _, ok := data["earlier_messages"]; ok {
		t.Error("an existing session got restored messages")
	}
	if err := b.Ingest.Flush(context.Background(), guild, opThread); err != nil {
		t.Fatal(err)
	}
	if len(agent.prompts)+len(mem.recalls)+len(mem.retained) != 0 {
		t.Errorf("the operator thread reached Bosun: %q %v %v", agent.prompts, mem.recalls, mem.retained)
	}
	if len(rep.sent) != 1 || rep.sent[0].Components[0].Content != "noted" || rep.sent[0].MessageReference.MessageID != "17" {
		t.Errorf("replies = %+v", rep.sent)
	}
}

// A new session gets Joe's earlier messages from its thread, oldest first,
// and nothing else from that history.
func TestARestoredSessionGetsOnlyJoesEarlierMessages(t *testing.T) {
	at := func(id, body string) HistoryMessage {
		return historyEvent(t, `{"id":"`+id+`","channel_id":"400","timestamp":"2026-11-17T11:`+id+`:00Z",`+body+`}`)
	}
	// Newest first, as Discord returns them.
	history := &threadHistory{msgs: []HistoryMessage{
		at("09", `"author":{"id":"700"},"content":"<@100> second",`+replyPreview),
		at("08", `"author":{"id":"100","bot":true},"components":[{"type":10,"content":"BOT-TEXT"}]`),
		at("07", `"author":{"id":"800"},"content":"OTHER-AUTHOR"`),
		at("06", `"author":{"id":"700"},"webhook_id":"9","content":"WEBHOOK-TEXT"`),
		at("05", `"author":{"id":"700"},"content":"FORWARDED-TEXT",`+forward),
		at("04", `"author":{"id":"700"},"type":18,"content":"SYSTEM-TEXT"`),
		at("03", `"author":{"id":"700","bot":true},"content":"BOT-TEXT"`),
		at("02", `"author":{"id":"700"},"content":"first"`),
	}}
	op := &fakeOperator{}
	b, _, mem, _ := newOperatorBridge(op, history)
	m := liveEvents(t, opThread)[6]
	b.operatorTurn(context.Background(), m)
	if len(op.prompts) != 1 {
		t.Fatalf("turns = %d", len(op.prompts))
	}
	p := op.prompts[0]
	for _, s := range leaks {
		if strings.Contains(p, s) {
			t.Errorf("restored prompt holds %s: %s", s, p)
		}
	}
	var data struct {
		Message string         `json:"message"`
		Earlier []OperatorLine `json:"earlier_messages"`
	}
	if err := json.Unmarshal([]byte(p[strings.Index(p, "{"):]), &data); err != nil {
		t.Fatal(err)
	}
	want := []OperatorLine{{"2026-11-17T11:02:00Z", "first"}, {"2026-11-17T11:09:00Z", "second"}}
	if data.Message != "plan the move" || len(data.Earlier) != 2 || data.Earlier[0] != want[0] || data.Earlier[1] != want[1] {
		t.Errorf("prompt data = %+v", data)
	}
	if len(mem.recalls)+len(mem.retained) != 0 {
		t.Errorf("an operator turn used Bosun's memory: %v %v", mem.recalls, mem.retained)
	}
	// The next turn is in a session with a turn: nothing is restored again.
	b.operatorTurn(context.Background(), liveEvents(t, opThread)[6])
	if strings.Contains(op.prompts[1], "earlier_messages") {
		t.Errorf("second turn restored again: %s", op.prompts[1])
	}
}

// Every turn in a thread uses the thread's one session, and a restarted
// bridge keeps it: the same context, and no restored history.
func TestAThreadKeepsOneSessionAcrossTurnsAndRestarts(t *testing.T) {
	op := &fakeOperator{}
	history := &threadHistory{msgs: []HistoryMessage{historyEvent(t, `{"id":"1","author":{"id":"700"},"content":"earlier"}`)}}
	joeSays := func(id string) Message {
		m := liveEvents(t, opThread)[6]
		m.ID = id
		return m
	}
	for _, ids := range [][]string{{"21", "22"}, {"23"}} {
		b, _, _, _ := newOperatorBridge(op, history)
		want := len(op.wait(0)) + len(ids)
		ctx, cancel := context.WithCancel(context.Background())
		for _, id := range ids {
			b.Handle(ctx, joeSays(id))
		}
		if got := len(op.wait(want)); got != want {
			t.Fatalf("turns = %d, want %d", got, want)
		}
		cancel()
		b.Wait()
	}
	if len(op.contexts) != 3 || op.contexts[0] != op.contexts[1] || op.contexts[1] != op.contexts[2] {
		t.Fatalf("contexts = %v", op.contexts)
	}
	restored := 0
	for _, p := range op.prompts {
		if strings.Contains(p, "earlier_messages") {
			restored++
		}
	}
	if restored != 1 {
		t.Errorf("restored into %d turns, want only the session's first", restored)
	}
}

// A channel the bridge has not placed is held, read once, then handled: an
// operator thread goes to the operator, a public thread to Bosun. A failed
// read drops the held messages; the next message reads again.
func TestChannelsArePlacedBeforeAnythingPasses(t *testing.T) {
	op := &fakeOperator{}
	b, agent, _, _ := newOperatorBridge(op, nil)
	chans := &fakeChannels{err: errors.New("down")}
	b.Channels = chans
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	pub := tagged("31", "hi")
	pub.ChannelID = pubThread
	b.Handle(ctx, pub)
	waitFor(t, func() bool { b.mu.Lock(); defer b.mu.Unlock(); return len(b.pending) == 0 })
	if len(agent.prompts) != 0 {
		t.Fatal("a message passed before its channel was placed")
	}
	chans.mu.Lock()
	chans.err = nil
	chans.mu.Unlock()
	pub.ID = "32"
	b.Handle(ctx, pub)
	if waitTurns(agent, 1) != 1 {
		t.Fatal("a public thread got no public turn")
	}
	m := liveEvents(t, opThread)[6]
	b.Handle(ctx, m)
	if len(op.wait(1)) != 1 {
		t.Fatal("the operator thread got no operator turn")
	}
	time.Sleep(50 * time.Millisecond) // Let any wrongly queued public turn run.
	agent.mu.Lock()
	public := len(agent.prompts)
	agent.mu.Unlock()
	if public != 1 {
		t.Error("the operator thread reached the public Agent")
	}
	chans.mu.Lock()
	reads := len(chans.reads)
	chans.mu.Unlock()
	b.Handle(ctx, func() Message { m := m; m.ID = "33"; return m }())
	op.wait(2)
	chans.mu.Lock()
	defer chans.mu.Unlock()
	if len(chans.reads) != reads {
		t.Error("a placed channel was read again")
	}
}

func waitFor(t *testing.T, ok func() bool) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for !ok() {
		if time.Now().After(deadline) {
			t.Fatal("timed out")
		}
		time.Sleep(10 * time.Millisecond)
	}
}

// Without a readable user ID the operator channels stay closed: no operator
// turn, no public turn and no ingest.
func TestOperatorChannelsStayClosedWithoutJoesID(t *testing.T) {
	op := &fakeOperator{}
	b, agent, mem, rep := newOperatorBridge(op, nil)
	b.Config.Operator.UserID = ""
	ctx, cancel := context.WithCancel(context.Background())
	for _, m := range liveEvents(t, opThread) {
		b.Handle(ctx, m)
	}
	m := liveEvents(t, opChannel)[6]
	b.Handle(ctx, m)
	time.Sleep(100 * time.Millisecond)
	cancel()
	b.Wait()
	_ = b.Ingest.Flush(context.Background(), guild, opThread)
	if len(op.prompts)+len(agent.prompts)+len(mem.retained)+len(rep.sent) != 0 {
		t.Errorf("closed operator channels passed messages: %q %q %v %v", op.prompts, agent.prompts, mem.retained, rep.sent)
	}
}

// Joe tagging Bosun in the operator channel itself gets a fixed pointer to
// threads, and starts no turn.
func TestTheOperatorChannelAsksForAThread(t *testing.T) {
	op := &fakeOperator{}
	b, agent, _, rep := newOperatorBridge(op, nil)
	ctx, cancel := context.WithCancel(context.Background())
	b.Handle(ctx, liveEvents(t, opChannel)[6])
	b.Handle(ctx, liveEvents(t, opChannel)[0])
	waitFor(t, func() bool { rep.mu.Lock(); defer rep.mu.Unlock(); return len(rep.sent) > 0 })
	cancel()
	b.Wait()
	if len(op.prompts)+len(agent.prompts) != 0 || len(rep.sent) != 1 || rep.sent[0].Components[0].Content != ReplyOperatorThread {
		t.Errorf("operator channel: %q %q %+v", op.prompts, agent.prompts, rep.sent)
	}
}

// With no operator in the policy, a message from Joe in any thread takes the
// public path exactly as before, and no channel is looked up.
func TestNoOperatorLeavesThePublicPathAsItWas(t *testing.T) {
	op := &fakeOperator{}
	b, agent, mem, _ := newOperatorBridge(op, nil)
	b.Config.Operator = Operator{}
	b.Config.Guilds[guild] = Guild{}
	b.Ingest = nil
	chans := &fakeChannels{}
	b.Channels = chans
	m := liveEvents(t, opThread)[6]
	ctx, cancel := context.WithCancel(context.Background())
	b.Handle(ctx, m)
	if waitTurns(agent, 1) != 1 {
		t.Fatal("no public turn")
	}
	cancel()
	b.Wait()
	if want := Prompt(m, Place{}, "plan the move", []string{"Earlier: the code word is heron."}); agent.prompts[0] != want {
		t.Errorf("prompt = %q, want %q", agent.prompts[0], want)
	}
	if len(op.prompts) != 0 || len(chans.reads) != 0 || len(mem.recalls) != 1 || len(mem.retained) != 1 {
		t.Errorf("operator off: ops %d reads %v recalls %v retained %v", len(op.prompts), chans.reads, mem.recalls, mem.retained)
	}
	c, err := ParsePolicy([]byte(`{"guilds":{"1":{}},"userLimit":{"count":5,"window":"10m"},"guildLimit":{"count":30,"window":"1h"},"globalLimit":{"count":200,"window":"24h"}}`))
	if err != nil || c.Operator.enabled() {
		t.Errorf("policy without operator = %+v, %v", c.Operator, err)
	}
}

func TestPolicyPlacesTheOperatorInAnAllowlistedServer(t *testing.T) {
	base := `"guilds":{"1":{}},"userLimit":{"count":5,"window":"10m"},"guildLimit":{"count":30,"window":"1h"},"globalLimit":{"count":200,"window":"24h"}`
	c, err := ParsePolicy([]byte(`{` + base + `,"operator":{"guild":"1","channels":["5"]}}`))
	if err != nil || c.Operator.Guild != "1" || len(c.Operator.Channels) != 1 || c.Operator.UserID != "" {
		t.Fatalf("operator = %+v, %v", c.Operator, err)
	}
	for _, bad := range []string{`{"guild":"2","channels":["5"]}`, `{"guild":"1","channels":[]}`, `{"guild":"1","channels":[""]}`, `{"channels":["5"]}`} {
		if _, err := ParsePolicy([]byte(`{` + base + `,"operator":` + bad + `}`)); err == nil {
			t.Errorf("accepted operator %s", bad)
		}
	}
}

// fakeSessions is kagent's session store: CreateSession returns the same
// session for the same request ID, across operator instances.
type fakeSessions struct {
	mu       sync.Mutex
	byReq    map[string]*kagent.Session
	requests []string
}

func (f *fakeSessions) CreateSession(_ context.Context, agent kagent.AgentRef, req string) (*kagent.Session, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.requests = append(f.requests, req)
	if f.byReq == nil {
		f.byReq = map[string]*kagent.Session{}
	}
	s, ok := f.byReq[req]
	if !ok {
		id := "sess-" + req
		s = &kagent.Session{ID: id, ContextID: id, AgentNamespace: agent.Namespace, AgentName: agent.Name, State: kagent.RuntimeReady}
		f.byReq[req] = s
	}
	c := *s
	return &c, nil
}

func (f *fakeSessions) RevertSession(_ context.Context, id string) (*kagent.Session, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	for _, s := range f.byReq {
		if s.ID == id {
			s.FailureReason = ""
			c := *s
			return &c, nil
		}
	}
	return nil, kagent.ErrSessionNotFound
}

// a2aServer is kagent's A2A gateway for one Agent: each sent message opens
// a completed task in its context.
type a2aServer struct {
	mu    sync.Mutex
	tasks map[string]int // context ID -> tasks
	sends []kagent.Message
}

func (s *a2aServer) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/agents/factory-discord-operator/bosun-operator" {
		w.WriteHeader(http.StatusNotFound)
		return
	}
	var req struct {
		ID     string          `json:"id"`
		Method string          `json:"method"`
		Params json.RawMessage `json:"params"`
	}
	_ = json.NewDecoder(r.Body).Decode(&req)
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.tasks == nil {
		s.tasks = map[string]int{}
	}
	var result any
	switch req.Method {
	case "SendMessage":
		var p struct {
			Message kagent.Message `json:"message"`
		}
		_ = json.Unmarshal(req.Params, &p)
		s.sends = append(s.sends, p.Message)
		s.tasks[p.Message.ContextID]++
		result = map[string]any{"task": map[string]any{"id": p.Message.MessageID, "contextId": p.Message.ContextID, "status": map[string]any{
			"state": kagent.StateCompleted, "message": map[string]any{"messageId": "a", "role": "ROLE_AGENT", "parts": []any{map[string]any{"text": "ok"}}},
		}}}
	case "ListTasks":
		var p struct {
			ContextID string `json:"contextId"`
		}
		_ = json.Unmarshal(req.Params, &p)
		n := s.tasks[p.ContextID]
		tasks := []any{}
		for range n {
			tasks = append(tasks, map[string]any{"id": "t", "contextId": p.ContextID, "status": map[string]any{"state": kagent.StateCompleted}})
		}
		result = map[string]any{"tasks": tasks, "totalSize": n, "pageSize": 100, "nextPageToken": ""}
	default:
		w.WriteHeader(http.StatusBadRequest)
		return
	}
	_ = json.NewEncoder(w).Encode(map[string]any{"jsonrpc": "2.0", "id": req.ID, "result": result})
}

func newKagentOperator(t *testing.T, url string, sessions *fakeSessions) *KagentOperator {
	t.Helper()
	tokenFile := t.TempDir() + "/token"
	if err := os.WriteFile(tokenFile, []byte("kagent-token"), 0o600); err != nil {
		t.Fatal(err)
	}
	return &KagentOperator{
		A2A:      &kagent.A2A{BaseURL: url, Tokens: &kagent.TokenSource{Path: tokenFile}},
		Sessions: sessions,
		Agent:    kagent.AgentRef{Namespace: "factory-discord-operator", Name: "bosun-operator"},
		Poll:     10 * time.Millisecond,
	}
}

// The kagent operator keeps one session per thread: turns reuse it, a new
// instance (a restarted bridge) finds it by its request ID and sees its
// turns, and a deleted session moves the thread to a new, fresh one.
func TestKagentOperatorRestoresTheThreadSession(t *testing.T) {
	gw := &a2aServer{}
	srv := httptest.NewServer(gw)
	defer srv.Close()
	sessions := &fakeSessions{}
	ctx := context.Background()

	k := newKagentOperator(t, srv.URL, sessions)
	s, err := k.Session(ctx, opThread)
	if err != nil || !s.Fresh || s.ContextID != "sess-"+OperatorRequestID(opThread, 0) {
		t.Fatalf("first session = %+v, %v", s, err)
	}
	for _, id := range []string{"m1", "m2"} {
		if answer, err := k.Turn(ctx, opThread, id, "p"); err != nil || answer != "ok" {
			t.Fatalf("turn %s = %q, %v", id, answer, err)
		}
	}
	if len(sessions.requests) != 1 {
		t.Errorf("one instance created sessions %v", sessions.requests)
	}

	restarted := newKagentOperator(t, srv.URL, sessions)
	r, err := restarted.Session(ctx, opThread)
	if err != nil || r.Fresh || r.ContextID != s.ContextID {
		t.Fatalf("restored session = %+v, %v; want %s with turns", r, err, s.ContextID)
	}
	if _, err := restarted.Turn(ctx, opThread, "m3", "p"); err != nil {
		t.Fatal(err)
	}
	for i, m := range gw.sends {
		if m.ContextID != s.ContextID || m.MessageID != "discord-m"+string(rune('1'+i)) {
			t.Errorf("send %d = %+v", i, m)
		}
	}
	if other, _ := restarted.Session(ctx, "401"); other.ContextID == s.ContextID {
		t.Error("two threads share a session")
	}

	// kagent deleted the session: the next instance opens generation 1.
	sessions.mu.Lock()
	sessions.byReq[OperatorRequestID(opThread, 0)].State = kagent.RuntimeDeleted
	sessions.mu.Unlock()
	next, err := newKagentOperator(t, srv.URL, sessions).Session(ctx, opThread)
	if err != nil || !next.Fresh || next.ContextID != "sess-"+OperatorRequestID(opThread, 1) {
		t.Errorf("replacement session = %+v, %v", next, err)
	}
}

// A session from another Agent is refused, and a crashed one is reverted.
func TestKagentOperatorChecksAndRepairsTheSession(t *testing.T) {
	srv := httptest.NewServer(&a2aServer{})
	defer srv.Close()
	sessions := &fakeSessions{byReq: map[string]*kagent.Session{
		OperatorRequestID("1", 0): {ID: "x", ContextID: "x", AgentNamespace: "factory-discord-agent", AgentName: "bosun"},
		OperatorRequestID("2", 0): {ID: "y", ContextID: "y", AgentNamespace: "factory-discord-operator", AgentName: "bosun-operator", FailureReason: kagent.ReasonRuntimeCrashed},
	}}
	k := newKagentOperator(t, srv.URL, sessions)
	if _, err := k.Session(context.Background(), "1"); err == nil {
		t.Error("accepted another Agent's session")
	}
	if s, err := k.Session(context.Background(), "2"); err != nil || s.ContextID != "y" {
		t.Errorf("crashed session = %+v, %v", s, err)
	}
}
