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

// With users: operator, Joe's tag in any server runs a turn in the one
// shared session, recalled from that message's own server; another author's
// tag starts no turn and gets no reply.
func TestOperatorOnlyAnswersJoeInOneSessionAcrossServers(t *testing.T) {
	bosun := &fakeAgent{answer: "bosun"}
	b, mem, rep := newBridge(bosun)
	const other = "201"
	b.Config.Guilds[other] = Guild{}
	b.Config.UserLimit.Count = 10
	b.Config.OperatorOnly = true
	b.Config.Operator.UserID = joe
	b.Config.ServerSessions = map[string]string{other: guild}
	server := &fakeOperator{}
	b.Servers = map[string]SessionAgent{guild: server, other: server}
	server.Session(context.Background(), guild)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	fromJoe := tagged("1", "hi")
	fromJoe.Author = User{ID: joe, Username: "joe"}
	elsewhere := fromJoe
	elsewhere.ID, elsewhere.GuildID = "2", other
	stranger := tagged("3", "hello")
	strangerElsewhere := tagged("4", "hello")
	strangerElsewhere.GuildID = other
	for _, m := range []Message{fromJoe, stranger, elsewhere, strangerElsewhere} {
		b.Handle(ctx, m)
	}
	if got := server.wait(2); len(got) != 2 {
		t.Fatalf("server agent turns = %d", len(got))
	}
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
	server.mu.Lock()
	for _, c := range server.contexts {
		if c != "ctx-"+guild {
			t.Errorf("turn ran in context %q, want the shared session", c)
		}
	}
	server.mu.Unlock()
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
	server := &fakeOperator{}
	b.Servers = map[string]SessionAgent{guild: server}
	ctx, cancel := context.WithCancel(context.Background())
	m := tagged("1", "hi")
	m.Author = User{ID: joe, Username: "joe"}
	b.Handle(ctx, m)
	b.Handle(ctx, tagged("2", "hi"))
	cancel()
	b.Wait()
	server.mu.Lock()
	defer server.mu.Unlock()
	bosun.mu.Lock()
	defer bosun.mu.Unlock()
	rep.mu.Lock()
	defer rep.mu.Unlock()
	if len(server.prompts) != 0 || len(bosun.prompts) != 0 || len(rep.sent) != 0 {
		t.Errorf("turns = %d + %d, replies = %d; want none", len(server.prompts), len(bosun.prompts), len(rep.sent))
	}
}

func TestPolicyReadsUsersAndServerSessions(t *testing.T) {
	base := `"guilds":{"1":{},"2":{}},"userLimit":{"count":5,"window":"10m"},"guildLimit":{"count":30,"window":"1h"},"globalLimit":{"count":200,"window":"24h"}`
	agents := `"serverAgents":{"1":"discord-1/homelab","2":"discord-1/homelab"}`
	c, err := ParsePolicy([]byte(`{` + base + `,` + agents + `,"serverSessions":{"2":"1"},"users":"operator"}`))
	if err != nil || !c.OperatorOnly || c.ServerSessions["2"] != "1" {
		t.Fatalf("config = %+v, %v", c, err)
	}
	if c, err := ParsePolicy([]byte(`{` + base + `}`)); err != nil || c.OperatorOnly {
		t.Fatalf("default users: %+v, %v", c, err)
	}
	for _, bad := range []string{
		`,"users":"friends"`,
		`,` + agents + `,"serverSessions":{"3":"1"}`,
		`,"serverAgents":{"1":"discord-1/homelab","2":"discord-2/other"},"serverSessions":{"2":"1"}`,
	} {
		if _, err := ParsePolicy([]byte(`{` + base + bad + `}`)); err == nil {
			t.Errorf("accepted %s", bad)
		}
	}
}
