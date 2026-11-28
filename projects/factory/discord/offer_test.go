package discord

import (
	"context"
	"fmt"
	"strings"
	"sync"
	"testing"
	"time"
)

// blockingAgent is a normal server agent whose filter refuses every turn.
type blockingAgent struct {
	mu    sync.Mutex
	turns int
}

func (f *blockingAgent) Turn(context.Context, string, string, string, ...TurnImage) (string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.turns++
	return "", blockedErr
}

func (f *blockingAgent) count() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.turns
}

// offerReplies posts with IDs and records reactions, safe for the notice
// sender and the channel workers at once.
type offerReplies struct {
	fakeReplies
	reacts []string
}

func (f *offerReplies) Post(ctx context.Context, c string, r Reply) (string, error) {
	_ = f.Send(ctx, c, r)
	f.mu.Lock()
	defer f.mu.Unlock()
	return fmt.Sprintf("o%d", len(f.sent)), nil
}

func (f *offerReplies) React(_ context.Context, c, m, e string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.reacts = append(f.reacts, c+"/"+m+" "+e)
	return nil
}

func (f *offerReplies) StartThread(context.Context, string, string, string) (string, error) {
	return "", nil
}

func (f *offerReplies) Pin(context.Context, string, string) error { return nil }

func (f *offerReplies) replyTo(n int) string {
	f.mu.Lock()
	defer f.mu.Unlock()
	if r := f.sent[n].MessageReference; r != nil {
		return r.MessageID
	}
	return ""
}

// offerBridge is unsafeBridge with a normal agent that is always blocked.
func offerBridge(t *testing.T) (*Bridge, *blockingAgent, *fakeSessionAgent, *offerReplies, *testClock) {
	t.Helper()
	b, _, unsafe, _, _, clock := unsafeBridge(t)
	normal := &blockingAgent{}
	b.Servers = map[string]SessionAgent{guild: normal}
	rep := &offerReplies{}
	b.Replies = rep
	return b, normal, unsafe, rep, clock
}

func joeReacts(offer string) ReactionAdd {
	r := ReactionAdd{UserID: joe, ChannelID: channel, MessageID: offer, GuildID: guild}
	r.Emoji.Name = glovesEmoji
	return r
}

