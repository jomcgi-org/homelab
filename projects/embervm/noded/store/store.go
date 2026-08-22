// Package store is embervm-noded's off-node durability client (R6): a stdlib
// net/http client against an S3-API object store (SeaweedFS in-cluster is the
// first backend) that moves banked artifacts between node disk and the store.
// It is the mechanism behind the continuity verbs (ExportArtifact /
// RestoreArtifact / EvictArtifact) so a node (or its NVMe) can be lost without
// losing data.
//
// Standing decision 5: this is a raw S3-API client (PUT/GET/HEAD/DELETE against
// <endpoint>/<bucket>/<key>), not the aws-sdk-go. Optional static credentials
// enable SigV4 signing; absent credentials preserve anonymous requests exactly.
//
// Artifact layout (Fork 3): every artifact is a set of files under a key prefix
// <kind>/<workload>/<ref> (kind lowercase; ref MAY be empty for a singleton
// VOLUME, so the prefix collapses to volume/<workload>). A meta.json
// completeness marker (per-file sizes + SHA-256, plus a generation and a
// created-at) is the LAST object written on export and the FIRST read on
// restore or a presence check, so a partially-written or partially-deleted
// artifact is invisible: no meta.json means "not present".
package store

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"encoding/xml"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/jomcgi/homelab/projects/embervm/noded/sparse"
	"github.com/klauspost/compress/zstd"
)

// metaObject is the object key (within an artifact prefix) of the completeness
// marker. It is written LAST on export and read FIRST on restore/presence: a
// prefix without it is an incomplete (or absent) artifact.
const metaObject = "meta.json"

// reachableTimeout bounds the cheap bucket-root HEAD the reachability probe
// issues, so an unreachable store fails the probe fast rather than hanging the
// caller (the probe feeds NodeStatus.store_reachable, a warmth hint, never a
// gate).
const reachableTimeout = 3 * time.Second

// ErrNotPresent is the sentinel a Restore (or a meta fetch) returns when the
// store holds no meta.json for the prefix (the artifact is absent or was only
// partially written). The verb handler maps it to codes.FailedPrecondition so a
// restore of an incomplete store copy surfaces loudly instead of overwriting
// local state with bad bytes.
var ErrNotPresent = errors.New("store: artifact not present (no meta.json)")

// ErrStaleGeneration is returned by Export when the store already holds a copy of
// this artifact at a HIGHER generation than the local one. Exporting anyway would
// overwrite newer durable state with older bytes.
//
// This matters most for VOLUME, which keys as a SINGLETON (volume/<workload>: no
// ref, no vendor segment), so every node that has ever held the volume writes the
// SAME object. Observed 2026-07-28 on demo-postgres: node-1 exported
// generation 5076 while node-2 exported generation 1472 to the same key, minutes
// apart. Whichever landed last won, so a node carrying a long-stale copy could
// silently replace the live one -- for a Postgres volume, that is data loss.
//
// The generation was already the fence everywhere else: the control plane
// quarantines a stateful volume whose reported generation runs ahead of the
// blessed watermark, and meta.json has carried `generation` since the artifact
// layout was introduced. Export simply never compared it, short-circuiting only
// on byte-identical content -- which is exactly the case that does NOT arise when
// two nodes have diverged.
var ErrStaleGeneration = errors.New("store: refusing export, store holds a newer generation")

// FileMeta is one file's completeness record within an artifact's meta.json:
// its exact byte size and hex SHA-256, verified on restore so a corrupt or
// truncated object never overwrites good local bytes.
type FileMeta struct {
	Size        int64  `json:"size"`
	Sha256      string `json:"sha256"`
	Compression string `json:"compression,omitempty"`
}

// Meta is the artifact completeness marker, JSON-serialized as meta.json and
// written LAST on export. Files maps each artifact file's base name to its size
// and checksum; Generation carries the volume generation for a VOLUME artifact
// (0 for kinds with no generation); CreatedAtUnixMs records when the export ran.
// CpuVendor and CpuTemplate (PR-E) stamp the exporting node's cpu_sku onto the
// artifact; both are omitempty so an artifact exported before PR-E landed
// serializes with NEITHER field present, which is exactly the grandfather
// rule's UNSTAMPED case (never a present-but-empty string, which JSON cannot
// distinguish from "not stamped" anyway, but omitempty keeps old fixtures and
// new code honest about the same thing).
type Meta struct {
	Files           map[string]FileMeta `json:"files"`
	Generation      uint64              `json:"generation"`
	CreatedAtUnixMs int64               `json:"createdAtUnixMs"`
	CpuVendor       string              `json:"cpuVendor,omitempty"`
	CpuTemplate     string              `json:"cpuTemplate,omitempty"`
}

