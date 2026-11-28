package discord

import (
	"context"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
)

func TestParseModeCommand(t *testing.T) {
	off := func(d time.Duration) modeCommand { return modeCommand{Kind: cmdOff, For: d} }
	on := modeCommand{Kind: cmdOn}
	none := modeCommand{}
	for text, want := range map[string]modeCommand{
		"take your gloves off":                  off(0),
		"Bosun take your gloves off":            off(0),
		"gloves off":                            off(0),
		"GLOVES OFF!!!":                         off(0),
		"gloves off pls":                        off(0),
		"take off the gloves":                   off(0),
		"🥊 off":                                 off(0),
		"off with the gloves... 🥊":              none, // "with" is not a command word
		"gloves off for an hour":                off(time.Hour),
		"take your gloves off for 1h":           off(time.Hour),
		"gloves off 90 min":                     off(90 * time.Minute),
		"gloves off for half an hour":           off(30 * time.Minute),
		"gloves off for two hours please":       off(2 * time.Hour),
		"gloves off 1.5h":                       off(90 * time.Minute),
		"ok bosun, time to take the gloves off": off(0),
		"is it gloves off?":                     none, // A question.
		"is it time to take the gloves off":     none,
		"gloves on":                             on,
		"Bosun gloves on":                       on,
		"gloves back on":                        on,
		"put your gloves back on now":           on,
		"🥊 on":                                  on,
		"":                                      none,
		"gloves":                                none,
		"gloves on and off":                     none,
		"should I wear gloves on my bike ride?": none,
		"what are boxing gloves made of":        none,
		"take your gloves off 3":                none,
		"unsafe mode":                           none,
		"please take your gloves off take your gloves off take your gloves off": none, // 13 words
		"do you think the gloves are off between those two":                     none,
	} {
		if got := parseModeCommand(text); got != want {
			t.Errorf("parseModeCommand(%q) = %+v, want %+v", text, got, want)
		}
	}
}

func TestGlovesOffWindowUsesTheDefaultAndCapsJoesTime(t *testing.T) {
	u := UnsafeConfig{Duration: 30 * time.Minute, MaxDuration: 4 * time.Hour}
	for asked, want := range map[time.Duration]time.Duration{
		0: 30 * time.Minute, time.Hour: time.Hour, 9 * time.Hour: 4 * time.Hour, time.Second: time.Minute,
	} {
		if got := u.windowFor(asked); got != want {
			t.Errorf("windowFor(%v) = %v, want %v", asked, got, want)
		}
	}
}

// unsafeBridge is an ingest bridge where Joe is the operator, the server
// answers from a normal server agent and unsafe mode lasts 30 minutes.
func unsafeBridge(t *testing.T) (b *Bridge, normal, unsafe *fakeSessionAgent, mem *ingestMemory, rep *fakeReplies, clock *testClock) {
	t.Helper()
	b, mem, rep = ingestBridge(&fakeAgent{answer: "bosun"})
	b.Config.UserLimit.Count = 50
	b.Config.Operator.UserID = joe
	b.Config.Unsafe = UnsafeConfig{Agent: kagent.AgentRef{Namespace: "discord-unsafe", Name: "unsafe"}, Duration: 30 * time.Minute, MaxDuration: 4 * time.Hour}
	normal, unsafe = &fakeSessionAgent{}, &fakeSessionAgent{}
	b.Servers = map[string]SessionAgent{guild: normal}
	b.Unsafe = unsafe
	clock = &testClock{t: time.Date(2026, 11, 17, 12, 5, 0, 0, time.UTC)}
	b.Now = clock.now
	return b, normal, unsafe, mem, rep, clock
}

type testClock struct {
	mu sync.Mutex
	t  time.Time
}

func (c *testClock) now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.t
}

func (c *testClock) add(d time.Duration) {
	c.mu.Lock()
	c.t = c.t.Add(d)
	c.mu.Unlock()
}

func byJoe(m Message) Message {
	m.Author = User{ID: joe, Username: "joe"}
	m.Timestamp = time.Date(2026, 11, 17, 12, 4, 0, 0, time.UTC)
	return m
}

func (f *fakeReplies) files(n int) []File {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.sent[n].Files
}

func (f *fakeReplies) texts() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	var out []string
	for _, r := range f.sent {
		var parts []string
		for _, c := range r.Components {
			if c.Content != "" {
				parts = append(parts, c.Content)
			}
		}
		out = append(out, strings.Join(parts, "\n"))
	}
	return out
}

