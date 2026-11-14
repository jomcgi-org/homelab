package main

import (
	"encoding/json"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/jomcgi/homelab/projects/factory/broker/config"
	"github.com/jomcgi/homelab/projects/factory/broker/internal/fakes"
	"github.com/jomcgi/homelab/projects/factory/broker/metrics"
	"github.com/jomcgi/homelab/projects/shared/go/tokenbroker/quota"
	"k8s.io/client-go/kubernetes/fake"
)

func TestHandlerAuthenticatesEveryCredentialRoute(t *testing.T) {
	idp := fakes.NewIdP(t)
	cfg := config.Defaults()
	cfg.Namespace = "factory"
	cfg.ActorJWT.Issuer = idp.URL
	cfg.TokenReview.AdminSubjects = []string{"system:serviceaccount:factory:admin"}
	d, err := build(cfg, fake.NewSimpleClientset(), quota.NewStore(), metrics.New(), slog.Default())
	if err != nil {
		t.Fatal(err)
	}
	h := d.handler(cfg, http.NotFoundHandler())
	for _, tc := range []struct {
		method, path string
		status       int
		code         string
	}{
		{http.MethodGet, "/healthz", http.StatusOK, ""},
		{http.MethodPost, "/admin/codex/login", http.StatusUnauthorized, "unauthenticated"},
		{http.MethodGet, "/github/connector-token?repository=o/r", http.StatusUnauthorized, "unauthenticated"},
		{http.MethodPost, "/v1/responses", http.StatusUnauthorized, "actor_unauthenticated"},
		{http.MethodGet, "/v1/other", http.StatusNotFound, "not_found"},
	} {
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, httptest.NewRequest(tc.method, tc.path, nil))
		if rec.Code != tc.status {
			t.Fatalf("%s %s = %d, want %d", tc.method, tc.path, rec.Code, tc.status)
		}
		if tc.code != "" {
			var body map[string]string
			_ = json.Unmarshal(rec.Body.Bytes(), &body)
			if body["factory_error"] != tc.code {
				t.Fatalf("%s %s factory_error = %q, want %q", tc.method, tc.path, body["factory_error"], tc.code)
			}
		}
	}
}
