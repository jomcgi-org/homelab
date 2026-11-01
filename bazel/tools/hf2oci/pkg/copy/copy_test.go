package copy

import (
	"bytes"
	"context"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"net/http/httptest"
	"runtime/debug"
	"strconv"
	"strings"
	"sync"
	"testing"

	"github.com/google/go-containerregistry/pkg/name"
	"github.com/google/go-containerregistry/pkg/registry"
	"github.com/google/go-containerregistry/pkg/v1/remote"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/jomcgi/homelab/bazel/tools/hf2oci/pkg/gguf"
	"github.com/jomcgi/homelab/bazel/tools/hf2oci/pkg/hf"
)

func TestCopySafetensors(t *testing.T) {
	// Mock HF server.
	hfSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == "/api/models/TestOrg/TestModel/tree/abc123def456":
			json.NewEncoder(w).Encode([]hf.TreeEntry{
				{Type: "file", Path: "config.json", Size: 100},
				{Type: "file", Path: "tokenizer.json", Size: 200},
				{Type: "file", Path: "model.safetensors", Size: 1024},
				{Type: "file", Path: "README.md", Size: 50},
			})
		case r.URL.Path == "/api/models/TestOrg/TestModel":
			json.NewEncoder(w).Encode(hf.ModelInfo{ID: "TestOrg/TestModel"})
		case r.URL.Path == "/TestOrg/TestModel/resolve/abc123def456/config.json":
			w.Write([]byte(`{"model_type":"test"}`))
		case r.URL.Path == "/TestOrg/TestModel/resolve/abc123def456/tokenizer.json":
			w.Write([]byte(`{"version":"1.0"}`))
		case r.URL.Path == "/TestOrg/TestModel/resolve/abc123def456/model.safetensors":
			w.Header().Set("Content-Length", "1024")
			w.Write(make([]byte, 1024))
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer hfSrv.Close()

	// In-memory OCI registry.
	reg := registry.New()
	regSrv := httptest.NewServer(reg)
	defer regSrv.Close()

	client := hf.NewClient(hf.WithBaseURL(hfSrv.URL))
	regHost := regSrv.Listener.Addr().String()

	ctx := context.Background()
	result, err := Copy(ctx, Options{
		Repo:       "TestOrg/TestModel",
		Registry:   regHost + "/models",
		Revision:   "abc123def456",
		HFClient:   client,
		RemoteOpts: []remote.Option{},
	})
	require.NoError(t, err)

	assert.Equal(t, regHost+"/models/testorg/testmodel:rev-abc123def456", result.Ref)
	assert.Contains(t, result.Digest, "sha256:")
	assert.False(t, result.Cached)
	assert.Equal(t, "TestOrg/TestModel", result.Repo)
	assert.Equal(t, "abc123def456", result.Revision)
	assert.Equal(t, FormatSafetensors, result.Format)
	assert.Equal(t, 3, result.FileCount) // 2 configs + 1 weight
	assert.Equal(t, int64(1324), result.TotalSize)

	// Verify it was actually pushed.
	ref, err := name.ParseReference(result.Ref)
	require.NoError(t, err)
	idx, err := remote.Index(ref)
	require.NoError(t, err)
	mf, err := idx.IndexManifest()
	require.NoError(t, err)
	assert.Len(t, mf.Manifests, 1, "single-manifest index for arch-independent model weights")
	assert.Equal(t, "TestOrg/TestModel", mf.Annotations["org.huggingface.repo"])
}

