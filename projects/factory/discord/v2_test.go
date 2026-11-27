package discord

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
)

// bosunCard is a Bosun reply as Discord's REST API returns it: Components
// V2 (flags 1<<15), empty content, text in a container's text displays and
// a diagram in a media gallery that points at an attachment.
const bosunCard = `{
  "id": "500", "channel_id": "300", "type": 0, "content": "", "flags": 32768,
  "author": {"id": "100", "username": "Bosun", "bot": true},
  "timestamp": "2026-11-27T22:40:00+00:00",
  "attachments": [{"id": "9", "filename": "diagram-1.png", "content_type": "image/png",
    "url": "https://cdn.discordapp.com/attachments/300/9/diagram-1.png?ex=1"}],
  "components": [{"type": 17, "id": 1, "accent_color": 5793266, "components": [
    {"type": 10, "id": 2, "content": "Here is the deploy flow."},
    {"type": 14, "id": 3, "divider": true, "spacing": 1},
    {"type": 12, "id": 4, "items": [{"media": {"url": "attachment://diagram-1.png"}}]},
    {"type": 9, "id": 5, "components": [{"type": 10, "id": 6, "content": "-# rendered in 1.2 s"}],
      "accessory": {"type": 11, "id": 7, "media": {"url": "https://cdn.discordapp.com/attachments/300/9/thumb.png"}}}
  ]}]
}`

func TestAComponentsV2MessageHasItsTextAndImages(t *testing.T) {
	var h HistoryMessage
	if err := json.Unmarshal([]byte(bosunCard), &h); err != nil {
		t.Fatal(err)
	}
	want := "Here is the deploy flow.\n[image: diagram-1.png]\n-# rendered in 1.2 s\n[image: thumb.png]"
	if got := h.Text(); got != want {
		t.Errorf("Text = %q, want %q", got, want)
	}
	var r Referenced
	if err := json.Unmarshal([]byte(bosunCard), &r); err != nil {
		t.Fatal(err)
	}
	if got := r.Text(); got != want {
		t.Errorf("Referenced.Text = %q, want %q (the attachment shows once)", got, want)
	}
}

// The 2026-11-27 22:57 bug: Bosun read its own diagram reply as empty and
// sent it again.
func TestBosunSeesItsOwnLastReplyInTheLinesAndTheReplyContext(t *testing.T) {
	var card HistoryMessage
	if err := json.Unmarshal([]byte(bosunCard), &card); err != nil {
		t.Fatal(err)
	}
	agent := &fakeAgent{answer: "ok"}
	b, _, _ := ingestBridge(agent)
	b.History = &channelHistory{msgs: []HistoryMessage{said("510", "dave", "nice diagram", 41), card}}
	m := tagged("520", "can you explain step 2?")
	m.Type = 19
	if err := json.Unmarshal([]byte(bosunCard), &m.Referenced); err != nil {
		t.Fatal(err)
	}
	b.Turn(context.Background(), m)
	p := lastPrompt(agent)
	lines := sinceLines(t, p)
	if len(lines) != 2 || lines[0].Author != OwnAuthor || !strings.HasPrefix(lines[0].Text, "Here is the deploy flow.\n[image: diagram-1.png]") {
		t.Fatalf("lines = %+v", lines)
	}
	var reply struct {
		Author  string `json:"author"`
		Message string `json:"message"`
		FromYou bool   `json:"from_you"`
	}
	if err := json.Unmarshal(promptData(t, p)["reply_to"], &reply); err != nil {
		t.Fatal(err)
	}
	if reply.Author != OwnAuthor || !reply.FromYou || !strings.Contains(reply.Message, "[image: diagram-1.png]") {
		t.Errorf("reply_to = %+v", reply)
	}
	if strings.Contains(p, "cdn.discordapp.com") {
		t.Errorf("prompt has a media URL: %s", p)
	}
}

func TestAFriendsImageAttachmentIsAnImage(t *testing.T) {
	agent := &fakeAgent{answer: "ok"}
	b, _, _ := ingestBridge(agent)
	h := said("10", "dave", "look", 1)
	h.Attachments = []Attachment{{Filename: "flat.jpg", ContentType: "image/jpeg"}, {Filename: "lease.pdf", ContentType: "application/pdf"}}
	b.History = &channelHistory{msgs: []HistoryMessage{h}}
	b.Turn(context.Background(), tagged("20", "?"))
	if lines := sinceLines(t, lastPrompt(agent)); len(lines) != 1 || lines[0].Text != "look [image: flat.jpg] [attachment: lease.pdf]" {
		t.Errorf("lines = %+v", lines)
	}
}
