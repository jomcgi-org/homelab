package store

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/shared/s3sigv4"
)

func TestSigV4AWSKnownAnswer(t *testing.T) {
	req, err := http.NewRequest(http.MethodGet, "https://examplebucket.s3.amazonaws.com/test.txt", nil)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Range", "bytes=0-9")
	req.Header.Set("x-amz-content-sha256", "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
	req.Header.Set("x-amz-date", "20130524T000000Z")
	signedHeaders := []string{"host", "range", "x-amz-content-sha256", "x-amz-date"}
	timestamp, _ := time.Parse("20060102T150405Z", "20130524T000000Z")
	result := s3sigv4.Sign(req, s3sigv4.Input{
		SecretAccessKey: "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", Region: "us-east-1",
		Timestamp: timestamp, PayloadHash: req.Header.Get("x-amz-content-sha256"), SignedHeaders: signedHeaders,
	})
	got := result.Signature
	const want = "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
	if got != want {
		t.Fatalf("signature = %s, want %s", got, want)
	}
}

func TestSignNoCredentialsLeavesRequestUntouched(t *testing.T) {
	s := New("http://example.test", "embervm", false)
	req, err := http.NewRequest(http.MethodGet, s.url("base/a/meta.json"), nil)
	if err != nil {
		t.Fatal(err)
	}
	before := req.Header.Clone()
	if err := s.sign(req); err != nil {
		t.Fatal(err)
	}
	if len(req.Header) != len(before) || req.Header.Get("Authorization") != "" || req.Header.Get("x-amz-date") != "" {
		t.Fatalf("anonymous signing changed headers: %#v", req.Header)
	}
}

func TestAnonymousRequestSendsNoAuthorizationHeader(t *testing.T) {
	authorization := make(chan string, 1)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		authorization <- req.Header.Get("Authorization")
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()
	s := New(srv.URL, "embervm", false)
	if ok, err := s.Head(context.Background(), "base/amd/demo/meta.json"); err != nil || !ok {
		t.Fatalf("Head = %v, %v", ok, err)
	}
	if got := <-authorization; got != "" {
		t.Fatalf("Authorization = %q, want absent", got)
	}
}

func TestSignUsesPinnedClock(t *testing.T) {
	s := New("http://example.test", "embervm", false, WithCredentials("id", "secret"))
	s.now = func() time.Time { return time.Date(2026, 8, 21, 12, 34, 56, 0, time.UTC) }
	req, _ := http.NewRequest(http.MethodGet, s.url("base/a/meta.json"), nil)
	if err := s.sign(req); err != nil {
		t.Fatal(err)
	}
	if got := req.Header.Get("x-amz-date"); got != "20260821T123456Z" {
		t.Fatalf("x-amz-date = %q", got)
	}
}

func TestSignAuthenticatesIfMatch(t *testing.T) {
	s := New("http://example.test", "embervm", false, WithCredentials("id", "secret"))
	req, _ := http.NewRequest(http.MethodPut, s.url("session/amd/demo/ref/meta.json"), nil)
	req.Header.Set("If-Match", `"quoted-etag"`)
	if err := s.sign(req); err != nil {
		t.Fatal(err)
	}
	if got := req.Header.Get("Authorization"); !strings.Contains(got, "SignedHeaders=host;if-match;x-amz-content-sha256;x-amz-date") {
		t.Fatalf("Authorization does not sign If-Match: %q", got)
	}
}

type createOnlyTransport func(*http.Request) (*http.Response, error)

func (f createOnlyTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	return f(req)
}

func TestPutIfAbsentSignsBackendPrecondition(t *testing.T) {
	for _, tc := range []struct {
		name     string
		endpoint string
		gcs      bool
	}{
		{name: "gcs", endpoint: "https://storage.googleapis.com", gcs: true},
		{name: "gcs-port", endpoint: "https://storage.googleapis.com:443", gcs: true},
		{name: "gcs-bucket", endpoint: "https://bucket.storage.googleapis.com", gcs: true},
		{name: "s3", endpoint: "https://s3.example.test"},
		{name: "not-gcs", endpoint: "https://notstorage.googleapis.com"},
		{name: "not-gcs-suffix", endpoint: "https://storage.googleapis.com.example.test"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var stored string
			fake := &fakeObjectStore{accessKeyID: "test-id", secretAccessKey: "test-secret"}
			client := &http.Client{Transport: createOnlyTransport(func(req *http.Request) (*http.Response, error) {
				if req.Method != http.MethodPut {
					t.Fatalf("method = %q, want PUT", req.Method)
				}
				precondition := "if-none-match"
				if tc.gcs {
					precondition = "x-goog-if-generation-match"
					if req.Header.Get(precondition) != "0" || req.Header.Get("If-None-Match") != "" {
						t.Fatalf("GCS create-only headers = %v", req.Header)
					}
				} else if req.Header.Get(precondition) != "*" || req.Header.Get("x-goog-if-generation-match") != "" {
					t.Fatalf("S3 create-only headers = %v", req.Header)
				}
				wantSigned := "SignedHeaders=content-type;host;"
				if !tc.gcs {
					wantSigned += precondition + ";"
				}
				wantSigned += "x-amz-content-sha256;x-amz-date"
				if tc.gcs {
					wantSigned += ";" + precondition
				}
				if !strings.Contains(req.Header.Get("Authorization"), wantSigned+",") {
					t.Fatalf("precondition is not signed: %q", req.Header.Get("Authorization"))
				}
				// Simulate the Host value net/http sends and verify with the
				// independent fake-store signer, not production helpers.
				received := req.Clone(req.Context())
				received.Host = req.URL.Host
				if code := fake.verifySigV4(received); code != "" {
					t.Fatalf("signature verification failed: %s", code)
				}
				body, err := io.ReadAll(req.Body)
				if err != nil {
					return nil, err
				}
				status := http.StatusOK
				// GCS ignores If-None-Match on PUT. Only its generation
				// precondition can prevent the second writer overwriting.
				conditional := req.Header.Get("x-goog-if-generation-match") == "0"
				if !tc.gcs {
					conditional = req.Header.Get("If-None-Match") == "*"
				}
				if conditional && stored != "" {
					status = http.StatusPreconditionFailed
				} else {
					stored = string(body)
				}
				return &http.Response{StatusCode: status, Body: io.NopCloser(strings.NewReader(""))}, nil
			})}
			s := New(tc.endpoint, "embervm", false, WithCredentials(fake.accessKeyID, fake.secretAccessKey), WithHTTPClient(client))
			if created, err := s.PutIfAbsent(context.Background(), "rootfs/marker", strings.NewReader("winner"), 6); err != nil || !created {
				t.Fatalf("first create = %v, %v, want true, nil", created, err)
			}
			if created, err := s.PutIfAbsent(context.Background(), "rootfs/marker", strings.NewReader("loser"), 5); err != nil || created {
				t.Fatalf("second create = %v, %v, want false, nil", created, err)
			}
			if stored != "winner" {
				t.Fatalf("stored marker = %q, want winner", stored)
			}
		})
	}
}

