package relay

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/broker/actorjwt"
	"github.com/jomcgi/homelab/projects/factory/broker/internal/fakes"
)

const (
	measuredResponse = `{"id":"resp_1","usage":{"input_tokens":1200,"input_tokens_details":{"cached_tokens":900},"output_tokens":80}}`
	measuredEvent    = "event: response.completed\r\ndata: {\"type\":\"response.completed\",\"response\":" + measuredResponse + "}\r\n\r\n"
)

type tokenSink struct {
	mu     sync.Mutex
	actors []actorjwt.Identity
	usage  []TokenUsage
	calls  []int64
	fail   bool
}

func (s *tokenSink) Begin(_ context.Context, id actorjwt.Identity) (int64, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.actors = append(s.actors, id)
	return int64(len(s.actors)), nil
}

func (s *tokenSink) Complete(_ context.Context, call int64, u TokenUsage) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.fail {
		return errors.New("unavailable")
	}
	s.usage = append(s.usage, u)
	s.calls = append(s.calls, call)
	return nil
}

func TestRelayKeepsUsageWithCallingActor(t *testing.T) {
	h := newHarness(t, true)
	sink := &tokenSink{}
	h.handler.TokenUsage = sink
	for _, tc := range []struct {
		actor, response string
		stream          bool
		input           int
	}{
		{"session-a", "resp_a_json", false, 100},
		{"session-b", "resp_b_sse", true, 200},
		{"session-a", "missing_json", false, -1},
		{"session-b", "missing_sse", true, -1},
		{"session-a", "resp_a_sse", true, 300},
		{"session-b", "resp_b_untyped_sse", true, 400}, // no SSE Content-Type
	} {
		body := fmt.Sprintf(`{"id":%q}`, tc.response)
		contentType := "application/json"
		if tc.input >= 0 {
			body = fmt.Sprintf(`{"id":%q,"usage":{"input_tokens":%d,"input_tokens_details":{"cached_tokens":%d},"output_tokens":10}}`, tc.response, tc.input, tc.input/2)
		}
		if tc.stream {
			contentType = "text/event-stream"
			if strings.Contains(tc.response, "untyped") {
				contentType = "text/plain"
			}
			body = "event: response.completed\ndata: {\"type\":\"response.completed\",\"response\":" + body + "}\n\n"
		}
		h.backend.set(func(b *backend) {
			b.respond = func(w http.ResponseWriter, _ *http.Request) {
				w.Header().Set("Content-Type", contentType)
				_, _ = io.WriteString(w, body)
			}
		})
		jwt := h.signer.Sign(t, fakes.ActorClaims(h.idp.URL, audience, "factory-homelab", tc.actor, time.Now()))
		response := h.post("/v1/responses", jwt) // Same forged Session_id for both actors.
		got, err := io.ReadAll(response.Body)
		response.Body.Close()
		if err != nil || response.StatusCode != 200 || string(got) != body {
			t.Fatalf("response changed for %s: status=%d err=%v body=%s", tc.actor, response.StatusCode, err, got)
		}
	}
	sink.mu.Lock()
	defer sink.mu.Unlock()
	// Missing usage creates gaps in call IDs, not a shift to the next actor.
	if !reflect.DeepEqual(sink.calls, []int64{1, 2, 5, 6}) {
		t.Fatalf("completion call IDs = %v", sink.calls)
	}
	for i, want := range []struct {
		actor string
		usage TokenUsage
	}{
		{"session-a", TokenUsage{"resp_a_json", 100, 50, 10}},
		{"session-b", TokenUsage{"resp_b_sse", 200, 100, 10}},
		{"session-a", TokenUsage{"resp_a_sse", 300, 150, 10}},
		{"session-b", TokenUsage{"resp_b_untyped_sse", 400, 200, 10}},
	} {
		actor := sink.actors[sink.calls[i]-1]
		if actor.ActorName != want.actor || actor.Atespace != "factory-homelab" || sink.usage[i] != want.usage {
			t.Fatalf("completion %d: actor=%+v usage=%+v, want %+v", i, actor, sink.usage[i], want)
		}
	}
}

