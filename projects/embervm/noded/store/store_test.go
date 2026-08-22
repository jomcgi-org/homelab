package store

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"testing"
)

// fakeObjectStore is an in-memory S3-API stand-in: a map from object path
// (/<bucket>/<key>) to bytes, served over an httptest.Server. It honours PUT /
// GET / HEAD / DELETE exactly as the Store client expects, so the tests exercise
// the real net/http round-trip without a network dependency.
type fakeObjectStore struct {
	mu      sync.Mutex
	objects map[string][]byte
	// putOrder records the path of every PUT in order, so a test can assert
	// meta.json is written LAST.
	putOrder        []string
	failFilePuts    bool
	accessKeyID     string
	secretAccessKey string
}

func newFakeObjectStore(creds ...string) *fakeObjectStore {
	f := &fakeObjectStore{objects: make(map[string][]byte)}
	if len(creds) == 2 {
		f.accessKeyID, f.secretAccessKey = creds[0], creds[1]
	}
	return f
}

func (f *fakeObjectStore) handler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if f.accessKeyID != "" {
			if code := f.verifySigV4(r); code != "" {
				w.WriteHeader(http.StatusForbidden)
				_, _ = w.Write([]byte(code))
				return
			}
		}
		key := r.URL.Path
		switch r.Method {
		case http.MethodPut:
			if f.failFilePuts && !strings.HasSuffix(key, "/"+metaObject) {
				w.WriteHeader(http.StatusInternalServerError)
				return
			}
			body, _ := io.ReadAll(r.Body)
			f.mu.Lock()
			f.objects[key] = body
			f.putOrder = append(f.putOrder, key)
			f.mu.Unlock()
			w.WriteHeader(http.StatusOK)
		case http.MethodGet:
			if r.URL.Query().Get("list-type") == "2" {
				w.Header().Set("Content-Type", "application/xml")
				_, _ = w.Write([]byte(`<ListBucketResult><IsTruncated>false</IsTruncated><CommonPrefixes><Prefix>base/amd/demo/ref-1/</Prefix></CommonPrefixes></ListBucketResult>`))
				return
			}
			f.mu.Lock()
			b, ok := f.objects[key]
			f.mu.Unlock()
			if !ok {
				w.WriteHeader(http.StatusNotFound)
				return
			}
			w.Header().Set("Content-Length", strconv.Itoa(len(b)))
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write(b)
		case http.MethodHead:
			f.mu.Lock()
			_, ok := f.objects[key]
			f.mu.Unlock()
			if !ok {
				w.WriteHeader(http.StatusNotFound)
				return
			}
			w.WriteHeader(http.StatusOK)
		case http.MethodDelete:
			f.mu.Lock()
			_, ok := f.objects[key]
			delete(f.objects, key)
			f.mu.Unlock()
			if !ok {
				w.WriteHeader(http.StatusNotFound)
				return
			}
			w.WriteHeader(http.StatusNoContent)
		default:
			w.WriteHeader(http.StatusMethodNotAllowed)
		}
	})
}

// verifySigV4 independently reconstructs the signature. It deliberately does
// not call any production signer helper, so round trips catch disagreement.
func (f *fakeObjectStore) verifySigV4(r *http.Request) string {
	auth := r.Header.Get("Authorization")
	if auth == "" {
		return "MissingAuthorization"
	}
	if !strings.HasPrefix(auth, "AWS4-HMAC-SHA256 ") || r.Header.Get("x-amz-content-sha256") != "UNSIGNED-PAYLOAD" {
		return "SignatureDoesNotMatch"
	}
	fields := map[string]string{}
	for _, part := range strings.Split(strings.TrimPrefix(auth, "AWS4-HMAC-SHA256 "), ", ") {
		pair := strings.SplitN(part, "=", 2)
		if len(pair) == 2 {
			fields[pair[0]] = pair[1]
		}
	}
	credential := strings.Split(fields["Credential"], "/")
	if len(credential) != 5 || credential[0] != f.accessKeyID || credential[2] != "us-east-1" || credential[3] != "s3" || credential[4] != "aws4_request" {
		return "SignatureDoesNotMatch"
	}
	signed := strings.Split(fields["SignedHeaders"], ";")
	var canonicalHeaders strings.Builder
	for _, name := range signed {
		value := r.Header.Get(name)
		if name == "host" {
			value = r.Host
		}
		canonicalHeaders.WriteString(name + ":" + strings.Join(strings.Fields(value), " ") + "\n")
	}
	canonical := r.Method + "\n" + testCanonicalPath(r.URL) + "\n" + testCanonicalQuery(r.URL) + "\n" +
		canonicalHeaders.String() + "\n" + fields["SignedHeaders"] + "\nUNSIGNED-PAYLOAD"
	scope := strings.Join(credential[1:], "/")
	stringToSign := "AWS4-HMAC-SHA256\n" + r.Header.Get("x-amz-date") + "\n" + scope + "\n" + testSHA256Hex(canonical)
	kDate := testHMAC([]byte("AWS4"+f.secretAccessKey), credential[1])
	kRegion := testHMAC(kDate, credential[2])
	kService := testHMAC(kRegion, credential[3])
	kSigning := testHMAC(kService, credential[4])
	want := hex.EncodeToString(testHMAC(kSigning, stringToSign))
	if !hmac.Equal([]byte(want), []byte(fields["Signature"])) {
		return "SignatureDoesNotMatch"
	}
	return ""
}

