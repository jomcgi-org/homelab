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
	authenticationv1 "k8s.io/api/authentication/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/kubernetes/fake"
	k8stesting "k8s.io/client-go/testing"
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
		{http.MethodGet, "/github/reviewer-token?repository=o/r", http.StatusUnauthorized, "unauthenticated"},
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
		{http.MethodGet, "/github/reviewer-token?repository=o/r", http.StatusNotFound, "not_found"},
		{http.MethodGet, "/metrics", http.StatusNotFound, "not_found"},
	})
	checkRoutes(t, d.adminHandler(cfg, metricsOK), []routeCase{
		{http.MethodGet, "/healthz", http.StatusOK, ""},
		{http.MethodGet, "/metrics", http.StatusOK, ""},
		{http.MethodPost, "/admin/codex/login", http.StatusUnauthorized, "unauthenticated"},
		{http.MethodGet, "/github/connector-token?repository=o/r", http.StatusUnauthorized, "unauthenticated"},
		{http.MethodGet, "/github/reviewer-token?repository=o/r", http.StatusUnauthorized, "unauthenticated"},
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

// The reviewer token route admits only reviewer subjects: the connector,
// the author's identity, is refused, and a reviewer reaches the handler
// (503 here, as no reviewer App key is loaded).
func TestReviewerTokenAdmitsOnlyReviewerSubjects(t *testing.T) {
	const connector, reviewer = "system:serviceaccount:factory:factory-connector", "system:serviceaccount:factory:factory-reviewer"
	client := fake.NewSimpleClientset()
	client.PrependReactor("create", "tokenreviews", func(action k8stesting.Action) (bool, runtime.Object, error) {
		review := action.(k8stesting.CreateAction).GetObject().(*authenticationv1.TokenReview).DeepCopy()
		user := map[string]string{"connector-token": connector, "reviewer-token": reviewer}[review.Spec.Token]
		if user != "" {
			review.Status = authenticationv1.TokenReviewStatus{Authenticated: true, Audiences: review.Spec.Audiences, User: authenticationv1.UserInfo{Username: user}}
		}
		return true, review, nil
	})
	idp := fakes.NewIdP(t)
	cfg := config.Defaults()
	cfg.Namespace = "factory"
	cfg.ActorJWT.Issuer = idp.URL
	cfg.TokenReview.ConnectorSubjects = []string{connector}
	cfg.TokenReview.ReviewerSubjects = []string{reviewer}
	d, err := build(context.Background(), cfg, client, quota.NewStore(), metrics.New(), slog.Default())
	if err != nil {
		t.Fatal(err)
	}
	h := d.adminHandler(cfg, http.NotFoundHandler())
	for _, tc := range []struct {
		path, token string
		status      int
	}{
		{"/github/reviewer-token?repository=o/r", "connector-token", http.StatusForbidden},
		{"/github/reviewer-token?repository=o/r", "reviewer-token", http.StatusServiceUnavailable},
		{"/github/connector-token?repository=o/r", "reviewer-token", http.StatusForbidden},
	} {
		req := httptest.NewRequest(http.MethodGet, tc.path, nil)
		req.Header.Set("Authorization", "Bearer "+tc.token)
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)
		if rec.Code != tc.status {
			t.Fatalf("%s with %s = %d, want %d", tc.path, tc.token, rec.Code, tc.status)
		}
	}
}
