package copy

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"math"
	"runtime/debug"
	"strings"
	"sync"

	v1 "github.com/google/go-containerregistry/pkg/v1"
	"github.com/google/go-containerregistry/pkg/v1/remote"
	"golang.org/x/sync/errgroup"

	"github.com/jomcgi/homelab/bazel/tools/hf2oci/pkg/gguf"
	"github.com/jomcgi/homelab/bazel/tools/hf2oci/pkg/hf"
	"github.com/jomcgi/homelab/bazel/tools/hf2oci/pkg/oci"
)

const (
	// estimatedMemPerStream is a conservative estimate of peak memory per
	// concurrent shard stream. The parallel downloader's sliding window
	// bounds the pending map to maxAhead (= workers = 8) entries of 10 MB
	// each, plus a 4 MB tar I/O buffer and GGUF shard header:
	//   8 × 10 MB (bounded pending + channel + workers) + ~10 MB overhead ≈ 90 MB
	// Rounded up to 100 MB for safety.
	estimatedMemPerStream = 100 << 20 // 100 MB
)

// AutoParallel calculates a safe concurrency limit from the process's
// GOMEMLIMIT. Returns 0 when no memory limit is configured, signalling
// the caller to fall back to a static default.
func AutoParallel() int {
	// SetMemoryLimit(-1) returns the current soft limit without changing it.
	// When GOMEMLIMIT is unset the runtime returns math.MaxInt64.
	limit := debug.SetMemoryLimit(-1)
	if limit == math.MaxInt64 || limit <= 0 {
		return 0
	}
	// Reserve 20% of the budget for GC overhead, runtime stacks, and
	// non-streaming allocations (config downloads, OCI manifest building, …).
	budget := limit * 4 / 5
	n := int(budget / estimatedMemPerStream)
	if n < 1 {
		return 1
	}
	return n
}

// Options configures the copy operation.
type Options struct {
	Repo          string // HuggingFace repo (e.g. "NousResearch/Hermes-3-8B")
	Registry      string // Target registry (e.g. "ghcr.io/jomcgi/models")
	Revision      string // HF revision (default "main")
	Tag           string // OCI tag override (default "rev-{revision[:12]}")
	ModelDir      string // In-image model path (default "/")
	File          string // GGUF filename prefix selector (e.g. "ModelName-Q4_K_M")
	IncludeMMProj bool   // Include mmproj GGUF alongside file-selected weights
	MaxShardSize  int64  // Max bytes per GGUF shard layer (0 = no splitting)
	MaxParallel   int    // Max concurrent layer uploads/downloads (0 = default 100)
	DryRun        bool

	// Callbacks for progress reporting.
	OnResolve      func(repo, revision string)
	OnClassified   func(configs, weights int, format ModelFormat)
	OnTarget       func(ref string)
	OnCacheHit     func(digest string)
	OnUploadConfig func(count int)
	OnUploadWeight func(index, total int, filename string)
	OnGGUFSplit    func(shards int, originalFile string)
	OnProgress     func(bytesRead, totalSize int64) // periodic per-layer transfer progress (deprecated, use OnShardProgress)

	// OnShardProgress reports per-shard transfer progress with shard context.
	// index is 1-based, total is the number of shards.
	OnShardProgress func(index, total int, bytesRead, shardSize, overallRead, overallSize int64)

	// Injected dependencies (for testing).
	HFClient   *hf.Client
	RemoteOpts []remote.Option
}

// Result contains the output of a successful copy or resolve operation.
// TotalSize reflects HuggingFace source file sizes, not OCI layer sizes
// after tar wrapping.
type Result struct {
	Ref       string      `json:"ref"`
	Digest    string      `json:"digest,omitempty"`
	Cached    bool        `json:"cached"`
	Repo      string      `json:"repo"`
	Revision  string      `json:"revision"`
	Format    ModelFormat `json:"format"`
	FileCount int         `json:"fileCount"`
	TotalSize int64       `json:"totalSize"`
}