func testCanonicalPath(u *url.URL) string {
	path := u.EscapedPath()
	if path == "" {
		return "/"
	}
	parts := strings.Split(path, "/")
	for i, part := range parts {
		decoded, err := url.PathUnescape(part)
		if err == nil {
			parts[i] = strings.ReplaceAll(url.QueryEscape(decoded), "+", "%20")
		}
	}
	return strings.Join(parts, "/")
}

func testCanonicalQuery(u *url.URL) string {
	values := u.Query()
	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	sort.Slice(keys, func(i, j int) bool { return url.QueryEscape(keys[i]) < url.QueryEscape(keys[j]) })
	var parts []string
	for _, key := range keys {
		vals := append([]string(nil), values[key]...)
		sort.Strings(vals)
		for _, value := range vals {
			parts = append(parts, strings.ReplaceAll(url.QueryEscape(key), "+", "%20")+"="+strings.ReplaceAll(url.QueryEscape(value), "+", "%20"))
		}
	}
	return strings.Join(parts, "&")
}

func testSHA256Hex(value string) string {
	sum := sha256.Sum256([]byte(value))
	return hex.EncodeToString(sum[:])
}

func testHMAC(key []byte, value string) []byte {
	h := hmac.New(sha256.New, key)
	_, _ = h.Write([]byte(value))
	return h.Sum(nil)
}

func (f *fakeObjectStore) has(key string) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	_, ok := f.objects[key]
	return ok
}

// object returns a stored object's bytes, for asserting that a REFUSED export
// left the newer copy byte-for-byte intact.
func (f *fakeObjectStore) object(key string) []byte {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.objects[key]
}

func (f *fakeObjectStore) putOrderCopy() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]string, len(f.putOrder))
	copy(out, f.putOrder)
	return out
}

// newTestStore stands up a fake object store and returns a Store pointed at it.
func newTestStore(t *testing.T) (*Store, *fakeObjectStore) {
	return newTestStoreWithCompression(t, false)
}

func newTestStoreWithCompression(t *testing.T, compress bool) (*Store, *fakeObjectStore) {
	t.Helper()
	fake := newFakeObjectStore()
	srv := httptest.NewServer(fake.handler())
	t.Cleanup(srv.Close)
	return New(srv.URL, "embervm", compress), fake
}

func newSignedTestStore(t *testing.T) (*Store, *fakeObjectStore) {
	t.Helper()
	const accessKeyID = "embervm-test"
	const secretAccessKey = "test-secret"
	fake := newFakeObjectStore(accessKeyID, secretAccessKey)
	srv := httptest.NewServer(fake.handler())
	t.Cleanup(srv.Close)
	return New(srv.URL, "embervm", false, WithCredentials(accessKeyID, secretAccessKey)), fake
}

// writeLocalArtifact writes a set of named files with the given contents into a
// fresh temp dir and returns the dir and the file names.
func writeLocalArtifact(t *testing.T, files map[string]string) (string, []string) {
	t.Helper()
	dir := t.TempDir()
	names := make([]string, 0, len(files))
	for name, content := range files {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(content), 0o600); err != nil {
			t.Fatalf("write %s: %v", name, err)
		}
		names = append(names, name)
	}
	sort.Strings(names)
	return dir, names
}

func TestNewDisabledOnEmptyEndpoint(t *testing.T) {
	if New("", "embervm", false) != nil {
		t.Fatal("New(\"\", ...) should return nil (store disabled)")
	}
}

