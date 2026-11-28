package discord

import (
	"context"
	"log/slog"
	"strings"
	"time"
	"unicode"
)

// Remember (HOME-919): when Joe tags Bosun with "remember X", the bridge
// stores X in the server's bank as an explicit fact. The model gets no
// retain tool and no write credential; only the bridge writes, and only for
// the operator (Joe's decision,
// https://factory-plan.jomcgi.dev/factory/issue/HOME-919#comment-12933).

// RememberedNote and NotRememberedNote are the trusted lines put before the
// prompt of a remember turn. Neither carries Discord text.
const (
	RememberedNote = "The bridge saved the fact in Joe's message below to this server's " +
		"memory, because Joe asked you to remember it. Confirm briefly that you will remember it."
	NotRememberedNote = "Joe asked you to remember the fact in his message below, but the " +
		"bridge could not save it to this server's memory. Tell him briefly that it was not saved."
)

// RememberFact returns the fact of a request to remember, with the bot
// mention already stripped: "remember X", "remember that X", "remember: X",
// with an optional "please" first, in any case. A question is not a fact.
func RememberFact(text string) (string, bool) {
	s := strings.TrimSpace(text)
	if p, ok := cutWord(s, "please"); ok {
		s = strings.TrimLeft(p, " ,")
	}
	s, ok := cutWord(s, "remember")
	if !ok {
		return "", false
	}
	s = strings.TrimLeft(s, " :,-")
	if rest, ok := cutWord(s, "that"); ok {
		s = strings.TrimLeft(rest, " :,")
	}
	s = strings.TrimSpace(s)
	if s == "" || strings.HasSuffix(s, "?") {
		return "", false
	}
	return s, true
}

// cutWord removes word from the start of s, in any case, when a non-letter
// or the end follows it.
func cutWord(s, word string) (string, bool) {
	if len(s) < len(word) || !strings.EqualFold(s[:len(word)], word) {
		return s, false
	}
	rest := s[len(word):]
	if rest != "" {
		r := []rune(rest)[0]
		if unicode.IsLetter(r) || unicode.IsDigit(r) {
			return s, false
		}
	}
	return rest, true
}

// RememberedContent is the stored line of a remembered fact.
func RememberedContent(channelID string, at time.Time, fact string) string {
	return "[kind=remembered] Joe asked Bosun to remember (channel " + channelID + ", " +
		at.UTC().Format(time.RFC3339) + "): " + fact
}

// remember stores the fact of an operator's request to remember and returns
// the trusted note for the turn's prompt, or "" when m is not one. Only
// fromOperator triggers it, whatever Config.OperatorOnly says. A failed
// store is logged; the turn goes on.
func (b *Bridge) remember(ctx context.Context, l *slog.Logger, m Message, text string) string {
	if !b.fromOperator(m) {
		return ""
	}
	fact, ok := RememberFact(text)
	if !ok {
		return ""
	}
	at := m.Timestamp
	if at.IsZero() {
		at = b.now()
	}
	content := RememberedContent(m.ChannelID, at, fact)
	var err error
	if f, ok := b.Memory.(FactMemory); ok {
		err = f.RetainFact(ctx, m.GuildID, m.ChannelID, m.ID, at, content)
	} else {
		err = b.Memory.Retain(ctx, m.GuildID, m.ChannelID, at, content)
	}
	if err != nil {
		l.WarnContext(ctx, "remember failed", "error", err)
		return NotRememberedNote
	}
	l.InfoContext(ctx, "remembered a fact")
	return RememberedNote
}
