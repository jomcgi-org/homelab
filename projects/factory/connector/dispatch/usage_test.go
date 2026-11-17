package dispatch_test

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/broker/actorjwt"
	"github.com/jomcgi/homelab/projects/factory/broker/grant"
	brokermetrics "github.com/jomcgi/homelab/projects/factory/broker/metrics"
	"github.com/jomcgi/homelab/projects/factory/broker/relay"
	"github.com/jomcgi/homelab/projects/factory/broker/usage"
	"github.com/jomcgi/homelab/projects/factory/connector/dispatch"
	"github.com/jomcgi/homelab/projects/factory/connector/internal/pgtest"
	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
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

// Use the production result client, so the relay-to-run tests also exercise
// outbox serialization, HTTP routing, authentication and the API's field names.
// Queue operations keep their deterministic fake clock.
type httpResultPlan struct {
	dispatch.Itsaplan
	client *itsaplan.Client
}

func (p *httpResultPlan) Result(ctx context.Context, agent string, id int64, result itsaplan.Result) error {
	return p.client.Result(ctx, agent, id, result)
}

type resultRequest struct {
	path string
	body []byte
}

func resultEndpoint(t *testing.T, v *env, failures int32) <-chan resultRequest {
	t.Helper()
	requests := make(chan resultRequest, 16)
	var remaining atomic.Int32
	remaining.Store(failures)
	mux := http.NewServeMux()
	mux.HandleFunc("POST /agent-runs/{runID}/result", func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("x-api-key") != "coder-fixture" || r.Header.Get("Content-Type") != "application/json" {
			t.Error("result request has incorrect authentication or content type")
			http.Error(w, "bad headers", http.StatusUnauthorized)
			return
		}
		id, err := strconv.ParseInt(r.PathValue("runID"), 10, 64)
		if err != nil {
			t.Error(err)
			http.Error(w, "bad run ID", http.StatusBadRequest)
			return
		}
		body, err := io.ReadAll(r.Body)
		if err != nil {
			t.Error(err)
			http.Error(w, "bad body", http.StatusBadRequest)
			return
		}
		requests <- resultRequest{path: r.URL.Path, body: body}
		if remaining.Add(-1) >= 0 {
			http.Error(w, "retry later", http.StatusServiceUnavailable)
			return
		}
		var result itsaplan.Result
		if err := json.Unmarshal(body, &result); err != nil {
			t.Error(err)
			http.Error(w, "bad JSON", http.StatusBadRequest)
			return
		}
		if err := v.its.Result(r.Context(), "coder", id, result); err != nil {
			t.Error(err)
			http.Error(w, "unexpected run or attempt", http.StatusConflict)
			return
		}
		w.WriteHeader(http.StatusNoContent)
	})
	server := httptest.NewServer(mux)
	t.Cleanup(server.Close)
	v.e.Release()
	v.plan = &httpResultPlan{Itsaplan: v.its, client: &itsaplan.Client{
		BaseURL: server.URL, HTTP: server.Client(), Keys: map[string]string{"coder": "coder-fixture"},
	}}
	v.e = v.engine("http-results")
	return requests
}

func assertResultRequest(t *testing.T, requests <-chan resultRequest, runID int64, input, output int) []byte {
	t.Helper()
	select {
	case request := <-requests:
		if want := fmt.Sprintf("/agent-runs/%d/result", runID); request.path != want {
			t.Fatalf("result path = %s, want %s", request.path, want)
		}
		// Check literal wire names independently of the production Result type.
		var body map[string]any
		if err := json.Unmarshal(request.body, &body); err != nil {
			t.Fatal(err)
		}
		if body["attempt"] != float64(1) || body["status"] != "success" {
			t.Fatalf("result = %s", request.body)
		}
		if input < 0 {
			if _, exists := body["usage"]; exists {
				t.Fatalf("unmeasured usage must be omitted: %s", request.body)
			}
		} else {
			usage, ok := body["usage"].(map[string]any)
			if !ok || len(usage) != 2 || usage["inputTokens"] != float64(input) || usage["outputTokens"] != float64(output) {
				t.Fatalf("usage = %s, want %d/%d", request.body, input, output)
			}
		}
		return request.body
	default:
		t.Fatal("no result reached the itsaplan HTTP endpoint")
		return nil
	}
}

