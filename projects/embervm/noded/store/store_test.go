package store

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
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
	putOrder []string
}

func newFakeObjectStore() *fakeObjectStore {
	return &fakeObjectStore{objects: make(map[string][]byte)}
}

func (f *fakeObjectStore) handler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		key := r.URL.Path
		switch r.Method {
		case http.MethodPut:
			body, _ := io.ReadAll(r.Body)
			f.mu.Lock()
			f.objects[key] = body
			f.putOrder = append(f.putOrder, key)
			f.mu.Unlock()
			w.WriteHeader(http.StatusOK)
		case http.MethodGet:
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

func (f *fakeObjectStore) has(key string) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	_, ok := f.objects[key]
	return ok
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
	t.Helper()
	fake := newFakeObjectStore()
	srv := httptest.NewServer(fake.handler())
	t.Cleanup(srv.Close)
	return New(srv.URL, "embervm"), fake
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
	if New("", "embervm") != nil {
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
	dead := New("http://127.0.0.1:1", "embervm") // nothing listens on port 1
	if dead.Reachable(context.Background()) {
		t.Fatal("Reachable should be false against a dead endpoint")
	}
	var nilStore *Store
	if nilStore.Reachable(context.Background()) {
		t.Fatal("a nil (disabled) store is never reachable")
	}
}
