package discord

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

// findingSessionAgent is a fakeSessionAgent that names a session per key.
type findingSessionAgent struct {
	fakeSessionAgent
	recorded *SessionPlaces
	seen     map[string]bool // session -> recorded before its turn
}

func (f *findingSessionAgent) Session(_ context.Context, key string) (OperatorSession, error) {
	return OperatorSession{ContextID: "0193AB-" + key}, nil
}

func (f *findingSessionAgent) Turn(ctx context.Context, key, id, prompt string) (string, error) {
	_, ok := f.recorded.Lookup("0193ab-" + key)
	f.mu.Lock()
	if f.seen == nil {
		f.seen = map[string]bool{}
	}
	f.seen[key] = ok
	f.mu.Unlock()
	return f.fakeSessionAgent.Turn(ctx, key, id, prompt)
}

// The bridge records a server agent session's server and channel before its
// turn, so the notes tools can name the server from the session alone.
func TestTheBridgeRecordsEachSessionsServerBeforeItsTurn(t *testing.T) {
	b, _, _ := newBridge(&fakeAgent{})
	b.Config.UserLimit.Count = 10
	places := &SessionPlaces{}
	b.SessionPlaces = places
	server := &findingSessionAgent{recorded: places}
	b.Servers = map[string]SessionAgent{guild: server}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	other := tagged("2", "here")
	other.ChannelID = "301"
	b.Handle(ctx, tagged("1", "hi"))
	b.Handle(ctx, other)
	waitFor(t, func() bool { return server.count() == 2 })
	cancel()
	b.Wait()
	server.mu.Lock()
	defer server.mu.Unlock()
	if !server.seen[channel] || !server.seen["301"] {
		t.Fatalf("recorded before turn = %v, want both channels", server.seen)
	}
	// Session IDs match whatever case the actor JWT uses.
	for _, c := range []string{channel, "301"} {
		p, ok := places.Lookup("0193AB-" + c)
		if !ok || p.Server != guild || p.Channel != c {
			t.Errorf("place of %s = %+v, %v", c, p, ok)
		}
	}
}

func TestSessionPlacesServesKnownSessionsOnly(t *testing.T) {
	places := &SessionPlaces{}
	places.Record("abc-1", guild, channel)
	mux := http.NewServeMux()
	mux.Handle("GET /sessions/{id}", places)
	srv := httptest.NewServer(mux)
	defer srv.Close()

	resp, err := http.Get(srv.URL + "/sessions/ABC-1")
	if err != nil {
		t.Fatal(err)
	}
	var p SessionPlace
	_ = json.NewDecoder(resp.Body).Decode(&p)
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK || p.Server != guild || p.Channel != channel {
		t.Fatalf("known session: %d %+v", resp.StatusCode, p)
	}
	resp, err = http.Get(srv.URL + "/sessions/nope")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusNotFound {
		t.Fatalf("unknown session: %d, want 404", resp.StatusCode)
	}
	resp, err = http.Post(srv.URL+"/sessions/abc-1", "application/json", nil)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("POST: %d, want 405", resp.StatusCode)
	}
}

func TestSessionPlacesStaysBounded(t *testing.T) {
	places := &SessionPlaces{}
	for i := range maxSessionPlaces + 10 {
		places.Record(string(rune('a'+i%26))+string(rune(i)), guild, channel)
	}
	places.mu.Lock()
	n := len(places.places)
	places.mu.Unlock()
	if n > maxSessionPlaces {
		t.Fatalf("%d places, want at most %d", n, maxSessionPlaces)
	}
}