// Copy copies a HuggingFace model to an OCI registry.
func Copy(ctx context.Context, opts Options) (*Result, error) {
	if opts.Revision == "" {
		opts.Revision = "main"
	}

	client := opts.HFClient
	if client == nil {
		return nil, fmt.Errorf("HFClient is required")
	}

	if opts.OnResolve != nil {
		opts.OnResolve(opts.Repo, opts.Revision)
	}

	// 1-3. List, classify, filter, derive ref.
	rm, err := resolveModel(ctx, client, opts.Repo, opts.Registry, opts.Revision, opts.Tag, opts.File, opts.IncludeMMProj, opts.RemoteOpts)
	if err != nil {
		return nil, err
	}

	if opts.OnClassified != nil {
		opts.OnClassified(len(rm.configs), len(rm.weights), rm.format)
	}

	if opts.OnTarget != nil {
		opts.OnTarget(rm.refStr)
	}

	if opts.DryRun {
		return &Result{
			Ref:       rm.refStr,
			Repo:      opts.Repo,
			Revision:  opts.Revision,
			Format:    rm.format,
			FileCount: rm.fileCount,
			TotalSize: rm.totalSize,
		}, nil
	}

	// 4. Check cache.
	digest, exists, err := oci.CheckExists(ctx, rm.ref, rm.remoteOpts...)
	if err != nil {
		return nil, wrapRegistryError(err)
	}
	if exists {
		if opts.OnCacheHit != nil {
			opts.OnCacheHit(digest)
		}
		return &Result{
			Ref:       rm.refStr,
			Digest:    digest,
			Cached:    true,
			Repo:      opts.Repo,
			Revision:  opts.Revision,
			Format:    rm.format,
			FileCount: rm.fileCount,
			TotalSize: rm.totalSize,
		}, nil
	}

	// 5. Build config layer.
	modelDir := opts.ModelDir
	if modelDir == "" {
		modelDir = "/"
	}

	var cfgLayer v1.Layer
	if len(rm.configs) > 0 {
		if opts.OnUploadConfig != nil {
			opts.OnUploadConfig(len(rm.configs))
		}
		cfgFiles := make(map[string][]byte)
		for _, c := range rm.configs {
			body, _, err := client.Download(ctx, opts.Repo, opts.Revision, c.Path)
			if err != nil {
				return nil, fmt.Errorf("downloading config %s: %w", c.Path, err)
			}
			data, err := io.ReadAll(body)
			body.Close()
			if err != nil {
				return nil, fmt.Errorf("reading config %s: %w", c.Path, err)
			}
			cfgFiles[c.Path] = data
		}
		cfgLayer, err = oci.ConfigLayer(cfgFiles, modelDir)
		if err != nil {
			return nil, fmt.Errorf("building config layer: %w", err)
		}
	}

	// 6. Build streaming weight layers (parallel connection establishment).
	// NOTE: We use the parent ctx (not the errgroup's derived context) for
	// downloads because the response bodies are consumed lazily during push.
	// The errgroup context is canceled when Wait() returns, which would kill
	// the in-flight reads from the response bodies.
	var weightLayers []v1.Layer

	parallel := opts.MaxParallel
	if parallel <= 0 {
		if n := AutoParallel(); n > 0 {
			parallel = n
		} else {
			parallel = 100
		}
	}

	if rm.format == FormatGGUF && len(rm.weights) == 1 && opts.MaxShardSize > 0 && rm.weights[0].Size > opts.MaxShardSize {
		// GGUF split path: single large file → multiple shard layers.
		weightLayers, err = buildSplitGGUFLayers(ctx, client, opts, rm, modelDir, parallel)
		if err != nil {
			return nil, err
		}
	} else {
		// Existing path: 1 layer per weight file.
		weightLayers = make([]v1.Layer, len(rm.weights))
		g, _ := errgroup.WithContext(ctx)
		g.SetLimit(parallel)
		var progressMu sync.Mutex
		for i, w := range rm.weights {
			i, w := i, w // capture for closure
			g.Go(func() error {
				if opts.OnUploadWeight != nil {
					progressMu.Lock()
					opts.OnUploadWeight(i+1, len(rm.weights), w.Path)
					progressMu.Unlock()
				}
				body, size, err := client.ParallelDownload(ctx, opts.Repo, opts.Revision, w.Path, w.Size)
				if err != nil {
					return fmt.Errorf("downloading weight %s: %w", w.Path, err)
				}
				weightLayers[i] = oci.StreamingWeightLayer(wrapProgress(body, size, opts.OnProgress), size, modelDir, w.Path)
				return nil
			})
		}
		if err := g.Wait(); err != nil {
			return nil, err
		}
	}

	// 7. Build index.
	annotations := map[string]string{
		"org.huggingface.repo":     opts.Repo,
		"org.huggingface.revision": opts.Revision,
	}
	idx, err := oci.BuildIndex(cfgLayer, weightLayers, annotations)
	if err != nil {
		return nil, fmt.Errorf("building OCI index: %w", err)
	}

	// 8. Push with explicit concurrency matching the download parallelism.
	// Without WithJobs, go-containerregistry defaults to runtime.NumCPU()
	// which limits concurrent blob uploads to ~2-4 in a typical pod.
	pushOpts := append(rm.remoteOpts, remote.WithJobs(parallel))
	digest, err = oci.PushIndex(ctx, rm.ref, idx, pushOpts...)
	if err != nil {
		return nil, fmt.Errorf("pushing: %w", err)
	}

	return &Result{
		Ref:       rm.refStr,
		Digest:    digest,
		Repo:      opts.Repo,
		Revision:  opts.Revision,
		Format:    rm.format,
		FileCount: rm.fileCount,
		TotalSize: rm.totalSize,
	}, nil
}