// Store is the S3-API object-store client. It is safe for concurrent use (the
// *http.Client is). A nil *Store means the store is disabled (New returns nil on
// an empty endpoint); every method is nil-safe so callers can hold a nil Store
// and let the verb handlers refuse with FAILED_PRECONDITION rather than panic.
type Store struct {
	endpoint    string // base URL, no trailing slash (e.g. http://seaweedfs-s3...:8333)
	bucket      string
	client      *http.Client
	compress    bool
	credentials credentials
	now         func() time.Time
}

// Option configures an optional Store capability.
type Option func(*Store)

// WithCredentials enables SigV4 signing with a static S3 identity.
func WithCredentials(accessKeyID, secretAccessKey string) Option {
	return func(s *Store) {
		s.credentials = credentials{accessKeyID: accessKeyID, secretAccessKey: secretAccessKey}
	}
}

// New builds a Store for endpoint + bucket. It returns nil when endpoint is
// empty, which every caller reads as "the store is disabled" (export is skipped,
// restore-on-miss is impossible, and the continuity verbs refuse). The endpoint
// is normalised to have no trailing slash so key joins are unambiguous. The
// compress enables zstd for newly exported objects.
func New(endpoint, bucket string, compress bool, opts ...Option) *Store {
	if endpoint == "" {
		return nil
	}
	s := &Store{
		endpoint: strings.TrimRight(endpoint, "/"),
		bucket:   bucket,
		client:   &http.Client{},
		compress: compress,
		now:      time.Now,
	}
	for _, opt := range opts {
		opt(s)
	}
	return s
}

// countingReader counts the bytes actually read out of a request body, so an
// export can report compressed bytes moved without knowing the encoded size in
// advance.
//
// Close is NOT optional and must not be dropped. net/http wraps a plain
// io.Reader body in io.NopCloser, so a counting reader that only implements
// Read would swallow the underlying Close. On the compressed path the body is
// an *io.PipeReader, and the transport closing it is the ONLY thing that
// unblocks the encoder goroutine when a request is aborted mid-body (a failed
// PUT, a cancelled context). Without this method that goroutine blocks forever
// on pw.Write, leaking itself and its encoder buffers on every failed export.
type countingReader struct {
	io.Reader
	n int64
}

func (r *countingReader) Read(p []byte) (int, error) {
	n, err := r.Reader.Read(p)
	r.n += int64(n)
	return n, err
}

func (r *countingReader) Close() error {
	if c, ok := r.Reader.(io.Closer); ok {
		return c.Close()
	}
	return nil
}

// url composes the object URL for a key under the bucket. The key is used
// verbatim (callers build it from a Fork-3 prefix plus a file base name, both
// filesystem-safe), so no escaping is applied beyond joining with slashes.
func (s *Store) url(key string) string {
	return s.endpoint + "/" + s.bucket + "/" + strings.TrimLeft(key, "/")
}

// ---- raw object operations -------------------------------------------------

// Put uploads size bytes read from r to the object key (HTTP PUT). size is sent
// as Content-Length so the store can size the object without buffering; a
// negative size lets net/http chunk it. Any non-2xx status is an error.
func (s *Store) Put(ctx context.Context, key string, r io.Reader, size int64) error {
	if s == nil {
		return ErrNotPresent
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPut, s.url(key), r)
	if err != nil {
		return fmt.Errorf("store: build PUT %q: %w", key, err)
	}
	if size >= 0 {
		req.ContentLength = size
	}
	req.Header.Set("Content-Type", "application/octet-stream")
	if err := s.sign(req); err != nil {
		return fmt.Errorf("store: sign PUT %q: %w", key, err)
	}
	resp, err := s.client.Do(req)
	if err != nil {
		return fmt.Errorf("store: PUT %q: %w", key, err)
	}
	defer drainClose(resp.Body)
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("store: PUT %q: unexpected status %d", key, resp.StatusCode)
	}
	return nil
}

