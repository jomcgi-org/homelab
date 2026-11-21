package review

import (
	"cmp"
	"encoding/json"
	"net/http"
	"slices"
	"strings"
	"time"
)

// QueuePath is served on the connector-only listener.
const QueuePath = "/v1/review-queue"

type QueueEntry struct {
	Repository  string `json:"repository"`
	PullRequest int    `json:"pullRequest"`
	Head        string `json:"head"`
	Position    int    `json:"position"`
	Resume      bool   `json:"resume"`
	Priority    string `json:"priority,omitempty"`
}

type ActiveReview struct {
	Repository  string `json:"repository"`
	PullRequest int    `json:"pullRequest"`
	Head        string `json:"head"`
}

// QueueSnapshot pairs current worker occupancy with the last observed queue.
// Candidates are ordered by the scheduling pass, including reattachments.
type QueueSnapshot struct {
	ObservedAt time.Time      `json:"observedAt"`
	Complete   bool           `json:"complete"`
	Waiting    []QueueEntry   `json:"waiting"`
	InFlight   int            `json:"inFlight"`
	Capacity   int            `json:"capacity"`
	QuotaHeld  []QueueEntry   `json:"quotaHeld"`
	Active     []ActiveReview `json:"active"`
}

func (e *Engine) QueueSnapshot() QueueSnapshot {
	e.mu.Lock()
	defer e.mu.Unlock()
	snapshot := e.queueSnapshot
	snapshot.Waiting = slices.Clone(snapshot.Waiting)
	snapshot.QuotaHeld = slices.Clone(snapshot.QuotaHeld)
	snapshot.InFlight = len(e.inflight)
	snapshot.Capacity = e.cfg.Concurrency
	snapshot.Active = make([]ActiveReview, 0, len(e.inflight))
	for _, b := range e.cfg.Bindings {
		for head, run := range e.inflight {
			if head.binding == b.Name {
				snapshot.Active = append(snapshot.Active, ActiveReview{
					Repository: b.repo(), PullRequest: run.number, Head: head.sha,
				})
			}
		}
	}
	slices.SortFunc(snapshot.Active, func(a, b ActiveReview) int {
		if byRepo := strings.Compare(a.Repository, b.Repository); byRepo != 0 {
			return byRepo
		}
		if byPR := cmp.Compare(a.PullRequest, b.PullRequest); byPR != 0 {
			return byPR
		}
		return strings.Compare(a.Head, b.Head)
	})
	return snapshot
}

func (e *Engine) ServeQueue(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	snapshot := e.QueueSnapshot()
	if snapshot.ObservedAt.IsZero() {
		http.Error(w, "review queue has not been observed", http.StatusServiceUnavailable)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(snapshot)
}
