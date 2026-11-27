package discord

import (
	"context"
	"encoding/json"
	"strings"
	"sync"
	"testing"
)

func pals() *Places {
	p := &Places{}
	p.Apply("GUILD_CREATE", json.RawMessage(`{"id":"200","name":"pals",
		"channels":[{"id":"300","name":"general","type":0}],
		"roles":[{"id":"900","name":"climbers"}]}`))
	return p
}

func TestResolveNamesEveryMentionForm(t *testing.T) {
	p := pals()
	users := []User{
		{ID: "1", Username: "wobblington", GlobalName: "Wobblington"},
		{ID: "2", Username: "dave", GlobalName: "dave"},
		{ID: "3", Username: "cat", GlobalName: "Cat", Member: &Member{Nick: "Kitty"}},
	}
	cases := map[string]string{
		"where is <@1> moving?":           "where is @wobblington (Wobblington) moving?",
		"ask <@!1>":                       "ask @wobblington (Wobblington)",
		"<@2> same name":                  "@dave same name",
		"nick wins <@3>":                  "nick wins @cat (Kitty)",
		"who is <@4242424242>?":           "who is @unknown-user?",
		"ping <@&900>":                    "ping @climbers",
		"ping <@&901>":                    "ping @unknown-role",
		"see <#300>":                      "see #general",
		"see <#301>":                      "see #unknown-channel",
		"nice <:pog:1234> <a:dance:5678>": "nice :pog: :dance:",
		"@everyone and @here":             "@everyone and @here",
		"run </remind me:42>":             "run /remind me",
		"at <t:1764237600:R>":             "at 2025-11-27T10:00:00Z",
		"plain < text > with <@ no id>":   "plain < text > with <@ no id>",
	}
	for in, want := range cases {
		if got := p.Resolve(guild, in, users); got != want {
			t.Errorf("Resolve(%q) = %q, want %q", in, got, want)
		}
	}
	var none *Places
	if got := none.Resolve(guild, "<@1> <#300> <@&900>", users); got != "@wobblington (Wobblington) #unknown-channel @unknown-role" {
		t.Errorf("nil Places = %q", got)
	}
}

func TestResolveUsesLearnedUsersAndRoleEvents(t *testing.T) {
	p := pals()
	p.Learn(guild, User{ID: "1", Username: "wobblington", GlobalName: "Wobblington", Member: &Member{Nick: "Wob"}})
	// A history read has no member: the nick stays.
	p.Learn(guild, User{ID: "1", Username: "wobblington", GlobalName: "Wobblington"})
	p.Learn("999", User{ID: "5", Username: "elsewhere"}) // Not a cached server.
	p.Apply("GUILD_ROLE_CREATE", json.RawMessage(`{"guild_id":"200","role":{"id":"901","name":"pub"}}`))
	p.Apply("GUILD_ROLE_UPDATE", json.RawMessage(`{"guild_id":"200","role":{"id":"900","name":"boulderers"}}`))
	p.Apply("GUILD_ROLE_DELETE", json.RawMessage(`{"guild_id":"200","role_id":"901"}`))
	if got := p.Resolve(guild, "<@1> <@&900> <@&901>", nil); got != "@wobblington (Wob) @boulderers @unknown-role" {
		t.Errorf("Resolve = %q", got)
	}
}

type fakeUsers struct {
	mu    sync.Mutex
	calls []string
	users map[string]User
}

func (f *fakeUsers) User(_ context.Context, id string) (User, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls = append(f.calls, id)
	u, ok := f.users[id]
	if !ok {
		return User{}, &HTTPError{Status: 404}
	}
	return u, nil
}

func TestUserLookupReadsAnUnnamedUserOnceAndRemembersAMissingOne(t *testing.T) {
	p := pals()
	f := &fakeUsers{users: map[string]User{"7": {Username: "eve", GlobalName: "Eve"}}}
	l := &UserLookup{Users: f}
	for range 2 {
		l.Fill(context.Background(), p, guild, "<@7> <@8> <@1>", []User{{ID: "1", Username: "known"}})
	}
	if got := p.Resolve(guild, "<@7> <@8>", nil); got != "@eve (Eve) @unknown-user" {
		t.Errorf("Resolve = %q", got)
	}
	if strings.Join(f.calls, ",") != "7,8" {
		t.Errorf("REST reads = %v, want 7 and 8 once each", f.calls)
	}
}

func TestATurnNamesMentionsInTheMessageAndItsReply(t *testing.T) {
	agent := &fakeAgent{answer: "Leith"}
	b, mem, _ := ingestBridge(agent)
	b.Places = pals()
	m := tagged("20", "where and when is <@1> moving?")
	m.Mentions = append(m.Mentions, User{ID: "1", Username: "wobblington", GlobalName: "Wobblington"})
	m.Type = 19
	m.Referenced = &Referenced{
		ID: "19", Author: User{ID: "2", Username: "dave"}, Content: "<@1> told <#300>",
		Mentions: []User{{ID: "1", Username: "wobblington"}},
	}
	b.ingest(m)
	b.Turn(context.Background(), m)
	d := promptData(t, lastPrompt(agent))
	if got := string(d["message"]); got != `"where and when is @wobblington (Wobblington) moving?"` {
		t.Errorf("message = %s", got)
	}
	var reply struct{ Message string }
	_ = json.Unmarshal(d["reply_to"], &reply)
	if reply.Message != "@wobblington told #general" {
		t.Errorf("reply_to = %s", d["reply_to"])
	}
	// The ingested text, which Hindsight stores, names the user too.
	if err := b.Ingest.Flush(context.Background(), guild, channel); err != nil {
		t.Fatal(err)
	}
	all := strings.Join(mem.contents(), "\n")
	if !strings.Contains(all, "where and when is @wobblington (Wobblington) moving?") || strings.Contains(all, "<@") {
		t.Errorf("stored = %q", all)
	}
}

func TestARetainedExchangeWithoutIngestNamesMentions(t *testing.T) {
	agent := &fakeAgent{answer: "ok"}
	b, mem, _ := ingestBridge(agent)
	m := tagged("20", "hi <@1>")
	m.GuildID = "201"
	m.Mentions = append(m.Mentions, User{ID: "1", Username: "wobblington"})
	b.Turn(context.Background(), m)
	if all := strings.Join(mem.contents(), "\n"); !strings.Contains(all, "hi @wobblington") || strings.Contains(all, "<@") {
		t.Errorf("stored = %q", all)
	}
}

func TestTheHistoryImportStoresNamesNotIDs(t *testing.T) {
	src := &fakeHistory{
		channels: map[string][]Channel{"200": {{ID: "300", Name: "general", Type: 0}}},
		messages: map[string][]HistoryMessage{"300": {}},
	}
	m := msg("m1", "ann", day(15, 9), "<@1> is moving, ask <@&900> in <#300> <:pog:1>")
	m.Mentions = []User{{ID: "1", Username: "wobblington", GlobalName: "Wobblington"}}
	src.messages["300"] = []HistoryMessage{m}
	docs := &fakeDocs{}
	im := newImporter(src, docs, map[string]Guild{"200": {History: true}})
	im.Places = pals()
	if _, err := im.Run(context.Background()); err != nil {
		t.Fatal(err)
	}
	docs.mu.Lock()
	defer docs.mu.Unlock()
	var all []string
	for _, item := range docs.docs {
		all = append(all, item.Content)
	}
	got := strings.Join(all, "\n")
	if !strings.Contains(got, "@wobblington (Wobblington) is moving, ask @climbers in #general :pog:") {
		t.Errorf("stored = %q", got)
	}
}
