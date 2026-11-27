package discord

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"
)

// channelHistory returns a fixed page of a channel's messages, newest first.
type channelHistory struct {
	msgs   []HistoryMessage
	err    error
	before []string
}

func (f *channelHistory) MessagesBefore(_ context.Context, _, before string) ([]HistoryMessage, error) {
	f.before = append(f.before, before)
	return f.msgs, f.err
}

// said is a history message by a user at minute min past 10:00.
func said(id, user, text string, min int) HistoryMessage {
	return HistoryMessage{Message: Message{
		ID: id, ChannelID: channel, GuildID: guild, Content: text,
		Author:    User{ID: "id-" + user, Username: user},
		Timestamp: time.Date(2026, 11, 27, 10, min, 0, 0, time.UTC),
	}}
}

func bosunSaid(id string, min int) HistoryMessage {
	h := said(id, "bosun", "", min)
	h.Author = User{ID: bot, Username: "Bosun", Bot: true}
	h.Components = []Component{{Type: componentText, Content: "an earlier answer"}}
	return h
}

// promptData is the JSON value of a turn's prompt.
func promptData(t *testing.T, prompt string) map[string]json.RawMessage {
	t.Helper()
	_, raw, ok := strings.Cut(prompt, "\n\n")
	if !ok {
		t.Fatalf("prompt has no JSON: %s", prompt)
	}
	var out map[string]json.RawMessage
	if err := json.Unmarshal([]byte(raw), &out); err != nil {
		t.Fatal(err)
	}
	return out
}

func sinceLines(t *testing.T, prompt string) []ChannelLine {
	t.Helper()
	d := promptData(t, prompt)
	if _, ok := d["recent_messages"]; ok {
		t.Errorf("prompt still has recent_messages: %s", prompt)
	}
	var lines []ChannelLine
	if raw, ok := d["channel_messages_since_your_last_turn"]; ok {
		if err := json.Unmarshal(raw, &lines); err != nil {
			t.Fatal(err)
		}
	}
	return lines
}

func texts(lines []ChannelLine) []string {
	var out []string
	for _, l := range lines {
		out = append(out, l.Author+": "+l.Text)
	}
	return out
}

func lastPrompt(a *fakeAgent) string {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.prompts[len(a.prompts)-1]
}

// The 2026-11-27 bug: the friend's lines were retained two hours before
// Joe's tag, so the ingest buffer was empty and the prompt had none of them.
func TestATurnAfterTheChannelWasRetainedStillGetsTheFriendsMessages(t *testing.T) {
	agent := &fakeAgent{answer: "he means the flat"}
	b, _, _ := ingestBridge(agent)
	b.Handle(context.Background(), human("10", "moving to Leith in March"))
	if err := b.Ingest.Flush(context.Background(), guild, channel); err != nil {
		t.Fatal(err)
	}
	if got := b.Ingest.Recent(guild, channel, "", 10, 1000); len(got) != 0 {
		t.Fatalf("ingest buffer still holds %q", got)
	}
	h := &channelHistory{msgs: []HistoryMessage{said("10", "dave", "moving to Leith in March", 0)}}
	b.History = h
	b.Turn(context.Background(), tagged("20", "what is he talking about"))
	if got := texts(sinceLines(t, lastPrompt(agent))); len(got) != 1 || got[0] != "dave: moving to Leith in March" {
		t.Errorf("lines = %q", got)
	}
	if len(h.before) != 1 || h.before[0] != "20" {
		t.Errorf("history read before %q, want the tagged message", h.before)
	}
}

func TestLinesStopAtBosunsLastMessageAndAreOldestFirst(t *testing.T) {
	agent := &fakeAgent{answer: "ok"}
	b, _, _ := ingestBridge(agent)
	b.History = &channelHistory{msgs: []HistoryMessage{
		said("15", "cat", "newest", 5),
		said("14", "dave", "middle", 4),
		bosunSaid("13", 3),
		said("12", "dave", "before Bosun answered", 2),
	}}
	b.Turn(context.Background(), tagged("20", "summary?"))
	got := texts(sinceLines(t, lastPrompt(agent)))
	if want := []string{"dave: middle", "cat: newest"}; fmt.Sprint(got) != fmt.Sprint(want) {
		t.Errorf("lines = %q, want %q", got, want)
	}
}

