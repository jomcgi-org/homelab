package main

import (
	"context"
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

type routeCase struct {
	method, path string
	status       int
	code         string
}

func testDeps(t *testing.T) (*deps, config.Config) {
	t.Helper()
	idp := fakes.NewIdP(t)
	cfg := config.Defaults()
	cfg.Namespace = "factory"
	cfg.ActorJWT.Issuer = idp.URL
	cfg.TokenReview.AdminSubjects = []string{"system:serviceaccount:factory:admin"}
	d, err := build(context.Background(), cfg, fake.NewSimpleClientset(), quota.NewStore(), metrics.New(), slog.Default())
	if err != nil {
		t.Fatal(err)
	}
	return d, cfg
}

func TestHandlerAuthenticatesEveryCredentialRoute(t *testing.T) {
	d, cfg := testDeps(t)
	checkRoutes(t, d.handler(cfg, http.NotFoundHandler()), []routeCase{
		{http.MethodGet, "/healthz", http.StatusOK, ""},
		{http.MethodPost, "/admin/codex/login", http.StatusUnauthorized, "unauthenticated"},
		{http.MethodGet, "/github/connector-token?repository=o/r", http.StatusUnauthorized, "unauthenticated"},
		{http.MethodPost, "/v1/responses", http.StatusUnauthorized, "actor_unauthenticated"},
		{http.MethodGet, "/v1/other", http.StatusNotFound, "not_found"},
	})
}

// With a separate admin listener, the relay port serves no admin, connector
// or metrics route, and the admin port serves no relay route.
func TestSplitListenersSeparateRoutes(t *testing.T) {
	d, cfg := testDeps(t)
	metricsOK := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusOK) })
	checkRoutes(t, d.relayHandler(), []routeCase{
		{http.MethodGet, "/healthz", http.StatusOK, ""},
		{http.MethodPost, "/v1/responses", http.StatusUnauthorized, "actor_unauthenticated"},
		{http.MethodPost, "/admin/codex/login", http.StatusNotFound, "not_found"},
		{http.MethodGet, "/github/connector-token?repository=o/r", http.StatusNotFound, "not_found"},
		{http.MethodGet, "/metrics", http.StatusNotFound, "not_found"},
	})
	checkRoutes(t, d.adminHandler(cfg, metricsOK), []routeCase{
		{http.MethodGet, "/healthz", http.StatusOK, ""},
		{http.MethodGet, "/metrics", http.StatusOK, ""},
		{http.MethodPost, "/admin/codex/login", http.StatusUnauthorized, "unauthenticated"},
		{http.MethodGet, "/github/connector-token?repository=o/r", http.StatusUnauthorized, "unauthenticated"},
		{http.MethodPost, "/v1/responses", http.StatusNotFound, ""},
	})
}

func checkRoutes(t *testing.T, h http.Handler, cases []routeCase) {
	t.Helper()
	for _, tc := range cases {
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
