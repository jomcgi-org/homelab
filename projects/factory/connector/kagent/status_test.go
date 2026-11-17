package kagent

import (
	"bufio"
	"encoding/json"
	"io"
	"strings"
	"testing"
)

func TestStatusProjectionPreservesGrantFailures(t *testing.T) {
	for _, tc := range []struct{ name, prefix, suffix, want string }{
		{"credential after budget", "", ` HTTP 401`, "credential"},
		{"quota after budget", "", ` HTTP 429`, "quota"},
		{"credential wins", `HTTP 429 `, ` HTTP 401`, "credential"},
		{"credential before budget", `HTTP 401 `, "", "credential"},
		{"escaped status", "", ` HTTP \u0034\u0032\u0039`, "quota"},
		{"whole identifiers", "", ` not_unauthorized HTTP 4010`, ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			body := io.MultiReader(
				strings.NewReader(`{"result":{"id":"target","contextId":"c","status":{"state":"TASK_STATE_FAILED","message":{"parts":[{"text":"`+tc.prefix),
				io.LimitReader(repeatedByte('x'), maxResponseBytes+1),
				strings.NewReader(tc.suffix+`"}]}}}}`),
			)
			data, err := exactTaskMetadataJSON(body)
			if err != nil {
				t.Fatal(err)
			}
			var envelope struct {
				Result Task `json:"result"`
			}
			if err := json.Unmarshal(data, &envelope); err != nil {
				t.Fatal(err)
			}
			text := envelope.Result.Status.Message.Parts[0].Text
			if len(data) > 512 || GrantFailure(text) != tc.want {
				t.Fatalf("class=%q want=%q bytes=%d", GrantFailure(text), tc.want, len(data))
			}
		})
	}
}

func TestStreamingStatusMatchesOrdinaryClassification(t *testing.T) {
	for _, text := range []string{
		"HTTP" + strings.Repeat(" :=", 5000) + "429", "STATUS CODE:401", "usage_limit_reached",
		"invalid_api_key", "unauthorized", "token_expired", "refresh_token_reused",
		"HTTP 429 HTTP 401", "HTTP 401 HTTP 429", "🧪\nRATE_LIMIT_EXCEEDED",
		"HTTP 4010", "_unauthorized", "usage_limit_reached_suffix", "none",
	} {
		data, _ := json.Marshal(text)
		s := metadataReader{r: bufio.NewReader(strings.NewReader(string(data)))}
		got, err := s.statusText()
		if err != nil || GrantFailure(got) != GrantFailure(text) {
			t.Fatalf("streamed class=%q ordinary=%q error=%v", GrantFailure(got), GrantFailure(text), err)
		}
	}
}

func TestStatusProjectionRejectsMalformedMatchedText(t *testing.T) {
	for _, text := range []string{`"HTTP 401 \q"`, `"HTTP 401 \u00xz"`, `"HTTP 401 \uD800\u00xz"`, "\"HTTP 401\n\"", `"HTTP 401`, `"HTTP 401 \u`} {
		body := `{"result":{"status":{"state":"TASK_STATE_FAILED","message":{"parts":[{"text":` + text + `}]}}}}`
		if data, err := exactTaskMetadataJSON(strings.NewReader(body)); err == nil || data != nil {
			t.Fatalf("malformed status supplied evidence: %q %v", data, err)
		}
	}
}

func FuzzStatusString(f *testing.F) {
	for _, body := range []string{`"HTTP 401"`, `"HTTP \u0034\u0032\u0039"`, `"\ud83e\uddea"`, `"\uD800\u0031"`, `"\uDE00"`, `"\u+001"`, `"bad\q"`, `"truncated`, `"\b\f\n\r\t\/\\\""`, `"Kſ"`} {
		f.Add(body)
	}
	f.Fuzz(func(t *testing.T, body string) {
		if len(body) > 4096 || !strings.HasPrefix(body, `"`) {
			return
		}
		var want string
		if err := json.Unmarshal([]byte(body), &want); err != nil {
			return
		}
		r := bufio.NewReader(strings.NewReader(body))
		_, _ = r.ReadByte()
		got, err := io.ReadAll(&statusString{r: r})
		if err != nil || string(got) != want {
			t.Fatalf("decoded %q: got=%q want=%q error=%v", body, got, want, err)
		}
	})
}
