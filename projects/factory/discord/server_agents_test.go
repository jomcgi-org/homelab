package discord

import (
	"context"
	"encoding/json"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
)

// fakeSessionAgent records each turn's session key and how many turns run at
// once, overall and per key. A turn waits for release when it is set.
type fakeSessionAgent struct {
	mu        sync.Mutex
	keys      []string
	prompts   []string
	images    [][]TurnImage
	active    map[string]int
	running   int
	maxAll    int
	maxPerKey int
	release   chan struct{}
}

func (f *fakeSessionAgent) Turn(ctx context.Context, key, _, prompt string, images ...TurnImage) (string, error) {
	f.mu.Lock()
	if f.active == nil {
		f.active = map[string]int{}
	}
	f.keys = append(f.keys, key)
	f.prompts = append(f.prompts, prompt)
	f.images = append(f.images, images)
	f.active[key]++
	f.running++
	f.maxAll = max(f.maxAll, f.running)
	f.maxPerKey = max(f.maxPerKey, f.active[key])
	release := f.release
	f.mu.Unlock()
	if release != nil {
		select {
		case <-release:
		case <-ctx.Done():
		}
	}
	f.mu.Lock()
	f.active[key]--
	f.running--
	f.mu.Unlock()
	return "noted", nil
}

func (f *fakeSessionAgent) count() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.keys)
}

// A server with its own agent answers from it in one session per channel;
// another server keeps Bosun.
func TestAServerAgentKeysItsSessionsByChannel(t *testing.T) {
	bosun := &fakeAgent{answer: "bosun"}
	b, mem, rep := newBridge(bosun)
	const other = "201"
	b.Config.Guilds[other] = Guild{}
	b.Config.UserLimit.Count = 10
	server := &fakeSessionAgent{}
	b.Servers = map[string]SessionAgent{guild: server}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	first := tagged("1", "hi")
	second := tagged("2", "and here")
	second.ChannelID = "301"
	thread := tagged("3", "in a thread")
	thread.ChannelID = "302"
	elsewhere := tagged("4", "hello")
	elsewhere.GuildID = other
	for _, m := range []Message{first, second, thread, elsewhere} {
		b.Handle(ctx, m)
	}
	waitFor(t, func() bool { return server.count() == 3 })
	waitFor(t, func() bool {
		bosun.mu.Lock()
		defer bosun.mu.Unlock()
		return len(bosun.prompts) == 1
	})
	waitFor(t, func() bool {
		rep.mu.Lock()
		defer rep.mu.Unlock()
		return len(rep.sent) == 4
	})
	cancel()
	b.Wait()
	server.mu.Lock()
	keys := append([]string(nil), server.keys...)
	server.mu.Unlock()
	got := map[string]bool{}
	for _, k := range keys {
		got[k] = true
	}
	if len(got) != 3 || !got[channel] || !got["301"] || !got["302"] {
		t.Errorf("session keys = %v, want one per channel", keys)
	}
	mem.mu.Lock()
	defer mem.mu.Unlock()
	if len(mem.recalls) != 4 {
		t.Errorf("recalls = %v, want one per turn", mem.recalls)
	}
}

// Channels run their turns in parallel, and one channel's session never
// gets two turns at once.
func TestChannelSessionsRunInParallelButOneTurnAtATimeEach(t *testing.T) {
	b, _, _ := newBridge(&fakeAgent{})
	b.Config.UserLimit.Count = 10
	server := &fakeSessionAgent{release: make(chan struct{})}
	b.Servers = map[string]SessionAgent{guild: server}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	a1, a2 := tagged("1", "one"), tagged("2", "two")
	other := tagged("3", "three")
	other.ChannelID = "301"
	for _, m := range []Message{a1, a2, other} {
		b.Handle(ctx, m)
	}
	waitFor(t, func() bool { return server.count() == 2 })
	server.mu.Lock()
	running, perKey := server.running, server.maxPerKey
	server.mu.Unlock()
	if running != 2 || perKey != 1 {
		t.Fatalf("running = %d, max per channel = %d; want both channels at once, one turn each", running, perKey)
	}
	for range 3 {
		server.release <- struct{}{}
	}
	waitFor(t, func() bool { return server.count() == 3 })
	cancel()
	b.Wait()
	server.mu.Lock()
	defer server.mu.Unlock()
	if server.maxPerKey != 1 || server.maxAll != 2 {
		t.Errorf("max per channel = %d, max overall = %d", server.maxPerKey, server.maxAll)
	}
}

