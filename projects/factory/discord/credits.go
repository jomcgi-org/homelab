package discord

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
)

// Out of gloves-off credits (HOME-938, Joe 2026-11-28). abliteration.ai
// answers 402 when the prepaid balance is spent (docs.abliteration.ai,
// "API errors"); Hermes fails the turn with "HTTP 402: …", and the kagent
// fork's Hermes adapter makes that the allowlisted reason
// ReasonNoCredits. A 429 is a rate limit, which Hermes retries itself; it
// stays a plain failure here.
//
//	gloves-off turn -> insufficient_credits -> notice + 🔄 (24 h)
//	Joe's 🔄 -> the same turn once more, in the same session
//	  answered -> reply as usual
//	  no credits again -> a new notice + 🔄; never an automatic retry
//
// A retry answers in gloves-off mode even when the window has ended, and
// never opens the window again. The pending retry is in bridge memory
// only. The offer line (offer.go) falls back to a canned line instead.

// ReasonNoCredits is the failure reason of a turn that the provider
// refused for an empty prepaid balance (kagent fork Hermes adapter).
const ReasonNoCredits = "insufficient_credits"

// CreditsExhausted reports whether err is a turn that failed for want of
// provider credits.
func CreditsExhausted(err error) bool {
	var f *TurnFailure
	return errors.As(err, &f) && f.State == kagent.StateFailed && strings.Contains(f.Message, ReasonNoCredits)
}

const (
	retryEmoji = "🔄"
	retryTTL   = 24 * time.Hour
	// maxRetries bounds the waiting retries; past it a notice asks Joe to
	// send the message again.
	maxRetries = 16
)

// glovesTopUpURL is where Joe buys credits: the console's billing page
// (Joe, 2026-11-28; docs.abliteration.ai "API errors" names the console).
const glovesTopUpURL = "https://abliteration.ai/console/billing"

// ReplyNoCredits answers a gloves-off turn with no credits left.
const ReplyNoCredits = "Out of gloves-off credits. Top up: <" + glovesTopUpURL + ">, then tap " + retryEmoji + " to retry."

// ReplyNoCreditsResend answers when no retry can wait: too many wait, or
// the Replier cannot react.
const ReplyNoCreditsResend = "Out of gloves-off credits. Top up: <" + glovesTopUpURL + ">, then send it again."

// pendingRetry is a gloves-off turn waiting for Joe's 🔄.
type pendingRetry struct {
	m       Message
	window  unsafeWindow
	attempt int
	until   time.Time
	// taking is set while a 🔄 tries to queue the turn.
	taking bool
}

// noCredits posts the notice for gloves-off turn m with the gloves-off
// footer, so history readers drop it and m, then reacts 🔄 and keeps the
// retry. Without an Actor it posts the notice with no retry.
func (b *Bridge) noCredits(ctx context.Context, l *slog.Logger, m Message, w unsafeWindow, attempt int) {
	actor, ok := b.Replies.(Actor)
	b.mu.Lock()
	for k, r := range b.retries {
		if !b.now().Before(r.until) {
			delete(b.retries, k)
		}
	}
	full := len(b.retries) >= maxRetries
	b.mu.Unlock()
	if !ok || full {
		// No retry to offer: the notice asks Joe to send it again.
		b.reply(ctx, m, withUnsafeFooter(ReplyNoCreditsResend, w.end))
		return
	}
	id, err := actor.Post(ctx, m.ChannelID, NewReply(withUnsafeFooter(ReplyNoCredits, w.end), m.ID))
	if err != nil || id == "" {
		l.WarnContext(ctx, "no-credits notice failed", "error", err)
		return
	}
	b.mu.Lock()
	if b.retries == nil {
		b.retries = map[string]pendingRetry{}
	}
	// Each notice lives retryTTL from its own post, as it says.
	b.retries[id] = pendingRetry{m: m, window: w, attempt: attempt, until: b.now().Add(retryTTL)}
	b.mu.Unlock()
	if err := actor.React(ctx, m.ChannelID, id, retryEmoji); err != nil {
		actionFailed(ctx, l, "retry react", err) // Joe can still add 🔄 himself.
	}
	l.InfoContext(ctx, "gloves off out of credits", "notice", id, "attempt", attempt)
}

// acceptRetry takes Joe's 🔄 on a no-credits notice: the same gloves-off
// turn once more, on the channel's queue. The caller checked that the
// reaction is Joe's 🔄.
func (b *Bridge) acceptRetry(ctx context.Context, r ReactionAdd) {
	now := b.now()
	b.mu.Lock()
	p, ok := b.retries[r.MessageID]
	if !ok || p.taking || p.m.ChannelID != r.ChannelID || !now.Before(p.until) {
		b.mu.Unlock()
		return
	}
	p.taking = true // One 🔄 at a time.
	b.retries[r.MessageID] = p
	b.mu.Unlock()
	release := func(used bool) {
		b.mu.Lock()
		if used {
			delete(b.retries, r.MessageID) // A notice is retried once.
		} else if q, ok := b.retries[r.MessageID]; ok {
			q.taking = false
			b.retries[r.MessageID] = q
		}
		b.mu.Unlock()
	}
	notices, admitted := b.admitReplay(ctx)
	if !admitted {
		release(false)
		return
	}
	b.mu.Lock()
	// The window as it was: an ended window stays ended (no openWindow).
	if b.decided == nil {
		b.decided = map[string]unsafeDecision{}
	}
	prev, hadPrev := b.decided[p.m.ID]
	b.decided[p.m.ID] = unsafeDecision{unsafe: true, window: p.window, at: now}
	if b.attempts == nil {
		b.attempts = map[string]int{}
	}
	b.attempts[p.m.ID] = p.attempt + 1
	b.mu.Unlock()
	if !b.reserveTurn(ctx, p.m) {
		b.unmarkGlovesTurn(p.m.ID, prev, hadPrev)
		b.mu.Lock()
		delete(b.attempts, p.m.ID)
		b.mu.Unlock()
		release(false)
		b.queueNotice(ctx, notices, Message{ID: r.MessageID, ChannelID: r.ChannelID, GuildID: p.m.GuildID}, ReplyGlovesBusy)
		return
	}
	release(true)
	b.Log.InfoContext(ctx, "gloves-off retry", "channel", p.m.ChannelID, "message", p.m.ID, "attempt", p.attempt+1)
}

// takeAttempt returns and clears m's retry attempt: 0 for a first attempt.
func (b *Bridge) takeAttempt(id string) int {
	b.mu.Lock()
	defer b.mu.Unlock()
	n := b.attempts[id]
	delete(b.attempts, id)
	return n
}

// attemptID is the A2A message ID of an attempt: kagent answers a repeated
// message ID with its earlier task, so each retry needs its own.
func attemptID(messageID string, attempt int) string {
	if attempt == 0 {
		return messageID
	}
	return fmt.Sprintf("%s-retry%d", messageID, attempt)
}
