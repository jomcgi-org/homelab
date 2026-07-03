package whatsapp

import (
	"encoding/json"
	"net/http"
	"sync/atomic"
)

// State is the gateway's connection lifecycle state, surfaced on /healthz.
type State int32

const (
	// StatePairing is the pre-connected state: the gateway is requesting a
	// pairing code (no stored session) or reconnecting a stored session and has
	// not yet seen the Connected event.
	StatePairing State = iota
	// StateConnected means the WhatsApp socket is live.
	StateConnected
	// StateParked means the device was logged out or banned: the gateway has
	// stopped work and will not retry on its own until an operator intervenes.
	StateParked
)

// String renders the state as the token exposed on the health endpoint.
func (s State) String() string {
	switch s {
	case StateConnected:
		return "connected"
	case StateParked:
		return "parked"
	default:
		return "pairing"
	}
}

// stateHolder is a concurrency-safe cell holding the current State. The gateway
// writes it from its event handler; the health handler reads it from the HTTP
// server goroutine, so it must be atomic.
type stateHolder struct {
	v atomic.Int32
}

func newStateHolder(initial State) *stateHolder {
	h := &stateHolder{}
	h.v.Store(int32(initial))
	return h
}

func (h *stateHolder) set(s State) { h.v.Store(int32(s)) }
func (h *stateHolder) get() State  { return State(h.v.Load()) }

// HealthHandler returns an http.Handler reporting the gateway state as
// `{"state": "connected|pairing|parked"}`. It is a static, dependency-free read
// of the atomic state cell, so it never blocks on the WhatsApp socket or the DB.
func (g *Gateway) HealthHandler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_ = json.NewEncoder(w).Encode(map[string]string{"state": g.state.get().String()})
	})
}
