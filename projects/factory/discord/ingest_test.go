package discord

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"strings"
	"sync"
	"testing"
	"time"
)

type retained struct {
	guild, channel string
	at             time.Time
	content        string
}

// ingestMemory records retains with their time, and can fail them.
type ingestMemory struct {
	mu      sync.Mutex
	fail    error
	retains []retained
	order   []string // "recall" and "retain", in call order
}

func (f *ingestMemory) Recall(_ context.Context, _, _, _ string) ([]string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.order = append(f.order, "recall")
	return nil, nil
}

func (f *ingestMemory) Retain(_ context.Context, g, c string, at time.Time, content string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.fail != nil {
		return f.fail
	}
	f.order = append(f.order, "retain")
	f.retains = append(f.retains, retained{g, c, at, content})
	return nil
}

func (f *ingestMemory) contents() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	var out []string
	for _, r := range f.retains {
		out = append(out, r.content)
	}
	return out
}

var quiet = slog.New(slog.NewTextHandler(io.Discard, nil))

// ingestBridge is a bridge whose server has `ingest` on.
func ingestBridge(agent *fakeAgent) (*Bridge, *ingestMemory, *fakeReplies) {
	mem, rep := &ingestMemory{}, &fakeReplies{}
	b := &Bridge{
		Config: Config{
			Guilds:     map[string]Guild{guild: {Ingest: true}, "201": {}},
			UserLimit:  Limit{Count: 2, Window: time.Hour},
			QueueDepth: 4,
		},
		Agent: agent, Memory: mem, Replies: rep, Log: quiet,
		Ingest: &Ingestor{Memory: mem, Log: quiet},
		Now:    func() time.Time { return time.Date(2026, 11, 17, 12, 5, 0, 0, time.UTC) },
	}
	b.SetBot(User{ID: bot})
	return b, mem, rep
}

func human(id, text string) Message {
	return Message{
		ID: id, ChannelID: channel, GuildID: guild, Content: text,
		Author:    User{ID: "u1", Username: "ann"},
		Timestamp: time.Date(2026, 11, 17, 12, 0, 0, 0, time.UTC),
	}
}

func TestAnUntaggedHumanMessageIsStoredWithNoTurnAndNoReply(t *testing.T) {
	agent := &fakeAgent{answer: "x"}
	b, mem, rep := ingestBridge(agent)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	b.Handle(ctx, human("1", "the code word is heron"))
	b.Handle(ctx, human("2", "and the second line"))
	cancel()
	b.Wait()
	if len(agent.prompts) != 0 || len(rep.sent) != 0 || len(mem.retains) != 0 {
		t.Fatalf("ingest started a turn, a reply or an early retain: %+v %+v %+v", agent.prompts, rep.sent, mem.retains)
	}
	if err := b.Ingest.Flush(context.Background(), guild, channel); err != nil {
		t.Fatal(err)
	}
	if len(mem.retains) != 1 {
		t.Fatalf("retains = %+v, want one batch", mem.retains)
	}
	r := mem.retains[0]
	want := "ann (2026-11-17T12:00:00Z): the code word is heron\nann (2026-11-17T12:00:00Z): and the second line"
	if r.guild != guild || r.channel != channel || r.content != want {
		t.Errorf("retained %+v, want %s/%s %q", r, guild, channel, want)
	}
}

func TestIngestSkipsWhatIsNotAHumanMessageOfTheServer(t *testing.T) {
	b, mem, _ := ingestBridge(&fakeAgent{})
	ctx := context.Background()
	with := func(f func(*Message)) Message { m := human("x", "hello"); f(&m); return m }
	skipped := map[string]Message{
		"another bot":           with(func(m *Message) { m.ID = "a"; m.Author.Bot = true }),
		"webhook":               with(func(m *Message) { m.ID = "b"; m.WebhookID = "w" }),
		"bot itself":            with(func(m *Message) { m.ID = "c"; m.Author.ID = bot }),
		"system message":        with(func(m *Message) { m.ID = "d"; m.Type = 7 }),
		"no text":               with(func(m *Message) { m.ID = "e"; m.Content = "  " }),
		"only the tag":          with(func(m *Message) { m.ID = "f"; m.Content = "<@" + bot + ">" }),
		"direct message":        with(func(m *Message) { m.ID = "g"; m.GuildID = "" }),
		"server without ingest": with(func(m *Message) { m.ID = "h"; m.GuildID = "201" }),
		"server not listed":     with(func(m *Message) { m.ID = "i"; m.GuildID = "999" }),
	}
	for _, m := range skipped {
		b.Handle(ctx, m)
	}
	b.Config.Guilds[guild] = Guild{Ingest: true, Channels: []string{"301"}}
	b.Handle(ctx, human("j", "outside the channel list"))
	b.Config.Guilds[guild] = Guild{Ingest: true}
	b.Handle(ctx, with(func(m *Message) { m.ID = "k"; m.Type = 19; m.Content = "a reply" }))
	if err := b.Ingest.Flush(ctx, guild, channel); err != nil {
		t.Fatal(err)
	}
	for _, g := range []string{"201", "999"} {
		if err := b.Ingest.Flush(ctx, g, channel); err != nil {
			t.Fatal(err)
		}
	}
	got := mem.contents()
	if len(got) != 1 || got[0] != "ann (2026-11-17T12:00:00Z): a reply" {
		t.Fatalf("retained %q, want only the reply message", got)
	}
}

