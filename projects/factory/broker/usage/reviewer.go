package usage

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
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

func (s *Store) BindReviewTask(ctx context.Context, t provenance.ReviewerTask) error {
	_, err := s.DB.ExecContext(ctx, `SELECT public.bind_reviewer_task($1,$2,$3,$4)`,
		t.MessageID, t.Agent, t.Session, t.Task)
	return err
}

func (s *Store) ReviewTaskMain(ctx context.Context, t provenance.ReviewerTask) (string, error) {
	var main string
	err := s.DB.QueryRowContext(ctx, `SELECT public.reviewer_task_main($1,$2,$3)`,
		t.Agent, t.Session, t.Task).Scan(&main)
	return main, err
}

// ReviewerTaskHandler binds or reads a dispatcher's own exact native task.
// The mounts on the broker admin listener require the reviewer ServiceAccount.
func (s *Store) ReviewerTaskHandler(bindings []config.Binding, bind bool) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		if r.Method != http.MethodPost {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		if s == nil || s.DB == nil {
			http.Error(w, "run store unavailable", http.StatusServiceUnavailable)
			return
		}
		var t provenance.ReviewerTask
		dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4096))
		dec.DisallowUnknownFields()
		if err := dec.Decode(&t); err != nil || dec.Decode(new(any)) != io.EOF ||
			t.Session == "" || t.Task == "" || bind != (t.MessageID != "") {
			http.Error(w, "invalid reviewer task", http.StatusBadRequest)
			return
		}
		namespace, agent, ok := strings.Cut(t.Agent, "/")
		if !ok || agent == "" || strings.Contains(agent, "/") || !matchesReviewBinding(bindings, t.Repository, namespace) {
			http.Error(w, "unbound reviewer task", http.StatusForbidden)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		if bind {
			if !strings.HasPrefix(t.MessageID, "factory-review/"+t.Repository+"/") {
				http.Error(w, "unbound review message", http.StatusForbidden)
				return
			}
			if err := s.BindReviewTask(r.Context(), t); err != nil {
				http.Error(w, "review acceptance unavailable", http.StatusServiceUnavailable)
				return
			}
			_ = json.NewEncoder(w).Encode(struct{}{})
			return
		}
		main, err := s.ReviewTaskMain(r.Context(), t)
		if err != nil && !errors.Is(err, sql.ErrNoRows) {
			http.Error(w, "review pin unavailable", http.StatusServiceUnavailable)
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]string{"main_sha": main})
	})
}

func matchesReviewBinding(bindings []config.Binding, repository, namespace string) bool {
	for _, b := range bindings {
		if repository == b.Repository && namespace == b.Namespace {
			return true
		}
	}
	return false
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
