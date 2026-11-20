package discord

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
	"nhooyr.io/websocket"
)

func key() (string, error) { return "tenant", nil }

// The recall carries only the channel's tag, strictly, in the server's bank;
// the retain carries the same tag, the day's document and the channel scope.
func TestHindsightRequestsStayInTheChannelScope(t *testing.T) {
	var mu sync.Mutex
	var calls []string
	bodies := map[string]map[string]any{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer tenant" {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		mu.Lock()
		calls = append(calls, r.Method+" "+r.URL.Path)
		bodies[r.URL.Path] = body
		mu.Unlock()
		if strings.HasSuffix(r.URL.Path, "/recall") {
			_, _ = w.Write([]byte(`{"results":[{"text":"heron"}]}`))
			return
		}
		_, _ = w.Write([]byte(`{}`))
	}))
	defer srv.Close()
	h := &Hindsight{BaseURL: srv.URL, Key: key, RecallTokens: 2000}
	ctx := context.Background()
	got, err := h.Recall(ctx, "g1", "c1", "code word?")
	if err != nil || len(got) != 1 || got[0] != "heron" {
		t.Fatalf("recall = %v, %v", got, err)
	}
	at := time.Date(2026, 11, 17, 23, 30, 0, 0, time.UTC)
	if err := h.Retain(ctx, "g1", "c1", at, "ann: hi"); err != nil {
		t.Fatal(err)
	}
	want := []string{"PUT /v1/default/banks/discord-g1", "POST /v1/default/banks/discord-g1/memories/recall", "POST /v1/default/banks/discord-g1/memories"}
	if strings.Join(calls, ";") != strings.Join(want, ";") {
		t.Fatalf("calls = %v, want %v (the bank is created once)", calls, want)
	}
	recall, _ := json.Marshal(bodies["/v1/default/banks/discord-g1/memories/recall"])
	if string(recall) != `{"max_tokens":2000,"query":"code word?","tags":["channel:c1"],"tags_match":"all_strict"}` {
		t.Errorf("recall body = %s", recall)
	}
	if bodies["/v1/default/banks/discord-g1/memories"]["async"] != false {
		t.Error("retain is asynchronous: the channel's next turn could recall before it is stored")
	}
	items := bodies["/v1/default/banks/discord-g1/memories"]["items"].([]any)
	item, _ := json.Marshal(items[0])
	if string(item) != `{"content":"ann: hi","context":"Discord channel c1","document_id":"discord:c1:2026-11-17","observation_scopes":[["channel:c1"]],"tags":["channel:c1"],"timestamp":"2026-11-17T23:30:00Z","update_mode":"append"}` {
		t.Errorf("retain item = %s", item)
	}
}

// The gateway identifies with the bot token and only guild and message
// intents, delivers READY and MESSAGE_CREATE, and stops on a fatal code.
func TestGatewayIdentifiesDispatchesAndStopsOnAFatalCode(t *testing.T) {
	identified := make(chan map[string]any, 1)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/gateway/bot" {
			_, _ = w.Write([]byte(`{"url":"ws://` + r.Host + `/ws"}`))
			return
		}
		if r.URL.Query().Get("v") != "10" || r.URL.Query().Get("encoding") != "json" {
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		c, err := websocket.Accept(w, r, nil)
		if err != nil {
			return
		}
		ctx := r.Context()
		send := func(s string) { _ = c.Write(ctx, websocket.MessageText, []byte(s)) }
		send(`{"op":10,"d":{"heartbeat_interval":60000}}`)
		_, data, err := c.Read(ctx)
		if err != nil {
			return
		}
		var p struct {
			Op int            `json:"op"`
			D  map[string]any `json:"d"`
		}
		_ = json.Unmarshal(data, &p)
		identified <- p.D
		send(`{"op":0,"s":1,"t":"READY","d":{"session_id":"s1","resume_gateway_url":"ws://` + r.Host + `/ws","user":{"id":"100"}}}`)
		send(`{"op":0,"s":2,"t":"MESSAGE_CREATE","d":{"id":"m1","channel_id":"c1","guild_id":"g1","content":"<@100> hi","author":{"id":"u1"},"mentions":[{"id":"100"}]}}`)
		time.Sleep(100 * time.Millisecond)
		c.Close(4004, "authentication failed")
	}))
	defer srv.Close()
	var ready User
	var msgs []Message
	var mu sync.Mutex
	g := &Gateway{
		REST:      &REST{Base: srv.URL, Token: func() (string, error) { return "bot-token", nil }},
		Token:     func() (string, error) { return "bot-token", nil },
		OnReady:   func(u User) { mu.Lock(); ready = u; mu.Unlock() },
		OnMessage: func(m Message) { mu.Lock(); msgs = append(msgs, m); mu.Unlock() },
		Log:       slog.New(slog.NewTextHandler(io.Discard, nil)),
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	err := g.Run(ctx)
	var fatal *FatalError
	if !errors.As(err, &fatal) || fatal.Code != 4004 {
		t.Fatalf("Run = %v, want the fatal 4004", err)
	}
	id := <-identified
	if id["token"] != "bot-token" || id["intents"] != float64(Intents) || Intents != 513 {
		t.Errorf("identify = %v", id)
	}
	mu.Lock()
	defer mu.Unlock()
	if ready.ID != "100" || len(msgs) != 1 || !msgs[0].MentionsUser("100") || msgs[0].GuildID != "g1" {
		t.Errorf("ready %+v, messages %+v", ready, msgs)
	}
}

