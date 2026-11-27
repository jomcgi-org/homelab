package discord

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// shape names a reply's components: T text, G gallery, - divider, . space.
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

func TestLayoutSplitsTheAnswerAtRules(t *testing.T) {
	text := "Plan for <t:1798761600:F> (<t:1798761600:R>)\n\n---\n\n```\n---\n```\n```x```\n***\n-# sources: met office"
	parts := NewReply(text, "9").Layout().Components
	if got := shape(parts); got != "T-T-T" {
		t.Fatalf("shape = %s, parts %+v", got, parts)
	}
	// Timestamps stay intact; the rule in the code block stays.
	if parts[0].Content != "Plan for <t:1798761600:F> (<t:1798761600:R>)" || parts[2].Content != "```\n---\n```\n```x```" || parts[4].Content != "-# sources: met office" {
		t.Errorf("parts = %+v", parts)
	}
}

func TestLayoutKeepsImagesInPlaceWithNoContainer(t *testing.T) {
	text := "Here:" + imageMark + "Next\n---\nEnd" + imageMark
	files := []File{{Name: "image-1.png"}, {Name: "image-2.png"}}
	r := WithImages(NewReply(text, "9"), text, files).Layout()
	if got := shape(r.Components); got != "TGT-TG" {
		t.Errorf("shape = %s", got)
	}
	if len(r.Attachments) != 2 || len(r.Files) != 2 {
		t.Errorf("attachments %+v", r.Attachments)
	}
}

func TestLayoutPastTheComponentLimitKeepsTheReplysOwnParts(t *testing.T) {
	text := strings.Repeat("a\n---\n", 30) + "z"
	parts := NewReply(text, "").Layout().Components
	if len(parts) != 1 || parts[0].Content != strings.TrimSpace(text) {
		t.Errorf("shape = %s", shape(parts))
	}
}

func TestLayoutLeavesAPollAlone(t *testing.T) {
	if p := NewPollReply(&Poll{}, ""); len(p.Layout().Components) != 0 {
		t.Errorf("poll got components")
	}
}

func TestPostSendsTopLevelPartsWithNoPings(t *testing.T) {
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
	want := `{"allowed_mentions":{"parse":[],"replied_user":false},"components":[` +
		`{"content":"at <t:1798761600:t> @everyone","type":10},{"divider":true,"spacing":1,"type":14},{"content":"-# done","type":10}],` +
		`"flags":32768,"message_reference":{"fail_if_not_exists":false,"message_id":"9"}}`
	if raw != want {
		t.Errorf("body:\n%s\nwant:\n%s", raw, want)
	}
}