func TestPolicyPlacesServerAgentsInAllowlistedServers(t *testing.T) {
	base := `"guilds":{"1":{}},"userLimit":{"count":5,"window":"10m"},"guildLimit":{"count":30,"window":"1h"},"globalLimit":{"count":200,"window":"24h"}`
	c, err := ParsePolicy([]byte(`{` + base + `,"serverAgents":{"1":"discord-1/homelab"}}`))
	want := kagent.AgentRef{Namespace: "discord-1", Name: "homelab"}
	if err != nil || c.ServerAgents["1"] != want {
		t.Fatalf("serverAgents = %+v, %v", c.ServerAgents, err)
	}
	for _, bad := range []string{`{"2":"discord-2/homelab"}`, `{"1":"homelab"}`, `{"1":"/homelab"}`, `{"1":"a/b/c"}`} {
		if _, err := ParsePolicy([]byte(`{` + base + `,"serverAgents":` + bad + `}`)); err == nil {
			t.Errorf("accepted serverAgents %s", bad)
		}
	}
}

// A server agent's channel session has its own request ID, so it never
// meets an operator thread's session or the old per-server one.
func TestKagentServerAgentUsesTheChannelRequestID(t *testing.T) {
	gw := &a2aServer{}
	srv := httptest.NewServer(gw)
	defer srv.Close()
	sessions := &fakeSessions{}
	k := newKagentOperator(t, srv.URL, sessions)
	k.RequestID = ChannelRequestID
	if answer, err := k.Turn(context.Background(), channel, "m1", "p"); err != nil || answer != "ok" {
		t.Fatalf("turn = %q, %v", answer, err)
	}
	if len(sessions.requests) != 1 || sessions.requests[0] != "discord-channel-"+channel {
		t.Errorf("requests = %v", sessions.requests)
	}
}

// With users: operator, only Joe's tags start turns, in each server, in
// each channel's session, recalled from that message's own server; another
// author's tag starts no turn and gets no reply.
func TestOperatorOnlyAnswersJoeInEveryServer(t *testing.T) {
	bosun := &fakeAgent{answer: "bosun"}
	b, mem, rep := newBridge(bosun)
	const other = "201"
	b.Config.Guilds[other] = Guild{}
	b.Config.UserLimit.Count = 10
	b.Config.OperatorOnly = true
	b.Config.Operator.UserID = joe
	server := &fakeSessionAgent{}
	b.Servers = map[string]SessionAgent{guild: server, other: server}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	fromJoe := tagged("1", "hi")
	fromJoe.Author = User{ID: joe, Username: "joe"}
	elsewhere := fromJoe
	elsewhere.ID, elsewhere.GuildID, elsewhere.ChannelID = "2", other, "401"
	stranger := tagged("3", "hello")
	strangerElsewhere := tagged("4", "hello")
	strangerElsewhere.GuildID = other
	for _, m := range []Message{fromJoe, stranger, elsewhere, strangerElsewhere} {
		b.Handle(ctx, m)
	}
	waitFor(t, func() bool { return server.count() == 2 })
	waitFor(t, func() bool {
		rep.mu.Lock()
		defer rep.mu.Unlock()
		return len(rep.sent) == 2
	})
	cancel()
	b.Wait()
	bosun.mu.Lock()
	if len(bosun.prompts) != 0 {
		t.Errorf("Bosun ran %d turns, want none", len(bosun.prompts))
	}
	bosun.mu.Unlock()
	rep.mu.Lock()
	if len(rep.sent) != 2 {
		t.Errorf("replies = %d, want Joe's two only", len(rep.sent))
	}
	rep.mu.Unlock()
	mem.mu.Lock()
	defer mem.mu.Unlock()
	if len(mem.recalls) != 2 || mem.recalls[0] == mem.recalls[1] {
		t.Errorf("recalls = %v, want one from each server's bank", mem.recalls)
	}
}

// With users: operator and no readable operator ID, nobody gets a turn.
func TestOperatorOnlyWithoutTheOperatorIDAnswersNobody(t *testing.T) {
	bosun := &fakeAgent{answer: "bosun"}
	b, _, rep := newBridge(bosun)
	b.Config.OperatorOnly = true
	server := &fakeSessionAgent{}
	b.Servers = map[string]SessionAgent{guild: server}
	ctx, cancel := context.WithCancel(context.Background())
	m := tagged("1", "hi")
	m.Author = User{ID: joe, Username: "joe"}
	b.Handle(ctx, m)
	b.Handle(ctx, tagged("2", "hi"))
	cancel()
	b.Wait()
	bosun.mu.Lock()
	defer bosun.mu.Unlock()
	rep.mu.Lock()
	defer rep.mu.Unlock()
	if server.count() != 0 || len(bosun.prompts) != 0 || len(rep.sent) != 0 {
		t.Errorf("turns = %d + %d, replies = %d; want none", server.count(), len(bosun.prompts), len(rep.sent))
	}
}

