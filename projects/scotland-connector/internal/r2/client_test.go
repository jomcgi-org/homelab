package r2

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// The "GET Object" example from the AWS SigV4 documentation for S3
// (sig-v4-header-based-auth), signed with the documented example key pair.
func TestAuthorizationMatchesAWSExample(t *testing.T) {
	req, err := http.NewRequest(http.MethodGet, "https://examplebucket.s3.amazonaws.com/test.txt", nil)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Range", "bytes=0-9")
	req.Header.Set("X-Amz-Content-Sha256", emptyPayloadHash)
	req.Header.Set("X-Amz-Date", "20130524T000000Z")

	got := authorization(req, []string{"host", "range", "x-amz-content-sha256", "x-amz-date"},
		"20130524T000000Z", "us-east-1",
		Credentials{AccessKeyID: "AKIAIOSFODNN7EXAMPLE", SecretAccessKey: "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"})

	want := "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request," +
		"SignedHeaders=host;range;x-amz-content-sha256;x-amz-date," +
		"Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
	if got != want {
		t.Fatalf("authorization mismatch\n got: %s\nwant: %s", got, want)
	}
}

func TestCanonicalURIEncodesReservedBytes(t *testing.T) {
	if got := canonicalURI("/stars/a b+c.json"); got != "/stars/a%20b%2Bc.json" {
		t.Fatalf("canonicalURI = %q", got)
	}
}

func TestGetConditionalAndErrors(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !strings.HasPrefix(r.Header.Get("Authorization"), "AWS4-HMAC-SHA256 Credential=key/") {
			t.Errorf("missing signature: %q", r.Header.Get("Authorization"))
		}
		switch r.URL.Path {
		case "/stars/sites.json":
			if r.Header.Get("If-None-Match") == `"v1"` {
				w.WriteHeader(http.StatusNotModified)
				return
			}
			w.Header().Set("ETag", `"v1"`)
			_, _ = w.Write([]byte(`{"sites":[]}`))
		case "/stars/big.json":
			w.Header().Set("ETag", `"big"`)
			_, _ = w.Write([]byte(strings.Repeat("x", 100)))
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer srv.Close()

	c, err := New(srv.URL, "auto", Credentials{AccessKeyID: "key", SecretAccessKey: "secret"}, srv.Client())
	if err != nil {
		t.Fatal(err)
	}
	c.now = func() time.Time { return time.Date(2026, 10, 19, 12, 0, 0, 0, time.UTC) }
	ctx := context.Background()

	obj, err := c.Get(ctx, "stars", "sites.json", "", 1<<20)
	if err != nil || obj.ETag != `"v1"` || string(obj.Body) != `{"sites":[]}` {
		t.Fatalf("first get = %+v, %v", obj, err)
	}
	obj, err = c.Get(ctx, "stars", "sites.json", `"v1"`, 1<<20)
	if err != nil || !obj.NotModified || obj.ETag != `"v1"` {
		t.Fatalf("conditional get = %+v, %v", obj, err)
	}
	if _, err := c.Get(ctx, "stars", "missing.json", "", 1<<20); !errors.Is(err, ErrNotFound) {
		t.Fatalf("missing object error = %v", err)
	}
	if _, err := c.Get(ctx, "stars", "big.json", "", 10); err == nil {
		t.Fatal("oversized object was accepted")
	}
}

func TestNewRejectsBadConfig(t *testing.T) {
	creds := Credentials{AccessKeyID: "a", SecretAccessKey: "b"}
	for _, endpoint := range []string{"", "r2.example.com", "ftp://r2.example.com"} {
		if _, err := New(endpoint, "auto", creds, nil); err == nil {
			t.Errorf("New(%q) accepted", endpoint)
		}
	}
	if _, err := New("https://r2.example.com", "auto", Credentials{}, nil); err == nil {
		t.Error("New accepted empty credentials")
	}
}