// Get fetches the object key (HTTP GET) and returns its body plus the reported
// size (Content-Length, -1 when unknown). The caller MUST close the returned
// reader. A 404 is reported as ErrNotPresent so callers can distinguish a
// missing object from a transport failure.
func (s *Store) Get(ctx context.Context, key string) (io.ReadCloser, int64, error) {
	if s == nil {
		return nil, 0, ErrNotPresent
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, s.url(key), nil)
	if err != nil {
		return nil, 0, fmt.Errorf("store: build GET %q: %w", key, err)
	}
	if err := s.sign(req); err != nil {
		return nil, 0, fmt.Errorf("store: sign GET %q: %w", key, err)
	}
	resp, err := s.client.Do(req)
	if err != nil {
		return nil, 0, fmt.Errorf("store: GET %q: %w", key, err)
	}
	if resp.StatusCode == http.StatusNotFound {
		drainClose(resp.Body)
		return nil, 0, ErrNotPresent
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		drainClose(resp.Body)
		return nil, 0, fmt.Errorf("store: GET %q: unexpected status %d", key, resp.StatusCode)
	}
	return resp.Body, resp.ContentLength, nil
}

// Head reports whether the object key exists (HTTP HEAD, true iff 200). A 404 is
// (false, nil); any other non-2xx status is an error so a transport fault is not
// silently read as absence.
func (s *Store) Head(ctx context.Context, key string) (bool, error) {
	if s == nil {
		return false, nil
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodHead, s.url(key), nil)
	if err != nil {
		return false, fmt.Errorf("store: build HEAD %q: %w", key, err)
	}
	if err := s.sign(req); err != nil {
		return false, fmt.Errorf("store: sign HEAD %q: %w", key, err)
	}
	resp, err := s.client.Do(req)
	if err != nil {
		return false, fmt.Errorf("store: HEAD %q: %w", key, err)
	}
	defer drainClose(resp.Body)
	if resp.StatusCode == http.StatusOK {
		return true, nil
	}
	if resp.StatusCode == http.StatusNotFound {
		return false, nil
	}
	return false, fmt.Errorf("store: HEAD %q: unexpected status %d", key, resp.StatusCode)
}

// Delete removes the object key (HTTP DELETE). It is idempotent: a 404 (the
// object is already gone) is success, matching the desired-end-state contract.
func (s *Store) Delete(ctx context.Context, key string) error {
	if s == nil {
		return nil
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodDelete, s.url(key), nil)
	if err != nil {
		return fmt.Errorf("store: build DELETE %q: %w", key, err)
	}
	if err := s.sign(req); err != nil {
		return fmt.Errorf("store: sign DELETE %q: %w", key, err)
	}
	resp, err := s.client.Do(req)
	if err != nil {
		return fmt.Errorf("store: DELETE %q: %w", key, err)
	}
	defer drainClose(resp.Body)
	if resp.StatusCode == http.StatusNotFound {
		return nil // already gone: the desired end-state holds
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("store: DELETE %q: unexpected status %d", key, resp.StatusCode)
	}
	return nil
}

// ---- artifact-level helpers ------------------------------------------------

