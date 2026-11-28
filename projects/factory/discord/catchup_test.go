package discord

import (
	"context"
	"errors"
	"fmt"
	"strconv"
	"sync"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
)

type catchUpSource struct {
	mu       sync.Mutex
	channels []Channel
	threads  []Channel
	messages map[string][]HistoryMessage
	reads    []string
}

func (f *catchUpSource) GuildChannels(context.Context, string) ([]Channel, error) {
	return f.channels, nil
}

func (f *catchUpSource) ActiveThreads(context.Context, string) ([]Channel, error) {
	return f.threads, nil
}

func (f *catchUpSource) MessagesAfter(_ context.Context, id, _ string) ([]HistoryMessage, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.reads = append(f.reads, id)
	return f.messages[id], nil
}

// at is a snowflake sent at t, with n in its low bits.
func at(t time.Time, n int) string {
	id, _ := strconv.ParseUint(Snowflake(t), 10, 64)
	return strconv.FormatUint(id+uint64(n), 10)
}

// The catch-up answers Joe's unanswered tags in the window once, and
// nothing else: answered, old, untagged or other people's messages, and
// channels with no recent message.
func TestCatchUpAnswersJoesUnansweredTagsOnce(t *testing.T) {
	agent := &fakeAgent{answer: "ok"}
	b, _, _ := newBridge(agent)
	b.Config.Operator.UserID = "u1"
	b.Config.UserLimit = Limit{}
	now := b.now()
	joe := User{ID: "u1", Username: "joe"}
	msg := func(id string, author User, content string, mentions ...User) HistoryMessage {
		return HistoryMessage{Message: Message{ID: id, ChannelID: channel, Author: author, Content: content, Mentions: mentions}}
	}
	tag := "<@" + bot + "> "
	answered := msg(at(now.Add(-10*time.Minute), 1), joe, tag+"answered", User{ID: bot})
	reply := msg(at(now.Add(-9*time.Minute), 2), User{ID: bot, Bot: true}, "")
	reply.Reference = &Reference{MessageID: answered.ID}
	missed := msg(at(now.Add(-3*time.Minute), 3), joe, tag+"missed", User{ID: bot})
	threaded := msg(at(now.Add(-2*time.Minute), 4), joe, tag+"has a thread", User{ID: bot})
	threaded.Thread = &Channel{ID: "999"}
	reacted := msg(at(now.Add(-2*time.Minute), 8), joe, tag+"react to this", User{ID: bot})
	reacted.Reactions = []Reaction{{Me: true}}
	src := &catchUpSource{
		channels: []Channel{
			{ID: channel, Type: 0, LastMessageID: missed.ID},
			{ID: "quiet", Type: 0, LastMessageID: at(now.Add(-2*time.Hour), 0)},
			{ID: "voice", Type: 2, LastMessageID: missed.ID},
		},
		messages: map[string][]HistoryMessage{channel: {
			missed, threaded, reacted, reply, answered, // Any order.
			msg(at(now.Add(-20*time.Minute), 5), joe, tag+"too old", User{ID: bot}),
			msg(at(now.Add(-1*time.Minute), 6), User{ID: "u2"}, tag+"not joe", User{ID: bot}),
			msg(at(now.Add(-1*time.Minute), 7), joe, "no tag"),
		}},
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	if n := b.CatchUp(ctx, src, 15*time.Minute); n != 1 {
		t.Fatalf("catch-up turns = %d, want 1", n)
	}
	if got := waitTurns(agent, 1); got != 1 {
		t.Fatalf("turns = %d", got)
	}
	// A second pass and the gateway's own copy start nothing.
	if n := b.CatchUp(ctx, src, 15*time.Minute); n != 0 {
		t.Errorf("second catch-up turns = %d, want 0", n)
	}
	live := missed.Message
	live.GuildID = guild
	b.Handle(ctx, live)
	cancel()
	b.Wait()
	if len(agent.prompts) != 1 {
		t.Errorf("turns = %d, want 1", len(agent.prompts))
	}
	for _, id := range src.reads {
		if id != channel {
			t.Errorf("read channel %s, which has no recent message or is not text", id)
		}
	}
}

// Without Joe's user ID the catch-up reads nothing.
func TestCatchUpNeedsJoesID(t *testing.T) {
	b, _, _ := newBridge(&fakeAgent{})
	src := &catchUpSource{channels: []Channel{{ID: channel, LastMessageID: at(b.now(), 0)}}}
	if n := b.CatchUp(context.Background(), src, 15*time.Minute); n != 0 || len(src.reads) != 0 {
		t.Errorf("turns = %d, reads = %v", n, src.reads)
	}
}

// After a restart, Joe's tag sent while Bosun's notice says gloves off was
// on is not a normal turn (HOME-938). A "Gloves back on" notice ends that.
func TestCatchUpLeavesGlovesOffTagsUnanswered(t *testing.T) {
	for name, tc := range map[string]struct {
		glovesOn bool
		want     int
	}{"gloves off": {false, 0}, "gloves back on": {true, 1}} {
		t.Run(name, func(t *testing.T) {
			agent := &fakeAgent{answer: "ok"}
			b, _, _ := newBridge(agent)
			b.Config.Operator.UserID = "u1"
			b.Config.UserLimit = Limit{}
			b.Config.Unsafe = UnsafeConfig{Agent: unsafeRef, Duration: 30 * time.Minute, MaxDuration: 4 * time.Hour}
			now := b.now()
			bosun := func(id, text string) HistoryMessage {
				h := HistoryMessage{Message: Message{ID: id, ChannelID: channel, Author: User{ID: bot, Bot: true}}}
				h.Components = []Component{{Type: componentText, Content: text}}
				return h
			}
			// The notice is older than the catch-up window.
			notice := bosun(at(now.Add(-20*time.Minute), 1), fmt.Sprintf("🥊 Gloves off until <t:%d:R>: plain chat, no tools and no memory.", now.Add(10*time.Minute).Unix()))
			b.History = &channelHistory{msgs: []HistoryMessage{notice}}
			var after []HistoryMessage
			if tc.glovesOn {
				after = append(after, bosun(at(now.Add(-5*time.Minute), 2), ReplyGlovesOn))
			}
			joe := User{ID: "u1", Username: "joe"}
			tag := HistoryMessage{Message: Message{
				ID: at(now.Add(-3*time.Minute), 3), ChannelID: channel, Author: joe,
				Content: "<@" + bot + "> a joke", Mentions: []User{{ID: bot}},
			}}
			after = append(after, tag)
			src := &catchUpSource{
				channels: []Channel{{ID: channel, Type: 0, LastMessageID: tag.ID}},
				messages: map[string][]HistoryMessage{channel: after},
			}
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			if n := b.CatchUp(ctx, src, 15*time.Minute); n != tc.want {
				t.Errorf("catch-up turns = %d, want %d", n, tc.want)
			}
			cancel()
			b.Wait()
		})
	}
}

var unsafeRef = kagent.AgentRef{Namespace: "discord-unsafe", Name: "unsafe"}

// When the page before the window cannot be read, the channel gets no
// catch-up turn: a tag could be a gloves-off one (HOME-938).
func TestCatchUpFailsClosedWithoutTheEarlierPage(t *testing.T) {
	agent := &fakeAgent{answer: "ok"}
	b, _, _ := newBridge(agent)
	b.Config.Operator.UserID = "u1"
	b.Config.UserLimit = Limit{}
	b.Config.Unsafe = UnsafeConfig{Agent: unsafeRef, Duration: 30 * time.Minute, MaxDuration: 4 * time.Hour}
	b.History = &channelHistory{err: errors.New("discord down")}
	now := b.now()
	tag := HistoryMessage{Message: Message{
		ID: at(now.Add(-3*time.Minute), 3), ChannelID: channel,
		Author: User{ID: "u1", Username: "joe"}, Content: "<@" + bot + "> hi", Mentions: []User{{ID: bot}},
	}}
	src := &catchUpSource{
		channels: []Channel{{ID: channel, Type: 0, LastMessageID: tag.ID}},
		messages: map[string][]HistoryMessage{channel: {tag}},
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	if n := b.CatchUp(ctx, src, 15*time.Minute); n != 0 {
		t.Errorf("catch-up turns = %d, want 0", n)
	}
	cancel()
	b.Wait()
}