func TestCopyGGUF(t *testing.T) {
	hfSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == "/api/models/Qwen/Qwen2.5-GGUF/tree/main":
			json.NewEncoder(w).Encode([]hf.TreeEntry{
				{Type: "file", Path: "model-q4.gguf", Size: 512},
				{Type: "file", Path: "README.md", Size: 50},
			})
		case r.URL.Path == "/api/models/Qwen/Qwen2.5-GGUF":
			json.NewEncoder(w).Encode(hf.ModelInfo{ID: "Qwen/Qwen2.5-GGUF"})
		case r.URL.Path == "/Qwen/Qwen2.5-GGUF/resolve/main/model-q4.gguf":
			w.Header().Set("Content-Length", "512")
			w.Write(make([]byte, 512))
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer hfSrv.Close()

	reg := registry.New()
	regSrv := httptest.NewServer(reg)
	defer regSrv.Close()

	client := hf.NewClient(hf.WithBaseURL(hfSrv.URL))
	regHost := regSrv.Listener.Addr().String()

	result, err := Copy(context.Background(), Options{
		Repo:       "Qwen/Qwen2.5-GGUF",
		Registry:   regHost + "/models",
		Revision:   "main",
		HFClient:   client,
		RemoteOpts: []remote.Option{},
	})
	require.NoError(t, err)

	assert.Equal(t, regHost+"/models/qwen/qwen2.5-gguf:rev-main", result.Ref)
	assert.False(t, result.Cached)
}

func TestCopyCacheHit(t *testing.T) {
	hfSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == "/api/models/Org/Model/tree/rev1":
			json.NewEncoder(w).Encode([]hf.TreeEntry{
				{Type: "file", Path: "model.safetensors", Size: 256},
			})
		case r.URL.Path == "/api/models/Org/Model":
			json.NewEncoder(w).Encode(hf.ModelInfo{ID: "Org/Model"})
		case r.URL.Path == "/Org/Model/resolve/rev1/model.safetensors":
			w.Header().Set("Content-Length", "256")
			w.Write(make([]byte, 256))
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer hfSrv.Close()

	reg := registry.New()
	regSrv := httptest.NewServer(reg)
	defer regSrv.Close()

	client := hf.NewClient(hf.WithBaseURL(hfSrv.URL))
	regHost := regSrv.Listener.Addr().String()

	opts := Options{
		Repo:       "Org/Model",
		Registry:   regHost + "/models",
		Revision:   "rev1",
		HFClient:   client,
		RemoteOpts: []remote.Option{},
	}

	// First copy.
	result1, err := Copy(context.Background(), opts)
	require.NoError(t, err)
	assert.False(t, result1.Cached)

	// Second copy should be a cache hit.
	result2, err := Copy(context.Background(), opts)
	require.NoError(t, err)
	assert.True(t, result2.Cached)
	assert.Equal(t, result1.Digest, result2.Digest)
}

func TestCopyDryRun(t *testing.T) {
	hfSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == "/api/models/Org/Model/tree/abc123":
			json.NewEncoder(w).Encode([]hf.TreeEntry{
				{Type: "file", Path: "model.safetensors", Size: 1024},
			})
		case r.URL.Path == "/api/models/Org/Model":
			json.NewEncoder(w).Encode(hf.ModelInfo{ID: "Org/Model"})
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer hfSrv.Close()

	client := hf.NewClient(hf.WithBaseURL(hfSrv.URL))

	result, err := Copy(context.Background(), Options{
		Repo:     "Org/Model",
		Registry: "ghcr.io/test",
		Revision: "abc123",
		DryRun:   true,
		HFClient: client,
	})
	require.NoError(t, err)
	assert.Equal(t, "ghcr.io/test/org/model:rev-abc123", result.Ref)
	assert.Empty(t, result.Digest)
}