// Export writes each of localDir's named files to the store under prefix, then
// meta.json LAST as the completeness marker. It first computes every file's
// SHA-256 and HEAD-compares against any existing meta.json: if the store already
// holds this exact artifact (identical file checksums), it returns skipped=true
// and bytesMoved=0 without re-uploading (idempotent per checksum). Otherwise it
// PUTs each file, then PUTs meta.json, and returns the sum of the uploaded file
// sizes. files are base names resolved against localDir; a missing local file is
// an error (an incomplete artifact must not be exported as complete). cpuVendor
// and cpuTemplate (PR-E) stamp the exporting node's cpu_sku into meta.json;
// either or both empty stamps the artifact UNSTAMPED for that half of the sku
// (the pre-PR-E / grandfathered shape), never a placeholder value.
func (s *Store) Export(ctx context.Context, prefix, localDir string, files []string, generation uint64, nowMs int64, cpuVendor, cpuTemplate string) (bytesMoved int64, skipped bool, err error) {
	if s == nil {
		return 0, false, ErrNotPresent
	}
	meta := Meta{
		Files:           make(map[string]FileMeta, len(files)),
		Generation:      generation,
		CreatedAtUnixMs: nowMs,
		CpuVendor:       cpuVendor,
		CpuTemplate:     cpuTemplate,
	}
	var totalSize int64
	for _, name := range files {
		path := filepath.Join(localDir, name)
		fm, ferr := fileMeta(path)
		if ferr != nil {
			return 0, false, fmt.Errorf("store: stat artifact file %q: %w", name, ferr)
		}
		meta.Files[name] = fm
		totalSize += fm.Size
	}

	// Idempotency short-circuit: if the store already holds a meta.json whose
	// per-file checksums match ours, the artifact is already durable at this
	// content. HEAD-then-GET the marker; a Head miss (or a mismatch) falls
	// through to a full re-upload.
	if present, remoteMeta, merr := s.getMeta(ctx, prefix); merr == nil && present {
		if sameFiles(remoteMeta.Files, meta.Files) {
			return 0, true, nil
		}
		// Content DIFFERS. Before this fence that fell straight through to a full
		// re-upload, so an older copy silently overwrote a newer one on the shared
		// singleton volume key. Compare the generation the marker already carries.
		// Strictly-greater only: equal generations still re-upload, which keeps the
		// repair path for a partially-written or corrupted remote copy at the
		// current generation, and generation 0 (unknown) can never win against a
		// known one.
		if remoteMeta.Generation > generation {
			return 0, false, fmt.Errorf(
				"%w: local generation %d, store generation %d",
				ErrStaleGeneration, generation, remoteMeta.Generation)
		}
	}

	for name, fm := range meta.Files {
		path := filepath.Join(localDir, name)
		f, oerr := os.Open(path)
		if oerr != nil {
			return bytesMoved, false, fmt.Errorf("store: open artifact file %q: %w", name, oerr)
		}
		var body io.Reader = f
		putSize := fm.Size
		if s.compress {
			// This is a two-phase rollout. A daemon that does not understand the
			// marker cannot restore a compressed object. Phase 1 ships the READER
			// everywhere with the writer OFF; only once the fleet understands
			// compression is the writer armed. Shipping both at once would let a
			// new writer produce objects an old reader cannot hydrate during
			// rollout, causing a self-inflicted outage. This follows the existing
			// ship-inert, arm-later pattern used by WarmthReaper's
			// EMBERVM_WARMTH_RETENTION_SWEEP gate.
			pr, pw := io.Pipe()
			enc, eerr := zstd.NewWriter(pw, zstd.WithEncoderConcurrency(1), zstd.WithEncoderLevel(zstd.SpeedFastest))
			if eerr != nil {
				_ = f.Close()
				return bytesMoved, false, fmt.Errorf("store: create zstd writer for %q: %w", name, eerr)
			}
			go func() {
				_, cerr := io.Copy(enc, f)
				pw.CloseWithError(errors.Join(cerr, enc.Close()))
			}()
			body = pr
			putSize = -1
			meta.Files[name] = FileMeta{Size: fm.Size, Sha256: fm.Sha256, Compression: "zstd"}
		}
		cr := &countingReader{Reader: body}
		perr := s.Put(ctx, prefix+"/"+name, cr, putSize)
		_ = f.Close()
		if perr != nil {
			return bytesMoved, false, perr
		}
		bytesMoved += cr.n
	}

	// meta.json LAST: only now is the artifact visible as complete.
	metaBytes, merr := json.Marshal(meta)
	if merr != nil {
		return bytesMoved, false, fmt.Errorf("store: marshal meta: %w", merr)
	}
	if perr := s.Put(ctx, prefix+"/"+metaObject, bytes.NewReader(metaBytes), int64(len(metaBytes))); perr != nil {
		return bytesMoved, false, perr
	}
	return bytesMoved, false, nil
}

