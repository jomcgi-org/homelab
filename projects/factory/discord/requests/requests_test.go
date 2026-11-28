package requests

import (
	"context"
	"database/sql"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"

	_ "github.com/lib/pq"

	"github.com/jomcgi/homelab/projects/factory/broker/actorjwt"
	"github.com/jomcgi/homelab/projects/factory/connector/internal/pgtest"
)

func TestMain(m *testing.M) { os.Exit(pgtest.Main(m)) }

const (
	guild   = "1501965852042330302"
	channel = "1575140752776757368"
	joe     = "111111111111111111"
)

type verifier struct{}

func (verifier) Verify(_ context.Context, raw string) (actorjwt.Identity, error) {
	if raw != "good" {
		return actorjwt.Identity{}, &actorjwt.Error{Reason: actorjwt.ReasonSignature}
	}
	return actorjwt.Identity{Subject: "actor/session-abc", ActorName: "session-abc"}, nil
}

type turns map[string]Origin

func (t turns) Turn(_ context.Context, id string) (Origin, error) {
	o, ok := t[id]
	if !ok {
		return Origin{}, ErrNoTurn
	}
	return o, nil
}

// requesterDB opens the connector schema as the bosun-requests login, and
// as its owner for fixtures.
func requesterDB(t *testing.T) (*Store, *sql.DB) {
	t.Helper()
	admin, err := sql.Open("postgres", pgtest.NewDB(t))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { admin.Close() })
	return &Store{DB: pgtest.AsRole(t, admin, "factory_bosun_requests")}, admin
}

func call(t *testing.T, h http.Handler, token, body string) (int, map[string]any) {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/mcp", strings.NewReader(body))
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	w := httptest.NewRecorder()
	h.ServeHTTP(w, req)
	var out map[string]any
	raw, _ := io.ReadAll(w.Body)
	_ = json.Unmarshal(raw, &out)
	return w.Code, out
}

func tool(name, args string) string {
	return `{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"` + name + `","arguments":` + args + `}}`
}

func result(t *testing.T, out map[string]any) (bool, string, map[string]any) {
	t.Helper()
	r, _ := out["result"].(map[string]any)
	isErr, _ := r["isError"].(bool)
	content, _ := r["content"].([]any)
	text := ""
	if len(content) > 0 {
		text, _ = content[0].(map[string]any)["text"].(string)
	}
	sc, _ := r["structuredContent"].(map[string]any)
	return isErr, text, sc
}

func handler(t *testing.T, author string) (*Handler, *sql.DB) {
	store, admin := requesterDB(t)
	return &Handler{
		Store: store, Verifier: verifier{}, Operator: joe, Servers: map[string]bool{guild: true},
		Turns: turns{"abc": {Guild: guild, Channel: channel, Message: "1600000000000000001", Author: author}},
		Log:   slog.New(slog.NewTextHandler(io.Discard, nil)),
	}, admin
}

func TestCreateRequestFromJoesTurn(t *testing.T) {
	h, admin := handler(t, joe)
	code, out := call(t, h, "good", tool("create_request", `{"title":"Add a status page","description":"**Problem:** none."}`))
	isErr, text, sc := result(t, out)
	if code != 200 || isErr || sc["state"] != "pending" || sc["request_id"] == nil {
		t.Fatalf("create: %d %v %s", code, isErr, text)
	}
	// The stored origin is the bridge's turn, whatever the model passed.
	var g, c, m, a string
	if err := admin.QueryRow(`SELECT guild_id, channel_id, message_id, author_id FROM bosun_request`).Scan(&g, &c, &m, &a); err != nil {
		t.Fatal(err)
	}
	if g != guild || c != channel || m != "1600000000000000001" || a != joe {
		t.Fatalf("origin = %s %s %s %s", g, c, m, a)
	}
	// A second call in the same turn files nothing new.
	_, out = call(t, h, "good", tool("create_request", `{"title":"Again","description":"twice"}`))
	if isErr, _, sc2 := result(t, out); isErr || sc2["request_id"] != sc["request_id"] {
		t.Fatalf("second call = %v", out)
	}
	var n int
	if err := admin.QueryRow(`SELECT count(*) FROM bosun_request`).Scan(&n); err != nil || n != 1 {
		t.Fatalf("rows = %d, %v", n, err)
	}
	// Status lists it, with no PRs yet.
	_, out = call(t, h, "good", tool("request_status", `{}`))
	_, _, sc = result(t, out)
	list, _ := sc["requests"].([]any)
	if len(list) != 1 || list[0].(map[string]any)["title"] != "Add a status page" {
		t.Fatalf("status = %v", sc)
	}
}

func TestCreateRequestRefusals(t *testing.T) {
	h, admin := handler(t, "222222222222222222")
	for name, body := range map[string]string{
		"another author":  tool("create_request", `{"title":"t","description":"d"}`),
		"origin argument": tool("create_request", `{"title":"t","description":"d","message_id":"1"}`),
	} {
		_, out := call(t, h, "good", body)
		if isErr, _, _ := result(t, out); !isErr {
			t.Errorf("%s: accepted", name)
		}
	}
	h.Turns.(turns)["abc"] = Origin{Guild: guild, Channel: channel, Message: "1600000000000000002", Author: joe}
	for name, body := range map[string]string{
		"extra argument": tool("create_request", `{"title":"t","description":"d","message_id":"1"}`),
		"empty title":    tool("create_request", `{"title":" ","description":"d"}`),
		"long title":     tool("create_request", `{"title":"`+strings.Repeat("x", MaxTitle+1)+`","description":"d"}`),
	} {
		_, out := call(t, h, "good", body)
		if isErr, _, _ := result(t, out); !isErr {
			t.Errorf("%s: accepted", name)
		}
	}
	h.Servers = map[string]bool{}
	if _, out := call(t, h, "good", tool("request_status", `{}`)); func() bool { e, _, _ := result(t, out); return !e }() {
		t.Error("unlisted server answered")
	}
	if code, _ := call(t, h, "bad", tool("request_status", `{}`)); code != http.StatusUnauthorized {
		t.Errorf("bad token: %d", code)
	}
	var n int
	if err := admin.QueryRow(`SELECT count(*) FROM bosun_request`).Scan(&n); err != nil || n != 0 {
		t.Fatalf("rows = %d, %v", n, err)
	}
}

