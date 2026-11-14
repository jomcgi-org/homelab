package dispatch_test

import (
	"strings"
	"testing"

	"github.com/jomcgi/homelab/projects/factory/connector/dispatch"
	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
)

func TestClassify(t *testing.T) {
	cases := []struct{ state, msg, want string }{
		{kagent.StateFailed, "relay answered codex_login_required", dispatch.ClassCredential},
		{kagent.StateAuthRequired, "", dispatch.ClassCredential},
		{kagent.StateFailed, "HTTP 401 Unauthorized", dispatch.ClassCredential},
		{kagent.StateFailed, "usage_limit_reached: try again in 3h", dispatch.ClassQuota},
		{kagent.StateFailed, "429 Too Many Requests", dispatch.ClassQuota},
		{kagent.StateFailed, "go test exited with status 1", dispatch.ClassEngineering},
		{kagent.StateCanceled, "", dispatch.ClassCanceled},
	}
	for _, c := range cases {
		if got := dispatch.Classify(c.state, c.msg); got != c.want {
			t.Errorf("Classify(%s, %q) = %s, want %s", c.state, c.msg, got, c.want)
		}
	}
}

func TestEventsAreDeterministicAndChunked(t *testing.T) {
	long := strings.Repeat("é", 7000) // 14000 bytes
	task := &kagent.Task{History: []*kagent.Message{
		{MessageID: "u", Role: "ROLE_USER", Parts: []kagent.Part{{Text: "do it"}}},
		{MessageID: "a1", Role: "ROLE_AGENT", Parts: []kagent.Part{{Text: long}}},
		{MessageID: "a2", Role: "ROLE_AGENT", Parts: []kagent.Part{{Data: map[string]any{"x": 1}}}},
	}}
	ev := dispatch.Events(task)
	if len(ev) != 4 {
		t.Fatalf("events = %d, want start, two chunks, end", len(ev))
	}
	if ev[0].Type != "TEXT_MESSAGE_START" || ev[3].Type != "TEXT_MESSAGE_END" {
		t.Fatalf("events = %+v", ev)
	}
	if ev[1].Delta+ev[2].Delta != long || len(ev[1].Delta) > 12000 {
		t.Fatal("chunks do not reassemble the message within the delta limit")
	}
	if again := dispatch.Events(task); len(again) != len(ev) || again[1].Delta != ev[1].Delta {
		t.Fatal("events differ between renders")
	}
}

func TestLastCallUsageIsNotATotal(t *testing.T) {
	task := &kagent.Task{History: []*kagent.Message{
		{Role: "ROLE_AGENT", Metadata: map[string]any{kagent.UsageMetadataKey: map[string]any{"promptTokenCount": float64(100), "candidatesTokenCount": float64(10)}}},
		{Role: "ROLE_AGENT", Metadata: map[string]any{kagent.UsageMetadataKey: map[string]any{"prompt_token_count": float64(300), "candidates_token_count": float64(30)}}},
	}}
	u := dispatch.LastCallUsage(task)
	if u == nil || u.InputTokens != 300 || u.OutputTokens != 30 {
		t.Fatalf("usage = %+v, want the last call's 300/30", u)
	}
	if dispatch.LastCallUsage(&kagent.Task{}) != nil {
		t.Fatal("usage without metadata")
	}
}
