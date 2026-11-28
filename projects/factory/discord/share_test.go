package discord

import (
	"context"
	"strings"
	"testing"
	"time"
)

const (
	shareGuild   = "1501965852042330302"
	shareChannel = "1501965852042330999"
	shareJoe     = "4242"
)

type fakeSigner struct {
	keys []string
	ttls []time.Duration
}

func (f *fakeSigner) Link(key string, ttl time.Duration) (string, time.Time, error) {
	f.keys = append(f.keys, key)
	f.ttls = append(f.ttls, ttl)
	return "https://artifact.jomcgi.dev/s/" + key + "?X-Amz-Signature=sig", time.Date(2026, 11, 18, 12, 5, 0, 0, time.UTC), nil
}

func shareBridge(t *testing.T, answer string) (*Bridge, *actingReplies, *fakeSigner, *time.Time) {
	t.Helper()
	b, _, _ := newBridge(&fakeAgent{answer: answer})
	b.Config.Guilds[shareGuild] = Guild{}
	b.Config.Operator.UserID = shareJoe
	rep := &actingReplies{}
	b.Replies = rep
	signer := &fakeSigner{}
	b.Share = &Sharing{Signer: signer, BaseURL: "https://artifact.jomcgi.dev"}
	now := time.Date(2026, 11, 17, 12, 5, 0, 0, time.UTC)
	b.Now = func() time.Time { return now }
	return b, rep, signer, &now
}

func shareTurn(id string) Message {
	m := tagged(id, "share the report")
	m.GuildID, m.ChannelID = shareGuild, shareChannel
	return m
}

func reaction(user, message, emoji string) ReactionAdd {
	r := ReactionAdd{UserID: user, ChannelID: shareChannel, MessageID: message, GuildID: shareGuild}
	r.Emoji.Name = emoji
	return r
}

const shareAnswer = "Here it is.\n```share\n{\"path\": \"report.html\", \"ttl\": \"3d\"}\n```"

func text(r Reply) string { return r.Components[0].Content }

func TestAShareBlockAsksJoeAndOnlyJoesTickMakesTheLink(t *testing.T) {
	b, rep, signer, _ := shareBridge(t, shareAnswer)
	ctx := context.Background()
	b.Turn(ctx, shareTurn("1"))
	if len(rep.sent) != 2 || text(rep.sent[0]) != "Here it is." {
		t.Fatalf("sent %+v", rep.sent)
	}
	ask := text(rep.sent[1])
	key := shareGuild + "/" + shareChannel + "/report.html"
	if !strings.Contains(ask, "Share `"+key+"` for 3 days?") || !strings.Contains(ask, "react ✅") ||
		len(rep.sent[1].AllowedMentions.Parse) != 0 {
		t.Fatalf("confirmation %q", ask)
	}
	// The confirmation is the second post, r2; the bot adds the ✅ to tap.
	if got := strings.Join(rep.actions, ","); got != "react "+shareChannel+"/r2 ✅" {
		t.Fatalf("actions %s", got)
	}

	for name, r := range map[string]ReactionAdd{
		"someone else":  reaction("777", "r2", "✅"),
		"the bot":       reaction(bot, "r2", "✅"),
		"other emoji":   reaction(shareJoe, "r2", "👍"),
		"other message": reaction(shareJoe, "r1", "✅"),
		"custom emoji":  func() ReactionAdd { r := reaction(shareJoe, "r2", "✅"); r.Emoji.ID = "99"; return r }(),
		"other channel": func() ReactionAdd { r := reaction(shareJoe, "r2", "✅"); r.ChannelID = "555"; return r }(),
	} {
		b.HandleReaction(ctx, r)
		if len(signer.keys) != 0 {
			t.Fatalf("%s: made a link", name)
		}
	}
	// "other channel" used the request up; ask again.
	b.Turn(ctx, shareTurn("2"))
	confirmation := "r" + string(rune('0'+len(rep.sent)))
	b.HandleReaction(ctx, reaction(shareJoe, confirmation, "✅"))
	if len(signer.keys) != 1 || signer.keys[0] != key || signer.ttls[0] != 72*time.Hour {
		t.Fatalf("signed %v %v", signer.keys, signer.ttls)
	}
	last := rep.sent[len(rep.sent)-1]
	if !strings.Contains(text(last), "<https://artifact.jomcgi.dev/s/"+key+"?X-Amz-Signature=sig>") ||
		!strings.Contains(text(last), "valid until 2026-11-18 12:05 UTC") || last.MessageReference.MessageID != confirmation {
		t.Fatalf("link reply %+v", last)
	}
	// One link per confirmation.
	b.HandleReaction(ctx, reaction(shareJoe, confirmation, "✅"))
	if len(signer.keys) != 1 {
		t.Fatal("a second ✅ made another link")
	}
}