func TestStatusShowsIssueAndPRs(t *testing.T) {
	h, admin := handler(t, joe)
	_, out := call(t, h, "good", tool("create_request", `{"title":"t","description":"d"}`))
	_, _, sc := result(t, out)
	id := int64(sc["request_id"].(float64))
	if _, err := admin.Exec(`
		INSERT INTO repo_binding (name, owner, repo, base_branch, itsaplan_project, kagent_namespace, kagent_agent, branch_prefix, max_engineering_retries)
			VALUES ('homelab', 'o', 'r', 'main', 'HOME', 'ns', 'coder', 'factory/', 2);
		INSERT INTO work_item (binding_name, issue_id, issue_key, branch, agent_key, kagent_namespace, kagent_agent)
			VALUES ('homelab', 950, 'HOME-950', 'b', 'coder', 'ns', 'coder');
		INSERT INTO pull_request (work_item_id, number, node_id, head_sha, state)
			SELECT id, 8820, 'n', 'h', 'open' FROM work_item;`); err != nil {
		t.Fatal(err)
	}
	if _, err := admin.Exec(`UPDATE bosun_request SET state = 'filed', issue_id = 950, issue_key = 'HOME-950', column_name = 'In Progress' WHERE id = $1`, id); err != nil {
		t.Fatal(err)
	}
	_, out = call(t, h, "good", tool("request_status", `{"request_id":`+jsonInt(id)+`}`))
	isErr, text, sc := result(t, out)
	list, _ := sc["requests"].([]any)
	if isErr || len(list) != 1 {
		t.Fatalf("status: %s", text)
	}
	st := list[0].(map[string]any)
	prs, _ := st["prs"].([]any)
	if st["ticket_url"] != "https://factory-plan.jomcgi.dev/factory/issue/HOME-950" || st["column"] != "In Progress" || len(prs) != 1 ||
		prs[0].(map[string]any)["url"] != "https://github.com/jomcgi-org/homelab/pull/8820" {
		t.Fatalf("status = %v", st)
	}
	// Another server's request ID is not found.
	h.Turns.(turns)["abc"] = Origin{Guild: "999", Channel: channel, Message: "1", Author: joe}
	h.Servers["999"] = true
	_, out = call(t, h, "good", tool("request_status", `{"request_id":`+jsonInt(id)+`}`))
	if isErr, _, _ := result(t, out); !isErr {
		t.Fatal("another server's request was shown")
	}
}

func jsonInt(n int64) string { b, _ := json.Marshal(n); return string(b) }

func TestBridgeTurns(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/sessions/abc":
			w.Write([]byte(`{"server_id":"` + guild + `","channel_id":"` + channel + `","message_id":"5","author_id":"` + joe + `"}`))
		case "/sessions/old":
			w.Write([]byte(`{"server_id":"` + guild + `","channel_id":"` + channel + `"}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()
	b := &BridgeTurns{URL: srv.URL}
	if o, err := b.Turn(context.Background(), "ABC"); err != nil || o.Message != "5" || o.Author != joe {
		t.Fatalf("turn = %+v, %v", o, err)
	}
	for _, id := range []string{"old", "nope", "../x"} {
		if _, err := b.Turn(context.Background(), id); err != ErrNoTurn {
			t.Errorf("%s: %v", id, err)
		}
	}
}

func TestUnrepliedAndMarkReplied(t *testing.T) {
	store, admin := requesterDB(t)
	ctx := context.Background()
	st, err := store.Create(ctx, Origin{Guild: guild, Channel: channel, Message: "9", Author: joe}, "t", "d")
	if err != nil {
		t.Fatal(err)
	}
	// Pending: not listed, and cannot be marked.
	if list, err := store.Unreplied(ctx, []string{guild}); err != nil || len(list) != 0 {
		t.Fatalf("pending listed: %v %v", list, err)
	}
	if err := store.MarkReplied(ctx, st.ID); err == nil {
		t.Fatal("a pending request was marked replied")
	}
	if _, err := admin.Exec(`UPDATE bosun_request SET state = 'filed', issue_id = 951, issue_key = 'HOME-951' WHERE id = $1;`, st.ID); err != nil {
		t.Fatal(err)
	}
	if _, err := admin.Exec(`UPDATE bosun_request SET state = 'done' WHERE id = $1`, st.ID); err != nil {
		t.Fatal(err)
	}
	if list, err := store.Unreplied(ctx, []string{"999"}); err != nil || len(list) != 0 {
		t.Fatalf("other server listed: %v %v", list, err)
	}
	list, err := store.Unreplied(ctx, []string{guild})
	if err != nil || len(list) != 1 || list[0].TicketURL != "https://factory-plan.jomcgi.dev/factory/issue/HOME-951" || list[0].Message != "9" {
		t.Fatalf("unreplied = %+v %v", list, err)
	}
	if err := store.MarkReplied(ctx, st.ID); err != nil {
		t.Fatal(err)
	}
	if list, _ := store.Unreplied(ctx, []string{guild}); len(list) != 0 {
		t.Fatal("still listed after reply")
	}
}