// TestRawRoundTrip proves Put/Get/Head/Delete against a raw key work and that
// Delete is idempotent (a 404 is success).
func TestRawRoundTrip(t *testing.T) {
	s, _ := newTestStore(t)
	ctx := context.Background()
	key := "stateful/scratch-postgres/state-abc/snapfile"
	payload := []byte("hello snapshot bytes")

	if err := s.Put(ctx, key, strings.NewReader(string(payload)), int64(len(payload))); err != nil {
		t.Fatalf("Put: %v", err)
	}
	ok, err := s.Head(ctx, key)
	if err != nil || !ok {
		t.Fatalf("Head after Put = (%v, %v), want (true, nil)", ok, err)
	}
	rc, size, err := s.Get(ctx, key)
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	got, _ := io.ReadAll(rc)
	_ = rc.Close()
	if string(got) != string(payload) {
		t.Fatalf("Get body = %q, want %q", got, payload)
	}
	if size != int64(len(payload)) {
		t.Fatalf("Get size = %d, want %d", size, len(payload))
	}
	if err := s.Delete(ctx, key); err != nil {
		t.Fatalf("Delete: %v", err)
	}
	ok, err = s.Head(ctx, key)
	if err != nil || ok {
		t.Fatalf("Head after Delete = (%v, %v), want (false, nil)", ok, err)
	}
	// Idempotent: a second Delete of an absent key succeeds.
	if err := s.Delete(ctx, key); err != nil {
		t.Fatalf("second Delete (absent) should be nil, got %v", err)
	}
	// Get of an absent key is ErrNotPresent.
	if _, _, err := s.Get(ctx, key); err != ErrNotPresent {
		t.Fatalf("Get absent = %v, want ErrNotPresent", err)
	}
}

