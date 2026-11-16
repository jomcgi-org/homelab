package main

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/json"
	"encoding/pem"
	"log/slog"
	"maps"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
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
		{http.MethodGet, "/codex/quota", http.StatusUnauthorized, "unauthenticated"},
		{http.MethodGet, "/github/reviewer-token?repository=o/r", http.StatusUnauthorized, "unauthenticated"},
		{http.MethodGet, "/github/reviewer-comments?repository=o/r&number=1&page=1", http.StatusUnauthorized, "unauthenticated"},
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
		{http.MethodGet, "/codex/quota", http.StatusNotFound, "not_found"},
		{http.MethodGet, "/github/reviewer-token?repository=o/r", http.StatusNotFound, "not_found"},
		{http.MethodGet, "/github/reviewer-comments?repository=o/r&number=1&page=1", http.StatusNotFound, "not_found"},
		{http.MethodGet, "/metrics", http.StatusNotFound, "not_found"},
	})
	checkRoutes(t, d.adminHandler(cfg, metricsOK), []routeCase{
		{http.MethodGet, "/healthz", http.StatusOK, ""},
		{http.MethodGet, "/metrics", http.StatusOK, ""},
		{http.MethodPost, "/admin/codex/login", http.StatusUnauthorized, "unauthenticated"},
		{http.MethodGet, "/github/connector-token?repository=o/r", http.StatusUnauthorized, "unauthenticated"},
		{http.MethodGet, "/codex/quota", http.StatusUnauthorized, "unauthenticated"},
		{http.MethodGet, "/github/reviewer-token?repository=o/r", http.StatusUnauthorized, "unauthenticated"},
		{http.MethodGet, "/github/reviewer-comments?repository=o/r&number=1&page=1", http.StatusUnauthorized, "unauthenticated"},
		{http.MethodPost, "/v1/responses", http.StatusNotFound, ""},
		// The comment token is never served; the proxy writes with it.
		{http.MethodGet, "/github/reviewer-comment-token?repository=o/r", http.StatusNotFound, ""},
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
// (503 here, as no reviewer App key is loaded). The connector's routes
// refuse the reviewer, except for the shared read-only quota endpoint.
func TestReviewerTokenAdmitsOnlyReviewerSubjects(t *testing.T) {
	const connector, reviewer = "system:serviceaccount:factory:factory-connector", "system:serviceaccount:factory:factory-reviewer"
	client := fake.NewSimpleClientset()
	client.PrependReactor("create", "tokenreviews", func(action k8stesting.Action) (bool, runtime.Object, error) {
		review := action.(k8stesting.CreateAction).GetObject().(*authenticationv1.TokenReview).DeepCopy()
		user := map[string]string{"connector-token": connector, "reviewer-token": reviewer, "actor-token": "system:serviceaccount:factory:actor"}[review.Spec.Token]
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
		{"/github/reviewer-comments?repository=o/r&number=1&page=1", "connector-token", http.StatusForbidden},
		{"/github/reviewer-comments?repository=o/r&number=1&page=1", "reviewer-token", http.StatusServiceUnavailable},
		{"/github/connector-token?repository=o/r", "reviewer-token", http.StatusForbidden},
		// Both dispatchers can read quota; unrelated subjects remain refused.
		{"/codex/quota", "reviewer-token", http.StatusOK},
		{"/codex/quota", "unknown-token", http.StatusUnauthorized},
		{"/codex/quota", "actor-token", http.StatusForbidden},
		{"/codex/quota", "connector-token", http.StatusOK},
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

// The comment token comes from its own minter, with only the comment
// permissions, so the check token never gains issues write and an App
// without it fails only the comment. An empty map serves none.
func TestCommentMinterHoldsOnlyCommentPermissions(t *testing.T) {
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "key.pem")
	pemKey := pem.EncodeToMemory(&pem.Block{Type: "RSA PRIVATE KEY", Bytes: x509.MarshalPKCS1PrivateKey(key)})
	if err := os.WriteFile(path, pemKey, 0o600); err != nil {
		t.Fatal(err)
	}
	idp := fakes.NewIdP(t)
	cfg := config.Defaults()
	cfg.Namespace = "factory"
	cfg.ActorJWT.Issuer = idp.URL
	cfg.GitHubReview.PrivateKeyFile, cfg.GitHubReview.AppID, cfg.GitHubReview.InstallationID = path, "9", "3"
	cfg.ReviewBindings = []config.Binding{{Name: "homelab", Repository: "o/r", Namespace: "factory-r-homelab-review"}}
	d, err := build(context.Background(), cfg, fake.NewSimpleClientset(), quota.NewStore(), metrics.New(), slog.Default())
	if err != nil {
		t.Fatal(err)
	}
	// Without the bot login there is no comment proxy and no comment token.
	if d.commentMinter != nil || d.comments != nil {
		t.Fatalf("comment minter without an author = %+v", d.commentMinter)
	}
	cfg.GitHubReview.CommentAuthor = "jomcgi-factory-review[bot]"
	d, err = build(context.Background(), cfg, fake.NewSimpleClientset(), quota.NewStore(), metrics.New(), slog.Default())
	if err != nil {
		t.Fatal(err)
	}
	if d.commentMinter == nil || !maps.Equal(d.commentMinter.ConnectorPermissions, map[string]string{"issues": "write", "pull_requests": "write"}) ||
		d.commentMinter.App != d.reviewMinter.App || d.reviewMinter.ConnectorPermissions["issues"] != "" ||
		d.reviewMinter.ConnectorPermissions["pull_requests"] != "read" ||
		d.comments == nil || d.comments.Minter != d.commentMinter || d.comments.Author != "jomcgi-factory-review[bot]" {
		t.Fatalf("comment minter = %+v, review minter permissions %v", d.commentMinter, d.reviewMinter.ConnectorPermissions)
	}
	cfg.GitHubReview.CommentPermissions = map[string]string{}
	d, err = build(context.Background(), cfg, fake.NewSimpleClientset(), quota.NewStore(), metrics.New(), slog.Default())
	if err != nil || d.commentMinter != nil {
		t.Fatalf("comment minter without permissions = %+v, %v", d.commentMinter, err)
	}
}
