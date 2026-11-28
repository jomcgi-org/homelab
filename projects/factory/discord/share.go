package discord

import (
	"context"
	"fmt"
	"log/slog"
	"strconv"
	"strings"
	"time"

	"github.com/jomcgi/homelab/projects/factory/discord/artifacts"
)

// Share links for artifacts (HOME-917). Bosun only asks: a share block
// names a file and a lifetime. The bridge posts a confirmation, and only
// Joe's ✅ on that exact message, within shareConfirmWindow, makes the link.
// The bridge signs it with its own read-only key; the agent holds none.
//
//	share block -> confirmation message (bot adds ✅)
//	  Joe's ✅ within 10 min -> sign, post the link, forget the request
//	  any other reaction, user or a late ✅ -> nothing

// IntentGuildMessageReactions sends reaction events, for the ✅ that confirms
// a share link. It is not privileged.
const IntentGuildMessageReactions = 1 << 10

// shareConfirmWindow is how long a confirmation waits for Joe's ✅.
const shareConfirmWindow = 10 * time.Minute

// shareConfirm is the reaction that confirms a share.
const shareConfirm = "✅"

// maxPendingShares bounds the confirmations kept at once.
const maxPendingShares = 20

// ShareRequest is a share block: a file of the current channel and how long
// its link works.
type ShareRequest struct {
	Path string
	TTL  time.Duration
}

// ShareSigner signs a link for a bucket key (artifacts.Sharer).
type ShareSigner interface {
	Link(key string, ttl time.Duration) (string, time.Time, error)
}

// Sharing is the bridge's share setup; a nil Bridge.Share turns share blocks
// into a refusal line.
type Sharing struct {
	Signer  ShareSigner
	BaseURL string // https://artifact.jomcgi.dev
}

// pendingShare is a confirmation that waits for Joe's ✅.
type pendingShare struct {
	channelID string
	key       string
	ttl       time.Duration
	until     time.Time
}

// ParseShare reads a share block: {"path": "report.html", "ttl": "24h"}. The
// path is a file of the current channel, its artifact URL or its key; ttl is
// a Go duration or a number of days ("3d"), 1 minute to 7 days, 24 hours by
// default.
func ParseShare(spec string) (ShareRequest, error) {
	var s struct {
		Path string `json:"path"`
		TTL  string `json:"ttl"`
	}
	if err := decodeStrict(spec, &s); err != nil {
		return ShareRequest{}, err
	}
	r := ShareRequest{Path: strings.TrimSpace(s.Path), TTL: artifacts.DefaultShareTTL}
	if r.Path == "" || len(r.Path) > 400 || strings.ContainsAny(r.Path, " \t\r\n`") {
		return ShareRequest{}, errAction
	}
	if s.TTL != "" {
		ttl, err := parseTTL(s.TTL)
		if err != nil || ttl < artifacts.MinShareTTL || ttl > artifacts.MaxShareTTL {
			return ShareRequest{}, errAction
		}
		r.TTL = ttl
	}
	return r, nil
}

func parseTTL(s string) (time.Duration, error) {
	if days, ok := strings.CutSuffix(s, "d"); ok {
		n, err := strconv.Atoi(days)
		if err != nil || n < 1 {
			return 0, errAction
		}
		return time.Duration(n) * 24 * time.Hour, nil
	}
	return time.ParseDuration(s)
}

// formatTTL names a lifetime for people: "24 hours", "3 days", "90 minutes".
func formatTTL(d time.Duration) string {
	switch {
	case d%(24*time.Hour) == 0:
		return plural(int(d/(24*time.Hour)), "day")
	case d%time.Hour == 0:
		return plural(int(d/time.Hour), "hour")
	default:
		return plural(int(d/time.Minute), "minute")
	}
}

func plural(n int, unit string) string {
	if n == 1 {
		return "1 " + unit
	}
	return fmt.Sprintf("%d %ss", n, unit)
}

