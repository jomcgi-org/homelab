package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

const (
	testDigest   = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
	testImageRef = "registry.example.test/embervm/guest:test"
)

// fakeObjectStore follows the HTTP helper used by the noded store package
// tests, keeping these tests on the real Store client and its S3 request path.
type fakeObjectStore struct {
	mu           sync.Mutex
	objects      map[string][]byte
	puts         []string
	gets         []string
	heads        []string
	headCount    int
	appearOnHead int
}

func newFakeObjectStore(t *testing.T) (*httptest.Server, *fakeObjectStore) {
	t.Helper()
	fake := &fakeObjectStore{objects: make(map[string][]byte)}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fake.mu.Lock()
		defer fake.mu.Unlock()
		switch r.Method {
		case http.MethodHead:
			fake.headCount++
			fake.heads = append(fake.heads, r.URL.Path)
			if fake.appearOnHead == fake.headCount {
				fake.objects[r.URL.Path] = []byte("winner")
			}
			if _, ok := fake.objects[r.URL.Path]; !ok {
				w.WriteHeader(http.StatusNotFound)
				return
			}
			w.WriteHeader(http.StatusOK)
		case http.MethodGet:
			fake.gets = append(fake.gets, r.URL.Path)
			body, ok := fake.objects[r.URL.Path]
			if !ok {
				w.WriteHeader(http.StatusNotFound)
				return
			}
			w.Header().Set("Content-Length", strconv.Itoa(len(body)))
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write(body)
		case http.MethodPut:
			body, err := io.ReadAll(r.Body)
			if err != nil {
				w.WriteHeader(http.StatusInternalServerError)
				return
			}
			fake.objects[r.URL.Path] = body
			fake.puts = append(fake.puts, r.URL.Path)
			w.WriteHeader(http.StatusOK)
		default:
			w.WriteHeader(http.StatusMethodNotAllowed)
		}
	}))
	t.Cleanup(server.Close)
	return server, fake
}

func storeEnv(endpoint string) getenvFunc {
	values := map[string]string{
		"EMBERVM_NODED_STORE_ENDPOINT":          endpoint,
		"EMBERVM_NODED_STORE_BUCKET":            "embervm",
		"EMBERVM_NODED_STORE_ACCESS_KEY_ID":     "",
		"EMBERVM_NODED_STORE_SECRET_ACCESS_KEY": "",
	}
	return func(key string) string { return values[key] }
}

func testCompletenessMarker(t *testing.T, digest string, contents []byte) (string, []byte) {
	t.Helper()
	sum := sha256.Sum256(contents)
	checksum := hex.EncodeToString(sum[:])
	payloadKey := payloadObjectKey(digest, checksum)
	marker, err := json.Marshal(completenessMarker{
		PayloadKey: payloadKey,
		SHA256:     checksum,
		ImageRef:   testImageRef,
		UploadedAt: "2026-09-05T00:00:00Z",
	})
	if err != nil {
		t.Fatal(err)
	}
	return payloadKey, append(marker, '\n')
}

