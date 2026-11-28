package discord

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestRequestRepliesAnswerOnceInAllowedServers(t *testing.T) {
	var mu sync.Mutex
	var posts []Reply
	var sent []string
	discordAPI := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		defer mu.Unlock()
		switch {
		case r.URL.Path == "/users/@me":
			w.Write([]byte(`{"id":"9"}`))
		case r.URL.Path == "/channels/700" && r.Method == "GET":
			w.Write([]byte(`{"id":"700","guild_id":"100"}`))
		case r.URL.Path == "/channels/701" && r.Method == "GET":
			w.Write([]byte(`{"id":"701","guild_id":"999"}`))
		case r.URL.Path == "/channels/700/messages" && r.Method == "GET":
			var msgs []HistoryMessage
			for i, p := range posts {
				m := HistoryMessage{Message: Message{ID: snowflakeAt(time.Now(), i+1), Author: User{ID: "9", Bot: true}}}
				for _, c := range p.Components {
					m.Components = append(m.Components, Component{Type: c.Type, Content: c.Content})
				}
				msgs = append([]HistoryMessage{m}, msgs...)
			}
			json.NewEncoder(w).Encode(msgs)
		case r.URL.Path == "/channels/700/messages" && r.Method == "POST":
			var p Reply
			json.NewDecoder(r.Body).Decode(&p)
			posts = append(posts, p)
		default:
			t.Errorf("unexpected Discord call %s %s", r.Method, r.URL.Path)
			w.WriteHeader(404)
		}
	}))
	defer discordAPI.Close()
	ackFails := true
	requestsAPI := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		defer mu.Unlock()
		switch {
		case r.Method == "GET" && r.URL.Path == "/replies":
			w.Write([]byte(`{"replies":[
				{"request_id":1,"guild_id":"100","channel_id":"700","message_id":"800","title":"Add a status page","state":"done",
				 "ticket_url":"https://factory-plan.jomcgi.dev/factory/issue/HOME-950","prs":[{"url":"https://github.com/jomcgi-org/homelab/pull/8820","merged":true}]},
				{"request_id":2,"guild_id":"999","channel_id":"701","message_id":"801","title":"x","state":"done","prs":[]},
				{"request_id":3,"guild_id":"100","channel_id":"701","message_id":"802","title":"y","state":"done","prs":[]}]}`))
		case r.Method == "POST" && strings.HasPrefix(r.URL.Path, "/replies/"):
			if ackFails {
				ackFails = false
				w.WriteHeader(503)
				return
			}
			sent = append(sent, r.URL.Path)
			w.WriteHeader(204)
		default:
			w.WriteHeader(404)
		}
	}))
	defer requestsAPI.Close()
	q := &RequestReplies{
		URL: requestsAPI.URL, REST: &REST{Base: discordAPI.URL, Token: func() (string, error) { return "bosun", nil }},
		Allowed: func(g string) bool { return g == "100" },
		Log:     slog.New(slog.NewTextHandler(io.Discard, nil)),
	}
	// The first acknowledgement is lost; the next poll does not post again.
	if err := q.Poll(context.Background()); err == nil {
		t.Fatal("lost acknowledgement not reported")
	}
	if err := q.Poll(context.Background()); err != nil {
		t.Fatal(err)
	}
	if len(posts) != 1 {
		t.Fatalf("posts = %d, want 1", len(posts))
	}
	p := posts[0]
	if p.MessageReference == nil || p.MessageReference.MessageID != "800" || p.MessageReference.FailIfNotExists ||
		p.Components[0].Content != "Request done: Add a status page\nTicket: https://factory-plan.jomcgi.dev/factory/issue/HOME-950\nPR (merged): https://github.com/jomcgi-org/homelab/pull/8820" ||
		len(p.AllowedMentions.Parse) != 0 || p.AllowedMentions.RepliedUser {
		t.Fatalf("post = %+v", p)
	}
	// The skipped requests are recorded too, so they leave the list.
	if strings.Join(sent, " ") != "/replies/1/sent /replies/2/sent /replies/3/sent" {
		t.Fatalf("sent = %v", sent)
	}
}