func TestIngestIsOffWithoutAnIngestor(t *testing.T) {
	b, mem, _ := ingestBridge(&fakeAgent{})
	b.Ingest = nil
	b.Handle(context.Background(), human("1", "hello"))
	if len(mem.retains) != 0 {
		t.Fatalf("retained %+v with no ingestor", mem.retains)
	}
}

func TestATaggedTurnStoresRecentLinesFirstAndRetainsOnlyTheReply(t *testing.T) {
	agent := &fakeAgent{answer: "heron"}
	b, mem, rep := ingestBridge(agent)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	b.Handle(ctx, human("1", "the code word is heron"))
	b.Handle(ctx, tagged("2", "what is the code word?"))
	deadline := time.After(5 * time.Second)
	for {
		rep.mu.Lock()
		n := len(rep.sent)
		rep.mu.Unlock()
		if n == 1 {
			break
		}
		select {
		case <-deadline:
			t.Fatal("no reply")
		case <-time.After(5 * time.Millisecond):
		}
	}
	cancel()
	b.Wait()
	// The recall waits for the lines already stored, the tagged one included.
	if len(mem.order) < 2 || mem.order[0] != "retain" || mem.order[1] != "recall" {
		t.Fatalf("call order = %v, want retain then recall", mem.order)
	}
	first := mem.contents()[0]
	if !strings.Contains(first, "the code word is heron") || !strings.Contains(first, "what is the code word?") || strings.Contains(first, "<@") {
		t.Errorf("stored lines = %q", first)
	}
	if err := b.Ingest.Flush(context.Background(), guild, channel); err != nil {
		t.Fatal(err)
	}
	all := strings.Join(mem.contents(), "\n")
	if n := strings.Count(all, "what is the code word?"); n != 1 {
		t.Errorf("the tagged message was stored %d times, want once:\n%s", n, all)
	}
	if n := strings.Count(all, "Bosun (2026-11-17T12:05:00Z): heron"); n != 1 {
		t.Errorf("the reply was stored %d times, want once:\n%s", n, all)
	}
}

func TestARepeatedMessageIDIsStoredOnce(t *testing.T) {
	b, mem, _ := ingestBridge(&fakeAgent{})
	b.Handle(context.Background(), human("1", "hello"))
	b.Handle(context.Background(), human("1", "hello"))
	if err := b.Ingest.Flush(context.Background(), guild, channel); err != nil {
		t.Fatal(err)
	}
	if got := mem.contents(); len(got) != 1 || strings.Count(got[0], "hello") != 1 {
		t.Fatalf("retained %q", got)
	}
}

func TestAFailedRetainKeepsTheLinesInOrderForTheNextFlush(t *testing.T) {
	mem := &ingestMemory{fail: errors.New("hindsight down")}
	in := &Ingestor{Memory: mem, Log: quiet}
	at := time.Date(2026, 11, 17, 12, 0, 0, 0, time.UTC)
	in.Add(guild, channel, "1", at, "one")
	if err := in.Flush(context.Background(), guild, channel); err == nil {
		t.Fatal("a failed retain returned no error")
	}
	in.Add(guild, channel, "2", at, "two")
	mem.fail = nil
	if err := in.Flush(context.Background(), guild, channel); err != nil {
		t.Fatal(err)
	}
	if got := mem.contents(); len(got) != 1 || got[0] != "one\ntwo" {
		t.Fatalf("retained %q, want one batch in order", got)
	}
}