func TestPutSparseFileStoresNominalSizeAndChecksum(t *testing.T) {
	server, fake := newFakeObjectStore(t)
	path := filepath.Join(t.TempDir(), "rootfs.ext4")
	file, err := os.Create(path)
	if err != nil {
		t.Fatal(err)
	}
	const nominalSize = 1 << 20
	if _, err := file.Write([]byte("ext4")); err != nil {
		t.Fatal(err)
	}
	if _, err := file.Seek(nominalSize-1, io.SeekStart); err != nil {
		t.Fatal(err)
	}
	if _, err := file.Write([]byte{0}); err != nil {
		t.Fatal(err)
	}
	if err := file.Close(); err != nil {
		t.Fatal(err)
	}

	var stdout, stderr bytes.Buffer
	code := run(context.Background(), []string{"put", "--digest", testDigest, "--file", path, "--image-ref", testImageRef}, storeEnv(server.URL), &stdout, &stderr)
	if code != 0 {
		t.Fatalf("put exit = %d, stderr = %q", code, stderr.String())
	}
	if stdout.String() != "uploaded\n" {
		t.Fatalf("put stdout = %q", stdout.String())
	}
	checksumKey := checksumObjectKey(testDigest)
	var marker completenessMarker
	if err := json.Unmarshal(fake.objects["/embervm/"+checksumKey], &marker); err != nil {
		t.Fatalf("decode sidecar: %v", err)
	}
	stored := fake.objects["/embervm/"+marker.PayloadKey]
	if len(stored) != nominalSize {
		t.Fatalf("stored sparse object size = %d, want %d", len(stored), nominalSize)
	}
	sum := sha256.Sum256(stored)
	wantPayloadKey := payloadObjectKey(testDigest, hex.EncodeToString(sum[:]))
	if marker.PayloadKey != wantPayloadKey {
		t.Fatalf("sidecar payload key = %q, want %q", marker.PayloadKey, wantPayloadKey)
	}
	if marker.SHA256 != hex.EncodeToString(sum[:]) {
		t.Fatalf("sidecar checksum = %q, want %q", marker.SHA256, hex.EncodeToString(sum[:]))
	}
	if marker.ImageRef != testImageRef {
		t.Fatalf("sidecar image ref = %q, want %q", marker.ImageRef, testImageRef)
	}
	if _, err := time.Parse(time.RFC3339Nano, marker.UploadedAt); err != nil {
		t.Fatalf("sidecar upload time = %q: %v", marker.UploadedAt, err)
	}
	wantChecksumPath := "/embervm/" + checksumKey
	if len(fake.heads) != 3 || fake.heads[0] != wantChecksumPath || fake.heads[1] != wantChecksumPath || fake.heads[2] != wantChecksumPath {
		t.Fatalf("presence checks = %v, want three sidecar HEADs", fake.heads)
	}
	if len(fake.puts) != 2 || fake.puts[0] != "/embervm/"+wantPayloadKey || fake.puts[1] != wantChecksumPath {
		t.Fatalf("PUT order = %v, want payload then sidecar", fake.puts)
	}
	t.Logf("small sparse fixture stored as a %d-byte object", len(stored))
}

func TestPutSkipsExistingCompletenessMarker(t *testing.T) {
	server, fake := newFakeObjectStore(t)
	checksumKey := checksumObjectKey(testDigest)
	fake.objects["/embervm/"+checksumKey] = []byte("winner")
	path := filepath.Join(t.TempDir(), "rootfs.ext4")
	if err := os.WriteFile(path, []byte("loser"), 0o600); err != nil {
		t.Fatal(err)
	}

	var stdout, stderr bytes.Buffer
	code := run(context.Background(), []string{"put", "--digest", testDigest, "--file", path, "--image-ref", testImageRef}, storeEnv(server.URL), &stdout, &stderr)
	if code != 0 || stdout.String() != "already present\n" {
		t.Fatalf("put exit = %d, stdout = %q, stderr = %q", code, stdout.String(), stderr.String())
	}
	if len(fake.puts) != 0 {
		t.Fatalf("existing object caused PUTs: %v", fake.puts)
	}
}

func TestPutRechecksHeadAfterHashing(t *testing.T) {
	server, fake := newFakeObjectStore(t)
	fake.appearOnHead = 2
	path := filepath.Join(t.TempDir(), "rootfs.ext4")
	if err := os.WriteFile(path, []byte("later writer"), 0o600); err != nil {
		t.Fatal(err)
	}

	var stdout, stderr bytes.Buffer
	code := run(context.Background(), []string{"put", "--digest", testDigest, "--file", path, "--image-ref", testImageRef}, storeEnv(server.URL), &stdout, &stderr)
	if code != 0 || stdout.String() != "already present\n" {
		t.Fatalf("put exit = %d, stdout = %q, stderr = %q", code, stdout.String(), stderr.String())
	}
	if len(fake.puts) != 0 {
		t.Fatalf("concurrent winner caused PUTs: %v", fake.puts)
	}
}