func TestCopyRegistryDeniedPassesCheckAndFailsOnPush(t *testing.T) {
	hfSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == "/api/models/Org/Model/tree/main":
			json.NewEncoder(w).Encode([]hf.TreeEntry{
				{Type: "file", Path: "model.safetensors", Size: 256},
			})
		case r.URL.Path == "/api/models/Org/Model":
			json.NewEncoder(w).Encode(hf.ModelInfo{ID: "Org/Model"})
		case r.URL.Path == "/Org/Model/resolve/main/model.safetensors":
			w.Header().Set("Content-Length", "256")
			w.Write(make([]byte, 256))
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer hfSrv.Close()

	// Registry that rejects all requests with 401 (GHCR behavior for nonexistent packages).
	regSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
	}))
	defer regSrv.Close()

	client := hf.NewClient(hf.WithBaseURL(hfSrv.URL))
	regHost := regSrv.Listener.Addr().String()

	_, err := Copy(context.Background(), Options{
		Repo:       "Org/Model",
		Registry:   regHost + "/models",
		Revision:   "main",
		HFClient:   client,
		RemoteOpts: []remote.Option{},
	})
	require.Error(t, err)
	assert.False(t, IsPermanent(err), "push failure to 401 registry should be transient")
	assert.Contains(t, err.Error(), "pushing", "should fail at push, not at registry check")
}

func TestCopyHF500IsTransient(t *testing.T) {
	hfSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		w.Write([]byte("internal error"))
	}))
	defer hfSrv.Close()

	client := hf.NewClient(hf.WithBaseURL(hfSrv.URL))
	_, err := Copy(context.Background(), Options{
		Repo:       "Org/Model",
		Registry:   "ghcr.io/test",
		Revision:   "main",
		HFClient:   client,
		RemoteOpts: []remote.Option{},
	})
	require.Error(t, err)
	assert.False(t, IsPermanent(err), "HF 500 should be transient (retryable)")
	assert.Contains(t, err.Error(), "listing repo")
}

func TestCopy404IsPermanent(t *testing.T) {
	hfSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNotFound)
		w.Write([]byte(`{"error":"Repository not found"}`))
	}))
	defer hfSrv.Close()

	client := hf.NewClient(hf.WithBaseURL(hfSrv.URL))
	_, err := Copy(context.Background(), Options{
		Repo:       "nonexistent/repo",
		Registry:   "ghcr.io/test",
		Revision:   "main",
		HFClient:   client,
		RemoteOpts: []remote.Option{},
	})
	require.Error(t, err)
	assert.True(t, IsPermanent(err), "404 should be a permanent error")
	assert.Contains(t, err.Error(), "not found (HTTP 404)")
}

func TestCopyHF429IsTransient(t *testing.T) {
	hfSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusTooManyRequests)
		w.Write([]byte(`{"error":"Rate limit exceeded"}`))
	}))
	defer hfSrv.Close()

	client := hf.NewClient(hf.WithBaseURL(hfSrv.URL))
	_, err := Copy(context.Background(), Options{
		Repo:       "Org/Model",
		Registry:   "ghcr.io/test",
		Revision:   "main",
		HFClient:   client,
		RemoteOpts: []remote.Option{},
	})
	require.Error(t, err)
	assert.False(t, IsPermanent(err), "HF 429 should be transient (retryable)")
	assert.Contains(t, err.Error(), "listing repo")
}

