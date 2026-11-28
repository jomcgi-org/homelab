package artifacts

import (
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// AWS's published presigned-URL example (Signature Version 4, "Example: A
// GET request with authentication information in the query string").
func TestPresignMatchesTheAWSExample(t *testing.T) {
	at := time.Date(2013, 5, 24, 0, 0, 0, 0, time.UTC)
	q := presign("examplebucket.s3.amazonaws.com", "/test.txt", "us-east-1",
		"AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", at, 24*time.Hour)
	if !strings.HasSuffix(q, "&X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404") {
		t.Fatalf("signature: %s", q)
	}
}

func sharer(t *testing.T) *Sharer {
	t.Helper()
	dir := t.TempDir()
	for name, v := range map[string]string{"AWS_ACCESS_KEY_ID": "GKshare\n", "AWS_SECRET_ACCESS_KEY": "s3cret"} {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(v), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	at := time.Date(2026, 11, 28, 9, 0, 0, 0, time.UTC)
	return &Sharer{BaseURL: "https://artifact.jomcgi.dev", Region: "garage", Bucket: "bosun-artifacts", KeyDir: dir, Now: func() time.Time { return at }}
}

// The link's path is /s/<key>; the signature covers what the edge forwards
// to Garage: host artifact.jomcgi.dev and path /bosun-artifacts/<key>.
func TestLinkSignsTheForwardedRequest(t *testing.T) {
	s := sharer(t)
	key := guild + "/" + channel + "/reports/q3.html"
	link, expires, err := s.Link(key, 2*time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	u, err := url.Parse(link)
	if err != nil {
		t.Fatal(err)
	}
	if u.Host != "artifact.jomcgi.dev" || u.Path != "/s/"+key {
		t.Fatalf("link %s", link)
	}
	q := u.Query()
	if q.Get("X-Amz-Expires") != "7200" || q.Get("X-Amz-Credential") != "GKshare/20261128/garage/s3/aws4_request" ||
		q.Get("X-Amz-SignedHeaders") != "host" {
		t.Fatalf("query %v", q)
	}
	if !expires.Equal(time.Date(2026, 11, 28, 11, 0, 0, 0, time.UTC)) {
		t.Fatalf("expires %s", expires)
	}
	want := presign("artifact.jomcgi.dev", "/bosun-artifacts/"+key, "garage", "GKshare", "s3cret",
		time.Date(2026, 11, 28, 9, 0, 0, 0, time.UTC), 2*time.Hour)
	if u.RawQuery != want {
		t.Fatalf("query %s, want %s", u.RawQuery, want)
	}
	// Another key, lifetime or secret gives another signature.
	for _, other := range []string{
		presign("artifact.jomcgi.dev", "/bosun-artifacts/"+key+"x", "garage", "GKshare", "s3cret", time.Date(2026, 11, 28, 9, 0, 0, 0, time.UTC), 2*time.Hour),
		presign("artifact.jomcgi.dev", "/bosun-artifacts/"+key, "garage", "GKshare", "other", time.Date(2026, 11, 28, 9, 0, 0, 0, time.UTC), 2*time.Hour),
	} {
		if other[strings.LastIndex(other, "="):] == want[strings.LastIndex(want, "="):] {
			t.Fatal("signature does not depend on the request")
		}
	}
}

func TestLinkRefusesBadLifetimesAndAnEmptyKey(t *testing.T) {
	s := sharer(t)
	for _, ttl := range []time.Duration{0, 30 * time.Second, MaxShareTTL + time.Second} {
		if _, _, err := s.Link("a/b/c.html", ttl); err == nil {
			t.Errorf("ttl %s accepted", ttl)
		}
	}
	if _, _, err := s.Link("a/b/c.html", MaxShareTTL); err != nil {
		t.Errorf("seven days refused: %v", err)
	}
	if err := os.WriteFile(filepath.Join(s.KeyDir, "AWS_SECRET_ACCESS_KEY"), nil, 0o600); err != nil {
		t.Fatal(err)
	}
	if _, _, err := s.Link("a/b/c.html", time.Hour); err == nil {
		t.Error("empty secret accepted")
	}
}

func TestShareKeyAllowsOnlyTheCurrentChannel(t *testing.T) {
	base := "https://artifact.jomcgi.dev"
	ok := map[string]string{
		"report.html":                                       guild + "/" + channel + "/report.html",
		guild + "/" + channel + "/charts/q3.png":            guild + "/" + channel + "/charts/q3.png",
		"private/" + channel + "/notes.md":                  "private/" + channel + "/notes.md",
		base + "/" + guild + "/" + channel + "/report.html": guild + "/" + channel + "/report.html",
		base + "/private/" + channel + "/notes.md":          "private/" + channel + "/notes.md",
	}
	for ref, want := range ok {
		got, err := ShareKey(base, guild, channel, ref)
		if err != nil || got != want {
			t.Errorf("%s: %q %v, want %q", ref, got, err, want)
		}
	}
	for _, ref := range []string{
		"https://evil.example/" + guild + "/" + channel + "/a.html",
		"/" + guild + "/" + channel + "/a.html",
		"../a.html",
		"private/" + channel + "/../x.html",
		"a.js",
		"",
	} {
		if got, err := ShareKey(base, guild, channel, ref); err == nil {
			t.Errorf("%q accepted as %q", ref, got)
		}
	}
	if _, err := ShareKey(base, "", channel, "a.html"); err == nil {
		t.Error("a direct message channel accepted")
	}
}
