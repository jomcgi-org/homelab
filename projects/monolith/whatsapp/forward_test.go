package whatsapp

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"
)

// recordingServer captures the authenticated, decoded payloads it accepts, in
// order, and can be told to fail a given message id a number of times first so a
// retry is exercised.
type recordingServer struct {
	mu       sync.Mutex
	accepted []InboundPayload
	auth     []string
	failLeft map[string]int // message_id -> remaining forced 500s
	done     chan struct{}
	want     int
}

func newRecordingServer(want int, failLeft map[string]int) *recordingServer {
	return &recordingServer{failLeft: failLeft, done: make(chan struct{}), want: want}
}

func (s *recordingServer) handler(w http.ResponseWriter, r *http.Request) {
	body, _ := io.ReadAll(r.Body)
	var p InboundPayload
	_ = json.Unmarshal(body, &p)

	s.mu.Lock()
	defer s.mu.Unlock()
	s.auth = append(s.auth, r.Header.Get("Authorization"))
	if n := s.failLeft[p.MessageID]; n > 0 {
		s.failLeft[p.MessageID] = n - 1
		w.WriteHeader(http.StatusInternalServerError)
		return
	}
	s.accepted = append(s.accepted, p)
	w.WriteHeader(http.StatusOK)
	if len(s.accepted) == s.want {
		close(s.done)
	}
}

func newTestForwarder(ctx context.Context, url, token string) *HTTPForwarder {
	f := NewHTTPForwarder(ctx, url, token, nil, testLogger())
	// Tiny backoff so a retry does not slow the test.
	f.initialBackoff = time.Millisecond
	f.maxBackoff = 2 * time.Millisecond
	return f
}

func TestForwardOrderedAuthenticatedRetried(t *testing.T) {
	// The first delivery of "A" is forced to 500 once; the group worker must retry
	// it and must not advance to "B"/"C" until "A" lands, so ordering holds under
	// a transient failure. Every request carries the bearer.
	rec := newRecordingServer(3, map[string]int{"A": 1})
	srv := httptest.NewServer(http.HandlerFunc(rec.handler))
	defer srv.Close()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	fwd := newTestForwarder(ctx, srv.URL, "tok")

	for _, id := range []string{"A", "B", "C"} {
		fwd.Forward(InboundPayload{GroupJID: "g@g.us", MessageID: id, Text: id})
	}

	select {
	case <-rec.done:
	case <-time.After(5 * time.Second):
		t.Fatal("timed out waiting for 3 accepted deliveries")
	}

	rec.mu.Lock()
	defer rec.mu.Unlock()
	gotOrder := []string{rec.accepted[0].MessageID, rec.accepted[1].MessageID, rec.accepted[2].MessageID}
	want := []string{"A", "B", "C"}
	for i := range want {
		if gotOrder[i] != want[i] {
			t.Fatalf("delivery order = %v, want %v", gotOrder, want)
		}
	}
	// A was retried, so there is at least one more request than accepted messages.
	if len(rec.auth) < 4 {
		t.Errorf("total requests = %d, want >= 4 (a retry of A)", len(rec.auth))
	}
	for _, a := range rec.auth {
		if a != "Bearer tok" {
			t.Errorf("Authorization = %q, want %q", a, "Bearer tok")
		}
	}
}

func TestReactionURLFrom(t *testing.T) {
	// The reaction endpoint is the inbound URL's sibling: swap the trailing path
	// segment. A non-/inbound URL just gets /reaction appended.
	cases := map[string]string{
		"http://monolith/internal/whatsapp/inbound": "http://monolith/internal/whatsapp/reaction",
		"http://localhost:8080":                     "http://localhost:8080/reaction",
	}
	for in, want := range cases {
		if got := reactionURLFrom(in); got != want {
			t.Errorf("reactionURLFrom(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestForwardReactionHitsReactionPath(t *testing.T) {
	// A forwarded reaction is POSTed to the derived reaction endpoint with the
	// bearer and the reaction body, going through the same ordered per-group
	// worker as messages.
	var (
		mu    sync.Mutex
		path  string
		auth  string
		body  ReactionPayload
		gotIt = make(chan struct{})
	)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		mu.Lock()
		path = r.URL.Path
		auth = r.Header.Get("Authorization")
		_ = json.Unmarshal(raw, &body)
		mu.Unlock()
		w.WriteHeader(http.StatusOK)
		close(gotIt)
	}))
	defer srv.Close()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	// srv.URL has no /inbound suffix, so the reaction URL is srv.URL + "/reaction".
	fwd := newTestForwarder(ctx, srv.URL+"/inbound", "tok")

	fwd.ForwardReaction(ReactionPayload{
		GroupJID:        "fam@g.us",
		ReactorJID:      "alice@s.whatsapp.net",
		TargetMessageID: "BOT_MSG_1",
		Emoji:           "👍",
	})

	select {
	case <-gotIt:
	case <-time.After(5 * time.Second):
		t.Fatal("timed out waiting for the reaction delivery")
	}

	mu.Lock()
	defer mu.Unlock()
	if path != "/reaction" {
		t.Errorf("reaction POSTed to %q, want /reaction", path)
	}
	if auth != "Bearer tok" {
		t.Errorf("Authorization = %q, want %q", auth, "Bearer tok")
	}
	if body.TargetMessageID != "BOT_MSG_1" || body.Emoji != "👍" || body.ReactorJID != "alice@s.whatsapp.net" {
		t.Errorf("reaction body = %+v, want target BOT_MSG_1 / 👍 / alice", body)
	}
}

func TestForwardPerGroupWorkersAreIndependent(t *testing.T) {
	// Two groups deliver concurrently; both messages must arrive regardless of
	// order across groups (per-group order is the only guarantee).
	rec := newRecordingServer(2, map[string]int{})
	srv := httptest.NewServer(http.HandlerFunc(rec.handler))
	defer srv.Close()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	fwd := newTestForwarder(ctx, srv.URL, "tok")

	fwd.Forward(InboundPayload{GroupJID: "g1@g.us", MessageID: "x"})
	fwd.Forward(InboundPayload{GroupJID: "g2@g.us", MessageID: "y"})

	select {
	case <-rec.done:
	case <-time.After(5 * time.Second):
		t.Fatal("timed out waiting for both deliveries")
	}

	rec.mu.Lock()
	defer rec.mu.Unlock()
	got := map[string]bool{}
	for _, p := range rec.accepted {
		got[p.MessageID] = true
	}
	if !got["x"] || !got["y"] {
		t.Errorf("accepted = %v, want both x and y", rec.accepted)
	}
}
