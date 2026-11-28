package discord

import (
	"context"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
)

// noCreditsErr is a gloves-off turn refused for an empty balance, as the
// kagent fork's Hermes adapter reports it.
var noCreditsErr = &TurnFailure{State: kagent.StateFailed, Message: "Hermes turn failed: " + ReasonNoCredits}

// scriptedAgent answers gloves-off turns from a script of errors, then
// "noted"; it records each session key and A2A message ID.
type scriptedAgent struct {
	mu   sync.Mutex
	errs []error
	keys []string
	ids  []string
}

func (f *scriptedAgent) Turn(_ context.Context, key, id, _ string) (string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.keys = append(f.keys, key)
	f.ids = append(f.ids, id)
	if len(f.errs) > 0 {
		err := f.errs[0]
		f.errs = f.errs[1:]
		if err != nil {
			return "", err
		}
	}
	return "noted", nil
}

func (f *scriptedAgent) calls() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.ids)
}

// creditsBridge has gloves off on in the channel and a gloves-off agent
// that runs out of credits.
func creditsBridge(t *testing.T, errs ...error) (*Bridge, *scriptedAgent, *offerReplies, *testClock, context.Context, context.CancelFunc) {
	t.Helper()
	b, _, _, rep, clock := offerBridge(t)
	agent := &scriptedAgent{errs: errs}
	b.Unsafe = agent
	ctx, cancel := context.WithCancel(context.Background())
	b.Handle(ctx, byJoe(tagged("1", "gloves off")))
	waitFor(t, func() bool { return len(rep.texts()) == 1 })
	return b, agent, rep, clock, ctx, cancel
}

func retryReaction(user, notice string) ReactionAdd {
	r := ReactionAdd{UserID: user, ChannelID: channel, MessageID: notice, GuildID: guild}
	r.Emoji.Name = retryEmoji
	return r
}

func TestCreditsExhaustedIsOnlyTheNoCreditsReason(t *testing.T) {
	for err, want := range map[error]bool{
		noCreditsErr: true,
		&TurnFailure{State: kagent.StateFailed, Message: "Hermes turn failed"}:                       false, // 429 and others
		&TurnFailure{State: kagent.StateFailed, Message: "Hermes turn failed: rate_limit"}:           false,
		&TurnFailure{State: kagent.StateCanceled, Message: "Hermes turn failed: " + ReasonNoCredits}: false,
		blockedErr: false,
		nil:        false,
	} {
		if got := CreditsExhausted(err); got != want {
			t.Errorf("CreditsExhausted(%v) = %v, want %v", err, got, want)
		}
	}
}

// No credits: a marked notice with the top-up link and Bosun's 🔄. Only
// Joe's 🔄 retries, once, in the same session with a new message ID.
func TestNoCreditsNoticeAndJoesRetry(t *testing.T) {
	b, agent, rep, _, ctx, cancel := creditsBridge(t, noCreditsErr)
	defer cancel()
	b.Handle(ctx, byJoe(tagged("2", "a joke")))
	waitFor(t, func() bool { return len(rep.texts()) == 2 })
	notice := rep.texts()[1]
	if !strings.HasPrefix(notice, ReplyNoCredits) || !IsUnsafeReply(notice) || rep.replyTo(1) != "2" {
		t.Fatalf("notice = %q replying to %q", notice, rep.replyTo(1))
	}
	if got := strings.Join(rep.reacts, ","); got != channel+"/o2 "+retryEmoji {
		t.Fatalf("reactions = %q", got)
	}
	b.HandleReaction(ctx, retryReaction("u1", "o2"))
	thumbs := retryReaction(joe, "o2")
	thumbs.Emoji.Name = "👍"
	b.HandleReaction(ctx, thumbs)
	time.Sleep(50 * time.Millisecond)
	if agent.calls() != 1 {
		t.Fatalf("someone else's 🔄 or Joe's 👍 retried: %d calls", agent.calls())
	}
	b.HandleReaction(ctx, retryReaction(joe, "o2"))
	b.HandleReaction(ctx, retryReaction(joe, "o2")) // Once.
	waitFor(t, func() bool { return len(rep.texts()) == 3 })
	time.Sleep(50 * time.Millisecond)
	cancel()
	b.Wait()
	if got := rep.texts()[2]; !strings.HasPrefix(got, "noted\n\n") || !IsUnsafeReply(got) || rep.replyTo(2) != "2" {
		t.Errorf("retry answer = %q replying to %q", got, rep.replyTo(2))
	}
	agent.mu.Lock()
	defer agent.mu.Unlock()
	if len(agent.ids) != 2 || agent.ids[0] != "2" || agent.ids[1] != "2-retry1" || agent.keys[0] != agent.keys[1] {
		t.Errorf("attempts ids %v keys %v; want 2 then 2-retry1 in one session", agent.ids, agent.keys)
	}
}