func TestALateTickMakesNoLink(t *testing.T) {
	b, rep, signer, now := shareBridge(t, shareAnswer)
	ctx := context.Background()
	b.Turn(ctx, shareTurn("1"))
	*now = now.Add(shareConfirmWindow)
	b.HandleReaction(ctx, reaction(shareJoe, "r2", "✅"))
	if len(signer.keys) != 0 || !strings.Contains(text(rep.sent[len(rep.sent)-1]), "expired") {
		t.Fatalf("signed %v, sent %+v", signer.keys, rep.sent)
	}
}

func TestShareRefusesAFileOutsideTheChannel(t *testing.T) {
	answer := "```share\n{\"path\": \"https://artifact.jomcgi.dev/185442367703220224/1/a.html\"}\n```"
	b, rep, signer, _ := shareBridge(t, answer)
	b.Turn(context.Background(), shareTurn("1"))
	// The other server's key becomes a path in this channel's folder, so
	// the confirmation names this channel's folder, never the other one.
	for _, r := range rep.sent {
		if strings.Contains(text(r), "Share `185442367703220224/") {
			t.Fatalf("asked to share another server's file: %q", text(r))
		}
	}
	answer = "```share\n{\"path\": \"../private/x.html\"}\n```"
	b, rep, signer, _ = shareBridge(t, answer)
	b.Turn(context.Background(), shareTurn("1"))
	if len(rep.actions) != 0 || len(signer.keys) != 0 || !strings.Contains(text(rep.sent[len(rep.sent)-1]), "only a file of this channel") {
		t.Fatalf("sent %+v actions %v", rep.sent, rep.actions)
	}
}

func TestShareIsOffWithoutJoeOrASigner(t *testing.T) {
	b, rep, _, _ := shareBridge(t, shareAnswer)
	b.Share = nil
	b.Turn(context.Background(), shareTurn("1"))
	if len(rep.sent) != 1 || len(rep.actions) != 0 {
		t.Fatalf("sent %+v actions %v", rep.sent, rep.actions)
	}
	b, rep, _, _ = shareBridge(t, shareAnswer)
	b.Config.Operator.UserID = ""
	b.Turn(context.Background(), shareTurn("1"))
	if len(rep.actions) != 0 {
		t.Fatalf("actions %v", rep.actions)
	}
}

func TestParseShare(t *testing.T) {
	for spec, want := range map[string]time.Duration{
		`{"path":"a.html"}`:              24 * time.Hour,
		`{"path":"a.html","ttl":"90m"}`:  90 * time.Minute,
		`{"path":"a.html","ttl":"7d"}`:   7 * 24 * time.Hour,
		`{"path":"a.html","ttl":"168h"}`: 168 * time.Hour,
	} {
		r, err := ParseShare(spec)
		if err != nil || r.TTL != want || r.Path != "a.html" {
			t.Errorf("%s: %+v %v", spec, r, err)
		}
	}
	for _, spec := range []string{
		`{"path":"a.html","ttl":"8d"}`, `{"path":"a.html","ttl":"30s"}`, `{"path":"a.html","ttl":"0d"}`,
		`{"path":""}`, `{"path":"a b.html"}`, `{"path":"a.html","public":true}`, `{"ttl":"1d"}`,
	} {
		if _, err := ParseShare(spec); err == nil {
			t.Errorf("%s accepted", spec)
		}
	}
	if formatTTL(24*time.Hour) != "1 day" || formatTTL(90*time.Minute) != "90 minutes" || formatTTL(2*time.Hour) != "2 hours" {
		t.Error("formatTTL")
	}
}