func TestCopyMultipleWeightShards(t *testing.T) {
	const shardCount = 8
	const shardSize = 512

	hfSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == "/api/models/Org/ShardedModel/tree/main":
			entries := []hf.TreeEntry{
				{Type: "file", Path: "config.json", Size: 64},
			}
			for i := 1; i <= shardCount; i++ {
				entries = append(entries, hf.TreeEntry{
					Type: "file",
					Path: fmt.Sprintf("model-%05d-of-%05d.safetensors", i, shardCount),
					Size: shardSize,
				})
			}
			json.NewEncoder(w).Encode(entries)
		case r.URL.Path == "/api/models/Org/ShardedModel":
			json.NewEncoder(w).Encode(hf.ModelInfo{ID: "Org/ShardedModel"})
		case r.URL.Path == "/Org/ShardedModel/resolve/main/config.json":
			w.Write([]byte(`{"model_type":"test"}`))
		default:
			// Serve all weight shard requests.
			w.Header().Set("Content-Length", fmt.Sprintf("%d", shardSize))
			w.Write(make([]byte, shardSize))
		}
	}))
	defer hfSrv.Close()

	reg := registry.New()
	regSrv := httptest.NewServer(reg)
	defer regSrv.Close()

	client := hf.NewClient(hf.WithBaseURL(hfSrv.URL))
	regHost := regSrv.Listener.Addr().String()

	// Track progress callbacks to verify all shards are reported.
	var mu sync.Mutex
	var reportedWeights []string

	result, err := Copy(context.Background(), Options{
		Repo:       "Org/ShardedModel",
		Registry:   regHost + "/models",
		Revision:   "main",
		HFClient:   client,
		RemoteOpts: []remote.Option{},
		OnUploadWeight: func(index, total int, filename string) {
			mu.Lock()
			reportedWeights = append(reportedWeights, filename)
			mu.Unlock()
		},
	})
	require.NoError(t, err)

	assert.Contains(t, result.Digest, "sha256:")
	assert.False(t, result.Cached)
	assert.Equal(t, shardCount+1, result.FileCount) // 1 config + 8 weights

	// All shards should be reported (order may vary due to parallelism).
	assert.Len(t, reportedWeights, shardCount)
	for i := 1; i <= shardCount; i++ {
		expected := fmt.Sprintf("model-%05d-of-%05d.safetensors", i, shardCount)
		assert.Contains(t, reportedWeights, expected)
	}

	// Verify pushed index has correct layer count.
	ref, err := name.ParseReference(result.Ref)
	require.NoError(t, err)
	idx, err := remote.Index(ref)
	require.NoError(t, err)
	mf, err := idx.IndexManifest()
	require.NoError(t, err)
	require.Len(t, mf.Manifests, 1)

	img, err := idx.Image(mf.Manifests[0].Digest)
	require.NoError(t, err)
	layers, err := img.Layers()
	require.NoError(t, err)
	assert.Len(t, layers, shardCount+1, "expected %d layers (1 config + %d weights)", shardCount+1, shardCount)
}

func TestCopyWithBaseModel(t *testing.T) {
	hfSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == "/api/models/Emilio407/nllb-200-distilled-1.3B-4bit/tree/main":
			json.NewEncoder(w).Encode([]hf.TreeEntry{
				{Type: "file", Path: "config.json", Size: 100},
				{Type: "file", Path: "model.safetensors", Size: 512},
			})
		case r.URL.Path == "/api/models/Emilio407/nllb-200-distilled-1.3B-4bit":
			json.NewEncoder(w).Encode(hf.ModelInfo{
				ID:     "Emilio407/nllb-200-distilled-1.3B-4bit",
				Author: "Emilio407",
				BaseModels: &hf.BaseModels{
					Relation: "quantized",
					Models:   []hf.BaseModel{{ID: "facebook/nllb-200-distilled-1.3B"}},
				},
			})
		case r.URL.Path == "/Emilio407/nllb-200-distilled-1.3B-4bit/resolve/main/config.json":
			w.Write([]byte(`{"model_type":"test"}`))
		case r.URL.Path == "/Emilio407/nllb-200-distilled-1.3B-4bit/resolve/main/model.safetensors":
			w.Header().Set("Content-Length", "512")
			w.Write(make([]byte, 512))
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer hfSrv.Close()

	reg := registry.New()
	regSrv := httptest.NewServer(reg)
	defer regSrv.Close()

	client := hf.NewClient(hf.WithBaseURL(hfSrv.URL))
	regHost := regSrv.Listener.Addr().String()

	result, err := Copy(context.Background(), Options{
		Repo:       "Emilio407/nllb-200-distilled-1.3B-4bit",
		Registry:   regHost + "/models",
		Revision:   "main",
		HFClient:   client,
		RemoteOpts: []remote.Option{},
	})
	require.NoError(t, err)

	// Derivative model: repo path uses base model, tag uses variant name.
	assert.Equal(t, regHost+"/models/facebook/nllb-200-distilled-1.3b:emilio407-nllb-200-distilled-1.3b-4bit", result.Ref)
	assert.Contains(t, result.Digest, "sha256:")
	assert.False(t, result.Cached)
}