// Restore fetches meta.json first (ErrNotPresent when absent, which the caller
// maps to FAILED_PRECONDITION), then GETs each listed file into localDir,
// verifying its SHA-256 against the marker. Each file is written to a temp path
// and renamed into place only after its checksum matches, so a mismatch (or a
// short read) never leaves a corrupt file on disk. It returns the bytes written
// and the marker's generation.
func (s *Store) Restore(ctx context.Context, prefix, localDir string) (bytesMoved int64, generation uint64, err error) {
	if s == nil {
		return 0, 0, ErrNotPresent
	}
	present, meta, merr := s.getMeta(ctx, prefix)
	if merr != nil {
		return 0, 0, merr
	}
	if !present {
		return 0, 0, ErrNotPresent
	}
	if err := os.MkdirAll(localDir, 0o700); err != nil {
		return 0, 0, fmt.Errorf("store: mkdir restore dir %q: %w", localDir, err)
	}
	for name, fm := range meta.Files {
		n, ferr := s.restoreFile(ctx, prefix+"/"+name, filepath.Join(localDir, name), fm)
		if ferr != nil {
			return bytesMoved, 0, ferr
		}
		bytesMoved += n
	}
	return bytesMoved, meta.Generation, nil
}

// restoreFile GETs one object into a temp file alongside dst, verifies its size
// and SHA-256 against the marker, and only then renames it into place. On any
// mismatch or read error it removes the temp file and returns an error, so a
// corrupt restore never leaves a bad file where a good one (or none) belongs.
func (s *Store) restoreFile(ctx context.Context, key, dst string, want FileMeta) (int64, error) {
	body, _, err := s.Get(ctx, key)
	if err != nil {
		return 0, err
	}
	defer drainClose(body)

	tmp := dst + ".restore.tmp"
	f, err := os.OpenFile(tmp, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o600)
	if err != nil {
		return 0, fmt.Errorf("store: create temp restore file %q: %w", tmp, err)
	}
	h := sha256.New()
	var decoded io.Reader = body
	var decoder *zstd.Decoder
	if want.Compression == "zstd" {
		decoder, err = zstd.NewReader(body, zstd.WithDecoderConcurrency(1))
		if err != nil {
			_ = f.Close()
			_ = os.Remove(tmp)
			return 0, fmt.Errorf("store: decode object %q: %w", key, err)
		}
		defer decoder.Close()
		decoded = decoder
	}
	n, err := io.Copy(io.MultiWriter(f, h), decoded)
	closeErr := f.Close()
	if err != nil {
		_ = os.Remove(tmp)
		return 0, fmt.Errorf("store: read object %q: %w", key, err)
	}
	if closeErr != nil {
		_ = os.Remove(tmp)
		return 0, fmt.Errorf("store: close temp restore file %q: %w", tmp, closeErr)
	}
	if want.Size >= 0 && n != want.Size {
		_ = os.Remove(tmp)
		return 0, fmt.Errorf("store: object %q size %d != meta %d", key, n, want.Size)
	}
	got := hex.EncodeToString(h.Sum(nil))
	if want.Sha256 != "" && got != want.Sha256 {
		_ = os.Remove(tmp)
		return 0, fmt.Errorf("store: object %q sha256 %s != meta %s", key, got, want.Sha256)
	}
	sparse.BestEffort(tmp, "restore")
	if err := os.Rename(tmp, dst); err != nil {
		_ = os.Remove(tmp)
		return 0, fmt.Errorf("store: publish restored file %q: %w", dst, err)
	}
	return n, nil
}

// DeleteArtifact removes an artifact from the store. It deletes meta.json FIRST
// so the artifact is immediately invisible (a presence check reads meta.json
// first), then best-effort deletes each file the marker named. If meta.json is
// already gone, the artifact is treated as absent and the call is a no-op
// success (idempotent, matching the desired-end-state contract).
func (s *Store) DeleteArtifact(ctx context.Context, prefix string) error {
	if s == nil {
		return nil
	}
	present, meta, err := s.getMeta(ctx, prefix)
	if err != nil {
		return err
	}
	if !present {
		return nil // already invisible: nothing to delete
	}
	// Delete the marker FIRST: from here the artifact is invisible to any
	// presence check even if the file deletes below partially fail.
	if err := s.Delete(ctx, prefix+"/"+metaObject); err != nil {
		return err
	}
	// Best-effort delete of each known file (each Delete is idempotent on 404),
	// so an orphaned file cannot survive a completed marker deletion silently.
	var firstErr error
	for name := range meta.Files {
		if derr := s.Delete(ctx, prefix+"/"+name); derr != nil && firstErr == nil {
			firstErr = fmt.Errorf("delete artifact file %q: %w", name, derr)
		}
	}
	return firstErr // nosemgrep: no-bare-error-return (already wrapped with fmt.Errorf above; the rule cannot see through the deferred-assignment indirection)
}

