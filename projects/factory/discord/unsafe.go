package discord

import (
	"context"
	_ "embed"
	"encoding/json"
	"fmt"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
)

// Unsafe mode, shown to people as "gloves off" (HOME-938, Joe 2026-11-28):
// Joe alone turns a channel to a second agent with no tools, for plain chat
// that the normal model's filter refuses. The state is in memory only, so a
// restart ends it with no message: the fail-safe direction. Unsafe turns
// stay out of the normal session, its per-turn channel context, Hindsight
// and the archive:
//
//	Joe tags "take your gloves off [for 1h]" -> window[channel] = now .. now+d
//	Joe tags Bosun in window -> unsafe turn: no ingest, no archive row,
//	                            UnsafeConfig.Agent, reply + footer, no retain
//	"gloves on" or the timer -> normal turns; the timer posts the gloves-on GIF
//
// Each unsafe reply ends with the footer, and history readers drop a Bosun
// message with the footer and the message it answers (IsUnsafeReply).

// UnsafeConfig places unsafe mode; zero for none. Duration is the default
// window and MaxDuration caps one that Joe names.
type UnsafeConfig struct {
	Agent       kagent.AgentRef
	Duration    time.Duration
	MaxDuration time.Duration
}

func (u UnsafeConfig) enabled() bool { return u.Agent.Name != "" && u.Duration > 0 }

// Fixed gloves replies.
const (
	ReplyGlovesNotSetUp  = "My gloves stay on: gloves off is not set up."
	ReplyGlovesAlreadyOn = "My gloves are already on in this channel."
	ReplyGlovesOn        = "🧤 Gloves back on."
)

// The GIFs sent with gloves off and gloves on. To use other files, replace
// gloves/gloves-off.gif and gloves/gloves-on.gif (keep each under 1 MiB).
//
//go:embed gloves/gloves-off.gif
var glovesOffGIF []byte

//go:embed gloves/gloves-on.gif
var glovesOnGIF []byte

// unsafeMarker starts the footer of every unsafe mode reply.
const unsafeMarker = "-# 🥊 gloves off"

func unsafeFooter(end time.Time) string {
	return fmt.Sprintf("%s · gloves back on <t:%d:R>", unsafeMarker, end.Unix())
}

// withUnsafeFooter clips text so that the footer always fits in one reply.
func withUnsafeFooter(text string, end time.Time) string {
	footer := unsafeFooter(end)
	text = strings.TrimSpace(text)
	if text == "" {
		text = "(no answer)"
	}
	room := maxReplyChars - utf8.RuneCountInString(footer) - 2
	if runes := []rune(text); len(runes) > room {
		text = string(runes[:room-1]) + "…"
	}
	return text + "\n\n" + footer
}

// IsUnsafeReply reports whether text is an unsafe mode reply: its last
// line is the footer. The marker quoted inside a message does not count.
func IsUnsafeReply(text string) bool {
	text = strings.TrimRight(text, " \n")
	last := text[strings.LastIndex(text, "\n")+1:]
	return strings.HasPrefix(last, unsafeMarker)
}

// unsafeReplyRef returns the message that h, an unsafe mode reply by bot,
// answers: "" for its ID when it answers none. ok is false when h is not an
// unsafe mode reply by bot.
func unsafeReplyRef(h HistoryMessage, bot string) (answers string, ok bool) {
	if bot == "" || h.Author.ID != bot || !IsUnsafeReply(h.Text()) {
		return "", false
	}
	if h.Reference != nil {
		return h.Reference.MessageID, true
	}
	return "", true
}

type modeKind int

const (
	cmdNone modeKind = iota
	cmdOff           // gloves off: unsafe mode on
	cmdOn            // gloves on: unsafe mode off
)

// modeCommand is a parsed gloves command; For is 0 when Joe names no time.
type modeCommand struct {
	Kind modeKind
	For  time.Duration
}

// maxCommandWords bounds a command, so a longer message that mentions
// gloves goes to the model.
const maxCommandWords = 12

