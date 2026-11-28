package discord

import (
	"context"
	"fmt"
	"hash/fnv"
	"log/slog"
	"time"
)

// A gloves-off offer (HOME-938, Joe 2026-11-28): when the normal model's
// filter blocks Joe's message, the bridge answers with a canned line and a
// 🥊 reaction in place of ReplyBlocked. Joe's 🥊 on it within offerTTL
// starts gloves off in that channel and replays the blocked message there
// as a gloves-off turn. The lines hold no message text and no model writes
// them, so nothing trips a filter again. The blocked message stays in
// bridge memory only, until the offer ends.
//
//	normal turn -> content_policy_blocked -> offer line + 🥊 (10 min)
//	Joe's 🥊 -> gloves off (default window, GIF) -> replay on the
//	            gloves-off agent, answering Joe's message
//	no 🥊, someone else's 🥊, or 10 min later -> nothing

const (
	glovesEmoji = "🥊"
	offerTTL    = 10 * time.Minute
	// maxOffers bounds the waiting offers; past it a new offer is not made.
	maxOffers = 16
)

// offerLines are the canned offers; offerLine picks one per message.
var offerLines = []string{
	"Dario doesn't think that's 🔥. React 🥊 if you want me to take my gloves off.",
	"My corner threw in the towel on that one. React 🥊 and I'll take my gloves off.",
	"That's above my weight class with the gloves on. React 🥊 to take them off.",
	"The ref stopped that one. React 🥊 if you want it bare-knuckle.",
}

// offerLine picks a line by message ID, so the choice is the same for the
// same message and needs no random source.
func offerLine(messageID string) string {
	h := fnv.New32a()
	_, _ = h.Write([]byte(messageID))
	return offerLines[h.Sum32()%uint32(len(offerLines))]
}

// glovesOffer is a waiting offer: the blocked message and when the offer
// ends.
type glovesOffer struct {
	m     Message
	until time.Time
}

// offerGloves posts a gloves-off offer for Joe's blocked message m and
// reacts 🥊 on it. It reports false, and posts nothing, when there is no
// offer to make: gloves off is not set up, m is not Joe's, the Replier
// cannot react, or too many offers wait.
func (b *Bridge) offerGloves(ctx context.Context, l *slog.Logger, m Message) bool {
	actor, ok := b.Replies.(Actor)
	if !ok || !b.glovesAvailable() || !b.fromOperator(m) {
		return false
	}
	b.mu.Lock()
	b.pruneOffers(b.now())
	full := len(b.offers) >= maxOffers
	b.mu.Unlock()
	if full {
		return false
	}
	until := b.now().Add(offerTTL)
	text := fmt.Sprintf("%s\n-# offer ends <t:%d:R>", offerLine(m.ID), until.Unix())
	id, err := actor.Post(ctx, m.ChannelID, NewReply(text, m.ID))
	if err != nil || id == "" {
		l.WarnContext(ctx, "gloves offer failed", "error", err)
		return false
	}
	b.mu.Lock()
	if b.offers == nil {
		b.offers = map[string]glovesOffer{}
	}
	b.offers[id] = glovesOffer{m: m, until: until}
	b.mu.Unlock()
	b.dropOfferAt(ctx, id, until)
	if err := actor.React(ctx, m.ChannelID, id, glovesEmoji); err != nil {
		actionFailed(ctx, l, "gloves offer react", err) // Joe can still add 🥊 himself.
	}
	l.InfoContext(ctx, "gloves offered", "offer", id, "until", until)
	return true
}

// pruneOffers drops ended offers. b.mu is held.
func (b *Bridge) pruneOffers(now time.Time) {
	for id, o := range b.offers {
		if !now.Before(o.until) {
			delete(b.offers, id)
		}
	}
}

// dropOfferAt forgets an offer, and with it the blocked text, when it ends.
func (b *Bridge) dropOfferAt(ctx context.Context, id string, until time.Time) {
	b.wg.Add(1)
	go func() {
		defer b.wg.Done()
		t := time.NewTimer(until.Sub(b.now()))
		defer t.Stop()
		select {
		case <-ctx.Done():
		case <-t.C:
		}
		b.mu.Lock()
		delete(b.offers, id)
		b.mu.Unlock()
	}()
}

// acceptOffer takes Joe's 🥊 on an offer: gloves off in the offer's channel
// for the default time, then the blocked message as a gloves-off turn. The
// caller checked that the reaction is Joe's 🥊.
func (b *Bridge) acceptOffer(ctx context.Context, r ReactionAdd) {
	now := b.now()
	b.mu.Lock()
	o, ok := b.offers[r.MessageID]
	if ok {
		delete(b.offers, r.MessageID) // An offer is used once.
	}
	b.mu.Unlock()
	if !ok || o.m.ChannelID != r.ChannelID || !now.Before(o.until) || !b.glovesAvailable() {
		return
	}
	notices, admitted := b.admitReplay(ctx)
	if !admitted {
		return
	}
	m := o.m
	b.mu.Lock()
	w := b.openWindow(ctx, m.ChannelID, now, 0)
	// The replay is a gloves-off turn in this window, whatever was decided
	// for m before (unsafe.go).
	if b.decided == nil {
		b.decided = map[string]unsafeDecision{}
	}
	b.decided[m.ID] = unsafeDecision{unsafe: true, window: w, at: now}
	b.mu.Unlock()
	b.Log.InfoContext(ctx, "gloves offer taken", "channel", m.ChannelID, "message", m.ID, "until", w.end)
	offer := Message{ID: r.MessageID, ChannelID: r.ChannelID, GuildID: m.GuildID}
	b.queueNoticeFiles(ctx, notices, offer, glovesOffText(w), []File{{Name: "gloves-off.gif", Data: glovesOffGIF}})
	b.queueTurn(ctx, notices, m, true)
}

// admitReplay returns the notice queue, or false while the bridge drains.
func (b *Bridge) admitReplay(ctx context.Context) (chan notice, bool) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.draining {
		return nil, false
	}
	b.start(ctx)
	return b.notices, true
}
