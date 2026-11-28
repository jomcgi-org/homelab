package discord

import (
	"context"
	"strings"
	"sync"
	"testing"
	"time"
)

// forgetter records the archive rows a refusal clears.
type forgetter struct {
	mu  sync.Mutex
	ids []string
}

func (f *forgetter) Forget(_, _, id string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.ids = append(f.ids, id)
}

// The live path through a refusal: Joe's tagged message is held in the
// ingest buffer while its turn runs, so no flush stores it. The refusal
// drops it, clears its archive row, and no later prompt sees it. A normal
// turn releases its message, which is then stored.
func TestARefusedMessageNeverLeavesLiveStorage(t *testing.T) {
	b, normal, _, rep, _ := offerBridge(t)
	b.Unsafe, b.OfferWriter = nil, nil // Plain ReplyBlocked: the hold is the same.
	gate := make(chan struct{})
	slow := &gatedBlocker{gate: gate}
	b.Servers = map[string]SessionAgent{guild: slow}
	arc := &forgetter{}
	b.Archive = arc
	mem := b.Ingest.Memory.(*ingestMemory)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	b.Handle(ctx, byJoe(tagged("1", "the spicy joke")))
	waitFor(t, func() bool { return slow.count() == 1 })
	// A flush while the turn runs does not send the held line, and a
	// prompt's recent lines leave it out.
	if err := b.Ingest.Flush(ctx, guild, channel); err != nil {
		t.Fatal(err)
	}
	if got := b.Ingest.Recent(guild, channel, "", 10, 10000); len(got) != 0 {
		t.Errorf("recent lines during the turn = %q", got)
	}
	close(gate)
	waitFor(t, func() bool { return len(rep.texts()) == 1 })
	_ = normal
	cancel()
	b.Wait()
	if err := b.Ingest.Flush(context.Background(), guild, channel); err != nil {
		t.Fatal(err)
	}
	for _, c := range mem.contents() {
		if strings.Contains(c, "spicy") {
			t.Errorf("Hindsight got the refused message: %q", c)
		}
	}
	arc.mu.Lock()
	defer arc.mu.Unlock()
	if strings.Join(arc.ids, ",") != "1" {
		t.Errorf("archive rows cleared = %v, want 1", arc.ids)
	}
}

func TestAnAnsweredMessageIsReleasedToHindsight(t *testing.T) {
	b, _, _, _, rep, _ := unsafeBridge(t)
	mem := b.Ingest.Memory.(*ingestMemory)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	b.Handle(ctx, byJoe(tagged("1", "a normal question")))
	waitFor(t, func() bool { return len(rep.texts()) == 1 })
	cancel()
	b.Wait()
	if err := b.Ingest.Flush(context.Background(), guild, channel); err != nil {
		t.Fatal(err)
	}
	if got := strings.Join(mem.contents(), "\n"); !strings.Contains(got, "a normal question") {
		t.Errorf("stored = %q, want the answered message", got)
	}
}

// A held line that no turn settles is stored after maxHold.
func TestAHeldLineIsReleasedAfterMaxHold(t *testing.T) {
	clock := &testClock{t: time.Date(2026, 11, 28, 12, 0, 0, 0, time.UTC)}
	mem := &ingestMemory{}
	in := &Ingestor{Memory: mem, Log: quiet, Now: clock.now}
	in.AddHeld(guild, channel, "1", clock.now(), "joe: held")
	if err := in.Flush(context.Background(), guild, channel); err != nil || len(mem.contents()) != 0 {
		t.Fatalf("flushed a held line: %v %q", err, mem.contents())
	}
	clock.add(maxHold)
	if err := in.Flush(context.Background(), guild, channel); err != nil || len(mem.contents()) != 1 {
		t.Fatalf("an expired hold was not stored: %v %q", err, mem.contents())
	}
}

// gatedBlocker is a normal agent that refuses once gate closes.
type gatedBlocker struct {
	gate  chan struct{}
	mu    sync.Mutex
	turns int
}

func (f *gatedBlocker) Turn(ctx context.Context, _, _, _ string, _ ...TurnImage) (string, error) {
	f.mu.Lock()
	f.turns++
	f.mu.Unlock()
	select {
	case <-f.gate:
	case <-ctx.Done():
	}
	return "", blockedErr
}

func (f *gatedBlocker) count() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.turns
}