// Present reports whether the store holds a complete artifact at prefix (a
// readable meta.json), its generation, and its stamped cpu_sku (PR-E: cpuVendor,
// cpuTemplate, either or both "" for an artifact exported before PR-E or by a
// node with an undetected vendor, the grandfathered/UNSTAMPED case). A missing
// marker is (false, 0, "", "", nil): absence is not an error, it is the answer.
// Callers validate the returned sku BEFORE Restore moves any bytes, so a
// mismatched-sku artifact is refused without a wasted network copy.
func (s *Store) Present(ctx context.Context, prefix string) (present bool, generation uint64, cpuVendor, cpuTemplate string, err error) {
	if s == nil {
		return false, 0, "", "", nil
	}
	present, meta, err := s.getMeta(ctx, prefix)
	if err != nil {
		return false, 0, "", "", err
	}
	if !present {
		return false, 0, "", "", nil
	}
	return true, meta.Generation, meta.CpuVendor, meta.CpuTemplate, nil
}

// Reachable is a cheap liveness probe against the bucket root with a short
// timeout, feeding NodeStatus.store_reachable (a warmth hint, never a gate). A
// HEAD on the bucket root that returns any HTTP status (2xx, 403, even 404)
// proves the endpoint answered; only a transport failure or timeout is
// unreachable. A nil store is never reachable.
func (s *Store) Reachable(ctx context.Context) bool {
	if s == nil {
		return false
	}
	probeCtx, cancel := context.WithTimeout(ctx, reachableTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(probeCtx, http.MethodHead, s.endpoint+"/"+s.bucket, nil)
	if err != nil {
		return false
	}
	if err := s.sign(req); err != nil {
		return false
	}
	resp, err := s.client.Do(req)
	if err != nil {
		return false
	}
	drainClose(resp.Body)
	// Any answered status means the endpoint is up; the reachability probe does
	// not judge the bucket's contents, only that the store responds.
	return true
}

// listBucketResult is the subset of the S3 ListObjectsV2 XML response this
// client reads. Only CommonPrefixes matters: every artifact is a set of files
// under a prefix, so a DELIMITED list one level below <kind>/<vendor>/<workload>
// enumerates the artifact refs without paging through their individual files.
type listBucketResult struct {
	IsTruncated    bool `xml:"IsTruncated"`
	CommonPrefixes []struct {
		Prefix string `xml:"Prefix"`
	} `xml:"CommonPrefixes"`
}

// ListRefs enumerates the immediate child "directories" under prefix and returns
// the last path segment of each, i.e. the artifact refs stored under it.
//
// This is the REMOTE inventory read behind PR-4 remote base retention. It exists
// because a superseded base is reclaimed from node disk long before its store
// object is, so by the time remote retention runs, no node necessarily holds the
// ref any more and the bucket is the only place that still knows.
//
// The S3 delimiter does the work: with delimiter=/ the store returns one
// CommonPrefix per ref instead of one key per file, so the response size tracks
// the ref count rather than the file count. limit caps max-keys; truncated
// reports whether the store had more. A truncated listing is safe for retention
// because the sweep only deletes BEYOND a keep-set computed from the newest
// entries, so seeing fewer refs can only evict less, never more.
func (s *Store) ListRefs(ctx context.Context, prefix string, limit int) (refs []string, truncated bool, err error) {
	if s == nil {
		return nil, false, ErrNotPresent
	}
	p := strings.TrimLeft(prefix, "/")
	if !strings.HasSuffix(p, "/") {
		p += "/"
	}
	q := url.Values{}
	q.Set("list-type", "2")
	q.Set("prefix", p)
	q.Set("delimiter", "/")
	if limit > 0 {
		q.Set("max-keys", strconv.Itoa(limit))
	}
	endpoint := s.endpoint + "/" + s.bucket + "?" + q.Encode()

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return nil, false, fmt.Errorf("store: build LIST %q: %w", p, err)
	}
	if err := s.sign(req); err != nil {
		return nil, false, fmt.Errorf("store: sign LIST %q: %w", p, err)
	}
	resp, err := s.client.Do(req)
	if err != nil {
		return nil, false, fmt.Errorf("store: LIST %q: %w", p, err)
	}
	defer drainClose(resp.Body)
	if resp.StatusCode == http.StatusNotFound {
		// An empty bucket or absent prefix is "nothing stored", not an error.
		return nil, false, nil
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, false, fmt.Errorf("store: LIST %q: unexpected status %d", p, resp.StatusCode)
	}

	var out listBucketResult
	if derr := xml.NewDecoder(resp.Body).Decode(&out); derr != nil {
		return nil, false, fmt.Errorf("store: decode LIST %q: %w", p, derr)
	}
	for _, cp := range out.CommonPrefixes {
		ref := strings.Trim(strings.TrimPrefix(cp.Prefix, p), "/")
		if ref == "" || strings.Contains(ref, "/") {
			continue
		}
		refs = append(refs, ref)
	}
	return refs, out.IsTruncated, nil
}

