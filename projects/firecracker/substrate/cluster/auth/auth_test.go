package auth

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
)

// fakeReviewer maps a token to a username, or an error, so the middleware can
// be exercised without a Kubernetes API server.
type fakeReviewer struct {
	username string
	err      error
	sawToken string
}

func (f *fakeReviewer) Review(_ context.Context, token string) (string, error) {
	f.sawToken = token
	return f.username, f.err
}

// nextRecorder is the wrapped handler; it records whether it was reached.
type nextRecorder struct{ called bool }

func (n *nextRecorder) ServeHTTP(w http.ResponseWriter, _ *http.Request) {
	n.called = true
	w.WriteHeader(http.StatusOK)
}

const allowedCaller = "system:serviceaccount:monolith:monolith"

func newMiddleware(rev Reviewer) (*nextRecorder, http.Handler) {
	next := &nextRecorder{}
	return next, Middleware(next, rev, []string{allowedCaller}, nil)
}

func TestHealthzBypassesAuth(t *testing.T) {
	rev := &fakeReviewer{err: errors.New("must not be called")}
	next, h := newMiddleware(rev)

	req := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, req)

	if rr.Code != http.StatusOK {
		t.Fatalf("healthz status = %d, want 200", rr.Code)
	}
	if !next.called {
		t.Fatal("healthz did not reach next handler")
	}
	if rev.sawToken != "" {
		t.Fatal("healthz should not invoke the reviewer")
	}
}

func TestMissingTokenIsUnauthorized(t *testing.T) {
	next, h := newMiddleware(&fakeReviewer{username: allowedCaller})

	req := httptest.NewRequest(http.MethodPost, "/invoke/agent", nil)
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, req)

	if rr.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, want 401", rr.Code)
	}
	if next.called {
		t.Fatal("next reached despite missing token")
	}
}

func TestMalformedAuthorizationHeaderIsUnauthorized(t *testing.T) {
	for _, h := range []string{"token abc", "Bearer", "Bearer ", "Basic abc"} {
		next, mw := newMiddleware(&fakeReviewer{username: allowedCaller})
		req := httptest.NewRequest(http.MethodPost, "/invoke/agent", nil)
		req.Header.Set("Authorization", h)
		rr := httptest.NewRecorder()
		mw.ServeHTTP(rr, req)
		if rr.Code != http.StatusUnauthorized {
			t.Fatalf("header %q: status = %d, want 401", h, rr.Code)
		}
		if next.called {
			t.Fatalf("header %q: next reached", h)
		}
	}
}

func TestValidAllowedCallerPasses(t *testing.T) {
	rev := &fakeReviewer{username: allowedCaller}
	next, h := newMiddleware(rev)

	req := httptest.NewRequest(http.MethodPost, "/invoke/agent", nil)
	req.Header.Set("Authorization", "Bearer good-token")
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, req)

	if rr.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rr.Code)
	}
	if !next.called {
		t.Fatal("allowed caller did not reach next handler")
	}
	if rev.sawToken != "good-token" {
		t.Fatalf("reviewer saw token %q, want good-token", rev.sawToken)
	}
}

func TestCaseInsensitiveBearerScheme(t *testing.T) {
	next, h := newMiddleware(&fakeReviewer{username: allowedCaller})
	req := httptest.NewRequest(http.MethodPost, "/invoke/agent", nil)
	req.Header.Set("Authorization", "bearer good-token")
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, req)
	if rr.Code != http.StatusOK || !next.called {
		t.Fatalf("lowercase bearer scheme rejected: status=%d called=%v", rr.Code, next.called)
	}
}

func TestAuthenticatedButNotAllowedIsForbidden(t *testing.T) {
	rev := &fakeReviewer{username: "system:serviceaccount:other:intruder"}
	next, h := newMiddleware(rev)

	req := httptest.NewRequest(http.MethodPost, "/invoke/agent", nil)
	req.Header.Set("Authorization", "Bearer valid-but-wrong-sa")
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, req)

	if rr.Code != http.StatusForbidden {
		t.Fatalf("status = %d, want 403", rr.Code)
	}
	if next.called {
		t.Fatal("next reached for unauthorized caller")
	}
}

func TestReviewerErrorIsUnauthorized(t *testing.T) {
	rev := &fakeReviewer{err: errors.New("token expired")}
	next, h := newMiddleware(rev)

	req := httptest.NewRequest(http.MethodPost, "/invoke/agent", nil)
	req.Header.Set("Authorization", "Bearer expired")
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, req)

	if rr.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, want 401", rr.Code)
	}
	if next.called {
		t.Fatal("next reached despite reviewer error")
	}
}
