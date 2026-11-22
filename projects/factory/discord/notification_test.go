package discord

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestNotification(t *testing.T) {
	calls := 0
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		var post Reply
		if json.NewDecoder(r.Body).Decode(&post) != nil || len(post.Components) != 1 || post.Components[0].Content != "info: Scheduled agent alert" || post.AllowedMentions.Parse == nil || len(post.AllowedMentions.Parse) != 0 || post.AllowedMentions.RepliedUser || r.URL.Path != "/channels/123/messages" || r.Header.Get("Authorization") != "Bot bosun" {
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