func TestInterruptedPayloadWithoutSidecarIsMissAndCanBeRepaired(t *testing.T) {
	server, fake := newFakeObjectStore(t)
	checksumKey := checksumObjectKey(testDigest)
	checksumPath := "/embervm/" + checksumKey
	orphan := []byte("orphaned partial payload")
	orphanKey, _ := testCompletenessMarker(t, testDigest, orphan)
	fake.objects["/embervm/"+orphanKey] = orphan

	var stdout, stderr bytes.Buffer
	out := filepath.Join(t.TempDir(), "rootfs.ext4")
	code := run(context.Background(), []string{"get", "--digest", testDigest, "--out", out}, storeEnv(server.URL), &stdout, &stderr)
	if code != exitMiss {
		t.Fatalf("get interrupted upload exit = %d, want %d, stderr = %q", code, exitMiss, stderr.String())
	}
	if len(fake.gets) != 1 || fake.gets[0] != checksumPath {
		t.Fatalf("get requests = %v, want only completeness marker %q", fake.gets, checksumPath)
	}

	replacement := []byte("complete replacement payload")
	path := filepath.Join(t.TempDir(), "replacement.ext4")
	if err := os.WriteFile(path, replacement, 0o600); err != nil {
		t.Fatal(err)
	}
	stdout.Reset()
	stderr.Reset()
	code = run(context.Background(), []string{"put", "--digest", testDigest, "--file", path, "--image-ref", testImageRef}, storeEnv(server.URL), &stdout, &stderr)
	if code != 0 || stdout.String() != "uploaded\n" {
		t.Fatalf("repair put exit = %d, stdout = %q, stderr = %q", code, stdout.String(), stderr.String())
	}
	var marker completenessMarker
	if err := json.Unmarshal(fake.objects[checksumPath], &marker); err != nil {
		t.Fatalf("decode repaired marker: %v", err)
	}
	if got := fake.objects["/embervm/"+marker.PayloadKey]; !bytes.Equal(got, replacement) {
		t.Fatalf("repaired payload = %q, want %q", got, replacement)
	}
	if _, ok := fake.objects[checksumPath]; !ok {
		t.Fatal("repair did not publish completeness marker")
	}
}

