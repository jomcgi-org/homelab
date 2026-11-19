package usage

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"strings"

	"github.com/jomcgi/homelab/projects/factory/broker/config"
	"github.com/jomcgi/homelab/projects/factory/connector/provenance"
)

func (s *Store) RecordReview(ctx context.Context, r provenance.ReviewerRun) (int64, error) {
	var id int64
	err := s.DB.QueryRowContext(ctx, `SELECT public.record_reviewer_run($1,$2,$3,$4,$5,$6,$7,$8,$9)`,
		r.MessageID, r.Agent, r.IssueKey, r.Model, r.ReasoningEffort, r.Arm, r.BaseSHA, r.PromptHash, r.TaskClass).Scan(&id)
	return id, err
}

// ReviewerHandler is mounted only on the admin listener behind TokenReview.
// The existing accounting login can append review evidence through a function;
// neither dispatcher receives new database privileges.
func (s *Store) ReviewerHandler(bindings []config.Binding) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fail := func(code int, reason string) {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(code)
			_ = json.NewEncoder(w).Encode(map[string]string{"factory_error": reason})
		}
		if r.Method != http.MethodPost {
			fail(http.StatusMethodNotAllowed, "method_not_allowed")
			return
		}
		if s == nil || s.DB == nil {
			fail(http.StatusServiceUnavailable, "run_store_unavailable")
			return
		}
		dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, 16<<10))
		dec.DisallowUnknownFields()
		var in provenance.ReviewerRun
		if err := dec.Decode(&in); err != nil {
			fail(http.StatusBadRequest, "invalid_run")
			return
		}
		if err := dec.Decode(new(any)); err != io.EOF {
			fail(http.StatusBadRequest, "invalid_run")
			return
		}
		allowed := false
		for _, b := range bindings {
			if in.Repository == b.Repository && strings.HasPrefix(in.Agent, b.Namespace+"/") && strings.HasPrefix(in.MessageID, "factory-review/"+b.Repository+"/") {
				allowed = true
			}
		}
		if !allowed {
			fail(http.StatusForbidden, "unbound_run")
			return
		}
		if err := in.Record.Validate(); err != nil {
			fail(http.StatusBadRequest, "incomplete_provenance")
			return
		}
		id, err := s.RecordReview(r.Context(), in)
		if err != nil {
			fail(http.StatusServiceUnavailable, "run_record_failed")
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]int64{"id": id})
	})
}