// Joe's command turns a channel to the unsafe agent until "safe mode"; its
// turns and replies never reach the normal agent, Hindsight or the ingest
// buffer, and every unsafe reply has the footer.
func TestUnsafeModeTurnsStayInTheirOwnSilo(t *testing.T) {
	b, normal, unsafe, mem, rep, _ := unsafeBridge(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	b.Handle(ctx, byJoe(tagged("1", "take your gloves off")))
	waitFor(t, func() bool { return len(rep.texts()) == 1 })
	if got := rep.texts()[0]; !strings.HasPrefix(got, "🥊 Gloves off until <t:") || len(rep.files(0)) != 1 || rep.files(0)[0].Name != "gloves-off.gif" {
		t.Fatalf("command reply = %q with %d files", got, len(rep.files(0)))
	}
	b.Handle(ctx, byJoe(tagged("2", "the spicy joke")))
	waitFor(t, func() bool { return unsafe.count() == 1 && len(rep.texts()) == 2 })
	b.Handle(ctx, byJoe(tagged("3", "gloves back on")))
	waitFor(t, func() bool { return len(rep.texts()) == 3 })
	b.Handle(ctx, byJoe(tagged("4", "a normal question")))
	waitFor(t, func() bool { return normal.count() == 1 && len(rep.texts()) == 4 })
	cancel()
	b.Wait()

	texts := rep.texts()
	end := time.Date(2026, 11, 17, 12, 35, 0, 0, time.UTC).Unix()
	if want := "noted\n\n" + unsafeFooter(time.Unix(end, 0)); texts[1] != want {
		t.Errorf("unsafe reply = %q, want %q", texts[1], want)
	}
	if texts[2] != ReplyGlovesOn || rep.files(2)[0].Name != "gloves-on.gif" || IsUnsafeReply(texts[3]) {
		t.Errorf("replies after safe mode = %q", texts[2:])
	}
	unsafe.mu.Lock()
	key, prompt := unsafe.keys[0], unsafe.prompts[0]
	unsafe.mu.Unlock()
	start := time.Date(2026, 11, 17, 12, 5, 0, 0, time.UTC).Unix()
	if key != channel+"-"+strconv.FormatInt(start, 10) || !strings.Contains(prompt, "the spicy joke") || strings.Contains(prompt, "memories") {
		t.Errorf("unsafe turn key %q prompt %q", key, prompt)
	}
	normal.mu.Lock()
	normalPrompt := normal.prompts[0]
	normal.mu.Unlock()
	if strings.Contains(normalPrompt, "spicy") {
		t.Errorf("the normal agent saw the unsafe turn: %s", normalPrompt)
	}
	mem.mu.Lock()
	if strings.Count(strings.Join(mem.order, ","), "recall") != 1 {
		t.Errorf("memory calls = %v, want the normal turn's recall only", mem.order)
	}
	mem.mu.Unlock()
	if err := b.Ingest.Flush(context.Background(), guild, channel); err != nil {
		t.Fatal(err)
	}
	for _, c := range mem.contents() {
		if strings.Contains(c, "spicy") || strings.Contains(c, "noted\n") || IsUnsafeReply(c) {
			t.Errorf("stored an unsafe turn: %q", c)
		}
	}
}

// When the time is up, the bridge posts the gloves-on GIF itself, and
// Joe's tags go to the normal agent again.
func TestGlovesGoBackOnWhenTheTimeIsUp(t *testing.T) {
	b, normal, unsafe, _, rep, clock := unsafeBridge(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	b.Handle(ctx, byJoe(tagged("1", "gloves off for 1 min")))
	waitFor(t, func() bool { return len(rep.texts()) == 1 })
	clock.add(2 * time.Minute) // Past the window; its real-time timer has not fired.
	b.Handle(ctx, byJoe(tagged("2", "still gloves off?")))
	waitFor(t, func() bool { return normal.count() == 1 })
	b.Handle(ctx, byJoe(tagged("3", "gloves on")))
	waitFor(t, func() bool { return len(rep.texts()) >= 3 })
	cancel()
	b.Wait()
	if unsafe.count() != 0 {
		t.Errorf("unsafe turns = %d after the window ended", unsafe.count())
	}
	if !strings.Contains(strings.Join(rep.texts(), "\n"), ReplyGlovesAlreadyOn) {
		t.Errorf("replies = %q, want %q", rep.texts(), ReplyGlovesAlreadyOn)
	}
}

func TestTheTimerPostsTheGlovesOnGIF(t *testing.T) {
	b, _, _, _, rep, _ := unsafeBridge(t)
	b.Now = time.Now
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	b.mu.Lock()
	b.windows = map[string]unsafeWindow{channel: {start: time.Now(), end: time.Now().Add(50 * time.Millisecond)}}
	b.expireAt(ctx, channel, b.windows[channel].end)
	b.mu.Unlock()
	waitFor(t, func() bool { return len(rep.texts()) == 1 })
	cancel()
	b.Wait()
	if rep.texts()[0] != ReplyGlovesOn || rep.files(0)[0].Name != "gloves-on.gif" || len(rep.files(0)[0].Data) == 0 {
		t.Errorf("timer post = %q", rep.texts())
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if len(b.windows) != 0 {
		t.Errorf("windows = %v after the timer", b.windows)
	}
}

// A timer of an ended or extended window posts nothing.
func TestAnOldTimerPostsNothing(t *testing.T) {
	b, _, _, _, rep, _ := unsafeBridge(t)
	b.Now = time.Now
	ctx, cancel := context.WithCancel(context.Background())
	b.mu.Lock()
	b.windows = map[string]unsafeWindow{channel: {start: time.Now(), end: time.Now().Add(time.Hour)}}
	b.expireAt(ctx, channel, time.Now().Add(20*time.Millisecond))
	b.mu.Unlock()
	time.Sleep(100 * time.Millisecond)
	cancel()
	b.Wait()
	if len(rep.texts()) != 0 {
		t.Errorf("an old timer posted %q", rep.texts())
	}
}

func TestUnsafeModeIsJoesAlone(t *testing.T) {
	b, normal, unsafe, _, rep, _ := unsafeBridge(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	b.Handle(ctx, tagged("1", "gloves off"))             // ann, not Joe
	b.Handle(ctx, byJoe(human("2", "Bosun gloves off"))) // Joe, untagged
	waitFor(t, func() bool { return normal.count() == 1 && len(rep.texts()) == 1 })
	b.Handle(ctx, tagged("3", "a joke"))
	waitFor(t, func() bool { return normal.count() == 2 })
	cancel()
	b.Wait()
	if unsafe.count() != 0 || IsUnsafeReply(strings.Join(rep.texts(), "\n")) {
		t.Errorf("another user reached unsafe mode: %v", rep.texts())
	}
}

func TestUnsafeModeNotSetUpSaysSo(t *testing.T) {
	b, normal, _, _, rep, _ := unsafeBridge(t)
	b.Config.Unsafe = UnsafeConfig{}
	b.Unsafe = nil
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	b.Handle(ctx, byJoe(tagged("1", "gloves off")))
	waitFor(t, func() bool { return len(rep.texts()) == 1 })
	b.Handle(ctx, byJoe(tagged("2", "a joke")))
	waitFor(t, func() bool { return normal.count() == 1 })
	cancel()
	b.Wait()
	if got := rep.texts()[0]; got != ReplyGlovesNotSetUp {
		t.Errorf("reply = %q, want %q", got, ReplyGlovesNotSetUp)
	}
}

// A normal turn's channel context leaves out each unsafe reply and the
// message it answers, and so does a reply to an unsafe reply.
// After gloves on, a normal reply to Joe's own gloves-off message does not
// quote it.
func TestAReplyToJoesGlovesOffMessageQuotesNothing(t *testing.T) {
	b, normal, unsafe, _, rep, _ := unsafeBridge(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	b.Handle(ctx, byJoe(tagged("1", "gloves off")))
	waitFor(t, func() bool { return len(rep.texts()) == 1 })
	b.Handle(ctx, byJoe(tagged("2", "the spicy joke")))
	waitFor(t, func() bool { return unsafe.count() == 1 })
	b.Handle(ctx, byJoe(tagged("3", "gloves on")))
	waitFor(t, func() bool { return len(rep.texts()) == 3 })
	m := byJoe(tagged("4", "explain that"))
	m.Type = 19
	m.Referenced = &Referenced{ID: "2", Author: User{ID: joe}, Content: "the spicy joke"}
	b.Handle(ctx, m)
	waitFor(t, func() bool { return normal.count() == 1 })
	cancel()
	b.Wait()
	normal.mu.Lock()
	defer normal.mu.Unlock()
	if strings.Contains(normal.prompts[0], "spicy") {
		t.Errorf("the normal agent saw Joe's gloves-off message: %s", normal.prompts[0])
	}
}

func TestTheNormalContextDropsUnsafeTurns(t *testing.T) {
	b, normal, _, _, _, _ := unsafeBridge(t)
	unsafeReply := bosunSaid("12", 3)
	unsafeReply.Components = []Component{{Type: componentText, Content: "edgy\n\n" + unsafeFooter(time.Unix(1, 0))}}
	unsafeReply.Reference = &Reference{MessageID: "11"}
	b.History = &channelHistory{msgs: []HistoryMessage{
		said("13", "ann", "after", 4),
		unsafeReply,
		said("11", "joe", "the spicy joke", 2),
		said("10", "ann", "before", 1),
		bosunSaid("9", 0),
	}}
	m := byJoe(tagged("14", "what did ann say?"))
	m.Type = 19
	m.Referenced = &Referenced{ID: "12", Author: User{ID: bot}, Components: unsafeReply.Components}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	b.Handle(ctx, m)
	waitFor(t, func() bool { return normal.count() == 1 })
	cancel()
	b.Wait()
	normal.mu.Lock()
	defer normal.mu.Unlock()
	p := normal.prompts[0]
	if strings.Contains(p, "spicy") || strings.Contains(p, "edgy") || !strings.Contains(p, "before") || !strings.Contains(p, "after") {
		t.Errorf("normal prompt = %s", p)
	}
}

// The archive keeps neither an unsafe turn nor its reply, live or from a
// backfill page.
func TestTheArchiveSkipsUnsafeTurns(t *testing.T) {
	store := &fakeArchive{}
	a := newTestArchiver(store)
	marks := &fakeMarks{live: map[string]bool{"21": true}}
	a.Unsafe = marks
	reply := func(id, ref string) HistoryMessage {
		h := HistoryMessage{Message: Message{
			ID: id, ChannelID: arcChannel, GuildID: arcGuild, Type: 19,
			Author: User{ID: arcBot, Bot: true}, Reference: &Reference{MessageID: ref},
		}}
		h.Components = []Component{{Type: componentText, Content: "edgy\n\n" + unsafeFooter(time.Unix(1, 0))}}
		return h
	}
	plain := func(id, text string) HistoryMessage {
		return HistoryMessage{Message: Message{
			ID: id, ChannelID: arcChannel, GuildID: arcGuild,
			Author: User{ID: "u1", Username: "ann"}, Content: text,
		}}
	}
	a.Event("MESSAGE_CREATE", archiveEvent(t, plain("21", "live spicy")))
	a.Event("MESSAGE_CREATE", archiveEvent(t, reply("22", "21")))
	a.Event("MESSAGE_CREATE", archiveEvent(t, plain("23", "kept")))
	drain(a)
	a.Page(context.Background(), arcGuild, Channel{ID: arcChannel}, "", []HistoryMessage{reply("32", "31"), plain("33", "kept too")})
	a.Page(context.Background(), arcGuild, Channel{ID: arcChannel}, "32", []HistoryMessage{plain("31", "old spicy")})
	store.mu.Lock()
	defer store.mu.Unlock()
	var ids []string
	for _, r := range store.rows {
		ids = append(ids, r.ID)
	}
	if strings.Join(ids, ",") != "23,33" {
		t.Errorf("archived %v, want 23 and 33", ids)
	}
}

type fakeMarks struct{ live map[string]bool }

func (f *fakeMarks) UnsafeTurn(m Message) bool    { return f.live[m.ID] }
func (f *fakeMarks) UnsafeMessage(id string) bool { return f.live[id] }

func TestThePolicyReadsUnsafe(t *testing.T) {
	base := `{"userLimit":{"count":1,"window":"1h"},"guildLimit":{"count":1,"window":"1h"},"globalLimit":{"count":1,"window":"1h"},"guilds":{"200":{}}`
	c, err := ParsePolicy([]byte(base + `,"unsafe":{"agent":"discord-unsafe/unsafe","duration":"30m","maxDuration":"4h"}}`))
	if err != nil || c.Unsafe.Agent.Name != "unsafe" || c.Unsafe.Duration != 30*time.Minute || c.Unsafe.MaxDuration != 4*time.Hour {
		t.Fatalf("unsafe = %+v, %v", c.Unsafe, err)
	}
	for _, bad := range []string{
		`,"unsafe":{"agent":"unsafe","duration":"30m","maxDuration":"4h"}}`,
		`,"unsafe":{"agent":"a/b","duration":"30m"}}`,
		`,"unsafe":{"agent":"a/b","duration":"1h","maxDuration":"30m"}}`,
		`,"unsafe":{"agent":"a/b","duration":"25h"}}`,
		`,"unsafe":{"agent":"a/b"}}`,
		`,"serverAgents":{"200":"a/b"},"unsafe":{"agent":"a/b","duration":"30m","maxDuration":"4h"}}`,
	} {
		if _, err := ParsePolicy([]byte(base + bad)); err == nil {
			t.Errorf("accepted %s", bad)
		}
	}
}