func TestCopyGGUFMultiFileRequiresSelector(t *testing.T) {
	hfSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == "/api/models/bartowski/Model-GGUF/tree/main":
			json.NewEncoder(w).Encode([]hf.TreeEntry{
				{Type: "file", Path: "Model-Q4_K_M.gguf", Size: 1024},
				{Type: "file", Path: "Model-Q8_0.gguf", Size: 2048},
				{Type: "file", Path: "README.md", Size: 50},
			})
		case r.URL.Path == "/api/models/bartowski/Model-GGUF":
			json.NewEncoder(w).Encode(hf.ModelInfo{ID: "bartowski/Model-GGUF"})
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer hfSrv.Close()

	client := hf.NewClient(hf.WithBaseURL(hfSrv.URL))

	// Without file selector, should error about multiple variants.
	_, err := Copy(context.Background(), Options{
		Repo:     "bartowski/Model-GGUF",
		Registry: "ghcr.io/test",
		DryRun:   true,
		HFClient: client,
	})
	require.Error(t, err)
	assert.True(t, IsPermanent(err))
	assert.Contains(t, err.Error(), "2 quantization variants")
	assert.Contains(t, err.Error(), ":filename")
}

func TestCopyGGUFWithFileSelector(t *testing.T) {
	hfSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == "/api/models/bartowski/Model-GGUF/tree/main":
			json.NewEncoder(w).Encode([]hf.TreeEntry{
				{Type: "file", Path: "Model-Q4_K_M.gguf", Size: 1024},
				{Type: "file", Path: "Model-Q8_0.gguf", Size: 2048},
				{Type: "file", Path: "README.md", Size: 50},
			})
		case r.URL.Path == "/api/models/bartowski/Model-GGUF":
			json.NewEncoder(w).Encode(hf.ModelInfo{ID: "bartowski/Model-GGUF"})
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer hfSrv.Close()

	client := hf.NewClient(hf.WithBaseURL(hfSrv.URL))

	// With file selector, should resolve to just one file.
	result, err := Copy(context.Background(), Options{
		Repo:     "bartowski/Model-GGUF",
		Registry: "ghcr.io/test",
		File:     "Model-Q4_K_M",
		DryRun:   true,
		HFClient: client,
	})
	require.NoError(t, err)
	assert.Equal(t, 1, result.FileCount)
	assert.Equal(t, int64(1024), result.TotalSize)
	assert.Equal(t, "ghcr.io/test/bartowski/model-gguf:bartowski-gguf-model-q4-k-m", result.Ref)
}

func TestCopyGGUFWithFileSelectorAndMMProj(t *testing.T) {
	hfSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == "/api/models/bartowski/Model-GGUF/tree/main":
			json.NewEncoder(w).Encode([]hf.TreeEntry{
				{Type: "file", Path: "Model-Q4_K_M.gguf", Size: 1024},
				{Type: "file", Path: "Model-Q8_0.gguf", Size: 2048},
				{Type: "file", Path: "mmproj-BF16.gguf", Size: 512},
				{Type: "file", Path: "README.md", Size: 50},
			})
		case r.URL.Path == "/api/models/bartowski/Model-GGUF":
			json.NewEncoder(w).Encode(hf.ModelInfo{ID: "bartowski/Model-GGUF"})
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer hfSrv.Close()

	client := hf.NewClient(hf.WithBaseURL(hfSrv.URL))

	// With file selector + include-mmproj, should include both the selected weight and mmproj.
	result, err := Copy(context.Background(), Options{
		Repo:          "bartowski/Model-GGUF",
		Registry:      "ghcr.io/test",
		File:          "Model-Q4_K_M",
		IncludeMMProj: true,
		DryRun:        true,
		HFClient:      client,
	})
	require.NoError(t, err)
	assert.Equal(t, 2, result.FileCount)               // model + mmproj
	assert.Equal(t, int64(1024+512), result.TotalSize) // model + mmproj sizes
	assert.Equal(t, "ghcr.io/test/bartowski/model-gguf:bartowski-gguf-model-q4-k-m-mmproj", result.Ref)
}