// Forget clears a live row and keeps a later backfill page from writing it
// again.
func TestArchiverForgetClearsARowForGood(t *testing.T) {
	store := &fakeArchive{}
	a := newTestArchiver(store)
	plain := HistoryMessage{Message: Message{
		ID: "21", ChannelID: arcChannel, GuildID: arcGuild,
		Author: User{ID: "u1", Username: "ann"}, Content: "the spicy joke",
	}}
	a.Event("MESSAGE_CREATE", archiveEvent(t, plain))
	drain(a)
	a.Forget(arcGuild, arcChannel, "21")
	drain(a)
	a.Page(context.Background(), arcGuild, Channel{ID: arcChannel}, "", []HistoryMessage{plain})
	store.mu.Lock()
	defer store.mu.Unlock()
	if len(store.deletes) != 1 || store.deletes[0][0] != "21" {
		t.Errorf("deletes = %v", store.deletes)
	}
	if len(store.rows) != 1 {
		t.Errorf("rows = %d, want only the live one before Forget", len(store.rows))
	}
}

// A 🥊 whose replay finds no place (the global limit) keeps the offer: no
// window opens, Joe hears why, and a later 🥊 works.
func TestAGlovesReactionWithNoPlaceKeepsTheOffer(t *testing.T) {
	b, _, unsafe, rep, _ := offerBridge(t)
	b.Config.GlobalLimit = Limit{Count: 1, Window: time.Hour}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	b.Handle(ctx, byJoe(tagged("1", "the spicy joke")))
	waitFor(t, func() bool { return len(rep.texts()) == 1 })
	b.HandleReaction(ctx, joeReacts("o1"))
	waitFor(t, func() bool { return len(rep.texts()) == 2 })
	if got := rep.texts()[1]; got != ReplyGlovesBusy || unsafe.count() != 0 {
		t.Fatalf("reply = %q, gloves-off turns %d", got, unsafe.count())
	}
	b.mu.Lock()
	_, on := b.windows[channel]
	_, waiting := b.offers["o1"]
	decided := b.decided["1"].unsafe
	b.mu.Unlock()
	if on || !waiting || decided {
		t.Fatalf("after no place: window %v, offer waiting %v, decided gloves off %v", on, waiting, decided)
	}
	b.mu.Lock()
	b.Config.GlobalLimit.Count = 5
	b.mu.Unlock()
	b.HandleReaction(ctx, joeReacts("o1"))
	waitFor(t, func() bool { return unsafe.count() == 1 })
	cancel()
	b.Wait()
}

// The same for a 🔄 on a no-credits notice.
func TestARetryReactionWithNoPlaceKeepsTheNotice(t *testing.T) {
	b, agent, rep, _, ctx, cancel := creditsBridge(t, noCreditsErr)
	defer cancel()
	b.Handle(ctx, byJoe(tagged("2", "a joke")))
	waitFor(t, func() bool { return len(rep.texts()) == 2 })
	b.mu.Lock()
	b.Config.GlobalLimit = Limit{Count: 1, Window: time.Hour}
	b.mu.Unlock()
	b.HandleReaction(ctx, retryReaction(joe, "o2"))
	waitFor(t, func() bool { return len(rep.texts()) == 3 })
	if got := rep.texts()[2]; got != ReplyGlovesBusy || agent.calls() != 1 {
		t.Fatalf("reply = %q after %d calls", got, agent.calls())
	}
	b.mu.Lock()
	_, waiting := b.retries["o2"]
	b.Config.GlobalLimit.Count = 5
	b.mu.Unlock()
	if !waiting {
		t.Fatal("the notice was used without a retry")
	}
	b.HandleReaction(ctx, retryReaction(joe, "o2"))
	waitFor(t, func() bool { return agent.calls() == 2 })
	cancel()
	b.Wait()
}

// A notice posted after a retry lives its own 24 h, not the first one's.
func TestARenewedNoticeGetsItsOwnLifetime(t *testing.T) {
	b, _, rep, clock, ctx, cancel := creditsBridge(t, noCreditsErr, noCreditsErr)
	defer cancel()
	b.Handle(ctx, byJoe(tagged("2", "a joke")))
	waitFor(t, func() bool { return len(rep.texts()) == 2 })
	clock.add(retryTTL - time.Minute)
	b.HandleReaction(ctx, retryReaction(joe, "o2"))
	waitFor(t, func() bool { return len(rep.texts()) == 3 })
	b.mu.Lock()
	until := b.retries["o3"].until
	b.mu.Unlock()
	if want := clock.now().Add(retryTTL); !until.Equal(want) {
		t.Errorf("renewed notice ends %s, want %s", until, want)
	}
	cancel()
	b.Wait()
}
