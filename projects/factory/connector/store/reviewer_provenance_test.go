package store_test

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	brokerconfig "github.com/jomcgi/homelab/projects/factory/broker/config"
	"github.com/jomcgi/homelab/projects/factory/broker/usage"
	"github.com/jomcgi/homelab/projects/factory/connector/github"
	"github.com/jomcgi/homelab/projects/factory/connector/internal/pgtest"
	"github.com/jomcgi/homelab/projects/factory/connector/provenance"
)

type runBearer struct{}

func (runBearer) Token() (string, error) { return "reviewer-test", nil }

func TestReviewerRunThroughBrokerUsesRestrictedDatabaseRole(t *testing.T) {
	st := open(t)
	db := pgtest.AsRole(t, st.DB, "factory_broker_accounting")
	handler := (&usage.Store{DB: db}).ReviewerHandler([]brokerconfig.Binding{{Repository: "org/repo", Namespace: "factory-review"}})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer reviewer-test" {
			t.Error("missing authentication")
		}
		handler.ServeHTTP(w, r)
	}))
	defer srv.Close()
	client := &github.BrokerRuns{URL: srv.URL, Bearer: runBearer{}, HTTP: srv.Client()}
	run := provenance.ReviewerRun{
		MessageID: "factory-review/org/repo/7/head", Repository: "org/repo", Agent: "factory-review/reviewer", IssueKey: "HOME-245",
		Record: provenance.New(provenance.Runtime{Model: "review-model", ReasoningEffort: "xhigh", Arm: "control"}, strings.Repeat("a", 40), "rendered review", "unclassified"),
	}
	for range 2 {
		if err := client.RecordReview(context.Background(), run); err != nil {
			t.Fatal(err)
		}
	}
	var id int64
	var count int
	if err := st.DB.QueryRow(`SELECT min(id),count(*) FROM run WHERE origin='reviewer'`).Scan(&id, &count); err != nil || count != 1 {
		t.Fatalf("rows=%d err=%v", count, err)
	}
	saved, err := st.GetRun(context.Background(), id)
	if err != nil || saved.Provenance != run.Record {
		t.Fatalf("saved=%+v err=%v", saved, err)
	}
	if _, err := db.Exec(`UPDATE run SET model='forged'`); err == nil {
		t.Fatal("broker has direct table access")
	}
	run.Repository = "other/repo"
	if err := client.RecordReview(context.Background(), run); err == nil {
		t.Fatal("accepted unbound repository")
	}
}