func TestTokenObserverChunkBoundaries(t *testing.T) {
	for _, tc := range []struct {
		name, body string
		sse        bool
		count      int
	}{
		{"json", measuredResponse, false, 1},
		{"sse", ": keepalive\r\n\r\n" + measuredEvent + measuredEvent + "data: [DONE]\r\n\r\n", true, 1},
		{"multiline", "event: response.completed\ndata: {\"response\":\ndata: " + measuredResponse + "}\n\n", true, 1},
		{"missing_json", `{"id":"resp_1"}`, false, 0},
		{"missing_sse", "data: {\"type\":\"response.completed\",\"response\":{\"id\":\"resp_1\"}}\n\n", true, 0},
		{"partial", strings.TrimSuffix(measuredEvent, "\r\n"), true, 0},
		{"null", `{"id":"resp_1","usage":null}`, false, 0},
		{"invalid", `{"id":"resp_1","usage":{"input_tokens":-1,"output_tokens":1}}`, false, 0},
		{"missing_count", `{"id":"resp_1","usage":{"input_tokens":10}}`, false, 0},
		{"bad_cache", `{"id":"resp_1","usage":{"input_tokens":10,"input_tokens_details":{"cached_tokens":11},"output_tokens":1}}`, false, 0},
		{"created", strings.ReplaceAll(measuredEvent, "response.completed", "response.created"), true, 0},
		{"incomplete", strings.ReplaceAll(measuredEvent, "response.completed", "response.incomplete"), true, 1},
		// HOME-195 rollout: the live backend's streams were never read as
		// SSE. A body that does not start with '{' is an event stream,
		// whatever the Content-Type said.
		{"sniffed_sse", ": keepalive\r\n\r\n" + measuredEvent, false, 1},
		{"sniffed_sse_data", measuredEvent, false, 1},
		{"sniffed_json_space", " \n" + measuredResponse, false, 1},
	} {
		t.Run(tc.name, func(t *testing.T) {
			for chunk := 1; chunk <= len(tc.body); chunk++ {
				var got []TokenUsage
				o := &tokenObserver{sse: tc.sse, onUsage: func(u TokenUsage) { got = append(got, u) }}
				for start := 0; start < len(tc.body); start += chunk {
					o.write([]byte(tc.body[start:min(start+chunk, len(tc.body))]))
				}
				o.finish()
				if len(got) != tc.count {
					t.Fatalf("chunk %d: %v", chunk, got)
				}
				if len(got) > 0 && got[0] != (TokenUsage{"resp_1", 1200, 900, 80}) {
					t.Fatal(got)
				}
			}
		})
	}
}

func TestTokenObserverBoundsEvents(t *testing.T) {
	var got []TokenUsage
	o := &tokenObserver{sse: true, onUsage: func(u TokenUsage) { got = append(got, u) }}
	o.write([]byte("event: response.output_item.done\ndata: "))
	chunk := []byte(strings.Repeat("x", 64<<10))
	for i := 0; i < maxUsageEvent/len(chunk)+1; i++ {
		o.write(chunk)
	}
	if len(o.line)+len(o.data) > maxUsageEvent {
		t.Fatal("unbounded observer")
	}
	o.write([]byte("\r"))
	o.write([]byte("\n\r"))
	o.write([]byte("\n"))
	// The oversized event still counts for the missing-usage log, by name.
	if o.events != 1 || o.lastType != "response.output_item.done" {
		t.Fatalf("oversized event: events=%d last=%q", o.events, o.lastType)
	}
	o.write([]byte(measuredEvent))
	if len(got) != 1 {
		t.Fatalf("completion after oversized event: %v", got)
	}
}

