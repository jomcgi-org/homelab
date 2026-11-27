package discord

import (
	"context"
	"net/http/httptest"
	"testing"

	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
)

// A server with its own agent answers every channel's turns from that
// agent's one session, keyed by the server; another server keeps Bosun.
func TestAServerWithItsOwnAgentAnswersFromIt(t *testing.T) {
	bosun := &fakeAgent{answer: "bosun"}
	b, mem, rep := newBridge(bosun)
	const other = "201"
	b.Config.Guilds[other] = Guild{}
	b.Config.UserLimit.Count = 10
	server := &fakeOperator{}
	b.Servers = map[string]SessionAgent{guild: server}
	server.Session(context.Background(), guild)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	first := tagged("1", "hi")
	second := tagged("2", "and here")
	second.ChannelID = "301"
	elsewhere := tagged("3", "hello")
	elsewhere.GuildID = other
	for _, m := range []Message{first, second, elsewhere} {
		b.Handle(ctx, m)
	}
	if got := server.wait(2); len(got) != 2 {
		t.Fatalf("server agent turns = %d", len(got))
	}
	waitFor(t, func() bool {
		bosun.mu.Lock()
		defer bosun.mu.Unlock()
		return len(bosun.prompts) == 1
	})
	waitFor(t, func() bool {
		rep.mu.Lock()
		defer rep.mu.Unlock()
		return len(rep.sent) == 3
	})
	cancel()
	b.Wait()
	server.mu.Lock()
	defer server.mu.Unlock()
	for _, c := range server.contexts {
		if c != "ctx-"+guild {
			t.Errorf("turn ran in context %q, want the server's", c)
		}
	}
	mem.mu.Lock()
	defer mem.mu.Unlock()
	if len(mem.recalls) != 3 {
		t.Errorf("recalls = %v, want one per turn", mem.recalls)
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

// A server agent's session has its own request ID, so it never meets an
// operator thread's session.
func TestKagentServerAgentUsesTheServerRequestID(t *testing.T) {
	gw := &a2aServer{}
	srv := httptest.NewServer(gw)
	defer srv.Close()
	sessions := &fakeSessions{}
	k := newKagentOperator(t, srv.URL, sessions)
	k.RequestID = ServerRequestID
	if answer, err := k.Turn(context.Background(), guild, "m1", "p"); err != nil || answer != "ok" {
		t.Fatalf("turn = %q, %v", answer, err)
	}
	if len(sessions.requests) != 1 || sessions.requests[0] != "discord-server-"+guild {
		t.Errorf("requests = %v", sessions.requests)
	}
}
