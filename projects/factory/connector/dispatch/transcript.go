package dispatch

import (
	"strings"

	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
)

const (
	maxDelta     = 12_000 // itsaplan DELTA_LIMIT
	maxIDLen     = 200
	maxBatch     = 200 // itsaplan events per batch
	maxOutput    = 10_000
	maxErrorText = 500
)

// Events renders a task's agent messages as AG-UI text events. The list is a
// pure function of the task history, so a restarted connector produces the
// same events at the same offsets.
func Events(t *kagent.Task) []itsaplan.Event {
	var out []itsaplan.Event
	for _, m := range t.History {
		if m == nil || m.Role == "ROLE_USER" {
			continue
		}
		text := messageText(m)
		if text == "" {
			continue
		}
		id := clip(m.MessageID, maxIDLen)
		out = append(out, itsaplan.Event{Type: "TEXT_MESSAGE_START", MessageID: id, Role: "assistant"})
		for len(text) > 0 {
			n := min(len(text), maxDelta)
			// Keep UTF-8 sequences whole.
			for n < len(text) && n > 0 && !utf8Start(text[n]) {
				n--
			}
			out = append(out, itsaplan.Event{Type: "TEXT_MESSAGE_CONTENT", MessageID: id, Delta: text[:n]})
			text = text[n:]
		}
		out = append(out, itsaplan.Event{Type: "TEXT_MESSAGE_END", MessageID: id})
	}
	return out
}

func utf8Start(b byte) bool { return b&0xC0 != 0x80 }

func messageText(m *kagent.Message) string {
	var b strings.Builder
	for _, p := range m.Parts {
		if p.Text == "" {
			continue
		}
		if b.Len() > 0 {
			b.WriteString("\n")
		}
		b.WriteString(p.Text)
	}
	return b.String()
}

// Output is the turn's final answer: the status message, else the last agent
// message, else the artifacts' text.
func Output(t *kagent.Task) string {
	if t.Status.Message != nil {
		if s := messageText(t.Status.Message); s != "" {
			return clip(s, maxOutput)
		}
	}
	for i := len(t.History) - 1; i >= 0; i-- {
		if m := t.History[i]; m != nil && m.Role != "ROLE_USER" {
			if s := messageText(m); s != "" {
				return clip(s, maxOutput)
			}
		}
	}
	var b strings.Builder
	for _, a := range t.Artifacts {
		for _, p := range a.Parts {
			b.WriteString(p.Text)
		}
	}
	return clip(b.String(), maxOutput)
}

// StatusText is the task's status message text, for error classification.
func StatusText(t *kagent.Task) string {
	if t.Status.Message == nil {
		return ""
	}
	return messageText(t.Status.Message)
}

// LastCallUsage is the token usage of the turn's last model call, as
// itsaplan records it: what that call read and wrote, not a turn total.
func LastCallUsage(t *kagent.Task) *itsaplan.Usage {
	var metas []map[string]any
	if t.Status.Message != nil {
		metas = append(metas, t.Status.Message.Metadata)
	}
	for i := len(t.Artifacts) - 1; i >= 0; i-- {
		if t.Artifacts[i] != nil {
			metas = append(metas, t.Artifacts[i].Metadata)
		}
	}
	for i := len(t.History) - 1; i >= 0; i-- {
		if t.History[i] != nil {
			metas = append(metas, t.History[i].Metadata)
		}
	}
	for _, md := range metas {
		u, ok := md[kagent.UsageMetadataKey].(map[string]any)
		if !ok {
			continue
		}
		in := number(u, "promptTokenCount", "prompt_token_count")
		out := number(u, "candidatesTokenCount", "candidates_token_count")
		if in > 0 || out > 0 {
			return &itsaplan.Usage{InputTokens: in, OutputTokens: out}
		}
	}
	return nil
}

func number(m map[string]any, keys ...string) int64 {
	for _, k := range keys {
		switch v := m[k].(type) {
		case float64:
			return int64(v)
		case int64:
			return v
		case int:
			return int64(v)
		}
	}
	return 0
}

func clip(s string, n int) string {
	if len(s) <= n {
		return s
	}
	for n > 0 && !utf8Start(s[n]) {
		n--
	}
	return s[:n]
}