func TestLinesAreCappedNewestKept(t *testing.T) {
	page := func(n int, bosunAt int, text func(int) string) []HistoryMessage {
		var out []HistoryMessage
		for i := 0; i < n; i++ {
			if i == bosunAt {
				out = append(out, bosunSaid(fmt.Sprint(1000-i), 0))
				continue
			}
			out = append(out, said(fmt.Sprint(1000-i), "dave", text(i), 0))
		}
		return out
	}
	short := func(i int) string { return fmt.Sprintf("line %d", i) }
	cases := []struct {
		name      string
		msgs      []HistoryMessage
		lines     int
		newest    string
		maxChars  int
		oldestIdx string
	}{
		// No message of Bosun's in the page: a new channel.
		{"no Bosun message", page(100, -1, short), maxFirstLines, "line 0", 0, "line 29"},
		// Bosun answered 80 messages ago.
		{"line cap", page(100, 80, short), maxSinceLines, "line 0", 0, "line 49"},
		{"char cap", page(100, 99, func(int) string { return strings.Repeat("x", 1000) }), maxSinceChars / 1000, "", maxSinceChars, ""},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			agent := &fakeAgent{answer: "ok"}
			b, _, _ := ingestBridge(agent)
			b.History = &channelHistory{msgs: c.msgs}
			b.Turn(context.Background(), tagged("2000", "hi"))
			lines := sinceLines(t, lastPrompt(agent))
			if len(lines) != c.lines {
				t.Fatalf("%d lines, want %d", len(lines), c.lines)
			}
			chars := 0
			for _, l := range lines {
				chars += len(l.Text)
			}
			if c.maxChars > 0 && chars > c.maxChars {
				t.Errorf("%d chars, want at most %d", chars, c.maxChars)
			}
			if c.newest != "" && lines[len(lines)-1].Text != c.newest {
				t.Errorf("last line = %q, want the newest %q", lines[len(lines)-1].Text, c.newest)
			}
			if c.oldestIdx != "" && lines[0].Text != c.oldestIdx {
				t.Errorf("first line = %q, want %q", lines[0].Text, c.oldestIdx)
			}
		})
	}
}

// A restarted bridge has an empty ingest buffer; Discord still has the
// channel.
func TestARestartedBridgeGetsTheChannelFromDiscord(t *testing.T) {
	agent := &fakeAgent{answer: "ok"}
	b, _, _ := ingestBridge(agent)
	b.History = &channelHistory{msgs: []HistoryMessage{said("10", "dave", "anyone up for the pub", 0), bosunSaid("9", 0)}}
	b.Turn(context.Background(), tagged("20", "who asked?"))
	if got := texts(sinceLines(t, lastPrompt(agent))); len(got) != 1 || got[0] != "dave: anyone up for the pub" {
		t.Errorf("lines = %q", got)
	}
}

func TestAFailedHistoryReadFallsBackToTheIngestBuffer(t *testing.T) {
	agent := &fakeAgent{answer: "ok"}
	b, _, _ := ingestBridge(agent)
	b.History = &channelHistory{err: errors.New("503")}
	b.Handle(context.Background(), human("10", "still buffered"))
	b.Turn(context.Background(), tagged("20", "what?"))
	lines := sinceLines(t, lastPrompt(agent))
	if len(lines) != 1 || !strings.HasSuffix(lines[0].Text, "still buffered") {
		t.Errorf("lines = %+v", lines)
	}
}

func TestAServerWithoutIngestReadsNoHistory(t *testing.T) {
	agent := &fakeAgent{answer: "ok"}
	b, _, _ := ingestBridge(agent)
	h := &channelHistory{msgs: []HistoryMessage{said("10", "dave", "private", 0)}}
	b.History = h
	m := tagged("20", "hi")
	m.GuildID = "201"
	b.Turn(context.Background(), m)
	if len(h.before) != 0 || len(sinceLines(t, lastPrompt(agent))) != 0 {
		t.Errorf("read history %v in a server without ingest", h.before)
	}
}

func TestLinesCarryReplyToPlaceholdersAndNamesAndSkipBots(t *testing.T) {
	agent := &fakeAgent{answer: "ok"}
	b, _, _ := ingestBridge(agent)
	reply := said("12", "cat", "<@55> same, see <#300>", 2)
	reply.Type = 19
	reply.Mentions = []User{{ID: "55", Username: "dave", GlobalName: "Dave"}}
	reply.Referenced = &Referenced{ID: "11", Author: User{Username: "dave"}, Content: "moving soon " + strings.Repeat("y", 300)}
	photo := said("13", "dave", "", 3)
	photo.Attachments = []Attachment{{Filename: "flat.jpg"}}
	photo.Embeds = []HistoryEmbed{{Title: "Leith flats"}, {}}
	photo.Stickers = []HistorySticker{{Name: "wave"}}
	other := said("14", "musicbot", "now playing", 4)
	other.Author.Bot = true
	join := said("15", "eve", "", 5)
	join.Type = 7
	b.History = &channelHistory{msgs: []HistoryMessage{join, other, photo, reply}}
	b.Places = &Places{}
	b.Places.Apply("GUILD_CREATE", json.RawMessage(`{"id":"200","name":"pals","channels":[{"id":"300","name":"general","type":0}]}`))
	b.Turn(context.Background(), tagged("20", "?"))
	lines := sinceLines(t, lastPrompt(agent))
	if len(lines) != 2 {
		t.Fatalf("lines = %+v", lines)
	}
	if l := lines[0]; l.Text != "@dave (Dave) same, see #general" || l.ReplyTo == nil || l.ReplyTo.Author != "dave" ||
		len([]rune(l.ReplyTo.Text)) != maxQuoteChars+1 || l.SentAt != "2026-11-27T10:02:00Z" {
		t.Errorf("reply line = %+v", l)
	}
	if l := lines[1]; l.Text != "[attachment: flat.jpg] [embed: Leith flats] [embed] [sticker: wave]" {
		t.Errorf("photo line = %q", l.Text)
	}
	if strings.Contains(lastPrompt(agent), "http") {
		t.Errorf("prompt has a URL: %s", lastPrompt(agent))
	}
}
