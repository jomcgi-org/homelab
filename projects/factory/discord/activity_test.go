package discord

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestPollLaunchAndGuildVotes(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/interactions/42/test/callback":
			if r.URL.Query().Get("with_response") != "true" {
				t.Error("missing callback resource request")
			}
			fmt.Fprint(w, `{"resource":{"activity_instance":{"id":"instance"}}}`)
		case "/users/@me/guilds/200/member":
			switch r.Header.Get("Authorization") {
			case "Bearer one":
				fmt.Fprint(w, `{"user":{"id":"1"}}`)
			case "Bearer two":
				fmt.Fprint(w, `{"user":{"id":"2"}}`)
			default:
				w.WriteHeader(404)
			}
		default:
			t.Errorf("unexpected Discord path %s", r.URL.Path)
			w.WriteHeader(404)
		}
	}))
	defer server.Close()
	a := NewActivities(nil, &REST{Base: server.URL, Token: func() (string, error) { return "test", nil }}, "", nil)
	rep := &fakeReplies{}
	if !a.publish(t.Context(), rep, tagged("43", "poll"), `{"version":"v0.9","updateComponents":{"surfaceId":"poll","components":[{"id":"question","component":"Text","text":"Friday?"},{"id":"option1","component":"Text","text":"yes"},{"id":"option2","component":"Text","text":"no"}]}}`) {
		t.Fatal("poll missing")
	}
	i := Interaction{ID: "42", Type: 3, Token: "test", GuildID: guild, ChannelID: channel}
	i.Data.CustomID = rep.sent[0].Components[1].Components[0].CustomID
	a.Launch(t.Context(), i)
	call := func(method, token, body string) *httptest.ResponseRecorder {
		r := httptest.NewRequest(method, "/activity/instances/instance", strings.NewReader(body))
		r.Header.Set("Authorization", "Bearer "+token)
		w := httptest.NewRecorder()
		a.ServeHTTP(w, r)
		return w
	}
	for _, method := range []string{"GET", "POST"} {
		if w := call(method, "outsider", `"option1"`); w.Code != 403 || w.Body.Len() != 0 {
			t.Fatalf("outsider received %d %q", w.Code, w.Body.String())
		}
	}
	for _, user := range []string{"one", "two", "two"} {
		if w := call("POST", user, `"option1"`); w.Code != 200 {
			t.Fatalf("vote HTTP %d", w.Code)
		}
	}
	for _, user := range []string{"one", "two"} {
		if w := call("GET", user, ""); !strings.Contains(w.Body.String(), `"option1":2`) {
			t.Fatal(w.Body.String())
		}
	}
}
