package requests

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"strconv"
)

// Replies serves the bridge's side (HOME-943): GET /replies lists finished
// requests whose Discord reply is not sent, and POST /replies/{id}/sent
// records one as sent. Only the bridge reaches these paths (Cilium); the
// MCP path is the egress gateway's.
type Replies struct {
	Store interface {
		Unreplied(ctx context.Context, servers []string) ([]Reply, error)
		MarkReplied(ctx context.Context, id int64) error
	}
	Servers []string
	Log     *slog.Logger
}

// Register adds the routes to mux.
func (h *Replies) Register(mux *http.ServeMux) {
	mux.HandleFunc("GET /replies", h.list)
	mux.HandleFunc("POST /replies/{id}/sent", h.sent)
}

func (h *Replies) list(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	out, err := h.Store.Unreplied(r.Context(), h.Servers)
	if err != nil {
		h.Log.Error("replies failed", "err", err)
		w.WriteHeader(http.StatusServiceUnavailable)
		return
	}
	if out == nil {
		out = []Reply{}
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]any{"replies": out})
}

func (h *Replies) sent(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(r.PathValue("id"), 10, 64)
	if err != nil || id <= 0 {
		w.WriteHeader(http.StatusBadRequest)
		return
	}
	if err := h.Store.MarkReplied(r.Context(), id); err != nil {
		h.Log.Error("mark replied failed", "request", id, "err", err)
		w.WriteHeader(http.StatusConflict)
		return
	}
	h.Log.Info("reply sent", "request", id)
	w.WriteHeader(http.StatusNoContent)
}