func TestABlockedTurnOffersGlovesOff(t *testing.T) {
	b, normal, _, rep, _ := offerBridge(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	b.Handle(ctx, byJoe(tagged("1", "the spicy joke")))
	waitFor(t, func() bool { return normal.count() == 1 && len(rep.texts()) == 1 })
	cancel()
	b.Wait()
	text := rep.texts()[0]
	if !strings.HasPrefix(text, offerLine("1")+" "+offerAsk+"\n") || !strings.Contains(text, "offer · ends <t:") {
		t.Errorf("offer = %q", text)
	}
	// The offer is marked like a gloves-off reply, so history readers drop
	// it and the blocked message it answers.
	h := HistoryMessage{Message: Message{ID: "o1", Author: User{ID: bot}, Reference: &Reference{MessageID: "1"}}}
	h.Components = []Component{{Type: componentText, Content: text}}
	if ref, ok := unsafeReplyRef(h, bot); !ok || ref != "1" {
		t.Errorf("offer is not a marked reply: %q", text)
	}
	if strings.Contains(text, "spicy") || strings.Contains(strings.ToLower(text), "unsafe") {
		t.Errorf("the offer holds the message or the word unsafe: %q", text)
	}
	if rep.replyTo(0) != "1" || strings.Join(rep.reacts, ",") != channel+"/o1 🥊" {
		t.Errorf("offer replies to %q, reactions %v", rep.replyTo(0), rep.reacts)
	}
}

// Only Joe's 🥊 takes the offer. It turns gloves off and replays the blocked
// message on the gloves-off agent alone, answering Joe's message, once.
func TestJoesGlovesReactionReplaysTheBlockedMessageOffTheRecord(t *testing.T) {
	b, normal, unsafe, rep, _ := offerBridge(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	b.Handle(ctx, byJoe(tagged("1", "the spicy joke")))
	waitFor(t, func() bool { return len(rep.texts()) == 1 })

	other := joeReacts("o1")
	other.UserID = "u1"
	b.HandleReaction(ctx, other)
	thumbs := joeReacts("o1")
	thumbs.Emoji.Name = "👍"
	b.HandleReaction(ctx, thumbs)
	b.mu.Lock()
	_, on := b.windows[channel]
	_, waiting := b.offers["o1"]
	b.mu.Unlock()
	if on || !waiting {
		t.Fatalf("someone else's 🥊 or Joe's 👍 took the offer: gloves off %v, offer waiting %v", on, waiting)
	}
	b.HandleReaction(ctx, joeReacts("o1"))
	b.HandleReaction(ctx, joeReacts("o1")) // Used once.
	waitFor(t, func() bool { return unsafe.count() == 1 && len(rep.texts()) == 3 })
	cancel()
	b.Wait()
	if unsafe.count() != 1 || normal.count() != 1 {
		t.Fatalf("gloves-off turns %d, normal turns %d; want 1 and 1", unsafe.count(), normal.count())
	}
	unsafe.mu.Lock()
	prompt := unsafe.prompts[0]
	unsafe.mu.Unlock()
	if !strings.Contains(prompt, "the spicy joke") {
		t.Errorf("replay prompt = %q", prompt)
	}
	gif, answer := -1, -1
	for n, text := range rep.texts()[1:] {
		switch {
		case strings.HasPrefix(text, "🥊 Gloves off until"):
			gif = n + 1
		case IsUnsafeReply(text):
			answer = n + 1
		}
	}
	if gif < 0 || rep.files(gif)[0].Name != "gloves-off.gif" || rep.replyTo(gif) != "o1" {
		t.Errorf("no gloves-off GIF on the offer: %q", rep.texts())
	}
	if answer < 0 || rep.replyTo(answer) != "1" {
		t.Errorf("the replay does not answer Joe's message: %q", rep.texts())
	}
	if !b.UnsafeMessage("1") || !b.blockedID("1") {
		t.Error("the blocked message is not marked gloves off and blocked")
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if len(b.offers) != 0 {
		t.Errorf("offers = %v after it was taken", b.offers)
	}
}

func TestAGlovesOfferExpires(t *testing.T) {
	b, _, unsafe, rep, clock := offerBridge(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	b.Handle(ctx, byJoe(tagged("1", "the spicy joke")))
	waitFor(t, func() bool { return len(rep.texts()) == 1 })
	clock.add(offerTTL + time.Second)
	b.HandleReaction(ctx, joeReacts("o1"))
	time.Sleep(50 * time.Millisecond)
	cancel()
	b.Wait()
	if unsafe.count() != 0 || len(rep.texts()) != 1 {
		t.Errorf("an expired offer ran %d turns and sent %q", unsafe.count(), rep.texts())
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if _, ok := b.windows[channel]; ok {
		t.Error("an expired offer turned gloves off")
	}
}

// The offer's timer forgets the blocked message when the offer ends.
func TestAnEndedOfferForgetsTheMessage(t *testing.T) {
	b, _, _, _, _ := offerBridge(t)
	b.Now = time.Now
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	b.mu.Lock()
	b.offers = map[string]glovesOffer{"o1": {m: byJoe(tagged("1", "the spicy joke")), until: time.Now().Add(20 * time.Millisecond)}}
	b.mu.Unlock()
	b.dropOfferAt(ctx, "o1", time.Now().Add(20*time.Millisecond))
	waitFor(t, func() bool {
		b.mu.Lock()
		defer b.mu.Unlock()
		return len(b.offers) == 0
	})
}

func TestABlockedTurnWithoutGlovesOffSaysBlocked(t *testing.T) {
	for name, setup := range map[string]func(b *Bridge){
		"not set up": func(b *Bridge) { b.Unsafe = nil },
		"not Joe": func(b *Bridge) {
			b.Config.OperatorOnly = false
		},
	} {
		t.Run(name, func(t *testing.T) {
			b, _, _, rep, _ := offerBridge(t)
			setup(b)
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			m := byJoe(tagged("1", "the spicy joke"))
			if name == "not Joe" {
				m = tagged("1", "the spicy joke")
			}
			b.Handle(ctx, m)
			waitFor(t, func() bool { return len(rep.texts()) == 1 })
			cancel()
			b.Wait()
			if got := rep.texts()[0]; got != ReplyBlocked || len(rep.reacts) != 0 {
				t.Errorf("reply = %q, reactions %v; want %q", got, rep.reacts, ReplyBlocked)
			}
		})
	}
}

// offerWriter is the gloves-off Agent writing offer lines.
type offerWriter struct {
	mu      sync.Mutex
	ids     []string
	prompts []string
	answer  string
	err     error
	delay   time.Duration
}

func (f *offerWriter) Turn(ctx context.Context, id, prompt string) (string, error) {
	f.mu.Lock()
	f.ids = append(f.ids, id)
	f.prompts = append(f.prompts, prompt)
	f.mu.Unlock()
	if f.delay > 0 {
		select {
		case <-time.After(f.delay):
		case <-ctx.Done():
			return "", ctx.Err()
		}
	}
	return f.answer, f.err
}

// The gloves-off model writes the line from the blocked text alone; the
// bridge adds the ask. An error, an empty answer or a slow one uses a
// canned line.
func TestTheGlovesOffModelWritesTheOfferLine(t *testing.T) {
	offerLineTimeout = 100 * time.Millisecond
	defer func() { offerLineTimeout = 5 * time.Second }()
	for name, tc := range map[string]struct {
		w    *offerWriter
		want string
	}{
		"written": {&offerWriter{answer: "\"Dario saw that joke and fainted.\"\nExtra line"}, "Dario saw that joke and fainted."},
		"error":   {&offerWriter{err: blockedErr}, offerLine("1")},
		"empty":   {&offerWriter{answer: "  \n"}, offerLine("1")},
		"slow":    {&offerWriter{answer: "too late", delay: time.Second}, offerLine("1")},
	} {
		t.Run(name, func(t *testing.T) {
			b, _, _, rep, _ := offerBridge(t)
			b.OfferWriter = tc.w
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			b.Handle(ctx, byJoe(tagged("1", "the spicy joke")))
			waitFor(t, func() bool { return len(rep.texts()) == 1 })
			cancel()
			b.Wait()
			if got := rep.texts()[0]; !strings.HasPrefix(got, tc.want+" "+offerAsk+"\n") {
				t.Errorf("offer = %q, want line %q", got, tc.want)
			}
			tc.w.mu.Lock()
			defer tc.w.mu.Unlock()
			if len(tc.w.ids) != 1 || tc.w.ids[0] != "offer-1" {
				t.Errorf("writer calls = %v, want one as offer-1", tc.w.ids)
			}
			p := tc.w.prompts[0]
			if !strings.Contains(p, `{"message":"the spicy joke"}`) || strings.Contains(p, "memories") || strings.Contains(p, "channel") {
				t.Errorf("writer prompt = %q", p)
			}
		})
	}
}

func TestCleanOfferLine(t *testing.T) {
	long := strings.Repeat("word ", 40)
	for in, want := range map[string]string{
		"Dario clutched his pearls.":              "Dario clutched his pearls.",
		"> \"Dario clutched his pearls.\"":        "Dario clutched his pearls.",
		"\n\nDario fainted. React 🥊 if you dare.": "Dario fainted.",
		"":            "",
		"React 🥊 now": "",
		long:          strings.TrimSpace(strings.Repeat("word ", 25)),
	} {
		if got := cleanOfferLine(in); got != want {
			t.Errorf("cleanOfferLine(%q) = %q, want %q", in, got, want)
		}
	}
}