// Still no credits: a new notice with a new 🔄, and no retry without Joe.
func TestARetryWithNoCreditsPostsTheNoticeAgain(t *testing.T) {
	b, agent, rep, _, ctx, cancel := creditsBridge(t, noCreditsErr, noCreditsErr)
	defer cancel()
	b.Handle(ctx, byJoe(tagged("2", "a joke")))
	waitFor(t, func() bool { return len(rep.texts()) == 2 })
	b.HandleReaction(ctx, retryReaction(joe, "o2"))
	waitFor(t, func() bool { return len(rep.texts()) == 3 })
	time.Sleep(100 * time.Millisecond)
	cancel()
	b.Wait()
	if agent.calls() != 2 {
		t.Errorf("calls = %d, want 2: no loop", agent.calls())
	}
	if got := rep.texts()[2]; !strings.HasPrefix(got, ReplyNoCredits) {
		t.Errorf("second notice = %q", got)
	}
	if got := strings.Join(rep.reacts, ","); got != channel+"/o2 🔄,"+channel+"/o3 🔄" {
		t.Errorf("reactions = %q", got)
	}
}

// A retry after the window ended answers in gloves-off mode once and does
// not open the window again.
func TestARetryAfterTheWindowEndsDoesNotReopenIt(t *testing.T) {
	b, agent, rep, clock, ctx, cancel := creditsBridge(t, noCreditsErr)
	defer cancel()
	b.Handle(ctx, byJoe(tagged("2", "a joke")))
	waitFor(t, func() bool { return len(rep.texts()) == 2 })
	clock.add(2 * time.Hour)
	b.mu.Lock()
	delete(b.windows, channel) // The timer has fired.
	b.mu.Unlock()
	b.HandleReaction(ctx, retryReaction(joe, "o2"))
	waitFor(t, func() bool { return len(rep.texts()) == 3 })
	cancel()
	b.Wait()
	if agent.calls() != 2 || !IsUnsafeReply(rep.texts()[2]) {
		t.Errorf("retry = %q after %d calls", rep.texts()[2], agent.calls())
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if _, on := b.windows[channel]; on {
		t.Error("the retry opened the window again")
	}
}

func TestARetryExpires(t *testing.T) {
	b, agent, rep, clock, ctx, cancel := creditsBridge(t, noCreditsErr)
	defer cancel()
	b.Handle(ctx, byJoe(tagged("2", "a joke")))
	waitFor(t, func() bool { return len(rep.texts()) == 2 })
	clock.add(retryTTL + time.Minute)
	b.HandleReaction(ctx, retryReaction(joe, "o2"))
	time.Sleep(50 * time.Millisecond)
	cancel()
	b.Wait()
	if agent.calls() != 1 || len(rep.texts()) != 2 {
		t.Errorf("an expired retry ran: %d calls, %q", agent.calls(), rep.texts())
	}
}

// A rate limit (or any other failure) is not out of credits.
func TestARateLimitIsAPlainFailure(t *testing.T) {
	b, _, rep, _, ctx, cancel := creditsBridge(t, &TurnFailure{State: kagent.StateFailed, Message: "Hermes turn failed"})
	defer cancel()
	b.Handle(ctx, byJoe(tagged("2", "a joke")))
	waitFor(t, func() bool { return len(rep.texts()) == 2 })
	cancel()
	b.Wait()
	if got := rep.texts()[1]; !strings.HasPrefix(got, ReplyFailed) || len(rep.reacts) != 0 {
		t.Errorf("reply = %q, reactions %v", got, rep.reacts)
	}
}

// The offer line falls back to a canned line with no notice; the replay
// after Joe's 🥊 shows the notice.
func TestNoCreditsForTheOfferLineIsSilentButTheReplayShowsIt(t *testing.T) {
	b, _, _, rep, _ := offerBridge(t)
	b.OfferWriter = &offerWriter{err: noCreditsErr}
	agent := &scriptedAgent{errs: []error{noCreditsErr}}
	b.Unsafe = agent
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	b.Handle(ctx, byJoe(tagged("1", "the spicy joke")))
	waitFor(t, func() bool { return len(rep.texts()) == 1 })
	if got := rep.texts()[0]; !strings.HasPrefix(got, offerLine("1")+" "+offerAsk) {
		t.Fatalf("offer = %q", got)
	}
	b.HandleReaction(ctx, joeReacts("o1"))
	waitFor(t, func() bool {
		for _, s := range rep.texts() {
			if strings.HasPrefix(s, ReplyNoCredits) {
				return true
			}
		}
		return false
	})
	cancel()
	b.Wait()
}