// ArtifactInfo reports the completeness-marker fields ListArtifacts needs that
// Present does not surface: the created-at it orders retention on and the summed
// file size, plus the vendor stamp so a caller can verify an object really
// belongs to the prefix it was listed under.
//
// Returned flat rather than as a Meta so the server package's artifactStore seam
// can declare it without importing this package (see the seam's own note). Same
// contract as getMeta: absent marker is (false, ...) with a nil error, so an
// incomplete artifact reads as not present.
func (s *Store) ArtifactInfo(ctx context.Context, prefix string) (present bool, createdAtUnixMs int64, sizeBytes uint64, cpuVendor, cpuTemplate string, err error) {
	if s == nil {
		return false, 0, 0, "", "", nil
	}
	ok, meta, merr := s.getMeta(ctx, prefix)
	if merr != nil {
		return false, 0, 0, "", "", merr
	}
	if !ok {
		return false, 0, 0, "", "", nil
	}
	var total int64
	for _, fm := range meta.Files {
		total += fm.Size
	}
	return true, meta.CreatedAtUnixMs, uint64(total), meta.CpuVendor, meta.CpuTemplate, nil
}

// getMeta fetches and decodes meta.json for a prefix. It returns (false, _, nil)
// when the marker is absent (ErrNotPresent from Get), so callers distinguish an
// absent artifact from a transport or decode error.
func (s *Store) getMeta(ctx context.Context, prefix string) (bool, Meta, error) {
	body, _, err := s.Get(ctx, prefix+"/"+metaObject)
	if errors.Is(err, ErrNotPresent) {
		return false, Meta{}, nil
	}
	if err != nil {
		return false, Meta{}, err
	}
	defer drainClose(body)
	var meta Meta
	if derr := json.NewDecoder(body).Decode(&meta); derr != nil {
		return false, Meta{}, fmt.Errorf("store: decode meta for %q: %w", prefix, derr)
	}
	return true, meta, nil
}

// fileMeta computes one local file's size and hex SHA-256 for the export marker.
func fileMeta(path string) (FileMeta, error) {
	f, err := os.Open(path)
	if err != nil {
		return FileMeta{}, err
	}
	defer f.Close()
	h := sha256.New()
	n, err := io.Copy(h, f)
	if err != nil {
		return FileMeta{}, err
	}
	return FileMeta{Size: n, Sha256: hex.EncodeToString(h.Sum(nil))}, nil
}

// sameFiles reports whether two file-meta maps carry identical names and
// checksums (the idempotency test: the store already holds this exact content).
// Sizes are implied by the checksum but compared too for a cheap early-out.
func sameFiles(a, b map[string]FileMeta) bool {
	if len(a) != len(b) {
		return false
	}
	for name, fa := range a {
		fb, ok := b[name]
		if !ok || fa.Sha256 != fb.Sha256 || fa.Size != fb.Size {
			return false
		}
	}
	return true
}

// drainClose drains and closes a response body so the underlying connection can
// be reused (the net/http keep-alive discipline the zip-lane fetch also follows).
func drainClose(rc io.ReadCloser) {
	if rc == nil {
		return
	}
	_, _ = io.Copy(io.Discard, io.LimitReader(rc, 4<<10))
	_ = rc.Close()
}