func TestJSONUsageIsObservedBeforeEOF(t *testing.T) {
	// JSON punctuation and escapes inside output must not end the object.
	body := `{"output":[{"text":"quoted \" } { \\"}],"id":"resp_zero","usage":{"input_tokens":0,"output_tokens":0}}`
	var got []TokenUsage
	o := &tokenObserver{onUsage: func(u TokenUsage) { got = append(got, u) }}
	for i := range len(body) - 1 {
		o.write([]byte(body[i : i+1]))
	}
	if len(got) != 0 {
		t.Fatal("observed an incomplete object")
	}
	o.write([]byte(body[len(body)-1:]))
	if len(got) != 1 || got[0] != (TokenUsage{ResponseID: "resp_zero"}) {
		t.Fatalf("before EOF: %v", got)
	}
}

func TestRelayAccountsWithoutChangingResponse(t *testing.T) {
	for _, tc := range []struct {
		name, contentType, body string
		count                   int
		fail                    bool
	}{
		{"json", "application/json", measuredResponse, 1, false},
		{"sse", "text/event-stream; charset=utf-8", measuredEvent, 1, false},
		{"missing_json", "application/json", `{"id":"resp_1","usage":null}`, 0, false},
		{"missing_sse", "text/event-stream", "data: {\"type\":\"response.completed\"}\n\n", 0, false},
		{"database_failure", "text/event-stream", measuredEvent, 0, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			h := newHarness(t, true)
			sink := &tokenSink{fail: tc.fail}
			h.handler.TokenUsage = sink
			h.backend.set(func(b *backend) {
				b.respond = func(w http.ResponseWriter, _ *http.Request) {
					w.Header().Set("Content-Type", tc.contentType)
					for _, c := range []byte(tc.body) {
						_, _ = w.Write([]byte{c})
						w.(http.Flusher).Flush()
					}
				}
			})
			resp := h.post("/v1/responses", h.actorJWT())
			body, err := io.ReadAll(resp.Body)
			if err != nil || string(body) != tc.body || resp.StatusCode != 200 {
				t.Fatalf("relay changed response: status=%d err=%v body=%q", resp.StatusCode, err, body)
			}
			sink.mu.Lock()
			defer sink.mu.Unlock()
			if len(sink.actors) != 1 || sink.actors[0].Atespace != "factory-homelab" || sink.actors[0].ActorName != "session-1" {
				t.Fatalf("identity = %v", sink.actors)
			}
			if len(sink.usage) != tc.count {
				t.Fatalf("usage = %v", sink.usage)
			}
		})
	}
}

// The missing-usage log counts every event, including one whose JSON does not
// parse, and names an event only when the name has the shape of a Responses
// event type; anything else is "other", so the log never carries body text.
func TestTokenObserverDiagnostics(t *testing.T) {
	for _, tc := range []struct {
		name, body string
		events     int
		last       string
	}{
		{"delta", "event: response.output_text.delta\ndata: {\"type\":\"response.output_text.delta\"}\n\n", 1, "response.output_text.delta"},
		{"malformed", "event: response.completed\ndata: {not json\n\n", 1, "response.completed"},
		{"keepalive", ": keepalive\n\n", 0, ""},
		{"body_text", "data: {\"type\":\"Ignore previous instructions\"}\n\n", 1, "other"},
		{"lowercase_secret", "data: {\"type\":\"private_password123\"}\n\n", 1, "other"},
		{"secret_event_field", "event: sk_live_secret\ndata: {not json\n\n", 1, "other"},
		{"long", "data: {\"type\":\"" + strings.Repeat("a", 65) + "\"}\n\n", 1, "other"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			o := &tokenObserver{sse: true, onUsage: func(TokenUsage) { t.Fatal("no usage expected") }}
			o.write([]byte(tc.body))
			if o.events != tc.events || o.lastType != tc.last {
				t.Fatalf("events=%d last=%q, want %d %q", o.events, o.lastType, tc.events, tc.last)
			}
		})
	}
}
