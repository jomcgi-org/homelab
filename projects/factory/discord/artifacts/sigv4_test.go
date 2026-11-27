package artifacts

import (
	"context"
	"net/http"
	"testing"
	"time"
)

// TestSignatureIsStable pins the SigV4 Authorization header for a fixed
// clock and key. The values were checked against Garage v2.5.0, which
// accepted the same client's put, list and delete; a change to the
// canonical request fails here before it fails against Garage.
func TestSignatureIsStable(t *testing.T) {
	c, err := NewS3("http://garage.garage.svc:3900", "garage", "bosun-artifacts", "GKtest", "secret")
	if err != nil {
		t.Fatal(err)
	}
	c.now = func() time.Time { return time.Date(2026, 11, 27, 12, 0, 0, 0, time.UTC) }
	var got []string
	c.http = &http.Client{Transport: roundTrip(func(r *http.Request) (*http.Response, error) {
		got = append(got, r.Method+" "+r.URL.RequestURI()+" "+r.Header.Get("Authorization"))
		return &http.Response{StatusCode: http.StatusNoContent, Body: http.NoBody, Request: r}, nil
	})}
	ctx := context.Background()
	_ = c.Put(ctx, "1501965852042330302/1/a b.html", []byte("<h1>x</h1>"), "text/html; charset=utf-8")
	_, _, _ = c.List(ctx, "1501965852042330302/1/", 200)
	_ = c.Delete(ctx, "1501965852042330302/1/a b.html")
	want := []string{
		"PUT /bosun-artifacts/1501965852042330302/1/a%20b.html AWS4-HMAC-SHA256 Credential=GKtest/20261127/garage/s3/aws4_request,SignedHeaders=cache-control;content-type;host;x-amz-content-sha256;x-amz-date,Signature=fea433bdd1f68a42a797a1f09c8b2838880c3f9bfcdbb22ebaaca7900f0b4bb0",
		"GET /bosun-artifacts?list-type=2&max-keys=200&prefix=1501965852042330302%2F1%2F AWS4-HMAC-SHA256 Credential=GKtest/20261127/garage/s3/aws4_request,SignedHeaders=host;x-amz-content-sha256;x-amz-date,Signature=e76746ef4a74248f3732fbadc8dea2b6e1bbc797b86b2bfb50e6149d689d6b97",
		"DELETE /bosun-artifacts/1501965852042330302/1/a%20b.html AWS4-HMAC-SHA256 Credential=GKtest/20261127/garage/s3/aws4_request,SignedHeaders=host;x-amz-content-sha256;x-amz-date,Signature=ec987c602592adda8ee1f5b1bac7313c1b0df14d391f69c661e507dc03689fd2",
	}
	for i := range want {
		if i >= len(got) || got[i] != want[i] {
			t.Errorf("request %d:\n got %q\nwant %q", i, at(got, i), want[i])
		}
	}
}

type roundTrip func(*http.Request) (*http.Response, error)

func (f roundTrip) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func at(s []string, i int) string {
	if i < len(s) {
		return s[i]
	}
	return ""
}
