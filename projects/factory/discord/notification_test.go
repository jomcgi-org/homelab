package discord

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestNotification(t *testing.T) {
	calls := 0
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/users/@me":
			w.Write([]byte(`{"id":"9"}`))
			return
		case "/channels/123/messages":
			if r.Method == "GET" {
				w.Write([]byte(`[]`))
				return
			}
		}
		calls++
		var post Reply
		if json.NewDecoder(r.Body).Decode(&post) != nil || len(post.Components) != 2 || post.Components[0].Content != "info: Scheduled agent alert" || post.AllowedMentions.Parse == nil || len(post.AllowedMentions.Parse) != 0 || post.AllowedMentions.RepliedUser || r.URL.Path != "/channels/123/messages" || r.Header.Get("Authorization") != "Bot bosun" {
			t.Error("incorrect Discord post")
		}
	}))
	defer api.Close()
	handler := NotificationHandler(map[string]string{"homelab": "123"}, func() (string, error) { return "caller", nil }, &REST{Base: api.URL, Token: func() (string, error) { return "bosun", nil }})
	for _, tc := range []struct {
		user, password, channel, title string
		status                         int
	}{
		{"other", "caller", "homelab", "Scheduled agent alert", 403},
		{"factory-connector", "", "homelab", "Scheduled agent alert", 403},
		{"factory-connector", "caller", "456", "Scheduled agent alert", 403},
		{"factory-connector", "caller", "homelab", "@everyone", 400},
		{"factory-connector", "caller", "homelab", "Scheduled agent alert", 204},
	} {
		req := httptest.NewRequest("POST", "/notify", strings.NewReader(`{"Text":{"Key":"unique","Title":"`+tc.title+`"},"Level":"info","Channel":"`+tc.channel+`"}`))
		req.SetBasicAuth(tc.user, tc.password)
		w := httptest.NewRecorder()
		handler(w, req)
		if w.Code != tc.status {
			t.Errorf("got %d want %d", w.Code, tc.status)
		}
	}
	if calls != 1 {
		t.Fatalf("got %d posts", calls)
	}
}

// fakeChannel is a Discord channel that keeps what Bosun posts, so a fresh
// handler (a restarted process) reads it back.
type fakeChannel struct {
	mu    sync.Mutex
	posts int
	msgs  []HistoryMessage // newest first
}

func snowflakeAt(t time.Time, seq int) string {
	return fmt.Sprint(uint64(t.UnixMilli()-discordEpoch)<<22 | uint64(seq))
}

func (f *fakeChannel) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	switch {
	case r.URL.Path == "/users/@me":
		w.Write([]byte(`{"id":"9"}`))
	case r.URL.Path == "/channels/123/messages" && r.Method == "GET":
		json.NewEncoder(w).Encode(f.msgs)
	case r.URL.Path == "/channels/123/messages" && r.Method == "POST":
		var post Reply
		json.NewDecoder(r.Body).Decode(&post)
		f.posts++
		m := HistoryMessage{Message: Message{ID: snowflakeAt(time.Now(), f.posts), Author: User{ID: "9", Bot: true}}}
		for _, c := range post.Components {
			m.Components = append(m.Components, Component{Type: c.Type, Content: c.Content})
		}
		f.msgs = append([]HistoryMessage{m}, f.msgs...)
	default:
		w.WriteHeader(404)
	}
}

// TestNotificationReplayAfterRestart covers HOME-228-1: a same-day replay of
// a key from a fresh process, after Discord's nonce window, posts nothing.
func TestNotificationReplayAfterRestart(t *testing.T) {
	ch := &fakeChannel{}
	api := httptest.NewServer(ch)
	defer api.Close()
	send := func(key string) int {
		// A new handler per call has no memory of earlier sends.
		handler := NotificationHandler(map[string]string{"homelab": "123"}, func() (string, error) { return "caller", nil }, &REST{Base: api.URL, Token: func() (string, error) { return "bosun", nil }})
		req := httptest.NewRequest("POST", "/notify", strings.NewReader(`{"Text":{"Key":"`+key+`","Title":"Factory notify sink is live"},"Level":"info","Channel":"homelab"}`))
		req.SetBasicAuth("factory-connector", "caller")
		w := httptest.NewRecorder()
		handler(w, req)
		return w.Code
	}
	if send("day-key") != 204 || send("day-key") != 204 {
		t.Fatal("send failed")
	}
	if ch.posts != 1 {
		t.Fatalf("same-day replay: got %d posts want 1", ch.posts)
	}
	if send("other-key") != 204 || ch.posts != 2 {
		t.Fatalf("new key: got %d posts want 2", ch.posts)
	}

	// Another author's copy of the ref line, or Bosun's from an earlier
	// UTC day, does not suppress a post.
	ref := ch.msgs[0].Components[1].Content
	ch.msgs = []HistoryMessage{
		{Message: Message{ID: snowflakeAt(time.Now(), 1), Author: User{ID: "7"}}, Components: []Component{{Type: componentText, Content: ref}}},
		{Message: Message{ID: snowflakeAt(time.Now().Add(-48*time.Hour), 1), Author: User{ID: "9"}}, Components: []Component{{Type: componentText, Content: ref}}},
	}
	ch.posts = 0
	if send("other-key") != 204 || ch.posts != 1 {
		t.Fatalf("spoofed or old ref: got %d posts want 1", ch.posts)
	}
}
