package discord

import (
	"context"
	"slices"
	"strconv"
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
	// maxSeedLines bounds the lines of a new session's first turn
	// (HOME-944): the channel's last messages, Bosun's own included, past
	// Bosun's last reply. maxSinceChars bounds them too.
	maxSeedLines = 20
	// maxLineChars bounds one line's text, and maxQuoteChars the text a
	// line quotes from the message it replies to.
	maxLineChars  = 1500
	maxQuoteChars = 200
)

// ChannelLine is one message of the channel since Bosun's last message
// there. Every field is text from Discord users, so it is untrusted data.
type ChannelLine struct {
	// ID labels the line for an action's target: "m1" is the oldest line.
	// MessageID is its Discord ID, which the prompt does not carry.
	ID        string     `json:"id,omitempty"`
	MessageID string     `json:"-"`
	Author    string     `json:"author,omitempty"`
	SentAt    string     `json:"sent_at,omitempty"`
	Text      string     `json:"text"`
	ReplyTo   *LineReply `json:"reply_to,omitempty"`
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
//
// With seed, for a new session's first turn (HOME-944), Bosun's messages
// do not end the lines: it keeps the newest maxSeedLines, Bosun's own as
// "Bosun (you)" lines, so the session gets the channel's recent thread. It
// stops at Bosun's newest ReplyBlocked message or gloves-off offer.
func (b *Bridge) sinceLastTurn(ctx context.Context, m Message, seed bool) (lines []ChannelLine, ok bool) {
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
	var last HistoryMessage
	// Unsafe mode turns (HOME-938) are not this agent's: drop each unsafe
	// reply and the message it answers. A reply is newer than its message.
	unsafe := map[string]bool{}
	for _, h := range msgs { // Newest first.
		if h.ChannelID != "" && h.ChannelID != m.ChannelID {
			continue
		}
		if ref, ok := unsafeReplyRef(h, bot); ok {
			unsafe[ref] = true // Its message is never a line or a quote.
			if seed && isOffer(h.Text()) {
				// A gloves-off offer answers a refused message (offer.go):
				// a seed stops there, as at ReplyBlocked.
				break
			}
			continue
		}
		if ref := blockedReplyRef(h, bot); ref != "" {
			unsafe[ref] = true // Refused: a later reply may not quote it.
		}
		if unsafe[h.ID] || b.UnsafeMessage(h.ID) {
			continue
		}
		if bot != "" && h.Author.ID == bot && (!seed || h.Text() == ReplyBlocked) {
			if seed {
				// A seed reads no further back than a blocked turn: what
				// came before it may hold what the filter refused, and the
				// in-memory blocked set is gone after a restart (HOME-944).
				break
			}
			found, last = true, h
			break
		}
		keep = append(keep, h)
	}
	switch {
	case seed:
		limit = maxSeedLines
	case !found:
		limit = maxFirstLines
	}
	chars, complete := 0, true
	for _, h := range keep {
		if len(lines) == limit {
			complete = false
			break
		}
		var l ChannelLine
		if bot != "" && h.Author.ID == bot {
			l, ok = b.ownLine(m.GuildID, h)
		} else {
			l, ok = b.channelLine(m.GuildID, h, unsafe)
		}
		if !ok {
			continue
		}
		n := len(l.Text)
		if l.ReplyTo != nil {
			n += len(l.ReplyTo.Text)
		}
		if chars+n > maxSinceChars {
			complete = false
			break
		}
		chars += n
		lines = append(lines, l)
	}
	if found && complete && len(lines) < limit {
		// Bosun's last message starts the lines, so Bosun sees what it
		// posted: its text and images, not an empty message.
		if l, ok := b.ownLine(m.GuildID, last); ok && chars+len(l.Text) <= maxSinceChars {
			lines = append(lines, l)
		}
	}
	slices.Reverse(lines)
	for n := range lines {
		lines[n].ID = "m" + strconv.Itoa(n+1)
	}
	return lines, true
}

// turnTargets labels the messages of a turn's context that an action can
// target: the tagged message, the message it replies to, and each channel
// line read from Discord.
func turnTargets(m Message, since []ChannelLine) Targets {
	t := Targets{TargetTagged: m.ID}
	if r := m.Referenced; m.Type == 19 && r != nil && r.ID != "" {
		t[TargetReplied] = r.ID
	}
	for _, l := range since {
		if l.ID != "" && l.MessageID != "" {
			t[l.ID] = l.MessageID
		}
	}
	return t
}

// channelLine is one history message as a prompt line. Other bots, webhooks
// and system messages are left out, as the ingestor leaves them out.
// refused holds messages found refused or gloves-off while reading the
// history; a line never quotes one.
func (b *Bridge) channelLine(guildID string, h HistoryMessage, refused map[string]bool) (ChannelLine, bool) {
	if h.Author.Bot || h.WebhookID != "" || h.Author.ID == "" || !conversationTypes[h.Type] || b.blockedID(h.ID) {
		return ChannelLine{}, false // A blocked message never reaches a prompt.
	}
	b.Places.Learn(guildID, h.Message.Users()...)
	text := b.lineText(guildID, h, maxLineChars)
	if text == "" {
		return ChannelLine{}, false
	}
	l := ChannelLine{MessageID: h.ID, Author: h.Author.Username, SentAt: SentAt(h.Message).UTC().Format(time.RFC3339), Text: text}
	if r := h.Referenced; h.Type == 19 && r != nil && b.quotable(r, refused) {
		l.ReplyTo = &LineReply{Author: b.authorName(r.Author), Text: clipRunes(b.Places.Resolve(guildID, r.Text(), r.Mentions), maxQuoteChars)}
		if r.Author.Username == "" {
			l.ReplyTo.Author = "unknown"
		}
	}
	return l, true
}

// quotable reports whether a line may quote the message r it replies to:
// not a refused message, a gloves-off turn, or Bosun's gloves-off offer or
// reply (HOME-945). A reply to the offer, such as Joe's "lol", keeps its
// own text and loses only the quote.
func (b *Bridge) quotable(r *Referenced, refused map[string]bool) bool {
	if refused[r.ID] || b.blockedID(r.ID) || b.UnsafeMessage(r.ID) {
		return false
	}
	bot := b.bot()
	return bot == "" || r.Author.ID != bot || !IsUnsafeReply(r.Text())
}

// blockedReplyRef returns the message that h, Bosun's plain ReplyBlocked,
// answers, or "".
func blockedReplyRef(h HistoryMessage, bot string) string {
	if bot == "" || h.Author.ID != bot || h.Reference == nil || strings.TrimSpace(h.Text()) != ReplyBlocked {
		return ""
	}
	return h.Reference.MessageID
}

// ownLine is Bosun's own message as a prompt line, by "Bosun (you)".
func (b *Bridge) ownLine(guildID string, h HistoryMessage) (ChannelLine, bool) {
	text := b.lineText(guildID, h, maxOwnChars)
	if text == "" {
		return ChannelLine{}, false
	}
	return ChannelLine{Author: OwnAuthor, SentAt: SentAt(h.Message).UTC().Format(time.RFC3339), Text: text}, true
}

// OwnAuthor names Bosun's own messages in a prompt.
const OwnAuthor = "Bosun (you)"

// maxOwnChars bounds Bosun's own message in the lines: the session holds
// the full answer.
const maxOwnChars = 400

// authorName is how a prompt names a message's author: OwnAuthor for Bosun.
func (b *Bridge) authorName(u User) string {
	if bot := b.bot(); bot != "" && u.ID == bot {
		return OwnAuthor
	}
	return u.Username
}

// lineText is a history message's text with its mentions named and its
// media as placeholders, at most n runes of text.
func (b *Bridge) lineText(guildID string, h HistoryMessage, n int) string {
	shown := map[string]bool{}
	text := strings.TrimSpace(h.Content)
	if text == "" {
		text = componentsText(h.Components, shown)
	}
	text = clipRunes(b.Places.Resolve(guildID, text, h.Mentions), n)
	if p := placeholders(h, shown); p != "" {
		text = strings.TrimSpace(text + " " + p)
	}
	return text
}

// placeholders names a message's attachments that no component shows,
// embeds and stickers briefly, with no URL to fetch.
func placeholders(h HistoryMessage, shown map[string]bool) string {
	var parts []string
	if a := attachmentNames(h.Attachments, shown); a != "" {
		parts = append(parts, a)
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