func TestCopyGGUFMultiFileWithMMProjNoSelectorNeeded(t *testing.T) {
	// When a repo has one model + one mmproj, no file selector should be required.
	hfSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == "/api/models/Org/Model-GGUF/tree/main":
			json.NewEncoder(w).Encode([]hf.TreeEntry{
				{Type: "file", Path: "Model.gguf", Size: 1024},
				{Type: "file", Path: "mmproj-BF16.gguf", Size: 512},
			})
		case r.URL.Path == "/api/models/Org/Model-GGUF":
			json.NewEncoder(w).Encode(hf.ModelInfo{ID: "Org/Model-GGUF"})
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer hfSrv.Close()

	client := hf.NewClient(hf.WithBaseURL(hfSrv.URL))

	// Without file selector, should succeed because only one non-mmproj weight exists.
	result, err := Copy(context.Background(), Options{
		Repo:     "Org/Model-GGUF",
		Registry: "ghcr.io/test",
		DryRun:   true,
		HFClient: client,
	})
	require.NoError(t, err)
	assert.Equal(t, 2, result.FileCount) // model + mmproj
	assert.Equal(t, int64(1024+512), result.TotalSize)
	assert.Equal(t, "ghcr.io/test/org/model-gguf:rev-main-mmproj", result.Ref)
}

func TestCopyGGUFFileSelectorNoMatch(t *testing.T) {
	hfSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == "/api/models/bartowski/Model-GGUF/tree/main":
			json.NewEncoder(w).Encode([]hf.TreeEntry{
				{Type: "file", Path: "Model-Q4_K_M.gguf", Size: 1024},
			})
		case r.URL.Path == "/api/models/bartowski/Model-GGUF":
			json.NewEncoder(w).Encode(hf.ModelInfo{ID: "bartowski/Model-GGUF"})
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer hfSrv.Close()

	client := hf.NewClient(hf.WithBaseURL(hfSrv.URL))

	_, err := Copy(context.Background(), Options{
		Repo:     "bartowski/Model-GGUF",
		Registry: "ghcr.io/test",
		File:     "NonExistent",
		DryRun:   true,
		HFClient: client,
	})
	require.Error(t, err)
	assert.True(t, IsPermanent(err))
	assert.Contains(t, err.Error(), "matched no files")
}

// buildTestGGUF constructs a minimal valid GGUF v3 binary with the given tensors.
// Each tensor is F32 with shape [tensorElements]. Returns the full file bytes.
func buildTestGGUF(t *testing.T, tensorNames []string, tensorElements uint64) []byte {
	t.Helper()
	var buf bytes.Buffer
	le := binary.LittleEndian

	// Header.
	binary.Write(&buf, le, gguf.Magic)
	binary.Write(&buf, le, uint32(3))                // version
	binary.Write(&buf, le, uint64(len(tensorNames))) // tensor count
	binary.Write(&buf, le, uint64(1))                // metadata KV count

	// One metadata KV: general.architecture = "test"
	writeTestString(&buf, le, "general.architecture")
	binary.Write(&buf, le, gguf.MetadataValueTypeSTRING)
	writeTestString(&buf, le, "test")

	// Tensor infos.
	var offset uint64
	for _, name := range tensorNames {
		writeTestString(&buf, le, name)
		binary.Write(&buf, le, uint32(1))                // nDimensions
		binary.Write(&buf, le, tensorElements)           // dimension[0]
		binary.Write(&buf, le, uint32(gguf.GGMLTypeF32)) // type
		binary.Write(&buf, le, offset)                   // offset
		offset += tensorElements * 4                     // F32 = 4 bytes per element
	}

	// Pad to 32-byte alignment.
	for buf.Len()%32 != 0 {
		buf.WriteByte(0)
	}

	// Tensor data (just zeros).
	totalDataSize := uint64(len(tensorNames)) * tensorElements * 4
	buf.Write(make([]byte, totalDataSize))

	return buf.Bytes()
}

