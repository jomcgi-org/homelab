package store

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
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
	canonical := canonicalRequest(req, signedHeaders, req.Header.Get("x-amz-content-sha256"))
	scope := "20130524/us-east-1/s3/aws4_request"
	stringToSign := "AWS4-HMAC-SHA256\n20130524T000000Z\n" + scope + "\n" + sha256Hex(canonical)
	got := calculateSignature(
		"wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
		"20130524", "us-east-1", "s3", stringToSign,
	)
	const want = "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
	if got != want {
		t.Fatalf("signature = %s, want %s\ncanonical request:\n%s", got, want, canonical)
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
