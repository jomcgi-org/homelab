package discord

import (
	"context"
	"encoding/json"
	"net/http"
	"strings"
	"sync"
)

// SessionPlaces records the server and channel of each server agent session
// the bridge sends a turn to (HOME-918). The bridge records a session before
// its turn starts, so a tool the session calls in that turn can ask which
// server it serves: the actor JWT names the session, and only this record,
// never the model, names the server. The record is in memory; after a
// restart a session is known again from its next turn, the only time it can
// call a tool.
type SessionPlaces struct {
	mu     sync.Mutex
	places map[string]SessionPlace
}

// SessionPlace is one session's server and channel, and the message and
// author of its current turn. bosun-requests reads the turn's message and
// author, so the request it files cites the message Joe wrote (HOME-941).
type SessionPlace struct {
	Server  string `json:"server_id"`
	Channel string `json:"channel_id"`
	Message string `json:"message_id,omitempty"`
	Author  string `json:"author_id,omitempty"`
}

// maxSessionPlaces bounds the record. Sessions are one per channel, so the
// bound is far above the channels Bosun answers in.
const maxSessionPlaces = 4096

// Record notes that session serves the place's channel, and the place's
// message and author for the turn about to start.
func (s *SessionPlaces) Record(session string, place SessionPlace) {
	if s == nil || session == "" {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.places == nil {
		s.places = map[string]SessionPlace{}
	}
	key := strings.ToLower(session)
	if _, ok := s.places[key]; !ok && len(s.places) >= maxSessionPlaces {
		for k := range s.places { // Drop any one; it returns on its next turn.
			delete(s.places, k)
			break
		}
	}
	s.places[key] = place
}

// Lookup returns a session's server and channel.
func (s *SessionPlaces) Lookup(session string) (SessionPlace, bool) {
	if s == nil {
		return SessionPlace{}, false
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	p, ok := s.places[strings.ToLower(session)]
	return p, ok
}

// ServeHTTP answers GET /sessions/{id} with the session's server and
// channel, or 404. Only bosun-artifacts' notes tools and bosun-requests
// reach it (Cilium).
func (s *SessionPlaces) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	p, ok := s.Lookup(r.PathValue("id"))
	if !ok {
		http.NotFound(w, r)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(p)
}

// SessionFinder is a SessionAgent that names the session a key's turns run
// in (KagentOperator).
type SessionFinder interface {
	Session(ctx context.Context, key string) (OperatorSession, error)
}
