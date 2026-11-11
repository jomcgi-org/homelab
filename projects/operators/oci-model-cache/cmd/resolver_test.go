package main

// resolver_test.go covers hf2ociResolver.Resolve() — the adapter between
// hf2oci's copy.PermanentError and the controller's PermanentError type.
//
// This is the only file in the operator that imports bazel/tools/hf2oci. The critical
// behaviour is the error-type mapping: 4xx non-retryable HF errors must be
// promoted to controller.PermanentError so the controller can avoid infinite
// retry loops. All other errors must pass through as-is.

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/jomcgi/homelab/bazel/tools/hf2oci/pkg/hf"
	"github.com/jomcgi/homelab/projects/operators/oci-model-cache/internal/controller"
)

// newFakeHFServer starts a test HTTP server that returns the given HTTP status
// code for every request, simulating HuggingFace API responses without making
// real network calls. The server is closed automatically when the test ends.
func newFakeHFServer(t *testing.T, statusCode int) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(statusCode)
	}))
	t.Cleanup(srv.Close)
	return srv
}

// newResolver constructs an hf2ociResolver whose HuggingFace client is pointed
// at the given test server URL so no real network traffic is generated.
func newResolver(t *testing.T, srv *httptest.Server) *hf2ociResolver {
	t.Helper()
	hfClient := hf.NewClient(hf.WithBaseURL(srv.URL))
	return &hf2ociResolver{client: hfClient}
}

// resolveWithTimeout calls resolver.Resolve with a short deadline to guard
// against tests hanging when the server is slow.
func resolveWithTimeout(t *testing.T, r *hf2ociResolver) (*controller.ResolveResult, error) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	return r.Resolve(ctx, "Org/TestModel", "ghcr.io/test-registry", "main", "")
}

// TestHF2OCIResolver_Resolve_404_ReturnsPermanentError checks that a 404
// response from the HuggingFace API causes Resolve to return a
// controller.PermanentError. 404 = repo not found; retrying is pointless.
func TestHF2OCIResolver_Resolve_404_ReturnsPermanentError(t *testing.T) {
	srv := newFakeHFServer(t, http.StatusNotFound)
	r := newResolver(t, srv)

	_, err := resolveWithTimeout(t, r)
	if err == nil {
		t.Fatal("expected error for HTTP 404, got nil")
	}

	var pe *controller.PermanentError
	if !errors.As(err, &pe) {
		t.Errorf("expected *controller.PermanentError for 404, got %T: %v", err, err)
	}
}

// TestHF2OCIResolver_Resolve_401_ReturnsPermanentError checks that a 401
// (bad/missing token) is also classified as permanent — re-queuing won't help
// until the token is fixed.
func TestHF2OCIResolver_Resolve_401_ReturnsPermanentError(t *testing.T) {
	srv := newFakeHFServer(t, http.StatusUnauthorized)
	r := newResolver(t, srv)

	_, err := resolveWithTimeout(t, r)
	if err == nil {
		t.Fatal("expected error for HTTP 401, got nil")
	}

	var pe *controller.PermanentError
	if !errors.As(err, &pe) {
		t.Errorf("expected *controller.PermanentError for 401, got %T: %v", err, err)
	}
}

// TestHF2OCIResolver_Resolve_403_ReturnsPermanentError checks that a 403
// (forbidden/private repo) is classified as permanent — also a client error
// that won't fix itself on retry.
func TestHF2OCIResolver_Resolve_403_ReturnsPermanentError(t *testing.T) {
	srv := newFakeHFServer(t, http.StatusForbidden)
	r := newResolver(t, srv)

	_, err := resolveWithTimeout(t, r)
	if err == nil {
		t.Fatal("expected error for HTTP 403, got nil")
	}

	var pe *controller.PermanentError
	if !errors.As(err, &pe) {
		t.Errorf("expected *controller.PermanentError for 403, got %T: %v", err, err)
	}
}

// TestHF2OCIResolver_Resolve_503_ReturnsNonPermanentError checks that a 503
// (service unavailable) is treated as a transient error. The controller should
// requeue and retry after backoff.
func TestHF2OCIResolver_Resolve_503_ReturnsNonPermanentError(t *testing.T) {
	srv := newFakeHFServer(t, http.StatusServiceUnavailable)
	r := newResolver(t, srv)

	_, err := resolveWithTimeout(t, r)
	if err == nil {
		t.Fatal("expected error for HTTP 503, got nil")
	}

	var pe *controller.PermanentError
	if errors.As(err, &pe) {
		t.Errorf("expected non-permanent error for 503 (transient), got *controller.PermanentError")
	}
}

// TestHF2OCIResolver_Resolve_429_ReturnsNonPermanentError checks that a 429
// (too many requests) is treated as transient even though it is a 4xx status.
// The hf client marks 429 as retryable, so it must NOT be wrapped as permanent.
func TestHF2OCIResolver_Resolve_429_ReturnsNonPermanentError(t *testing.T) {
	srv := newFakeHFServer(t, http.StatusTooManyRequests)
	r := newResolver(t, srv)

	_, err := resolveWithTimeout(t, r)
	if err == nil {
		t.Fatal("expected error for HTTP 429, got nil")
	}

	var pe *controller.PermanentError
	if errors.As(err, &pe) {
		t.Errorf("expected non-permanent error for 429 (rate-limited), got *controller.PermanentError")
	}
}

// TestHF2OCIResolver_Resolve_500_ReturnsNonPermanentError checks that an
// internal server error from HuggingFace is treated as transient.
func TestHF2OCIResolver_Resolve_500_ReturnsNonPermanentError(t *testing.T) {
	srv := newFakeHFServer(t, http.StatusInternalServerError)
	r := newResolver(t, srv)

	_, err := resolveWithTimeout(t, r)
	if err == nil {
		t.Fatal("expected error for HTTP 500, got nil")
	}

	var pe *controller.PermanentError
	if errors.As(err, &pe) {
		t.Errorf("expected non-permanent error for 500 (server error), got *controller.PermanentError")
	}
}

// TestHF2OCIResolver_Resolve_PermanentError_WrapsOriginalCause verifies that
// the controller.PermanentError wraps the original hf2oci error so that the
// original message is accessible via .Error().
func TestHF2OCIResolver_Resolve_PermanentError_WrapsOriginalCause(t *testing.T) {
	srv := newFakeHFServer(t, http.StatusNotFound)
	r := newResolver(t, srv)

	_, err := resolveWithTimeout(t, r)
	if err == nil {
		t.Fatal("expected error for HTTP 404, got nil")
	}

	var pe *controller.PermanentError
	if !errors.As(err, &pe) {
		t.Fatalf("expected *controller.PermanentError, got %T", err)
	}

	// The wrapped error must preserve the original error message so that it
	// appears in controller conditions / logs.
	if pe.Error() == "" {
		t.Error("controller.PermanentError.Error() is empty; original cause was lost")
	}
}