func TestSignRequestIncompleteCredentialsIsStrictNoop(t *testing.T) {
	for _, s := range []*Store{nil, New("https://example.test", "bucket", false), New("https://example.test", "bucket", false, WithCredentials("id", "")), New("https://example.test", "bucket", false, WithCredentials("", "secret"))} {
		req, _ := http.NewRequest(http.MethodGet, "https://example.test/bucket/key", nil)
		req.Header.Set("Authorization", "existing")
		req.Header.Set("X-Amz-Date", "existing-date")
		before := req.Header.Clone()
		if err := s.SignRequest(req); err != nil {
			t.Fatal(err)
		}
		if !reflect.DeepEqual(before, req.Header) {
			t.Fatalf("headers changed: %v", req.Header)
		}
	}
}

// Expected signature was derived with Python hashlib/hmac over literal
// canonical bytes, independently of the production signer.
func TestSignRequestPolicyAndPathBytes(t *testing.T) {
	s := New("https://url.test", "bucket", false, WithCredentials("id", "secret"))
	s.now = func() time.Time { return time.Date(2026, 8, 21, 12, 34, 56, 0, time.UTC) }
	req, _ := http.NewRequest(http.MethodPut, "https://url.test/bucket/a%2Fb//./../%25?z=1&%C3%A9=1", nil)
	req.Host = "override.test"
	req.Header.Set("If-Match", `"etag"`)
	req.Header.Set("X-Custom", " one\t two ")
	req.Header.Set("Authorization", "old")
	if err := s.SignRequest(req); err != nil {
		t.Fatal(err)
	}
	const want = "AWS4-HMAC-SHA256 Credential=id/20260821/us-east-1/s3/aws4_request, SignedHeaders=host;if-match;x-amz-content-sha256;x-amz-date;x-custom, Signature=1c6b2ecbf6c17c5b3990e357d3e8b60c0a853b4dbf0f89fc2bdec1cb67119009"
	if got := req.Header.Get("Authorization"); got != want {
		t.Fatalf("Authorization = %q, want %q", got, want)
	}
	if got := req.Header.Get("X-Amz-Content-Sha256"); got != "UNSIGNED-PAYLOAD" {
		t.Fatalf("payload hash = %q", got)
	}
	if got := req.URL.EscapedPath(); got != "/bucket/a%2Fb//./../%25" {
		t.Fatalf("path = %q", got)
	}
}