// askShare posts the confirmation for a share block and records it. asked
// reports a confirmation that waits for Joe; replied reports any message
// posted, a refusal included.
func (b *Bridge) askShare(ctx context.Context, l *slog.Logger, actor Actor, m Message, channelID string, req ShareRequest) (asked, replied bool) {
	if b.Share == nil || b.Config.Operator.UserID == "" {
		l.WarnContext(ctx, "share block ignored: sharing is off")
		return false, false
	}
	key, err := artifacts.ShareKey(b.Share.BaseURL, m.GuildID, m.ChannelID, req.Path)
	if err != nil {
		l.InfoContext(ctx, "share refused", "error", err)
		_, err := b.post(ctx, actor, channelID, NewReply("I can share only a file of this channel, such as `report.html`.", ""))
		return false, err == nil
	}
	text := fmt.Sprintf("Share `%s` for %s? Anyone with the link can open it. Joe, react %s on this message within %d minutes to create the link.",
		key, formatTTL(req.TTL), shareConfirm, int(shareConfirmWindow/time.Minute))
	id, err := b.post(ctx, actor, channelID, NewReply(text, ""))
	if err != nil || id == "" {
		l.WarnContext(ctx, "share confirmation failed", "error", err)
		return false, false
	}
	now := b.now()
	b.mu.Lock()
	if b.shares == nil {
		b.shares = map[string]pendingShare{}
	}
	for msg, p := range b.shares {
		if !now.Before(p.until) {
			delete(b.shares, msg)
		}
	}
	if len(b.shares) >= maxPendingShares {
		b.mu.Unlock()
		l.WarnContext(ctx, "share confirmation not kept: too many waiting")
		return false, true
	}
	b.shares[id] = pendingShare{channelID: channelID, key: key, ttl: req.TTL, until: now.Add(shareConfirmWindow)}
	b.mu.Unlock()
	// One tap for Joe. Discord sends the bot's own reaction too; it is not Joe's.
	if actor != nil {
		if err := actor.React(ctx, channelID, id, shareConfirm); err != nil {
			actionFailed(ctx, l, "share react", err)
		}
	}
	l.InfoContext(ctx, "share asked", "key", key, "ttl", req.TTL.String(), "confirmation", id)
	return true, true
}

// Reaction is a MESSAGE_REACTION_ADD event.
type Reaction struct {
	UserID    string `json:"user_id"`
	ChannelID string `json:"channel_id"`
	MessageID string `json:"message_id"`
	GuildID   string `json:"guild_id"`
	Emoji     struct {
		ID   string `json:"id"`
		Name string `json:"name"`
	} `json:"emoji"`
}

// HandleReaction makes the share link when Joe reacts ✅ on a waiting
// confirmation. Every other reaction is ignored. A confirmation is used once.
func (b *Bridge) HandleReaction(ctx context.Context, r Reaction) {
	uid := b.Config.Operator.UserID
	if b.Share == nil || uid == "" || r.UserID != uid || r.UserID == b.bot() ||
		r.Emoji.ID != "" || r.Emoji.Name != shareConfirm {
		return
	}
	b.mu.Lock()
	p, ok := b.shares[r.MessageID]
	if ok {
		delete(b.shares, r.MessageID)
	}
	b.mu.Unlock()
	if !ok || p.channelID != r.ChannelID {
		return
	}
	l := b.Log.With("channel", r.ChannelID, "confirmation", r.MessageID)
	if !b.now().Before(p.until) {
		l.InfoContext(ctx, "share confirmation expired")
		_ = b.Replies.Send(ctx, r.ChannelID, NewReply("That share request expired. Ask again for a new one.", r.MessageID))
		return
	}
	link, expires, err := b.Share.Signer.Link(p.key, p.ttl)
	if err != nil {
		l.WarnContext(ctx, "share link failed", "error", err)
		_ = b.Replies.Send(ctx, r.ChannelID, NewReply("I could not create the link. Try again later.", r.MessageID))
		return
	}
	l.InfoContext(ctx, "share link created", "key", p.key, "expires", expires)
	text := fmt.Sprintf("Link to `%s`, valid until %s UTC:\n<%s>", p.key, expires.UTC().Format("2006-01-02 15:04"), link)
	if err := b.Replies.Send(ctx, r.ChannelID, NewReply(text, r.MessageID)); err != nil {
		l.WarnContext(ctx, "share link post failed", "error", err)
	}
}