// Drive the real relay and Postgres recorder, with a provider fixture. No
// harness metadata is needed for the connector's itsaplan result.
func relayUsage(t *testing.T, v *env, namespace, session, response string, stream bool, input int) {
	t.Helper()
	if v.accounting == nil {
		v.accounting = pgtest.AsRole(t, v.st.DB, "factory_broker_accounting")
	}
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
		Tokens:   usageGrant{}, TokenUsage: &usage.Store{DB: v.accounting},
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
			requests := resultEndpoint(t, v, 0)
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
			assertResultRequest(t, requests, 1, 300, 20)
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
			assertResultRequest(t, requests, 2, 400, 10)
			if len(results) != 1 || results[0].Usage == nil || results[0].Usage.InputTokens != 400 || results[0].Usage.OutputTokens != 10 {
				t.Fatalf("second run = %+v", results)
			}
		})
	}
}

func TestInterleavedRunTokenAttribution(t *testing.T) {
	v := newEnv(t)
	requests := resultEndpoint(t, v, 0)
	v.its.add(1, "coder", 101, "HL-1", "First issue.")
	v.its.add(2, "coder", 102, "HL-2", "Second issue.")
	v.tick(v.e)
	v.tick(v.e)
	first, second := v.work(101).SessionID, v.work(102).SessionID
	if first == "" || second == "" || first == second {
		t.Fatalf("expected separate concurrent sessions: %q %q", first, second)
	}
	relayUsage(t, v, "factory-homelab", first, "a_json", false, 100)
	relayUsage(t, v, "factory-homelab", second, "b_sse", true, 200)
	relayUsage(t, v, "factory-homelab", first, "a_missing", true, -1)
	relayUsage(t, v, "factory-homelab", second, "b_missing", false, -1)
	relayUsage(t, v, "factory-homelab", first, "a_sse", true, 300)
	relayUsage(t, v, "factory-homelab", second, "b_json", false, 400)
	// Finish in the opposite order from dispatch. The second run must not
	// absorb the first run's observations just because it finishes first.
	for _, expected := range []struct {
		id            int64
		input, cached int
	}{{2, 600, 300}, {1, 400, 200}} {
		attempt, err := v.st.LatestAttempt(v.ctx, expected.id)
		if err != nil || attempt == nil {
			t.Fatalf("attempt for %d: %v", expected.id, err)
		}
		v.kg.finish(attempt.TaskID, kagent.StateCompleted, "Pushed.", nil)
		v.clk.Advance(5 * time.Second)
		v.tick(v.e)
		assertResultRequest(t, requests, expected.id, expected.input, 20)
		var input, cached, output int
		if err := v.st.DB.QueryRow(`SELECT input_tokens, cached_input_tokens, output_tokens FROM run WHERE id=$1`, expected.id).Scan(&input, &cached, &output); err != nil {
			t.Fatal(err)
		}
		if input != expected.input || cached != expected.cached || output != 20 {
			t.Fatalf("run %d: %d/%d/%d", expected.id, input, cached, output)
		}
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
			requests := resultEndpoint(t, v, 0)
			v.its.add(1, "coder", 101, "HL-1", "Do it.")
			v.tick(v.e)
			relayUsage(t, v, "factory-homelab", v.work(101).SessionID, "resp_missing", stream, -1)
			v.kg.finish(v.kg.lastTask(), kagent.StateCompleted, "Pushed.", nil)
			v.clk.Advance(5 * time.Second)
			v.tick(v.e)
			results := v.its.Run(1).results
			assertResultRequest(t, requests, 1, -1, -1)
			if len(results) != 1 || results[0].Usage != nil {
				t.Fatalf("unmeasured result = %+v", results)
			}
		})
	}
}

func TestRunTokenResultSurvivesHTTPFailureAndRestart(t *testing.T) {
	v := newEnv(t)
	requests := resultEndpoint(t, v, 1)
	v.its.add(1, "coder", 101, "HL-1", "Do it.")
	v.tick(v.e)
	session := v.work(101).SessionID
	relayUsage(t, v, "factory-homelab", session, "resp_json", false, 100)
	relayUsage(t, v, "factory-homelab", session, "resp_sse", true, 200)
	v.kg.finish(v.kg.lastTask(), kagent.StateCompleted, "Pushed.", nil)
	v.clk.Advance(5 * time.Second)
	v.tick(v.e)
	var tries int
	var lastError string
	if err := v.st.DB.QueryRow(`SELECT tries, last_error FROM outbox WHERE kind = 'result' AND run_id = 1`).Scan(&tries, &lastError); err != nil {
		t.Fatal(err)
	}
	if tries != 1 || !strings.Contains(lastError, "503") {
		t.Fatalf("expected recorded HTTP failure, got tries=%d error=%s", tries, lastError)
	}
	first := assertResultRequest(t, requests, 1, 300, 20)
	if v.run(1).State != "finishing" || len(v.its.Run(1).results) != 0 {
		t.Fatal("failed result was treated as delivered")
	}
	v.e.Release()
	v.e = v.engine("result-retry")
	v.clk.Advance(5 * time.Second)
	v.tick(v.e)
	retry := assertResultRequest(t, requests, 1, 300, 20)
	if string(first) != string(retry) {
		t.Fatalf("result changed after restart: %s -> %s", first, retry)
	}
	if v.run(1).State != "closed" || len(v.its.Run(1).results) != 1 {
		t.Fatal("retried result did not close the run exactly once")
	}
}

