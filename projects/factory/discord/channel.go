package discord

import (
	"context"
	"slices"
	"strings"
	"time"
)

// Bounds on the channel messages a turn reads from Discord.
const (
	// maxSinceLines and maxSinceChars bound the lines a prompt carries;
	// the newest are kept.
	maxSinceLines = 50
	maxSinceChars = 8000
	// maxFirstLines bounds the lines when no message of Bosun's is in the
	// page read: a new channel, or a quiet bot.
	maxFirstLines = 30
	// maxLineChars bounds one line's text, and maxQuoteChars the text a
	// line quotes from the message it replies to.
	maxLineChars  = 1500
	maxQuoteChars = 200
)

// ChannelLine is one message of the channel since Bosun's last message
// there. Every field is text from Discord users, so it is untrusted data.
type ChannelLine struct {
	Author  string     `json:"author,omitempty"`
	SentAt  string     `json:"sent_at,omitempty"`
	Text    string     `json:"text"`
	ReplyTo *LineReply `json:"reply_to,omitempty"`
}

// LineReply is the message a channel line replies to.
type LineReply struct {
	Author string `json:"author"`
	Text   string `json:"text,omitempty"`
}

// HistoryEmbed is the subset of a message embed the bridge reads.
type HistoryEmbed struct {
	Title string `json:"title"`
}

// HistorySticker is the subset of a message sticker the bridge reads.
type HistorySticker struct {
	Name string `json:"name"`
}

// sinceLastTurn reads the channel's messages from Bosun's last message there
// to m, oldest first. It reads one page (100 messages) before m, so a
// restarted bridge or a retained ingest buffer loses nothing: the boundary
// comes from Discord, not from bridge state. Without a message of Bosun's
// in the page, it keeps the newest maxFirstLines. ok is false when the
// read failed.
func (b *Bridge) sinceLastTurn(ctx context.Context, m Message) (lines []ChannelLine, ok bool) {
	msgs, err := b.History.MessagesBefore(ctx, m.ChannelID, m.ID)
	if err != nil {
		b.Log.WarnContext(ctx, "channel history unavailable", "channel", m.ChannelID, "error", err)
		return nil, false
	}
	b.mu.Lock()
	bot := b.botID
	b.mu.Unlock()
	limit, found := maxSinceLines, false
	var keep []HistoryMessage
	for _, h := range msgs { // Newest first.
		if h.ChannelID != "" && h.ChannelID != m.ChannelID {
			continue
		}
		if bot != "" && h.Author.ID == bot {
			found = true
			break
		}
		keep = append(keep, h)
	}
	if !found {
		limit = maxFirstLines
	}
	chars := 0
	for _, h := range keep {
		if len(lines) == limit {
			break
		}
		l, ok := b.channelLine(m.GuildID, h)
		if !ok {
			continue
		}
		n := len(l.Text)
		if l.ReplyTo != nil {
			n += len(l.ReplyTo.Text)
		}
		if chars+n > maxSinceChars {
			break
		}
		chars += n
		lines = append(lines, l)
	}
	slices.Reverse(lines)
	return lines, true
}

// channelLine is one history message as a prompt line. Other bots, webhooks
// and system messages are left out, as the ingestor leaves them out.
func (b *Bridge) channelLine(guildID string, h HistoryMessage) (ChannelLine, bool) {
	if h.Author.Bot || h.WebhookID != "" || h.Author.ID == "" || !conversationTypes[h.Type] {
		return ChannelLine{}, false
	}
	b.Places.Learn(guildID, h.Message.Users()...)
	text := clipRunes(b.Places.Resolve(guildID, h.Text(), h.Mentions), maxLineChars)
	if p := placeholders(h); p != "" {
		text = strings.TrimSpace(text + " " + p)
	}
	if text == "" {
		return ChannelLine{}, false
	}
	l := ChannelLine{Author: h.Author.Username, SentAt: SentAt(h.Message).UTC().Format(time.RFC3339), Text: text}
	if r := h.Referenced; h.Type == 19 && r != nil {
		l.ReplyTo = &LineReply{Author: r.Author.Username, Text: clipRunes(b.Places.Resolve(guildID, r.Content, r.Mentions), maxQuoteChars)}
		if r.Author.Username == "" {
			l.ReplyTo.Author = "unknown"
		}
	}
	return l, true
}

// placeholders names a message's attachments, embeds and stickers briefly,
// with no URL to fetch.
func placeholders(h HistoryMessage) string {
	var parts []string
	for _, a := range h.Attachments {
		parts = append(parts, "[attachment: "+clipRunes(a.Filename, 80)+"]")
	}
	for _, e := range h.Embeds {
		if e.Title != "" {
			parts = append(parts, "[embed: "+clipRunes(e.Title, 80)+"]")
		} else {
			parts = append(parts, "[embed]")
		}
	}
	for _, s := range h.Stickers {
		parts = append(parts, "[sticker: "+clipRunes(s.Name, 40)+"]")
	}
	return strings.Join(parts, " ")
}