func writeTestString(buf *bytes.Buffer, le binary.ByteOrder, s string) {
	binary.Write(buf, le, uint64(len(s)))
	buf.WriteString(s)
}

func TestCopyGGUFSplit(t *testing.T) {
	// Build a GGUF with 4 tensors of 2048 F32 elements each (8KB per tensor, 32KB total).
	ggufData := buildTestGGUF(t, []string{"t0", "t1", "t2", "t3"}, 2048)
	ggufSize := len(ggufData)

	// Mock HF server with Range support.
	hfSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == "/api/models/Org/BigModel-GGUF/tree/main":
			json.NewEncoder(w).Encode([]hf.TreeEntry{
				{Type: "file", Path: "BigModel.gguf", Size: int64(ggufSize)},
			})
		case r.URL.Path == "/api/models/Org/BigModel-GGUF":
			json.NewEncoder(w).Encode(hf.ModelInfo{ID: "Org/BigModel-GGUF"})
		case r.URL.Path == "/Org/BigModel-GGUF/resolve/main/BigModel.gguf":
			rangeHdr := r.Header.Get("Range")
			if rangeHdr != "" {
				// Parse "bytes=start-end".
				parts := strings.SplitN(strings.TrimPrefix(rangeHdr, "bytes="), "-", 2)
				start, _ := strconv.ParseInt(parts[0], 10, 64)
				end, _ := strconv.ParseInt(parts[1], 10, 64)
				if end >= int64(ggufSize) {
					end = int64(ggufSize) - 1
				}
				data := ggufData[start : end+1]
				w.Header().Set("Content-Length", strconv.Itoa(len(data)))
				w.WriteHeader(http.StatusPartialContent)
				w.Write(data)
			} else {
				w.Header().Set("Content-Length", strconv.Itoa(ggufSize))
				w.Write(ggufData)
			}
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer hfSrv.Close()

	reg := registry.New()
	regSrv := httptest.NewServer(reg)
	defer regSrv.Close()

	client := hf.NewClient(hf.WithBaseURL(hfSrv.URL))
	regHost := regSrv.Listener.Addr().String()

	var splitCalled bool
	var splitShardCount int
	var reportedWeights []string
	var mu sync.Mutex

	result, err := Copy(context.Background(), Options{
		Repo:         "Org/BigModel-GGUF",
		Registry:     regHost + "/models",
		Revision:     "main",
		MaxShardSize: 16 * 1024, // 16KB — should split 32KB of tensor data into 2 shards
		HFClient:     client,
		RemoteOpts:   []remote.Option{},
		OnGGUFSplit: func(shards int, file string) {
			splitCalled = true
			splitShardCount = shards
		},
		OnUploadWeight: func(index, total int, filename string) {
			mu.Lock()
			reportedWeights = append(reportedWeights, filename)
			mu.Unlock()
		},
	})
	require.NoError(t, err)

	assert.True(t, splitCalled, "OnGGUFSplit should have been called")
	assert.Equal(t, 2, splitShardCount, "should split into 2 shards")
	assert.Contains(t, result.Digest, "sha256:")
	assert.False(t, result.Cached)

	// Verify shard filenames follow the naming convention.
	assert.Len(t, reportedWeights, 2)
	assert.Contains(t, reportedWeights, "BigModel-00001-of-00002.gguf")
	assert.Contains(t, reportedWeights, "BigModel-00002-of-00002.gguf")

	// Verify the pushed index has correct layer count.
	ref, err := name.ParseReference(result.Ref)
	require.NoError(t, err)
	idx, err := remote.Index(ref)
	require.NoError(t, err)
	mf, err := idx.IndexManifest()
	require.NoError(t, err)
	require.Len(t, mf.Manifests, 1)

	img, err := idx.Image(mf.Manifests[0].Digest)
	require.NoError(t, err)
	layers, err := img.Layers()
	require.NoError(t, err)
	assert.Len(t, layers, 2, "expected 2 layers (2 shard layers, no config)")
}