func TestSignedRawVerbsListAndReachable(t *testing.T) {
	s, _ := newSignedTestStore(t)
	ctx := context.Background()
	key := "base/amd/demo/ref-1/meta.json"
	if err := s.Put(ctx, key, strings.NewReader("body"), 4); err != nil {
		t.Fatalf("Put: %v", err)
	}
	if ok, err := s.Head(ctx, key); err != nil || !ok {
		t.Fatalf("Head = %v, %v", ok, err)
	}
	r, _, err := s.Get(ctx, key)
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	_ = r.Close()
	refs, _, err := s.ListRefs(ctx, "base/amd/demo", 10)
	if err != nil || len(refs) != 1 || refs[0] != "ref-1" {
		t.Fatalf("ListRefs = %#v, %v", refs, err)
	}
	listURL := s.endpoint + "/" + s.bucket + "?continuation-token=ref%2F1%2Bnext%3D&list-type=2&prefix=base%2Famd%2Fdemo%2F"
	listReq, err := http.NewRequestWithContext(ctx, http.MethodGet, listURL, nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := s.sign(listReq); err != nil {
		t.Fatal(err)
	}
	listResp, err := s.client.Do(listReq)
	if err != nil {
		t.Fatalf("paginated LIST: %v", err)
	}
	if listResp.StatusCode != http.StatusOK {
		t.Fatalf("paginated LIST status = %v", listResp.StatusCode)
	}
	_ = listResp.Body.Close()
	if !s.Reachable(ctx) {
		t.Fatal("Reachable returned false")
	}
	if err := s.Delete(ctx, key); err != nil {
		t.Fatalf("Delete: %v", err)
	}
}

// TestExportWritesMetaLast proves every artifact file is present in the store
// BEFORE meta.json, so a reader that sees meta.json always finds a complete
// artifact (the completeness-marker discipline).
func TestExportWritesMetaLast(t *testing.T) {
	s, fake := newTestStore(t)
	ctx := context.Background()
	dir, names := writeLocalArtifact(t, map[string]string{
		"snapfile": "snap-content",
		"memfile":  "mem-content",
		"gen":      "7",
	})
	prefix := "stateful/scratch-postgres/state-abc"

	moved, skipped, err := s.Export(ctx, prefix, dir, names, 7, 111, "", "")
	if err != nil {
		t.Fatalf("Export: %v", err)
	}
	if skipped {
		t.Fatal("first Export should not be skipped")
	}
	wantBytes := int64(len("snap-content") + len("mem-content") + len("7"))
	if moved != wantBytes {
		t.Fatalf("Export bytesMoved = %d, want %d", moved, wantBytes)
	}

	order := fake.putOrderCopy()
	if len(order) == 0 {
		t.Fatal("no PUTs recorded")
	}
	last := order[len(order)-1]
	if !strings.HasSuffix(last, "/"+metaObject) {
		t.Fatalf("last PUT = %q, want the meta.json marker written LAST", last)
	}
	// Every file object landed before the marker.
	for _, name := range names {
		if !fake.has("/embervm/" + prefix + "/" + name) {
			t.Fatalf("file object %q missing after Export", name)
		}
	}
	if got := string(fake.object("/embervm/" + prefix + "/snapfile")); got != "snap-content" {
		t.Fatalf("compression-off object = %q, want plaintext bytes", got)
	}
	if !fake.has("/embervm/" + prefix + "/" + metaObject) {
		t.Fatal("meta.json missing after Export")
	}
}

// TestExportSkipsUnchanged proves a second Export of identical content HEAD/GET-
// compares the marker and returns skipped=true without re-uploading.
func TestExportSkipsUnchanged(t *testing.T) {
	s, fake := newTestStore(t)
	ctx := context.Background()
	dir, names := writeLocalArtifact(t, map[string]string{
		"snapfile": "snap-content",
		"memfile":  "mem-content",
	})
	prefix := "session/sandbox-session/sess-1"

	if _, skipped, err := s.Export(ctx, prefix, dir, names, 0, 1, "", ""); err != nil || skipped {
		t.Fatalf("first Export = (skipped=%v, %v), want (false, nil)", skipped, err)
	}
	putsAfterFirst := len(fake.putOrderCopy())

	moved, skipped, err := s.Export(ctx, prefix, dir, names, 0, 2, "", "")
	if err != nil {
		t.Fatalf("second Export: %v", err)
	}
	if !skipped {
		t.Fatal("second Export of unchanged content should be skipped")
	}
	if moved != 0 {
		t.Fatalf("skipped Export bytesMoved = %d, want 0", moved)
	}
	if got := len(fake.putOrderCopy()); got != putsAfterFirst {
		t.Fatalf("skipped Export issued %d new PUTs, want 0", got-putsAfterFirst)
	}
}

// TestRestoreRoundTrip proves Restore fetches the bytes back identically and
// reports the marker's generation.
func TestRestoreRoundTrip(t *testing.T) {
	s, _ := newTestStore(t)
	ctx := context.Background()
	contents := map[string]string{"snapfile": "snap-bytes", "memfile": "mem-bytes"}
	srcDir, names := writeLocalArtifact(t, contents)
	prefix := "stateful/scratch-postgres/state-xyz"

	if _, _, err := s.Export(ctx, prefix, srcDir, names, 9, 42, "", ""); err != nil {
		t.Fatalf("Export: %v", err)
	}

	dstDir := t.TempDir()
	moved, gen, err := s.Restore(ctx, prefix, dstDir)
	if err != nil {
		t.Fatalf("Restore: %v", err)
	}
	if gen != 9 {
		t.Fatalf("Restore generation = %d, want 9", gen)
	}
	if moved != int64(len("snap-bytes")+len("mem-bytes")) {
		t.Fatalf("Restore bytesMoved = %d", moved)
	}
	for name, want := range contents {
		got, rerr := os.ReadFile(filepath.Join(dstDir, name))
		if rerr != nil {
			t.Fatalf("read restored %s: %v", name, rerr)
		}
		if string(got) != want {
			t.Fatalf("restored %s = %q, want %q", name, got, want)
		}
	}
}

func TestCompressedExportRoundTrip(t *testing.T) {
	s, fake := newTestStoreWithCompression(t, true)
	ctx := context.Background()
	plaintext := bytes.Repeat([]byte{0}, 1<<20)
	srcDir := t.TempDir()
	if err := os.WriteFile(filepath.Join(srcDir, "memfile"), plaintext, 0o600); err != nil {
		t.Fatal(err)
	}
	if _, _, err := s.Export(ctx, "base/zeros", srcDir, []string{"memfile"}, 1, 1, "", ""); err != nil {
		t.Fatalf("Export: %v", err)
	}
	if stored := fake.object("/embervm/base/zeros/memfile"); len(stored) >= len(plaintext) {
		t.Fatalf("compressed object size = %d, want less than plaintext %d", len(stored), len(plaintext))
	}
	var meta Meta
	if err := json.Unmarshal(fake.object("/embervm/base/zeros/meta.json"), &meta); err != nil {
		t.Fatal(err)
	}
	if got := meta.Files["memfile"].Compression; got != "zstd" {
		t.Fatalf("compression marker = %q, want zstd", got)
	}
	dstDir := t.TempDir()
	if _, _, err := s.Restore(ctx, "base/zeros", dstDir); err != nil {
		t.Fatalf("Restore: %v", err)
	}
	got, err := os.ReadFile(filepath.Join(dstDir, "memfile"))
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got, plaintext) {
		t.Fatal("compressed restore changed the plaintext")
	}
}