// A reply to Bosun's message starts a turn without the ping and puts the
// replied-to message in the prompt; a reply to someone else needs a tag.
// The users: operator gate still applies.
func TestAReplyToBosunIsATagAndCarriesWhatItReplies(t *testing.T) {
	b, _, _ := newBridge(&fakeAgent{})
	b.Config.UserLimit.Count = 10
	b.Config.OperatorOnly = true
	b.Config.Operator.UserID = joe
	server := &fakeSessionAgent{}
	b.Servers = map[string]SessionAgent{guild: server}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	reply := func(id string, to User, text string) Message {
		m := Message{
			ID: id, ChannelID: channel, GuildID: guild, Type: 19, Content: "go on",
			Author: User{ID: joe, Username: "joe"}, Timestamp: tagged(id, "").Timestamp,
			Referenced: &Referenced{ID: "90", Author: to, Content: text},
		}
		return m
	}
	toBosun := reply("1", User{ID: bot, Username: "Bosun", Bot: true}, "The plan has three steps.")
	toAnn := reply("2", User{ID: "u1", Username: "ann"}, "lunch?")
	taggedReply := reply("3", User{ID: "u1", Username: "ann"}, "what about dinner")
	taggedReply.Content = "<@" + bot + "> what does ann mean"
	taggedReply.Mentions = []User{{ID: bot}}
	strangerToBosun := reply("4", User{ID: bot, Username: "Bosun", Bot: true}, "The plan has three steps.")
	strangerToBosun.Author = User{ID: "u1", Username: "ann"}
	if !b.Accept(toBosun) || b.Accept(toAnn) || !b.Accept(taggedReply) {
		t.Fatalf("accept: to Bosun %v, to ann %v, tagged %v", b.Accept(toBosun), b.Accept(toAnn), b.Accept(taggedReply))
	}
	for _, m := range []Message{toBosun, toAnn, taggedReply, strangerToBosun} {
		b.Handle(ctx, m)
	}
	waitFor(t, func() bool { return server.count() == 2 })
	cancel()
	b.Wait()
	server.mu.Lock()
	prompts := append([]string(nil), server.prompts...)
	server.mu.Unlock()
	if len(prompts) != 2 {
		t.Fatalf("prompts = %d, want Joe's reply to Bosun and his tagged reply", len(prompts))
	}
	want := []map[string]any{
		{"author": "Bosun (you)", "message": "The plan has three steps.", "from_you": true},
		{"author": "ann", "message": "what about dinner", "from_you": false},
	}
	for i, p := range prompts {
		var fields map[string]any
		if err := json.Unmarshal([]byte(p[strings.Index(p, "{"):]), &fields); err != nil {
			t.Fatal(err)
		}
		got, _ := fields["reply_to"].(map[string]any)
		if len(got) != 4 || got["id"] != "replied" || got["author"] != want[i]["author"] || got["message"] != want[i]["message"] || got["from_you"] != want[i]["from_you"] {
			t.Errorf("prompt %d reply_to = %v, want %v", i, got, want[i])
		}
	}
}

func TestPolicyReadsUsers(t *testing.T) {
	base := `"guilds":{"1":{}},"userLimit":{"count":5,"window":"10m"},"guildLimit":{"count":30,"window":"1h"},"globalLimit":{"count":200,"window":"24h"}`
	c, err := ParsePolicy([]byte(`{` + base + `,"users":"operator"}`))
	if err != nil || !c.OperatorOnly {
		t.Fatalf("config = %+v, %v", c, err)
	}
	if c, err := ParsePolicy([]byte(`{` + base + `}`)); err != nil || c.OperatorOnly {
		t.Fatalf("default users: %+v, %v", c, err)
	}
	if _, err := ParsePolicy([]byte(`{` + base + `,"users":"friends"}`)); err == nil {
		t.Error("accepted users friends")
	}
}

// A session epoch puts every channel in a new session: kagent pins a session
// to the Agent revision that created it (HOME-915).
func TestSessionEpochStartsNewChannelSessions(t *testing.T) {
	if got := EpochChannelRequestID("")(channel, 0); got != ChannelRequestID(channel, 0) {
		t.Errorf("no epoch = %q", got)
	}
	if got := EpochChannelRequestID("tools1")(channel, 2); got != "discord-channel-tools1-"+channel+"-2" {
		t.Errorf("epoch tools1 = %q", got)
	}
	base := `"guilds":{"1":{}},"userLimit":{"count":5,"window":"10m"},"guildLimit":{"count":30,"window":"1h"},"globalLimit":{"count":200,"window":"24h"}`
	c, err := ParsePolicy([]byte(`{` + base + `,"sessionEpoch":"tools1"}`))
	if err != nil || c.SessionEpoch != "tools1" {
		t.Fatalf("policy epoch = %q, %v", c.SessionEpoch, err)
	}
	for _, bad := range []string{`"Tools1"`, `"a-b"`, `"12345678901234567"`} {
		if _, err := ParsePolicy([]byte(`{` + base + `,"sessionEpoch":` + bad + `}`)); err == nil {
			t.Errorf("accepted sessionEpoch %s", bad)
		}
	}
}
