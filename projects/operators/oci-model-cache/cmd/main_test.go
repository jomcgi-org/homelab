/*
Copyright 2025.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package main

// main_test.go covers the success path of hf2ociResolver.Resolve() — specifically
// the field mapping from copy.Result to controller.ResolveResult.
//
// resolver_test.go covers the error-type promotion path (4xx permanent, 5xx/429
// transient). This file completes coverage by verifying that on a successful
// resolve all fields are correctly mapped across the adapter boundary, and that
// the "no weight files found" permanent-error path works for non-HTTP errors.
//
// chart_startup_test.go exercises main() in a subprocess with a local API fixture.

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/google/go-containerregistry/pkg/registry"

	"github.com/jomcgi/homelab/bazel/tools/hf2oci/pkg/hf"
	"github.com/jomcgi/homelab/projects/operators/oci-model-cache/internal/controller"
)

// newHFServer starts a test HuggingFace API server that serves the given tree
// entries for the repo/revision path. Requests for the model info endpoint
// return a minimal ModelInfo. All other paths return 404.
func newHFServer(t *testing.T, repo, revision string, entries []hf.TreeEntry) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		treePath := "/api/models/" + repo + "/tree/" + revision
		infoPath := "/api/models/" + repo
		switch r.URL.Path {
		case treePath:
			if err := json.NewEncoder(w).Encode(entries); err != nil {
				http.Error(w, "encode error", http.StatusInternalServerError)
			}
		case infoPath:
			if err := json.NewEncoder(w).Encode(hf.ModelInfo{ID: repo}); err != nil {
				http.Error(w, "encode error", http.StatusInternalServerError)
			}
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	t.Cleanup(srv.Close)
	return srv
}

// newOCIRegistry starts an in-process OCI registry and returns its host address.
func newOCIRegistry(t *testing.T) string {
	t.Helper()
	reg := registry.New()
	srv := httptest.NewServer(reg)
	t.Cleanup(srv.Close)
	return srv.Listener.Addr().String()
}

// resolveCtx calls r.Resolve with the given parameters under a 10-second deadline.
func resolveCtx(t *testing.T, r *hf2ociResolver, repo, reg, revision, file string) (*controller.ResolveResult, error) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	return r.Resolve(ctx, repo, reg, revision, file)
}

// TestHF2OCIResolver_Resolve_SuccessFieldMapping verifies that every field of
// copy.Result is correctly projected into controller.ResolveResult when the
// model exists in HuggingFace but not yet in the OCI registry (cache-miss path).
//
// This exercises the field mapping in hf2ociResolver.Resolve() — the code path
// that resolver_test.go does not reach because all its test servers return error
// status codes.
func TestHF2OCIResolver_Resolve_SuccessFieldMapping(t *testing.T) {
	const repo = "Org/TestModel"
	const revision = "abc123"

	entries := []hf.TreeEntry{
		{Type: "file", Path: "config.json", Size: 100},
		{Type: "file", Path: "model.safetensors", Size: 4096},
	}

	hfSrv := newHFServer(t, repo, revision, entries)
	regHost := newOCIRegistry(t)

	r := &hf2ociResolver{
		client: hf.NewClient(hf.WithBaseURL(hfSrv.URL)),
	}

	result, err := resolveCtx(t, r, repo, regHost+"/models", revision, "")
	if err != nil {
		t.Fatalf("expected success, got error: %v", err)
	}

	// Ref must be a non-empty OCI reference containing the registry host.
	if result.Ref == "" {
		t.Error("Ref is empty; expected a populated OCI reference")
	}

	// The model is not in the registry yet, so Cached must be false.
	if result.Cached {
		t.Error("Cached is true; expected false for a freshly started registry")
	}

	// Digest should be empty for a cache-miss.
	if result.Digest != "" {
		t.Errorf("Digest is %q; expected empty for cache-miss", result.Digest)
	}

	// Revision must be echoed back from the HF server.
	if result.Revision != revision {
		t.Errorf("Revision = %q; want %q", result.Revision, revision)
	}

	// Format must be "safetensors" (detected from the tree entries).
	const wantFormat = "safetensors"
	if result.Format != wantFormat {
		t.Errorf("Format = %q; want %q", result.Format, wantFormat)
	}

	// FileCount must match the number of tree entries (config + weights = 2).
	const wantFileCount = 2
	if result.FileCount != wantFileCount {
		t.Errorf("FileCount = %d; want %d", result.FileCount, wantFileCount)
	}

	// TotalSize must equal the sum of all entry sizes (100 + 4096 = 4196).
	const wantTotalSize = int64(4196)
	if result.TotalSize != wantTotalSize {
		t.Errorf("TotalSize = %d; want %d", result.TotalSize, wantTotalSize)
	}
}

// TestHF2OCIResolver_Resolve_DefaultRevision verifies that when an empty revision
// is supplied the adapter defaults to "main" (delegated to copy.Resolve).
func TestHF2OCIResolver_Resolve_DefaultRevision(t *testing.T) {
	const repo = "Org/DefaultRevModel"

	entries := []hf.TreeEntry{
		{Type: "file", Path: "model.safetensors", Size: 512},
	}

	// Serve the tree only for "main" — any other revision returns 404.
	hfSrv := newHFServer(t, repo, "main", entries)
	regHost := newOCIRegistry(t)

	r := &hf2ociResolver{client: hf.NewClient(hf.WithBaseURL(hfSrv.URL))}

	// Pass empty revision — copy.Resolve should default it to "main".
	result, err := resolveCtx(t, r, repo, regHost+"/models", "", "")
	if err != nil {
		t.Fatalf("expected success with empty revision defaulting to main, got error: %v", err)
	}

	if result.Revision != "main" {
		t.Errorf("Revision = %q; want %q (default)", result.Revision, "main")
	}
}

// TestHF2OCIResolver_Resolve_GGUFFormat verifies that the Format field is set
// to "gguf" when the tree entries contain a GGUF weight file.
func TestHF2OCIResolver_Resolve_GGUFFormat(t *testing.T) {
	const repo = "Org/GGUFModel"
	const revision = "main"

	entries := []hf.TreeEntry{
		{Type: "file", Path: "config.json", Size: 200},
		{Type: "file", Path: "model-Q4_K_M.gguf", Size: 8192},
	}

	hfSrv := newHFServer(t, repo, revision, entries)
	regHost := newOCIRegistry(t)

	r := &hf2ociResolver{client: hf.NewClient(hf.WithBaseURL(hfSrv.URL))}

	result, err := resolveCtx(t, r, repo, regHost+"/models", revision, "")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	const wantFormat = "gguf"
	if result.Format != wantFormat {
		t.Errorf("Format = %q; want %q", result.Format, wantFormat)
	}
}

// TestHF2OCIResolver_Resolve_NoWeightsReturnsPermanentError verifies that a
// repository with no recognisable weight files results in a PermanentError.
// This exercises the permanent-error promotion path for a non-HTTP classification
// error from the copy package (distinct from the HTTP 4xx path in resolver_test.go).
func TestHF2OCIResolver_Resolve_NoWeightsReturnsPermanentError(t *testing.T) {
	const repo = "Org/NoWeights"
	const revision = "main"

	// Only a README — no weights — so copy.Resolve returns a permanent error.
	entries := []hf.TreeEntry{
		{Type: "file", Path: "README.md", Size: 500},
		{Type: "file", Path: "config.json", Size: 100},
	}

	hfSrv := newHFServer(t, repo, revision, entries)

	r := &hf2ociResolver{client: hf.NewClient(hf.WithBaseURL(hfSrv.URL))}

	// Registry address is never reached; pass any syntactically valid reference.
	_, err := resolveCtx(t, r, repo, "127.0.0.1:0/models", revision, "")
	if err == nil {
		t.Fatal("expected error for repo with no weights, got nil")
	}

	var pe *controller.PermanentError
	if !errors.As(err, &pe) {
		t.Errorf("expected *controller.PermanentError for no-weights repo, got %T: %v", err, err)
	}
}