func TestCompressedIncompressibleRoundTrip(t *testing.T) {
	s, fake := newTestStoreWithCompression(t, true)
	ctx := context.Background()
	plaintext := make([]byte, 256<<10)
	if _, err := rand.Read(plaintext); err != nil {
		t.Fatal(err)
	}
	srcDir := t.TempDir()
	if err := os.WriteFile(filepath.Join(srcDir, "random"), plaintext, 0o600); err != nil {
		t.Fatal(err)
	}
	if _, _, err := s.Export(ctx, "base/random", srcDir, []string{"random"}, 1, 1, "", ""); err != nil {
		t.Fatalf("Export: %v", err)
	}
	if stored := fake.object("/embervm/base/random/random"); len(stored) <= len(plaintext) {
		t.Fatalf("incompressible stored object size = %d, want greater than plaintext %d", len(stored), len(plaintext))
	}
	dstDir := t.TempDir()
	if _, _, err := s.Restore(ctx, "base/random", dstDir); err != nil {
		t.Fatalf("Restore: %v", err)
	}
	got, err := os.ReadFile(filepath.Join(dstDir, "random"))
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got, plaintext) {
		t.Fatal("incompressible compressed restore changed the plaintext")
	}
}

func dirEntryNames(t *testing.T, dir string) []string {
	t.Helper()
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	names := make([]string, 0, len(entries))
	for _, entry := range entries {
		names = append(names, entry.Name())
	}
	sort.Strings(names)
	return names
}

func TestCompressedExportLeavesArtifactDirUntouched(t *testing.T) {
	s, _ := newTestStoreWithCompression(t, true)
	ctx := context.Background()
	srcDir, names := writeLocalArtifact(t, map[string]string{
		"memfile":  "mem-content",
		"snapfile": "snap-content",
	})
	if err := os.WriteFile(filepath.Join(srcDir, ".artifact-marker"), []byte("keep"), 0o600); err != nil {
		t.Fatal(err)
	}
	wantEntries := dirEntryNames(t, srcDir)

	if _, _, err := s.Export(ctx, "base/clean", srcDir, names, 1, 1, "", ""); err != nil {
		t.Fatalf("first Export: %v", err)
	}
	if got := dirEntryNames(t, srcDir); !slicesEqual(got, wantEntries) {
		t.Fatalf("artifact directory after first Export = %v, want %v", got, wantEntries)
	}
	if _, skipped, err := s.Export(ctx, "base/clean", srcDir, names, 1, 2, "", ""); err != nil || !skipped {
		t.Fatalf("second Export = (skipped=%v, %v), want (true, nil)", skipped, err)
	}
	if got := dirEntryNames(t, srcDir); !slicesEqual(got, wantEntries) {
		t.Fatalf("artifact directory after second Export = %v, want %v", got, wantEntries)
	}
}

func TestCompressedExportPutFailureLeavesDirClean(t *testing.T) {
	s, fake := newTestStoreWithCompression(t, true)
	fake.failFilePuts = true
	ctx := context.Background()
	srcDir, names := writeLocalArtifact(t, map[string]string{
		"memfile":  "mem-content",
		"snapfile": "snap-content",
	})
	if err := os.WriteFile(filepath.Join(srcDir, ".artifact-marker"), []byte("keep"), 0o600); err != nil {
		t.Fatal(err)
	}
	wantEntries := dirEntryNames(t, srcDir)
	if _, _, err := s.Export(ctx, "base/failure", srcDir, names, 1, 1, "", ""); err == nil {
		t.Fatal("Export succeeded, want file PUT failure")
	}
	if fake.has("/embervm/base/failure/meta.json") {
		t.Fatal("meta.json was PUT after file PUT failure")
	}
	if got := dirEntryNames(t, srcDir); !slicesEqual(got, wantEntries) {
		t.Fatalf("artifact directory after failed Export = %v, want %v", got, wantEntries)
	}
}