// commandWords are the only words a command may hold besides a duration:
// a question about gloves ("should I wear gloves on my ride?") is no
// command.
var commandWords = map[string]bool{
	"bosun": true, "gloves": true, "glove": true, "off": true, "on": true, "back": true,
	"take": true, "taking": true, "put": true, "your": true, "the": true, "them": true, "em": true,
	"pls": true, "plz": true, "please": true, "now": true, "ok": true, "okay": true, "hey": true,
	"yo": true, "mate": true, "time": true, "to": true, "go": true, "again": true, "and": true,
	"can": true, "could": true, "you": true, "lets": true, "let": true, "us": true, "for": true, "a": true, "an": true, "one": true, "two": true,
	"three": true, "four": true, "half": true, "hour": true, "hours": true, "hr": true, "hrs": true,
	"h": true, "min": true, "mins": true, "minute": true, "minutes": true, "m": true, "s": true,
}

var numberWords = map[string]float64{"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4}

// normalize lowers text, makes 🥊 the word gloves and drops every other
// character that is not a letter or digit.
func normalize(text string) []string {
	text = strings.ReplaceAll(strings.ToLower(text), "🥊", " gloves ")
	text = strings.ReplaceAll(text, "'", "")
	var b strings.Builder
	for _, r := range text {
		if unicode.IsLetter(r) || unicode.IsDigit(r) || r == '.' {
			b.WriteRune(r)
		} else {
			b.WriteRune(' ')
		}
	}
	var words []string
	for _, w := range strings.Fields(b.String()) {
		if w = strings.Trim(w, "."); w != "" {
			words = append(words, w)
		}
	}
	return words
}

var durationRE = regexp.MustCompile(`^(\d+(?:\.\d+)?)(h|hr|hrs|hour|hours|m|min|mins|minute|minutes)?$`)

// unitOf reads an hour or minute unit word.
func unitOf(w string) time.Duration {
	switch w {
	case "h", "hr", "hrs", "hour", "hours":
		return time.Hour
	case "m", "min", "mins", "minute", "minutes":
		return time.Minute
	}
	return 0
}

// parseDuration finds a duration in words: "1h", "90 min", "for an hour",
// "half an hour", "two hours". It reports ok false for a number with no
// unit, which is no command word.
func parseDuration(words []string) (d time.Duration, rest []string, ok bool) {
	ok = true
	for i := 0; i < len(words); i++ {
		w := words[i]
		if w == "half" && i+1 < len(words) {
			j := i + 1
			if words[j] == "an" || words[j] == "a" {
				j++
			}
			if j < len(words) && unitOf(words[j]) == time.Hour {
				d, i = 30*time.Minute, j
				continue
			}
		}
		if m := durationRE.FindStringSubmatch(w); m != nil {
			n, _ := strconv.ParseFloat(m[1], 64)
			unit := unitOf(m[2])
			if unit == 0 && i+1 < len(words) {
				unit = unitOf(words[i+1])
				if unit != 0 {
					i++
				}
			}
			if unit == 0 {
				ok = false // A bare number: not a command.
				continue
			}
			d = time.Duration(n * float64(unit))
			continue
		}
		if n, isNum := numberWords[w]; isNum && i+1 < len(words) && unitOf(words[i+1]) != 0 {
			d = time.Duration(n * float64(unitOf(words[i+1])))
			i++
			continue
		}
		rest = append(rest, w)
	}
	return d, rest, ok
}

// parseModeCommand reads a gloves command loosely and deterministically:
// "gloves" with "off" starts gloves off, "gloves" with "on" ends it, in any
// order, with an optional duration. Any case, punctuation and emoji; at
// most maxCommandWords words, all of them command words.
func parseModeCommand(text string) modeCommand {
	words := normalize(text)
	if len(words) == 0 || len(words) > maxCommandWords {
		return modeCommand{}
	}
	d, rest, ok := parseDuration(words)
	if !ok {
		return modeCommand{}
	}
	var gloves, off, on bool
	for _, w := range rest {
		if !commandWords[w] {
			return modeCommand{}
		}
		switch w {
		case "gloves", "glove":
			gloves = true
		case "off":
			off = true
		case "on":
			on = true
		}
	}
	switch {
	case !gloves || off == on:
		return modeCommand{}
	case off:
		return modeCommand{Kind: cmdOff, For: d}
	default:
		return modeCommand{Kind: cmdOn}
	}
}

// modeCommand reads Joe's gloves command: only a tag (or a reply to Bosun)
// of his in an allowlisted channel.
func (b *Bridge) modeCommand(m Message) modeCommand {
	if !b.fromOperator(m) || !b.Accept(m) {
		return modeCommand{}
	}
	return parseModeCommand(b.stripMention(m.Content))
}

// unsafeWindow is one channel's unsafe mode. start names its session, so
// each window starts a new one.
type unsafeWindow struct {
	start, end time.Time
}

type unsafeDecision struct {
	unsafe bool
	window unsafeWindow
	at     time.Time
}

// UnsafeTurn reports whether m is an unsafe turn: Joe tags Bosun, with no
// mode command, in a channel in unsafe mode. The first call decides and
// later calls return the same answer, so the archive (which sees a message
// first), the ingestor and the turn agree.
func (b *Bridge) UnsafeTurn(m Message) bool {
	if !b.Config.Unsafe.enabled() || b.Unsafe == nil || m.ID == "" {
		return false
	}
	b.mu.Lock()
	d, ok := b.decided[m.ID]
	b.mu.Unlock()
	if ok {
		return d.unsafe
	}
	if !b.fromOperator(m) || !b.Accept(m) || b.modeCommand(m).Kind != cmdNone {
		return false
	}
	now := b.now()
	b.mu.Lock()
	defer b.mu.Unlock()
	if d, ok := b.decided[m.ID]; ok {
		return d.unsafe
	}
	w, on := b.windows[m.ChannelID]
	d = unsafeDecision{unsafe: on && now.Before(w.end), window: w, at: now}
	if b.decided == nil {
		b.decided = map[string]unsafeDecision{}
	}
	for id, old := range b.decided {
		if now.Sub(old.at) > dedupeWindow {
			delete(b.decided, id)
		}
	}
	b.decided[m.ID] = d
	return d.unsafe
}

// UnsafeMessage reports whether message id was decided an unsafe turn. It
// never decides.
func (b *Bridge) UnsafeMessage(id string) bool {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.decided[id].unsafe
}

func (b *Bridge) unsafeWindowOf(id string) (unsafeWindow, bool) {
	b.mu.Lock()
	defer b.mu.Unlock()
	d, ok := b.decided[id]
	return d.window, ok && d.unsafe
}

// noticeFor is a fixed reply to m, with the unsafe footer when m is an
// unsafe turn, so history readers drop m.
func (b *Bridge) noticeFor(m Message, text string) string {
	if w, ok := b.unsafeWindowOf(m.ID); ok {
		return withUnsafeFooter(text, w.end)
	}
	return text
}

// glovesAvailable reports whether gloves off is set up.
func (b *Bridge) glovesAvailable() bool { return b.Config.Unsafe.enabled() && b.Unsafe != nil }

func glovesOffText(w unsafeWindow) string {
	return fmt.Sprintf("🥊 Gloves off until <t:%d:R>: plain chat, no tools and no memory. "+
		"Say \"Bosun gloves on\" to end it.", w.end.Unix())
}

// openWindow starts or extends a channel's gloves off for asked (0 for the
// default) and arms its timer. b.mu is held.
func (b *Bridge) openWindow(ctx context.Context, channelID string, now time.Time, asked time.Duration) unsafeWindow {
	w, on := b.windows[channelID]
	if !on || !now.Before(w.end) {
		w.start = now
	}
	w.end = now.Add(b.Config.Unsafe.windowFor(asked))
	if b.windows == nil {
		b.windows = map[string]unsafeWindow{}
	}
	b.windows[channelID] = w
	b.expireAt(ctx, channelID, w.end)
	return w
}

// windowFor is the window Joe asked for: the default, or his time capped
// at MaxDuration and at least a minute.
func (u UnsafeConfig) windowFor(asked time.Duration) time.Duration {
	if asked <= 0 {
		return u.Duration
	}
	if limit := u.MaxDuration; limit > 0 && asked > limit {
		return limit
	}
	return max(asked, time.Minute)
}

// setMode runs Joe's gloves command in m's channel.
func (b *Bridge) setMode(ctx context.Context, notices chan notice, m Message, cmd modeCommand) {
	l := b.Log.With("guild", m.GuildID, "channel", m.ChannelID, "message", m.ID)
	now := b.now()
	b.mu.Lock()
	w, on := b.windows[m.ChannelID]
	on = on && now.Before(w.end)
	var text string
	var gif []File
	switch {
	case cmd.Kind == cmdOff && !b.glovesAvailable():
		text = ReplyGlovesNotSetUp
	case cmd.Kind == cmdOff:
		w = b.openWindow(ctx, m.ChannelID, now, cmd.For)
		text, gif = glovesOffText(w), []File{{Name: "gloves-off.gif", Data: glovesOffGIF}}
	case on:
		delete(b.windows, m.ChannelID)
		text, gif = ReplyGlovesOn, []File{{Name: "gloves-on.gif", Data: glovesOnGIF}}
	default:
		text = ReplyGlovesAlreadyOn
	}
	b.mu.Unlock()
	l.InfoContext(ctx, "gloves command", "off", cmd.Kind == cmdOff, "until", w.end, "reply", text)
	b.queueNoticeFiles(ctx, notices, m, text, gif)
}

// expireAt ends a channel's window at end, if it is still that window, and
// posts the gloves-on GIF there. b.mu is held.
func (b *Bridge) expireAt(ctx context.Context, channelID string, end time.Time) {
	b.wg.Add(1)
	go func() {
		defer b.wg.Done()
		t := time.NewTimer(end.Sub(b.now()))
		defer t.Stop()
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
		b.mu.Lock()
		w, ok := b.windows[channelID]
		current := ok && w.end.Equal(end)
		if current {
			delete(b.windows, channelID)
		}
		b.mu.Unlock()
		if !current {
			return // Ended or extended since.
		}
		b.Log.InfoContext(ctx, "gloves back on: time is up", "channel", channelID)
		reply := WithImages(NewReply(ReplyGlovesOn, ""), ReplyGlovesOn, []File{{Name: "gloves-on.gif", Data: glovesOnGIF}})
		if err := b.Replies.Send(ctx, channelID, reply); err != nil {
			b.Log.WarnContext(ctx, "gloves-on post failed", "channel", channelID, "error", err)
		}
	}()
}

// UnsafeRequestID names an unsafe mode session: one per channel window.
func UnsafeRequestID(key string, generation int) string {
	if generation == 0 {
		return "discord-unsafe-" + key
	}
	return fmt.Sprintf("discord-unsafe-%s-%d", key, generation)
}

// unsafeTurn answers an unsafe turn on the unsafe agent. Nothing is
// recalled, retained or ingested, and the reply does no actions.
func (b *Bridge) unsafeTurn(ctx context.Context, m Message, w unsafeWindow) {
	l := b.Log.With("guild", m.GuildID, "channel", m.ChannelID, "message", m.ID, "mode", "unsafe")
	text := b.Places.Resolve(m.GuildID, b.stripMention(m.Content), m.Mentions)
	key := m.ChannelID + "-" + strconv.FormatInt(w.start.Unix(), 10)
	attempt, until := b.takeAttempt(m.ID)
	answer, err := b.Unsafe.Turn(ctx, key, attemptID(m.ID, attempt), UnsafePrompt(m, text))
	if CreditsExhausted(err) {
		b.noCredits(ctx, l, m, w, attempt, until) // credits.go
		return
	}
	if err != nil {
		l.WarnContext(ctx, "turn failed", "error", err)
		answer = ReplyFailed
	}
	b.reply(ctx, m, withUnsafeFooter(answer, w.end))
}

// UnsafePrompt is an unsafe turn's message: Joe's text only, with no
// memories, channel messages or tools.
func UnsafePrompt(m Message, text string) string {
	data, _ := json.Marshal(map[string]any{
		"author":  m.Author.Username,
		"sent_at": m.Timestamp.UTC().Format(time.RFC3339),
		"message": text,
	})
	return "Joe tagged you in Discord. The JSON below is his message. Answer it.\n\n" + string(data)
}
