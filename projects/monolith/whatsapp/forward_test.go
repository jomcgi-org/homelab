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