func slicesEqual(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func TestLegacyObjectRestoresWithCompressionEnabled(t *testing.T) {
	legacy, fake := newTestStoreWithCompression(t, false)
	ctx := context.Background()
	srcDir, names := writeLocalArtifact(t, map[string]string{"memfile": "legacy-bytes"})
	if _, _, err := legacy.Export(ctx, "base/legacy", srcDir, names, 1, 1, "", ""); err != nil {
		t.Fatalf("legacy Export: %v", err)
	}
	reader := New(legacy.endpoint, legacy.bucket, true)
	dstDir := t.TempDir()
	if _, _, err := reader.Restore(ctx, "base/legacy", dstDir); err != nil {
		t.Fatalf("legacy Restore: %v", err)
	}
	if got, _ := os.ReadFile(filepath.Join(dstDir, "memfile")); string(got) != "legacy-bytes" {
		t.Fatalf("legacy restore = %q", got)
	}
	if got := fake.object("/embervm/base/legacy/memfile"); string(got) != "legacy-bytes" {
		t.Fatalf("legacy object was rewritten: %q", got)
	}
}

func TestTruncatedCompressedObjectLeavesNoPartialFile(t *testing.T) {
	s, fake := newTestStoreWithCompression(t, true)
	ctx := context.Background()
	plaintext := bytes.Repeat([]byte("memfile"), 1<<16)
	srcDir := t.TempDir()
	if err := os.WriteFile(filepath.Join(srcDir, "memfile"), plaintext, 0o600); err != nil {
		t.Fatal(err)
	}
	if _, _, err := s.Export(ctx, "base/truncated", srcDir, []string{"memfile"}, 1, 1, "", ""); err != nil {
		t.Fatalf("Export: %v", err)
	}
	compressed := fake.object("/embervm/base/truncated/memfile")
	fake.mu.Lock()
	fake.objects["/embervm/base/truncated/memfile"] = compressed[:len(compressed)/2]
	fake.mu.Unlock()
	dstDir := t.TempDir()
	if _, _, err := s.Restore(ctx, "base/truncated", dstDir); err == nil {
		t.Fatal("truncated compressed restore succeeded")
	}
	if _, err := os.Stat(filepath.Join(dstDir, "memfile")); !os.IsNotExist(err) {
		t.Fatalf("truncated restore left destination file, stat err = %v", err)
	}
	if _, err := os.Stat(filepath.Join(dstDir, "memfile.restore.tmp")); !os.IsNotExist(err) {
		t.Fatalf("truncated restore left temp file, stat err = %v", err)
	}
}

// TestRestoreAbsentIsNotPresent proves a restore with no meta.json returns the
// ErrNotPresent sentinel (which the verb handler maps to FAILED_PRECONDITION).
func TestRestoreAbsentIsNotPresent(t *testing.T) {
	s, _ := newTestStore(t)
	if _, _, err := s.Restore(context.Background(), "stateful/nope/none", t.TempDir()); err != ErrNotPresent {
		t.Fatalf("Restore of absent artifact = %v, want ErrNotPresent", err)
	}
}

// TestRestoreChecksumMismatchLeavesNoCorruptFile proves a corrupted object fails
// the restore and does NOT leave the corrupt file on local disk.
func TestRestoreChecksumMismatchLeavesNoCorruptFile(t *testing.T) {
	s, fake := newTestStore(t)
	ctx := context.Background()
	srcDir, names := writeLocalArtifact(t, map[string]string{"snapfile": "good-bytes"})
	prefix := "session/sandbox-session/sess-corrupt"
	if _, _, err := s.Export(ctx, prefix, srcDir, names, 0, 1, "", ""); err != nil {
		t.Fatalf("Export: %v", err)
	}
	// Corrupt the stored object WITHOUT updating meta.json, so the restore's
	// checksum verification fails.
	fake.mu.Lock()
	fake.objects["/embervm/"+prefix+"/snapfile"] = []byte("tampered!!")
	fake.mu.Unlock()

	dstDir := t.TempDir()
	if _, _, err := s.Restore(ctx, prefix, dstDir); err == nil {
		t.Fatal("Restore of a checksum-mismatched object should fail")
	}
	if _, err := os.Stat(filepath.Join(dstDir, "snapfile")); !os.IsNotExist(err) {
		t.Fatalf("corrupt restore left a file on disk (stat err = %v), want it absent", err)
	}
	// No temp file left behind either.
	if _, err := os.Stat(filepath.Join(dstDir, "snapfile.restore.tmp")); !os.IsNotExist(err) {
		t.Fatalf("corrupt restore left a temp file (stat err = %v)", err)
	}
}

// TestDeleteArtifactRemovesMetaFirst proves DeleteArtifact removes meta.json
// first (making the artifact invisible) and then its files.
func TestDeleteArtifactRemovesMetaFirst(t *testing.T) {
	s, fake := newTestStore(t)
	ctx := context.Background()
	srcDir, names := writeLocalArtifact(t, map[string]string{"snapfile": "a", "memfile": "b"})
	prefix := "serving/serving-test/serv-1"
	if _, _, err := s.Export(ctx, prefix, srcDir, names, 0, 1, "", ""); err != nil {
		t.Fatalf("Export: %v", err)
	}

	if err := s.DeleteArtifact(ctx, prefix); err != nil {
		t.Fatalf("DeleteArtifact: %v", err)
	}
	// Everything is gone.
	if fake.has("/embervm/" + prefix + "/" + metaObject) {
		t.Fatal("meta.json still present after DeleteArtifact")
	}
	for _, name := range names {
		if fake.has("/embervm/" + prefix + "/" + name) {
			t.Fatalf("file %q still present after DeleteArtifact", name)
		}
	}
	// Idempotent: deleting an already-absent artifact is a no-op success.
	if err := s.DeleteArtifact(ctx, prefix); err != nil {
		t.Fatalf("second DeleteArtifact should be nil, got %v", err)
	}
}

// TestPresentPartialWriteInvisible proves that files present WITHOUT a meta.json
// read as not-present (partial-write invisibility).
func TestPresentPartialWriteInvisible(t *testing.T) {
	s, _ := newTestStore(t)
	ctx := context.Background()
	prefix := "stateful/scratch-postgres/state-partial"
	// Write a file object but NOT the marker (a partial/interrupted export).
	body := []byte("orphan-bytes")
	if err := s.Put(ctx, prefix+"/snapfile", strings.NewReader(string(body)), int64(len(body))); err != nil {
		t.Fatalf("Put: %v", err)
	}
	present, gen, _, _, err := s.Present(ctx, prefix)
	if err != nil {
		t.Fatalf("Present: %v", err)
	}
	if present {
		t.Fatal("a prefix with files but no meta.json must read as NOT present")
	}
	if gen != 0 {
		t.Fatalf("Present generation = %d, want 0 for absent", gen)
	}
}

// TestPresentReportsGeneration proves Present returns the marker's generation
// for a complete volume artifact.
func TestPresentReportsGeneration(t *testing.T) {
	s, _ := newTestStore(t)
	ctx := context.Background()
	srcDir, names := writeLocalArtifact(t, map[string]string{"vol.img": "volbytes", "gen": "12"})
	prefix := "volume/scratch-postgres"
	if _, _, err := s.Export(ctx, prefix, srcDir, names, 12, 1, "", ""); err != nil {
		t.Fatalf("Export: %v", err)
	}
	present, gen, _, _, err := s.Present(ctx, prefix)
	if err != nil {
		t.Fatalf("Present: %v", err)
	}
	if !present || gen != 12 {
		t.Fatalf("Present = (%v, %d), want (true, 12)", present, gen)
	}
}

// TestReachable proves the reachability probe answers true against a live
// endpoint and false against a dead one.
func TestReachable(t *testing.T) {
	s, _ := newTestStore(t)
	if !s.Reachable(context.Background()) {
		t.Fatal("Reachable should be true against a live fake store")
	}
	dead := New("http://127.0.0.1:1", "embervm", false) // nothing listens on port 1
	if dead.Reachable(context.Background()) {
		t.Fatal("Reachable should be false against a dead endpoint")
	}
	var nilStore *Store
	if nilStore.Reachable(context.Background()) {
		t.Fatal("a nil (disabled) store is never reachable")
	}
}

// TestExportRefusesOlderGenerationOverNewer is the split-brain guard. VOLUME keys
// as a singleton (volume/<workload>: no ref, no vendor segment), so every node
// that has ever held the volume writes the SAME object. Observed 2026-07-28 on
// demo-postgres: node-1 exported generation 5076 and node-2 exported generation
// 1472 to the same key minutes apart, and whichever landed last won. Before the
// fence, differing content fell straight through to a full re-upload, so a stale
// node could silently replace the live volume -- data loss for a Postgres volume.
func TestExportRefusesOlderGenerationOverNewer(t *testing.T) {
	s, fake := newTestStore(t)
	ctx := context.Background()
	prefix := "volume/demo-postgres"

	newer, newerNames := writeLocalArtifact(t, map[string]string{"vol.img": "live-data-gen-5076"})
	if _, _, err := s.Export(ctx, prefix, newer, newerNames, 5076, 1, "", ""); err != nil {
		t.Fatalf("seed Export: %v", err)
	}
	putsAfterSeed := len(fake.putOrderCopy())

	// A different node, holding a long-stale copy, exports the same singleton key.
	stale, staleNames := writeLocalArtifact(t, map[string]string{"vol.img": "stale-data-gen-1472"})
	moved, skipped, err := s.Export(ctx, prefix, stale, staleNames, 1472, 2, "", "")

	if !errors.Is(err, ErrStaleGeneration) {
		t.Fatalf("Export err = %v, want ErrStaleGeneration", err)
	}
	if moved != 0 || skipped {
		t.Fatalf("refused Export = (moved=%d, skipped=%v), want (0, false)", moved, skipped)
	}
	if got := len(fake.putOrderCopy()); got != putsAfterSeed {
		t.Fatalf("refused Export issued %d PUTs, want 0 (it must not overwrite)", got-putsAfterSeed)
	}
	// The newer bytes are still what the store holds.
	if got := string(fake.object("/embervm/" + prefix + "/vol.img")); got != "live-data-gen-5076" {
		t.Fatalf("store content = %q, want the newer copy intact", got)
	}
}

// TestExportAllowsNewerGenerationOverOlder is the other half: real progress must
// still land. The fence is strictly-greater on the REMOTE generation, so a newer
// local copy overwrites an older store copy exactly as before.
func TestExportAllowsNewerGenerationOverOlder(t *testing.T) {
	s, _ := newTestStore(t)
	ctx := context.Background()
	prefix := "volume/demo-postgres"

	old, oldNames := writeLocalArtifact(t, map[string]string{"vol.img": "old"})
	if _, _, err := s.Export(ctx, prefix, old, oldNames, 100, 1, "", ""); err != nil {
		t.Fatalf("seed Export: %v", err)
	}

	newer, newerNames := writeLocalArtifact(t, map[string]string{"vol.img": "new"})
	if _, skipped, err := s.Export(ctx, prefix, newer, newerNames, 101, 2, "", ""); err != nil || skipped {
		t.Fatalf("Export = (skipped=%v, %v), want (false, nil): progress must not be fenced", skipped, err)
	}
}

// TestExportAllowsEqualGenerationRepair keeps the repair path open: an equal
// generation with differing content re-uploads, so a partially-written or
// corrupted remote copy at the CURRENT generation can be fixed. Only a strictly
// newer remote generation is refused.
func TestExportAllowsEqualGenerationRepair(t *testing.T) {
	s, _ := newTestStore(t)
	ctx := context.Background()
	prefix := "volume/demo-postgres"

	a, aNames := writeLocalArtifact(t, map[string]string{"vol.img": "corrupt"})
	if _, _, err := s.Export(ctx, prefix, a, aNames, 42, 1, "", ""); err != nil {
		t.Fatalf("seed Export: %v", err)
	}

	b, bNames := writeLocalArtifact(t, map[string]string{"vol.img": "repaired"})
	if _, skipped, err := s.Export(ctx, prefix, b, bNames, 42, 2, "", ""); err != nil || skipped {
		t.Fatalf("Export = (skipped=%v, %v), want a repair re-upload", skipped, err)
	}
}

// TestExportUnknownGenerationCannotWin: generation 0 means "unknown" (the server's
// artifactGeneration returns 0 when it cannot resolve one). It must never
// overwrite a copy with a known, higher generation.
func TestExportUnknownGenerationCannotWin(t *testing.T) {
	s, _ := newTestStore(t)
	ctx := context.Background()
	prefix := "volume/demo-postgres"

	known, knownNames := writeLocalArtifact(t, map[string]string{"vol.img": "known-good"})
	if _, _, err := s.Export(ctx, prefix, known, knownNames, 900, 1, "", ""); err != nil {
		t.Fatalf("seed Export: %v", err)
	}

	unknown, unknownNames := writeLocalArtifact(t, map[string]string{"vol.img": "unknown-gen"})
	if _, _, err := s.Export(ctx, prefix, unknown, unknownNames, 0, 2, "", ""); !errors.Is(err, ErrStaleGeneration) {
		t.Fatalf("Export err = %v, want ErrStaleGeneration for an unknown generation", err)
	}
}