// A turn sends one message with an ID from the Discord message, waits for
// the task, returns the answer.
func TestKagentTurnAnswers(t *testing.T) {
	var sent kagent.Message
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/agents/factory-discord-agent/discord" || r.Header.Get("Authorization") != "Bearer kagent-token" {
			w.WriteHeader(http.StatusForbidden)
			return
		}
		var req struct {
			ID     string          `json:"id"`
			Method string          `json:"method"`
			Params json.RawMessage `json:"params"`
		}
		_ = json.NewDecoder(r.Body).Decode(&req)
		task := map[string]any{"id": "t1", "contextId": "ctx1", "status": map[string]any{"state": kagent.StateWorking}}
		if req.Method == "SendMessage" {
			var p struct {
				Message kagent.Message `json:"message"`
			}
			_ = json.Unmarshal(req.Params, &p)
			sent = p.Message
			_ = json.NewEncoder(w).Encode(map[string]any{"jsonrpc": "2.0", "id": req.ID, "result": map[string]any{"task": task}})
			return
		}
		task["status"] = map[string]any{"state": kagent.StateCompleted, "message": map[string]any{
			"messageId": "a1", "role": "ROLE_AGENT", "parts": []any{map[string]any{"text": "heron"}},
		}}
		_ = json.NewEncoder(w).Encode(map[string]any{"jsonrpc": "2.0", "id": req.ID, "result": task})
	}))
	defer srv.Close()
	tokenFile := t.TempDir() + "/token"
	if err := os.WriteFile(tokenFile, []byte("kagent-token"), 0o600); err != nil {
		t.Fatal(err)
	}
	k := &Kagent{
		A2A:   &kagent.A2A{BaseURL: srv.URL, Tokens: &kagent.TokenSource{Path: tokenFile}},
		Agent: kagent.AgentRef{Namespace: "factory-discord-agent", Name: "discord"},
		Poll:  10 * time.Millisecond,
	}
	answer, err := k.Turn(context.Background(), "m1", "prompt")
	if err != nil || answer != "heron" {
		t.Fatalf("turn = %q, %v", answer, err)
	}
	if sent.MessageID != "discord-m1" || sent.Parts[0].Text != "prompt" {
		t.Errorf("sent %+v", sent)
	}
}

// Every limit is required, so a missing value cannot leave spend unbounded.
func TestPolicyRequiresEveryLimit(t *testing.T) {
	full := `{"guilds":{"1":{}},"userLimit":{"count":5,"window":"10m"},"guildLimit":{"count":30,"window":"1h"},"globalLimit":{"count":200,"window":"24h"},"queueDepth":3}`
	c, err := ParsePolicy([]byte(full))
	if err != nil || c.UserLimit != (Limit{5, 10 * time.Minute}) || c.GlobalLimit.Window != 24*time.Hour || c.QueueDepth != 3 {
		t.Fatalf("policy = %+v, %v", c, err)
	}
	if _, ok := c.Guilds["1"]; !ok {
		t.Fatal("guild allowlist lost")
	}
	for _, bad := range []string{
		`{"userLimit":{"count":5,"window":"10m"},"guildLimit":{"count":30,"window":"1h"}}`,
		`{"userLimit":{"count":0,"window":"10m"},"guildLimit":{"count":30,"window":"1h"},"globalLimit":{"count":200,"window":"24h"}}`,
		`{"userLimit":{"count":5,"window":"soon"},"guildLimit":{"count":30,"window":"1h"},"globalLimit":{"count":200,"window":"24h"}}`,
	} {
		if _, err := ParsePolicy([]byte(bad)); err == nil {
			t.Errorf("accepted %s", bad)
		}
	}
}
