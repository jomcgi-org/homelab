package discord

import (
	"strings"
	"time"
	"unicode"
)

// drainPoll is how often Drain checks for in-flight work.
const drainPoll = 50 * time.Millisecond

// track adds n to the in-flight count: queued and running turns, queued
// notices and explorer turns.
func (b *Bridge) track(n int) {
	b.mu.Lock()
	b.active += n
	b.mu.Unlock()
}

// Drain stops the bridge taking new turns, then waits up to grace for the
// in-flight turns, notices and explorer turns to end. It reports whether
// they all ended. The caller then cancels the turns' context and calls
// Wait. A message that arrives during the drain gets no turn here; the
// catch-up of the next bridge answers it (catchup.go).
func (b *Bridge) Drain(grace time.Duration) bool {
	b.mu.Lock()
	b.draining = true
	active := b.active
	b.mu.Unlock()
	b.Log.Info("drain started", "in_flight", active, "grace", grace.String())
	deadline := time.Now().Add(grace)
	tick := time.NewTicker(drainPoll)
	defer tick.Stop()
	for {
		b.mu.Lock()
		active = b.active
		b.mu.Unlock()
		if active <= 0 {
			b.Log.Info("drain done")
			return true
		}
		if !time.Now().Before(deadline) {
			b.Log.Warn("drain timed out; in-flight turns stop", "in_flight", active)
			return false
		}
		<-tick.C
	}
}

// Draining reports whether Drain has started.
func (b *Bridge) Draining() bool {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.draining
}

// hasWord reports whether s holds a word character, which a Hindsight
// recall query needs (it answers HTTP 422 otherwise).
func hasWord(s string) bool {
	return strings.IndexFunc(s, func(r rune) bool {
		return r == '_' || unicode.IsLetter(r) || unicode.IsDigit(r)
	}) >= 0
}

// recallQuery is a turn's memory query: the message's text, or for a
// message that only tags Bosun, the text it replies to, else the channel's
// newest line with words. Empty means no recall: the turn still answers,
// from the channel's context.
func recallQuery(text string, m Message, since []ChannelLine) string {
	if hasWord(text) {
		return text
	}
	if r := m.Referenced; r != nil && hasWord(r.Content) {
		return clipRunes(r.Content, maxReplyToChars)
	}
	for i := len(since) - 1; i >= 0; i-- {
		if hasWord(since[i].Text) {
			return since[i].Text
		}
	}
	return ""
}
