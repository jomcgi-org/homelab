package discord

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// shape names a card's components: T text, G gallery, - divider, . space.
func shape(cs []ReplyComponent) string {
	var b strings.Builder
	for _, c := range cs {
		switch c.Type {
		case componentText:
			b.WriteString("T")
		case componentGallery:
			b.WriteString("G")
		case componentSeparator:
			if *c.Divider {
				b.WriteString("-")
			} else {
				b.WriteString(".")
			}
		default:
			b.WriteString("?")
		}
	}
	return b.String()
}

func TestACardWrapsTheAnswerAndSplitsItAtRules(t *testing.T) {
	text := "Plan for <t:1798761600:F> (<t:1798761600:R>)\n\n---\n\n```\n---\n```\n***\n-# sources: met office"
	r := NewReply(text, "9").Card()
	if len(r.Components) != 1 || r.Components[0].Type != componentContainer || r.Components[0].AccentColor != AccentAnswer {
		t.Fatalf("components = %+v", r.Components)
	}
	parts := r.Components[0].Components
	if got := shape(parts); got != "T-T-T" {
		t.Fatalf("shape = %s, parts %+v", got, parts)
	}
	// Timestamps stay intact; the rule in the code block stays.
	if parts[0].Content != "Plan for <t:1798761600:F> (<t:1798761600:R>)" || parts[2].Content != "```\n---\n```" || parts[4].Content != "-# sources: met office" {
		t.Errorf("parts = %+v", parts)
	}
}

func TestACardPartsTextFromImagesWithSpace(t *testing.T) {
	text := "Here:" + imageMark + "Next\n---\nEnd" + imageMark
	files := []File{{Name: "image-1.png"}, {Name: "image-2.png"}}
	r := WithImages(NewReply(text, "9"), text, files).Card()
	if got := shape(r.Components[0].Components); got != "T.G.T-T.G" {
		t.Errorf("shape = %s", got)
	}
	if len(r.Attachments) != 2 || len(r.Files) != 2 {
		t.Errorf("attachments %+v", r.Attachments)
	}
}

func TestACardPastTheComponentLimitKeepsTheReplysOwnParts(t *testing.T) {
	text := strings.Repeat("a\n---\n", 30) + "z"
	r := NewReply(text, "").Card()
	parts := r.Components[0].Components
	if len(parts) != 1 || parts[0].Content != strings.TrimSpace(text) {
		t.Errorf("shape = %s", shape(parts))
	}
}

func TestNoCardWithoutAnAccentOrForAPoll(t *testing.T) {
	r := NewReply("hi", "")
	r.Accent = 0
	if got := r.Card(); got.Components[0].Type != componentText {
		t.Errorf("components = %+v", got.Components)
	}
	if p := NewPollReply(&Poll{}, ""); len(p.Card().Components) != 0 {
		t.Errorf("poll got components")
	}
}

func TestFixedRepliesGetNoticeAndFailureAccents(t *testing.T) {
	if noticeAccent(ReplyFailed) != AccentFailure || noticeAccent(ReplyActionFailed) != AccentFailure ||
		noticeAccent(ReplyBusy) != AccentNotice || noticeAccent(ReplyLimited) != AccentNotice {
		t.Error("wrong accents")
	}
	b, _, rep := newBridge(&fakeAgent{err: io.ErrUnexpectedEOF})
	b.Turn(context.Background(), tagged("1", "hi"))
	if len(rep.sent) != 1 || rep.sent[0].Accent != AccentFailure {
		t.Errorf("sent %+v", rep.sent)
	}
	b, _, rep = newBridge(&fakeAgent{answer: "ok"})
	b.Turn(context.Background(), tagged("2", "hi"))
	if len(rep.sent) != 1 || rep.sent[0].Accent != AccentAnswer {
		t.Errorf("sent %+v", rep.sent)
	}
}

func TestPostSendsTheCardWithNoPings(t *testing.T) {
	var body map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewDecoder(r.Body).Decode(&body)
		_, _ = w.Write([]byte(`{"id":"1"}`))
	}))
	defer srv.Close()
	rest := &REST{Base: srv.URL, Token: func() (string, error) { return "t", nil }}
	if _, err := rest.Post(context.Background(), "5", NewReply("at <t:1798761600:t> @everyone\n---\n-# done", "9")); err != nil {
		t.Fatal(err)
	}
	// Go escapes < in JSON strings; Discord reads the same text.
	var buf strings.Builder
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	_ = enc.Encode(body)
	raw := strings.TrimSpace(buf.String())
	want := `{"allowed_mentions":{"parse":[],"replied_user":false},"components":[{"accent_color":2783446,"components":[` +
		`{"content":"at <t:1798761600:t> @everyone","type":10},{"divider":true,"spacing":1,"type":14},{"content":"-# done","type":10}],"type":17}],` +
		`"flags":32768,"message_reference":{"fail_if_not_exists":false,"message_id":"9"}}`
	if raw != want {
		t.Errorf("body:\n%s\nwant:\n%s", raw, want)
	}
}