// buildSplitGGUFLayers handles the GGUF split path: probes the file header
// via a range request, plans tensor-boundary splits, and creates one streaming
// OCI layer per shard.
func buildSplitGGUFLayers(ctx context.Context, client *hf.Client, opts Options, rm *resolvedModel, modelDir string, parallel int) ([]v1.Layer, error) {
	w := rm.weights[0]

	// 1. Probe: range-request first 10MB to parse GGUF header.
	probeSize := int64(10 << 20) // 10MB
	probeBody, _, fallback, err := client.DownloadRange(ctx, opts.Repo, opts.Revision, w.Path, 0, probeSize-1)
	if err != nil {
		return nil, fmt.Errorf("probing GGUF header: %w", err)
	}

	if fallback {
		// Server doesn't support range requests — fall back to single layer.
		probeBody.Close()
		return buildSingleWeightLayer(ctx, client, opts, w, modelDir)
	}

	probeData, err := io.ReadAll(probeBody)
	probeBody.Close()
	if err != nil {
		return nil, fmt.Errorf("reading GGUF probe: %w", err)
	}

	// 2. Parse GGUF header, retrying with larger probes if needed.
	const maxProbe = int64(50 << 20) // 50MB cap
	gf, err := gguf.Parse(bytes.NewReader(probeData))
	for err != nil && probeSize < maxProbe {
		probeSize *= 2
		if probeSize > maxProbe {
			probeSize = maxProbe
		}
		retryBody, _, retryFallback, retryErr := client.DownloadRange(ctx, opts.Repo, opts.Revision, w.Path, 0, probeSize-1)
		if retryErr != nil {
			return nil, fmt.Errorf("retrying GGUF header probe (%dMB): %w", probeSize>>20, retryErr)
		}
		if retryFallback {
			retryBody.Close()
			return buildSingleWeightLayer(ctx, client, opts, w, modelDir)
		}
		probeData, err = io.ReadAll(retryBody)
		retryBody.Close()
		if err != nil {
			return nil, fmt.Errorf("reading GGUF retry probe: %w", err)
		}
		gf, err = gguf.Parse(bytes.NewReader(probeData))
	}
	if err != nil {
		return nil, fmt.Errorf("parsing GGUF header: %w", err)
	}

	// 3. Plan splits.
	shards := gguf.PlanSplit(gf, uint64(opts.MaxShardSize))

	if len(shards) <= 1 {
		// Fits in one shard, no splitting needed.
		return buildSingleWeightLayer(ctx, client, opts, w, modelDir)
	}

	if opts.OnGGUFSplit != nil {
		opts.OnGGUFSplit(len(shards), w.Path)
	}

	// 4. Build shard layers in parallel.
	basename := strings.TrimSuffix(w.Path, ".gguf")
	layers := make([]v1.Layer, len(shards))
	g, _ := errgroup.WithContext(ctx)
	g.SetLimit(parallel)
	var progressMu sync.Mutex

	agg := &aggregateProgress{
		totalSize:  w.Size,
		shardCount: len(shards),
		onProgress: opts.OnShardProgress,
	}

	for i, shard := range shards {
		i, shard := i, shard
		g.Go(func() error {
			shardFilename := fmt.Sprintf("%s-%05d-of-%05d.gguf", basename, i+1, len(shards))

			if opts.OnUploadWeight != nil {
				progressMu.Lock()
				opts.OnUploadWeight(i+1, len(shards), shardFilename)
				progressMu.Unlock()
			}

			// Build shard header in memory.
			var headerBuf bytes.Buffer
			if err := gguf.WriteShardHeader(&headerBuf, gf, shard, len(shards)); err != nil {
				return fmt.Errorf("building shard %d header: %w", i+1, err)
			}

			// Range-request this shard's tensor data using parallel connections.
			body, bodySize, err := client.ParallelDownloadRange(ctx, opts.Repo, opts.Revision, w.Path, int64(shard.DataStart), int64(shard.DataEnd))
			if err != nil {
				return fmt.Errorf("downloading shard %d data: %w", i+1, err)
			}

			// Wrap with shard-aware progress (aggregate + per-shard),
			// falling back to the legacy per-layer callback.
			var wrappedBody io.ReadCloser
			if opts.OnShardProgress != nil {
				wrappedBody = agg.wrapShardProgress(body, int64(i+1), bodySize)
			} else {
				wrappedBody = wrapProgress(body, bodySize, opts.OnProgress)
			}

			layers[i] = oci.StreamingSplitGGUFLayer(headerBuf.Bytes(), wrappedBody, bodySize, modelDir, shardFilename)
			return nil
		})
	}

	if err := g.Wait(); err != nil {
		return nil, err
	}

	return layers, nil
}

