package dispatch_test

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/broker/actorjwt"
	"github.com/jomcgi/homelab/projects/factory/broker/grant"
	brokermetrics "github.com/jomcgi/homelab/projects/factory/broker/metrics"
	"github.com/jomcgi/homelab/projects/factory/broker/relay"
	"github.com/jomcgi/homelab/projects/factory/broker/usage"
	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
)

type usageIdentity struct{ actorjwt.Identity }

func (v usageIdentity) Verify(context.Context, string) (actorjwt.Identity, error) {
	return v.Identity, nil
}

type usageGrant struct{}

func (usageGrant) Grant() string                                    { return "test" }
func (usageGrant) AccessToken(context.Context) (grant.Token, error) { return grant.Token{}, nil }
func (usageGrant) RefreshRejected(context.Context, string) (grant.Token, error) {
	return grant.Token{}, nil
}

// Drive the real relay and Postgres recorder, with a provider fixture. No
// harness metadata is needed for the connector's itsaplan result.
func relayUsage(t *testing.T, v *env, namespace, session, response string, stream bool, input int) {
	t.Helper()
	body := fmt.Sprintf(`{"id":%q}`, response)
	if input >= 0 {
		body = fmt.Sprintf(`{"id":%q,"usage":{"input_tokens":%d,"input_tokens_details":{"cached_tokens":%d},"output_tokens":10}}`, response, input, input/2)
	}
	contentType := "application/json"
	if stream {
		contentType = "text/event-stream"
		body = "event: response.completed\ndata: {\"type\":\"response.completed\",\"response\":" + body + "}\n\n"
	}
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", contentType)
		_, _ = io.WriteString(w, body)
	}))
	defer up.Close()
	r := &relay.Relay{
		Verifier: usageIdentity{actorjwt.Identity{Atespace: namespace, ActorName: "session-" + strings.ToLower(session), ActorUID: "uid-" + session}},
		Tokens:   usageGrant{}, TokenUsage: &usage.Store{DB: v.st.DB},
		Upstream: up.URL, Client: up.Client(), Metrics: brokermetrics.New(),
		Logger: slog.New(slog.NewTextHandler(io.Discard, nil)),
	}
	req := httptest.NewRequest(http.MethodPost, "/v1/responses", strings.NewReader(`{}`))
	req.Header.Set("Authorization", "Bearer fixture")
	req.Header.Set("Session_id", "another-session")
	out := httptest.NewRecorder()
	r.ServeHTTP(out, req)
	if out.Code != 200 || out.Body.String() != body {
		t.Fatalf("relay changed bytes: %d %q", out.Code, out.Body.String())
	}
}

func TestRunTokenTotalsFromRelay(t *testing.T) {
	for _, stream := range []bool{false, true} {
		t.Run(fmt.Sprintf("stream=%t", stream), func(t *testing.T) {
			v := newEnv(t)
			v.its.add(1, "coder", 101, "HL-1", "Do it.")
			v.tick(v.e)
			session := v.work(101).SessionID
			relayUsage(t, v, "factory-homelab", session, "resp_1", stream, 100)
			relayUsage(t, v, "factory-homelab", session, "resp_2", stream, 200)
			relayUsage(t, v, "factory-homelab", session, "resp_2", stream, 200) // replay
			relayUsage(t, v, "factory-homelab", session, "resp_missing", stream, -1)
			relayUsage(t, v, "factory-homelab", "other", "resp_other", stream, 9000)
			relayUsage(t, v, "another-binding", session, "resp_namespace", stream, 9000)
			v.kg.finish(v.kg.lastTask(), kagent.StateCompleted, "Pushed.", nil)
			v.clk.Advance(5 * time.Second)
			v.tick(v.e)
			results := v.its.Run(1).results
			if len(results) != 1 || results[0].Usage == nil || results[0].Usage.InputTokens != 300 || results[0].Usage.OutputTokens != 20 {
				t.Fatalf("results = %+v", results)
			}
			var cached int64
			if err := v.st.DB.QueryRow(`SELECT cached_input_tokens FROM run WHERE id=1`).Scan(&cached); err != nil || cached != 150 {
				t.Fatalf("cached=%d err=%v", cached, err)
			}
			// A restarted writer and another run in the same session must
			// not count the previous run's calls again.
			v.e.Release()
			v.e = v.engine("replacement")
			v.its.add(2, "coder", 101, "HL-1", "Follow up.")
			v.tick(v.e)
			if v.work(101).SessionID != session {
				t.Fatal("expected session reuse")
			}
			relayUsage(t, v, "factory-homelab", session, "resp_3", stream, 400)
			v.kg.finish(v.kg.lastTask(), kagent.StateCompleted, "Pushed again.", nil)
			v.clk.Advance(5 * time.Second)
			v.tick(v.e)
			results = v.its.Run(2).results
			if len(results) != 1 || results[0].Usage == nil || results[0].Usage.InputTokens != 400 || results[0].Usage.OutputTokens != 10 {
				t.Fatalf("second run = %+v", results)
			}
		})
	}
}

func TestUsageBeforeSessionAcknowledgementAndAcrossRetry(t *testing.T) {
	v := newEnv(t)
	v.its.add(1, "coder", 101, "HL-1", "Do it.")
	v.kg.loseResponse = true
	v.tick(v.e)
	task, err := v.kg.GetTask(v.ctx, kagent.AgentRef{Namespace: "factory-homelab", Name: "coder"}, v.kg.lastTask())
	if err != nil {
		t.Fatal(err)
	}
	relayUsage(t, v, "factory-homelab", task.ContextID, "resp_early", true, 100)
	var assigned bool
	if err := v.st.DB.QueryRow(`SELECT delivery_attempt_id IS NOT NULL FROM codex_call`).Scan(&assigned); err != nil || assigned {
		t.Fatalf("early call assigned=%t err=%v", assigned, err)
	}
	v.clk.Advance(16 * time.Second)
	v.tick(v.e) // discovers and acknowledges the task
	v.kg.finish(task.ID, kagent.StateFailed, "test failed", nil)
	v.clk.Advance(5 * time.Second)
	v.tick(v.e)
	if v.kg.lastTask() == task.ID {
		t.Fatal("expected engineering retry")
	}
	relayUsage(t, v, "factory-homelab", task.ContextID, "resp_retry", false, 200)
	v.kg.finish(v.kg.lastTask(), kagent.StateCompleted, "Fixed.", nil)
	v.clk.Advance(5 * time.Second)
	v.tick(v.e)
	results := v.its.Run(1).results
	if len(results) != 1 || results[0].Usage == nil || results[0].Usage.InputTokens != 300 || results[0].Usage.OutputTokens != 20 {
		t.Fatalf("retry totals = %+v", results)
	}
}

func TestRunWithoutResponseUsageStaysUnknown(t *testing.T) {
	for _, stream := range []bool{false, true} {
		t.Run(fmt.Sprintf("stream=%t", stream), func(t *testing.T) {
			v := newEnv(t)
			v.its.add(1, "coder", 101, "HL-1", "Do it.")
			v.tick(v.e)
			relayUsage(t, v, "factory-homelab", v.work(101).SessionID, "resp_missing", stream, -1)
			v.kg.finish(v.kg.lastTask(), kagent.StateCompleted, "Pushed.", nil)
			v.clk.Advance(5 * time.Second)
			v.tick(v.e)
			results := v.its.Run(1).results
			if len(results) != 1 || results[0].Usage != nil {
				t.Fatalf("unmeasured result = %+v", results)
			}
		})
	}
}