func TestADocumentHoldsOneUTCDay(t *testing.T) {
	mem := &ingestMemory{}
	in := &Ingestor{Memory: mem, Log: quiet}
	in.Add(guild, channel, "1", time.Date(2026, 11, 17, 23, 59, 0, 0, time.UTC), "late")
	in.Add(guild, channel, "2", time.Date(2026, 11, 18, 0, 1, 0, 0, time.UTC), "early")
	if err := in.Flush(context.Background(), guild, channel); err != nil {
		t.Fatal(err)
	}
	if len(mem.retains) != 2 || mem.retains[0].content != "late" || mem.retains[1].content != "early" {
		t.Fatalf("retains = %+v, want one per day", mem.retains)
	}
	if DocumentID(channel, mem.retains[0].at) == DocumentID(channel, mem.retains[1].at) {
		t.Error("two days share a document")
	}
}

func TestChannelsFlushOnWindowLinesAndBytes(t *testing.T) {
	now := time.Date(2026, 11, 17, 12, 0, 0, 0, time.UTC)
	in := &Ingestor{Memory: &ingestMemory{}, Log: quiet, Now: func() time.Time { return now }, MaxLines: 3, MaxBytes: 100}
	in.Add(guild, "c1", "1", now, "a")
	if got := in.due(false); len(got) != 0 {
		t.Fatalf("a fresh line is due: %v", got)
	}
	now = now.Add(defaultIngestWindow)
	if got := in.due(false); len(got) != 1 {
		t.Fatalf("a line past the window is not due: %v", got)
	}
	in.Add(guild, "c2", "2", now, "a")
	in.Add(guild, "c2", "3", now, "b")
	in.Add(guild, "c2", "4", now, "c")
	in.Add(guild, "c3", "5", now, strings.Repeat("x", 100))
	if got := in.due(false); len(got) != 3 {
		t.Fatalf("due = %d channels, want c1 (window), c2 (lines), c3 (bytes)", len(got))
	}
}

func TestABusyMemoryDropsTheOldestLines(t *testing.T) {
	mem := &ingestMemory{}
	in := &Ingestor{Memory: mem, Log: quiet, MaxBuffered: 2}
	at := time.Date(2026, 11, 17, 12, 0, 0, 0, time.UTC)
	for _, id := range []string{"1", "2", "3"} {
		in.Add(guild, channel, id, at, "line"+id)
	}
	if err := in.Flush(context.Background(), guild, channel); err != nil {
		t.Fatal(err)
	}
	if got := mem.contents(); len(got) != 1 || got[0] != "line2\nline3" {
		t.Fatalf("retained %q", got)
	}
}

func TestRunStoresWhatIsLeftWhenItStops(t *testing.T) {
	mem := &ingestMemory{}
	in := &Ingestor{Memory: mem, Log: quiet}
	in.Add(guild, channel, "1", time.Date(2026, 11, 17, 12, 0, 0, 0, time.UTC), "last words")
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { in.Run(ctx); close(done) }()
	cancel()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("Run did not stop")
	}
	if got := mem.contents(); len(got) != 1 || got[0] != "last words" {
		t.Fatalf("retained %q", got)
	}
}

func TestOnlyAnIngestPolicyAsksForTheMessageContentIntent(t *testing.T) {
	off := Config{Guilds: map[string]Guild{"1": {}, "2": {History: true}}}
	on := Config{Guilds: map[string]Guild{"1": {}, "2": {Ingest: true}}}
	if got := IntentsFor(off); got != Intents || got&IntentMessageContent != 0 {
		t.Errorf("IntentsFor(off) = %d, want %d", got, Intents)
	}
	if got := IntentsFor(on); got != 33281 {
		t.Errorf("IntentsFor(on) = %d, want 33281", got)
	}
	if (&Gateway{}).intents() != Intents || (&Gateway{Intents: 33281}).intents() != 33281 {
		t.Error("the gateway ignores its intents")
	}
}

func TestThePolicyReadsIngest(t *testing.T) {
	c, err := ParsePolicy([]byte(`{"guilds":{"1":{"ingest":true,"history":true},"2":{}},
		"userLimit":{"count":1,"window":"1h"},"guildLimit":{"count":1,"window":"1h"},"globalLimit":{"count":1,"window":"1h"}}`))
	if err != nil {
		t.Fatal(err)
	}
	if !c.Guilds["1"].Ingest || c.Guilds["2"].Ingest {
		t.Errorf("guilds = %+v", c.Guilds)
	}
}