func TestConcurrentPutWritersLeaveMatchingPayloadAndSidecar(t *testing.T) {
	checksumKey := checksumObjectKey(testDigest)
	checksumPath := "/embervm/" + checksumKey
	payloads := [][]byte{
		[]byte("rootfs with ext4 UUID aaaaaaaa"),
		[]byte("rootfs with ext4 UUID bbbbbbbb"),
	}
	paths := make([]string, len(payloads))
	payloadPaths := make([]string, len(payloads))
	for i, payload := range payloads {
		path := filepath.Join(t.TempDir(), "rootfs.ext4")
		if err := os.WriteFile(path, payload, 0o600); err != nil {
			t.Fatal(err)
		}
		paths[i] = path
		sum := sha256.Sum256(payload)
		payloadPaths[i] = "/embervm/" + payloadObjectKey(testDigest, hex.EncodeToString(sum[:]))
	}
	loserPayloadPath := payloadPaths[1]
	objects := make(map[string][]byte)
	var mu sync.Mutex
	var puts []string
	var gets []string
	var headCalls atomic.Int32
	firstHeadsDone := make(chan struct{})
	secondHeadsDone := make(chan struct{})
	winnerSidecarWritten := make(chan struct{})
	var closeWinnerSidecar sync.Once

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.Method {
		case http.MethodHead:
			mu.Lock()
			_, ok := objects[r.URL.Path]
			mu.Unlock()
			call := headCalls.Add(1)
			switch {
			case call <= 2:
				if call == 2 {
					close(firstHeadsDone)
				}
				<-firstHeadsDone
			case call <= 4:
				if call == 4 {
					close(secondHeadsDone)
				}
				<-secondHeadsDone
			}
			if !ok {
				w.WriteHeader(http.StatusNotFound)
				return
			}
			w.WriteHeader(http.StatusOK)
		case http.MethodGet:
			mu.Lock()
			body, ok := objects[r.URL.Path]
			gets = append(gets, r.URL.Path)
			mu.Unlock()
			if !ok {
				w.WriteHeader(http.StatusNotFound)
				return
			}
			w.Header().Set("Content-Length", strconv.Itoa(len(body)))
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write(body)
		case http.MethodPut:
			body, err := io.ReadAll(r.Body)
			if err != nil {
				w.WriteHeader(http.StatusInternalServerError)
				return
			}
			mu.Lock()
			objects[r.URL.Path] = body
			puts = append(puts, r.URL.Path)
			mu.Unlock()
			if r.URL.Path == checksumPath {
				closeWinnerSidecar.Do(func() { close(winnerSidecarWritten) })
			}
			if r.URL.Path == loserPayloadPath {
				<-winnerSidecarWritten
			}
			w.WriteHeader(http.StatusOK)
		default:
			w.WriteHeader(http.StatusMethodNotAllowed)
		}
	}))
	t.Cleanup(server.Close)

	start := make(chan struct{})
	type result struct {
		code   int
		stdout string
		stderr string
	}
	results := make(chan result, 2)
	for _, path := range paths {
		go func(path string) {
			<-start
			var stdout, stderr bytes.Buffer
			code := run(context.Background(), []string{"put", "--digest", testDigest, "--file", path, "--image-ref", testImageRef}, storeEnv(server.URL), &stdout, &stderr)
			results <- result{code: code, stdout: stdout.String(), stderr: stderr.String()}
		}(path)
	}
	close(start)
	var uploaded, orphaned int
	wantOrphanOutput := "already present; orphan payload " + strings.TrimPrefix(loserPayloadPath, "/embervm/") + " is eligible for retention sweep\n"
	for i := 0; i < 2; i++ {
		got := <-results
		if got.code != 0 {
			t.Fatalf("concurrent put = code %d, stdout %q, stderr %q", got.code, got.stdout, got.stderr)
		}
		switch {
		case got.stdout == "uploaded\n":
			uploaded++
		case got.stdout == wantOrphanOutput:
			orphaned++
		default:
			t.Fatalf("unexpected concurrent put output %q", got.stdout)
		}
	}
	if uploaded != 1 || orphaned != 1 {
		t.Fatalf("concurrent results: uploaded=%d orphaned=%d, want one each", uploaded, orphaned)
	}

	mu.Lock()
	storedMarker := append([]byte(nil), objects[checksumPath]...)
	storedPuts := append([]string(nil), puts...)
	mu.Unlock()
	for i, payloadPath := range payloadPaths {
		if got := objects[payloadPath]; !bytes.Equal(got, payloads[i]) {
			t.Fatalf("stored payload %q = %q, want %q", payloadPath, got, payloads[i])
		}
	}
	var marker completenessMarker
	if err := json.Unmarshal(storedMarker, &marker); err != nil {
		t.Fatalf("decode final marker: %v", err)
	}
	storedPayload, ok := objects["/embervm/"+marker.PayloadKey]
	if !ok {
		t.Fatalf("sidecar names missing payload %q", marker.PayloadKey)
	}
	sum := sha256.Sum256(storedPayload)
	if marker.SHA256 != hex.EncodeToString(sum[:]) {
		t.Fatalf("final marker checksum = %q, want %q", marker.SHA256, hex.EncodeToString(sum[:]))
	}
	if len(storedPuts) != 3 {
		t.Fatalf("concurrent PUTs = %v, want two payload and one marker upload", storedPuts)
	}
	markerPuts := 0
	markerObjects := 0
	for _, key := range storedPuts {
		if key == checksumPath {
			markerPuts++
		}
	}
	for key := range objects {
		if strings.HasSuffix(key, "/"+checksumObjectName) {
			markerObjects++
		}
	}
	if markerPuts != 1 {
		t.Fatalf("sidecar PUT count = %d, want 1; PUTs = %v", markerPuts, storedPuts)
	}
	if markerObjects != 1 {
		t.Fatalf("sidecar object count = %d, want 1", markerObjects)
	}

	mu.Lock()
	gets = nil
	mu.Unlock()
	out := filepath.Join(t.TempDir(), "download.ext4")
	var stdout, stderr bytes.Buffer
	code := run(context.Background(), []string{"get", "--digest", testDigest, "--out", out}, storeEnv(server.URL), &stdout, &stderr)
	if code != 0 {
		t.Fatalf("get after concurrent puts exit = %d, stderr = %q", code, stderr.String())
	}
	downloaded, err := os.ReadFile(out)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(downloaded, storedPayload) {
		t.Fatalf("downloaded payload = %q, want sidecar payload %q", downloaded, storedPayload)
	}
	mu.Lock()
	gotGets := append([]string(nil), gets...)
	mu.Unlock()
	if len(gotGets) != 2 || gotGets[0] != checksumPath || gotGets[1] != "/embervm/"+marker.PayloadKey {
		t.Fatalf("get order = %v, want sidecar then named payload", gotGets)
	}
}