func TestCopyGGUFNoSplitWhenSmall(t *testing.T) {
	// GGUF smaller than MaxShardSize should not split.
	hfSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == "/api/models/Org/SmallModel-GGUF/tree/main":
			json.NewEncoder(w).Encode([]hf.TreeEntry{
				{Type: "file", Path: "SmallModel.gguf", Size: 512},
			})
		case r.URL.Path == "/api/models/Org/SmallModel-GGUF":
			json.NewEncoder(w).Encode(hf.ModelInfo{ID: "Org/SmallModel-GGUF"})
		case r.URL.Path == "/Org/SmallModel-GGUF/resolve/main/SmallModel.gguf":
			w.Header().Set("Content-Length", "512")
			w.Write(make([]byte, 512))
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer hfSrv.Close()

	reg := registry.New()
	regSrv := httptest.NewServer(reg)
	defer regSrv.Close()

	client := hf.NewClient(hf.WithBaseURL(hfSrv.URL))
	regHost := regSrv.Listener.Addr().String()

	var splitCalled bool
	result, err := Copy(context.Background(), Options{
		Repo:         "Org/SmallModel-GGUF",
		Registry:     regHost + "/models",
		Revision:     "main",
		MaxShardSize: 4 << 30, // 4GB — much larger than the model
		HFClient:     client,
		RemoteOpts:   []remote.Option{},
		OnGGUFSplit: func(shards int, file string) {
			splitCalled = true
		},
	})
	require.NoError(t, err)

	assert.False(t, splitCalled, "OnGGUFSplit should NOT be called for small model")
	assert.Contains(t, result.Digest, "sha256:")

	// Should have 1 layer (single weight, no config).
	ref, err := name.ParseReference(result.Ref)
	require.NoError(t, err)
	idx, err := remote.Index(ref)
	require.NoError(t, err)
	mf, err := idx.IndexManifest()
	require.NoError(t, err)
	require.Len(t, mf.Manifests, 1)
	img, err := idx.Image(mf.Manifests[0].Digest)
	require.NoError(t, err)
	layers, err := img.Layers()
	require.NoError(t, err)
	assert.Len(t, layers, 1, "expected 1 layer for small model")
}

func TestAutoParallel(t *testing.T) {
	// Save and restore the original GOMEMLIMIT.
	original := debug.SetMemoryLimit(-1)
	t.Cleanup(func() { debug.SetMemoryLimit(original) })

	tests := []struct {
		name  string
		limit int64
		want  int
	}{
		{
			name:  "no limit returns 0",
			limit: math.MaxInt64,
			want:  0,
		},
		{
			name:  "4Gi memory limit",
			limit: 4 * (1 << 30) * 4 / 5, // GOMEMLIMIT = 80% of 4Gi ≈ 3.4Gi
			want:  26,                    // 3.4Gi * 80% / 100MB ≈ 26
		},
		{
			name:  "2Gi memory limit",
			limit: 2 * (1 << 30) * 4 / 5, // GOMEMLIMIT = 80% of 2Gi ≈ 1.7Gi
			want:  13,                    // 1.7Gi * 80% / 100MB ≈ 13
		},
		{
			name:  "very small limit clamps to 1",
			limit: 10 << 20, // 10MB
			want:  1,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			debug.SetMemoryLimit(tt.limit)
			got := AutoParallel()
			assert.Equal(t, tt.want, got)
		})
	}
}