// buildSingleWeightLayer is a fallback that downloads the full file as one layer.
func buildSingleWeightLayer(ctx context.Context, client *hf.Client, opts Options, w hf.TreeEntry, modelDir string) ([]v1.Layer, error) {
	body, size, err := client.ParallelDownload(ctx, opts.Repo, opts.Revision, w.Path, w.Size)
	if err != nil {
		return nil, fmt.Errorf("downloading weight %s: %w", w.Path, err)
	}
	if opts.OnUploadWeight != nil {
		opts.OnUploadWeight(1, 1, w.Path)
	}
	body = wrapProgress(body, size, opts.OnProgress)
	return []v1.Layer{oci.StreamingWeightLayer(body, size, modelDir, w.Path)}, nil
}

// wrapProgress wraps an io.ReadCloser with periodic progress reporting.
// Reports every 100MB of data read. Returns the original body if onProgress is nil.
func wrapProgress(body io.ReadCloser, total int64, onProgress func(int64, int64)) io.ReadCloser {
	if onProgress == nil {
		return body
	}
	return &progressReader{
		inner:      body,
		total:      total,
		onProgress: onProgress,
		interval:   100 << 20, // 100MB
	}
}

// progressReader wraps an io.ReadCloser and reports bytes transferred periodically.
type progressReader struct {
	inner      io.ReadCloser
	total      int64
	read       int64
	onProgress func(bytesRead, totalSize int64)
	interval   int64
	lastReport int64
}

func (r *progressReader) Read(p []byte) (int, error) {
	n, err := r.inner.Read(p)
	r.read += int64(n)
	if r.read-r.lastReport >= r.interval || err == io.EOF {
		r.onProgress(r.read, r.total)
		r.lastReport = r.read
	}
	return n, err
}

func (r *progressReader) Close() error {
	return r.inner.Close()
}

// aggregateProgress tracks overall transfer progress across all concurrent shards.
type aggregateProgress struct {
	mu         sync.Mutex
	totalSize  int64
	totalRead  int64
	shardCount int
	onProgress func(index, total int, bytesRead, shardSize, overallRead, overallSize int64)
}

// wrapShardProgress wraps a body with per-shard + aggregate progress reporting.
func (a *aggregateProgress) wrapShardProgress(body io.ReadCloser, shardIndex, shardSize int64) io.ReadCloser {
	if a.onProgress == nil {
		return body
	}
	return &shardProgressReader{
		inner:      body,
		shardIndex: int(shardIndex),
		shardSize:  shardSize,
		shardCount: a.shardCount,
		agg:        a,
		interval:   25 << 20, // 25MB — frequent enough for many concurrent shards sharing upload bandwidth
	}
}

type shardProgressReader struct {
	inner      io.ReadCloser
	shardIndex int
	shardSize  int64
	shardCount int
	shardRead  int64
	lastReport int64
	agg        *aggregateProgress
	interval   int64
}

func (r *shardProgressReader) Read(p []byte) (int, error) {
	n, err := r.inner.Read(p)
	r.shardRead += int64(n)

	r.agg.mu.Lock()
	r.agg.totalRead += int64(n)
	overallRead := r.agg.totalRead
	overallSize := r.agg.totalSize
	r.agg.mu.Unlock()

	if r.shardRead-r.lastReport >= r.interval || err == io.EOF {
		r.agg.onProgress(r.shardIndex, r.shardCount, r.shardRead, r.shardSize, overallRead, overallSize)
		r.lastReport = r.shardRead
	}
	return n, err
}

func (r *shardProgressReader) Close() error {
	return r.inner.Close()
}