func TestGetVerifiesChecksumAndPublishesOutput(t *testing.T) {
	server, fake := newFakeObjectStore(t)
	contents := []byte("byte-identical-rootfs")
	payloadKey, marker := testCompletenessMarker(t, testDigest, contents)
	checksumKey := checksumObjectKey(testDigest)
	fake.objects["/embervm/"+payloadKey] = contents
	fake.objects["/embervm/"+checksumKey] = marker
	out := filepath.Join(t.TempDir(), "nested", "rootfs.ext4")

	var stdout, stderr bytes.Buffer
	code := run(context.Background(), []string{"get", "--digest", "sha256:" + testDigest, "--out", out}, storeEnv(server.URL), &stdout, &stderr)
	if code != 0 {
		t.Fatalf("get exit = %d, stderr = %q", code, stderr.String())
	}
	got, err := os.ReadFile(out)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got, contents) {
		t.Fatalf("downloaded contents = %q, want %q", got, contents)
	}
}

func TestGetMissExitsThree(t *testing.T) {
	server, _ := newFakeObjectStore(t)
	var stdout, stderr bytes.Buffer
	code := run(context.Background(), []string{"get", "--digest", testDigest, "--out", filepath.Join(t.TempDir(), "rootfs.ext4")}, storeEnv(server.URL), &stdout, &stderr)
	if code != exitMiss {
		t.Fatalf("get miss exit = %d, want %d, stderr = %q", code, exitMiss, stderr.String())
	}
}

func TestGetMalformedOrOversizedSidecarFails(t *testing.T) {
	for _, tc := range []struct {
		name    string
		sidecar []byte
		error   string
	}{
		{name: "malformed", sidecar: []byte(`{"sha256":`), error: "decode completeness marker"},
		{name: "oversized", sidecar: bytes.Repeat([]byte("x"), maxMarkerBytes+1), error: "exceeds 1024 bytes"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			server, fake := newFakeObjectStore(t)
			checksumKey := checksumObjectKey(testDigest)
			fake.objects["/embervm/"+checksumKey] = tc.sidecar
			out := filepath.Join(t.TempDir(), "rootfs.ext4")

			var stdout, stderr bytes.Buffer
			code := run(context.Background(), []string{"get", "--digest", testDigest, "--out", out}, storeEnv(server.URL), &stdout, &stderr)
			if code != exitFailure {
				t.Fatalf("get exit = %d, want %d, stderr = %q", code, exitFailure, stderr.String())
			}
			if !strings.Contains(stderr.String(), tc.error) {
				t.Fatalf("get stderr = %q, want %q", stderr.String(), tc.error)
			}
			if len(fake.gets) != 1 || fake.gets[0] != "/embervm/"+checksumKey {
				t.Fatalf("get requests = %v, want only completeness marker", fake.gets)
			}
			if _, err := os.Stat(out); !os.IsNotExist(err) {
				t.Fatalf("output exists after invalid sidecar, stat error = %v", err)
			}
		})
	}
}