func TestAccountingLoginCannotChangeDispatchState(t *testing.T) {
	v := newEnv(t)
	v.its.add(1, "coder", 101, "HL-1", "Do it.")
	v.tick(v.e)
	v.accounting = pgtest.AsRole(t, v.st.DB, "factory_broker_accounting")
	// Pin one session so the search-path attack below reaches the connection
	// used by the real broker recorder too.
	v.accounting.SetMaxOpenConns(1)
	for _, statement := range []string{
		`SELECT * FROM work_item`,
		`UPDATE work_item SET state = 'done'`,
		`UPDATE run SET state = 'closed'`,
		`UPDATE delivery_attempt SET issues = 0`,
		`DELETE FROM outbox`,
		`TRUNCATE cancellation`,
		`UPDATE codex_call SET delivery_attempt_id = NULL`,
		`DELETE FROM codex_usage`,
		`INSERT INTO codex_usage VALUES (1, 'forged', 1, 0, 1)`,
		`CREATE TABLE public.broker_owned (id bigint)`,
		`SET ROLE factory_connector`,
	} {
		if _, err := v.accounting.Exec(statement); err == nil || !strings.Contains(err.Error(), "permission denied") {
			t.Fatalf("accounting login executed %q: %v", statement, err)
		}
	}
	// Check every application table and sequence, including future dispatch
	// tables: the login must never gain direct access through a broad grant.
	var grants int
	if err := v.st.DB.QueryRow(`SELECT count(*) FROM pg_tables WHERE schemaname = 'public'
		AND has_table_privilege('factory_broker_accounting', format('%I.%I', schemaname, tablename),
		'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')`).Scan(&grants); err != nil || grants != 0 {
		t.Fatalf("broker table grants = %d: %v", grants, err)
	}
	if err := v.st.DB.QueryRow(`SELECT count(*) FROM pg_sequences WHERE schemaname = 'public'
		AND has_sequence_privilege('factory_broker_accounting', format('%I.%I', schemaname, sequencename),
		'USAGE,SELECT,UPDATE')`).Scan(&grants); err != nil || grants != 0 {
		t.Fatalf("broker sequence grants = %d: %v", grants, err)
	}
	if err := v.st.DB.QueryRow(`SELECT count(*) FROM pg_proc p, aclexplode(p.proacl) a
		WHERE p.pronamespace = 'public'::regnamespace AND p.proname LIKE 'record_codex_%'
		AND a.grantee = 0`).Scan(&grants); err != nil || grants != 0 {
		t.Fatalf("PUBLIC can execute accounting functions: %d, %v", grants, err)
	}
	if _, err := v.accounting.Exec(`CREATE TEMP TABLE codex_call (id bigint);
		CREATE TEMP TABLE codex_usage (call_id bigint);
		CREATE TEMP TABLE delivery_attempt (id bigint);
		SET search_path = pg_temp, public`); err != nil {
		t.Fatal(err)
	}
	session := v.work(101).SessionID
	relayUsage(t, v, "factory-homelab", session, "resp_restricted", true, 100)
	if err := v.st.DB.QueryRow(`SELECT count(*) FROM public.codex_usage WHERE input_tokens = 100`).Scan(&grants); err != nil || grants != 1 {
		t.Fatalf("function did not use the real accounting tables: %d, %v", grants, err)
	}
	v.kg.finish(v.kg.lastTask(), kagent.StateCompleted, "Pushed.", nil)
	v.clk.Advance(5 * time.Second)
	v.tick(v.e)
	results := v.its.Run(1).results
	if len(results) != 1 || results[0].Usage == nil || results[0].Usage.InputTokens != 100 || results[0].Usage.OutputTokens != 10 {
		t.Fatalf("restricted login totals = %+v", results)
	}
}
