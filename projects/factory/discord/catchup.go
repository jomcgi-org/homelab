package discord

import (
	"cmp"
	"context"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"time"
)

// Bounds on the catch-up's Discord reads.
const (
	// CatchUpWindow is how far back the catch-up looks by default.
	CatchUpWindow = 15 * time.Minute
	// maxCatchUpChannels bounds the channels read per server: those with
	// the newest messages.
	maxCatchUpChannels = 20
	// maxCatchUpPages bounds the pages of 100 messages read per channel.
	maxCatchUpPages = 3
)

// CatchUpSource reads a server's channels and their recent messages.
type CatchUpSource interface {
	GuildChannels(ctx context.Context, guildID string) ([]Channel, error)
	ActiveThreads(ctx context.Context, guildID string) ([]Channel, error)
	MessagesAfter(ctx context.Context, channelID, after string) ([]HistoryMessage, error)
}

// CatchUp answers Joe's tags that a restart left unanswered. It runs once,
// after the gateway is ready. For each allowlisted server, it reads the
// channels and active threads whose newest message is inside window, and
// their messages since then. A message from Joe that tags Bosun or replies
// to him starts a turn when no later message of Bosun's replies to it, Bosun
// did not react to it and no thread starts from it. The turn enters as a gateway message does
// (answer), so the dedupe and the limits apply and a message never gets two
// turns. The operator's channels are left out. It returns the turns it
// queued.
func (b *Bridge) CatchUp(ctx context.Context, src CatchUpSource, window time.Duration) int {
	bot := b.bot()
	if bot == "" || b.Config.Operator.UserID == "" {
		b.Log.InfoContext(ctx, "catch-up skipped: no bot or operator user")
		return 0
	}
	cutoff := b.now().Add(-window)
	guilds := make([]string, 0, len(b.Config.Guilds))
	for id := range b.Config.Guilds {
		guilds = append(guilds, id)
	}
	slices.Sort(guilds)
	queued, read := 0, 0
	for _, gid := range guilds {
		if ctx.Err() != nil || b.Draining() {
			break
		}
		channels := b.catchUpChannels(ctx, src, gid, cutoff)
		read += len(channels)
		for _, c := range channels {
			queued += b.catchUpChannel(ctx, src, gid, c, cutoff, bot)
		}
	}
	b.Log.InfoContext(ctx, "catch-up done", "window", window.String(), "channels", read, "turns", queued)
	return queued
}

// catchUpChannels lists a server's channels where the bridge answers and
// whose newest message is at or after cutoff, newest first.
func (b *Bridge) catchUpChannels(ctx context.Context, src CatchUpSource, gid string, cutoff time.Time) []Channel {
	channels, err := src.GuildChannels(ctx, gid)
	if err != nil {
		b.Log.WarnContext(ctx, "catch-up: channels unavailable", "guild", gid, "error", err)
		return nil
	}
	threads, err := src.ActiveThreads(ctx, gid)
	if err != nil {
		b.Log.WarnContext(ctx, "catch-up: threads unavailable", "guild", gid, "error", err)
	}
	g, o := b.Config.Guilds[gid], b.Config.Operator
	var out []Channel
	for _, c := range append(channels, threads...) {
		switch {
		case !historyChannelTypes[c.Type], snowflakeTime(c.LastMessageID).Before(cutoff):
		case len(g.Channels) > 0 && !contains(g.Channels, c.ID):
		case o.enabled() && gid == o.Guild && (contains(o.Channels, c.ID) || contains(o.Channels, c.ParentID)):
		default:
			out = append(out, c)
		}
	}
	slices.SortFunc(out, func(a, c Channel) int { return cmp.Compare(snowflakeID(c.LastMessageID), snowflakeID(a.LastMessageID)) })
	return out[:min(len(out), maxCatchUpChannels)]
}