func TestGetChecksumMismatchDoesNotPublishOutput(t *testing.T) {
	server, fake := newFakeObjectStore(t)
	checksum := strings.Repeat("0", 64)
	payloadKey := payloadObjectKey(testDigest, checksum)
	checksumKey := checksumObjectKey(testDigest)
	fake.objects["/embervm/"+payloadKey] = []byte("corrupt")
	marker, err := json.Marshal(completenessMarker{
		PayloadKey: payloadKey,
		SHA256:     checksum,
		ImageRef:   testImageRef,
		UploadedAt: "2026-09-05T00:00:00Z",
	})
	if err != nil {
		t.Fatal(err)
	}
	fake.objects["/embervm/"+checksumKey] = marker
	out := filepath.Join(t.TempDir(), "rootfs.ext4")

	var stdout, stderr bytes.Buffer
	code := run(context.Background(), []string{"get", "--digest", testDigest, "--out", out}, storeEnv(server.URL), &stdout, &stderr)
	if code == 0 || code == exitMiss {
		t.Fatalf("checksum mismatch exit = %d, stderr = %q", code, stderr.String())
	}
	if _, err := os.Stat(out); !os.IsNotExist(err) {
		t.Fatalf("corrupt output exists, stat error = %v", err)
	}
}

func TestPutNonexistentFileFailsClearly(t *testing.T) {
	server, fake := newFakeObjectStore(t)
	path := filepath.Join(t.TempDir(), "missing-rootfs.ext4")

	var stdout, stderr bytes.Buffer
	code := run(context.Background(), []string{"put", "--digest", testDigest, "--file", path, "--image-ref", testImageRef}, storeEnv(server.URL), &stdout, &stderr)
	if code != exitFailure {
		t.Fatalf("put exit = %d, want %d, stderr = %q", code, exitFailure, stderr.String())
	}
	if !strings.Contains(stderr.String(), "open rootfs") || !strings.Contains(stderr.String(), "no such file") {
		t.Fatalf("put stderr = %q, want clear missing-file error", stderr.String())
	}
	if len(fake.puts) != 0 {
		t.Fatalf("missing file caused PUTs: %v", fake.puts)
	}
}

func TestDisabledStoreExitsThreeImmediately(t *testing.T) {
	for _, args := range [][]string{
		{"get", "--digest", "invalid", "--out", "ignored"},
		{"put", "--digest", "invalid", "--file", "ignored"},
	} {
		var stdout, stderr bytes.Buffer
		if code := run(context.Background(), args, storeEnv(""), &stdout, &stderr); code != exitMiss {
			t.Fatalf("%s disabled exit = %d, want %d", args[0], code, exitMiss)
		}
	}
}

func TestStoreOperationTimeoutsExitAsFailures(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		<-r.Context().Done()
	}))
	t.Cleanup(server.Close)
	path := filepath.Join(t.TempDir(), "rootfs.ext4")
	if err := os.WriteFile(path, []byte("rootfs"), 0o600); err != nil {
		t.Fatal(err)
	}

	for _, tc := range []struct {
		name string
		args []string
	}{
		{name: "get", args: []string{"get", "--timeout", "25ms", "--digest", testDigest, "--out", filepath.Join(t.TempDir(), "download.ext4")}},
		{name: "put", args: []string{"put", "--timeout", "25ms", "--digest", testDigest, "--file", path, "--image-ref", testImageRef}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var stdout, stderr bytes.Buffer
			code := run(context.Background(), tc.args, storeEnv(server.URL), &stdout, &stderr)
			if code != exitFailure {
				t.Fatalf("timeout exit = %d, want %d, stderr = %q", code, exitFailure, stderr.String())
			}
			if !strings.Contains(stderr.String(), context.DeadlineExceeded.Error()) {
				t.Fatalf("timeout stderr = %q, want deadline error", stderr.String())
			}
		})
	}
}