// catchUpChannel queues the unanswered tags of one channel since cutoff.
func (b *Bridge) catchUpChannel(ctx context.Context, src CatchUpSource, gid string, c Channel, cutoff time.Time, bot string) int {
	var msgs []HistoryMessage
	after := Snowflake(cutoff)
	for range maxCatchUpPages {
		page, err := src.MessagesAfter(ctx, c.ID, after)
		if err != nil {
			b.Log.WarnContext(ctx, "catch-up: messages unavailable", "guild", gid, "channel", c.ID, "error", err)
			return 0 // A partial read could miss Bosun's reply.
		}
		msgs = append(msgs, page...)
		for _, h := range page {
			if snowflakeID(h.ID) > snowflakeID(after) {
				after = h.ID
			}
		}
		if len(page) < 100 {
			break
		}
	}
	slices.SortFunc(msgs, func(a, c HistoryMessage) int { return cmp.Compare(snowflakeID(a.ID), snowflakeID(c.ID)) })
	gloves := b.glovesDuring(ctx, c.ID, cutoff, msgs, bot)
	answered := map[string]bool{}
	for _, h := range msgs {
		if h.Author.ID == bot && h.Reference != nil && h.Reference.MessageID != "" {
			answered[h.Reference.MessageID] = true
		}
		for _, r := range h.Reactions {
			if r.Me {
				answered[h.ID] = true // An answer of only a reaction.
			}
		}
	}
	queued := 0
	for _, h := range msgs {
		m := h.Message
		m.GuildID = gid // Discord's REST messages leave it out.
		if m.ChannelID == "" {
			m.ChannelID = c.ID
		}
		if SentAt(m).Before(cutoff) || answered[m.ID] || h.Thread != nil || !b.fromOperator(m) || !b.Accept(m) {
			continue
		}
		if b.modeCommand(m).Kind != cmdNone {
			continue // A restart puts the gloves back on; a missed command stays missed (HOME-938).
		}
		if gloves[m.ID] {
			// Sent in gloves off: never a normal turn (HOME-938). It gets
			// no answer; Joe can send it again.
			b.Log.InfoContext(ctx, "catch-up: gloves-off message left unanswered", "guild", gid, "channel", m.ChannelID, "message", m.ID)
			continue
		}
		if b.answer(ctx, m) {
			b.Log.InfoContext(ctx, "catch-up turn", "guild", gid, "channel", m.ChannelID, "message", m.ID)
			queued++
		}
	}
	return queued
}

// snowflakeID is a snowflake as a number; an unreadable ID is 0.
func snowflakeID(id string) uint64 {
	n, _ := strconv.ParseUint(id, 10, 64)
	return n
}

// glovesDuring returns Joe's messages in msgs (oldest first, since cutoff)
// sent while Bosun's notices say gloves off was on (HOME-938): after a
// "🥊 Gloves off until <t:X:R>" notice and before X or a "Gloves back on".
// The page before cutoff is read too, for a notice older than the window.
// A message the bridge cannot place stays a normal one.
func (b *Bridge) glovesDuring(ctx context.Context, channelID string, cutoff time.Time, msgs []HistoryMessage, bot string) map[string]bool {
	var older []HistoryMessage
	if b.History != nil && b.Config.Unsafe.enabled() {
		page, err := b.History.MessagesBefore(ctx, channelID, Snowflake(cutoff))
		if err != nil {
			b.Log.WarnContext(ctx, "catch-up: history before the window unavailable", "channel", channelID, "error", err)
		}
		older = slices.Clone(page)
		slices.SortFunc(older, func(a, c HistoryMessage) int { return cmp.Compare(snowflakeID(a.ID), snowflakeID(c.ID)) })
	}
	var until time.Time
	out := map[string]bool{}
	for _, h := range append(older, msgs...) {
		at := SentAt(h.Message)
		if h.Author.ID == bot {
			text := h.Text()
			if end, ok := glovesOffUntil(text); ok {
				until = end
			} else if strings.HasPrefix(text, ReplyGlovesOn) {
				until = time.Time{}
			}
			continue
		}
		if at.Before(until) {
			out[h.ID] = true
		}
	}
	return out
}

var glovesOffNotice = regexp.MustCompile(`^🥊 Gloves off until <t:(\d+):R>`)

// glovesOffUntil reads the end time of a gloves-off notice.
func glovesOffUntil(text string) (time.Time, bool) {
	m := glovesOffNotice.FindStringSubmatch(text)
	if m == nil {
		return time.Time{}, false
	}
	n, err := strconv.ParseInt(m[1], 10, 64)
	if err != nil {
		return time.Time{}, false
	}
	return time.Unix(n, 0), true
}
