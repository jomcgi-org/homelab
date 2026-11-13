package s3sigv4

import (
	"net/http"
	"reflect"
	"testing"
	"time"
)

// Canonical requests are fixed protocol fixtures. Signatures were derived
// independently with Python hashlib/hmac over these literal canonical bytes.
func TestFixedVectors(t *testing.T) {
	for _, tc := range []struct{ name, url, canonical, signature string }{
		{"encoded-keys", "https://url.test/bucket/key?z=1&%C3%A9=1", "GET\n/bucket/key\n%C3%A9=1&z=1\nhost:override.test:9443\nx-test:one two three\n\nhost;x-test\nUNSIGNED-PAYLOAD", "f851c2965a6749d02926bc53acf1b9d329163a6db6367303d85bfccb74daf99b"},
		{"encoded-values", "https://url.test/bucket/key?k=z&k=%C3%A9&empty=", "GET\n/bucket/key\nempty=&k=%C3%A9&k=z\nhost:override.test:9443\nx-test:one two three\n\nhost;x-test\nUNSIGNED-PAYLOAD", "cd4fee74fdd8b9a473c925c06afc70141c6546c8ba043234111e679208a68ef6"},
		{"query-bytes", "https://url.test/?s=a+b&s=a%2Bb&%E9%9B%AA=%25&empty&x=&x=1", "GET\n/\n%E9%9B%AA=%25&empty=&s=a%20b&s=a%2Bb&x=&x=1\nhost:override.test:9443\nx-test:one two three\n\nhost;x-test\nUNSIGNED-PAYLOAD", "142e9f6aa5485613aafcdc42bfd5b6eb70ca3ed3bc5a766f2c9eda4bb87a8c8b"},
		{"reserved", "https://url.test/bucket/a%20b%2Bc/%E9%9B%AA%3F%23%3A%40%26%3D", "GET\n/bucket/a%20b%2Bc/%E9%9B%AA%3F%23%3A%40%26%3D\n\nhost:override.test:9443\nx-test:one two three\n\nhost;x-test\nUNSIGNED-PAYLOAD", "afc74e2387a53b4aa489b9d33508af3cec0628760c932a0f64813bd2ed3412ee"},
		{"literal-percent", "https://url.test/bucket/a%252Fb%25", "GET\n/bucket/a%252Fb%25\n\nhost:override.test:9443\nx-test:one two three\n\nhost;x-test\nUNSIGNED-PAYLOAD", "35e30b6466e160dd0be82d30157827583f8822490d2f312e2a1fe6423d89175a"},
		{"escaped-slash", "https://url.test/bucket/a%2Fb", "GET\n/bucket/a%2Fb\n\nhost:override.test:9443\nx-test:one two three\n\nhost;x-test\nUNSIGNED-PAYLOAD", "3f73253feb4dfaab42a907b2fd698eef3c88c26c5f672554af1ded9ef20a152d"},
		{"empty", "https://url.test", "GET\n/\n\nhost:override.test:9443\nx-test:one two three\n\nhost;x-test\nUNSIGNED-PAYLOAD", "917618d6634e0783041f1d6f73004dcd0752f0223646bf363b39ec0ffd415de1"},
		{"no-cleaning", "https://url.test/bucket//./a/../b/", "GET\n/bucket//./a/../b/\n\nhost:override.test:9443\nx-test:one two three\n\nhost;x-test\nUNSIGNED-PAYLOAD", "52550d008f04713d55450510ce02dec9e3bfc7fe7386dfd07ae76cc7c0b4e4ad"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			req, err := http.NewRequest(http.MethodGet, tc.url, nil)
			if err != nil {
				t.Fatal(err)
			}
			req.Host = "override.test:9443"
			req.Header.Set("X-Test", "  one\t two   three \t")
			names := []string{"X-Test", "host", "x-test"}
			original := append([]string(nil), names...)
			headers := req.Header.Clone()
			now := time.Date(2026, 8, 21, 12, 34, 56, 0, time.UTC)
			got := Sign(req, Input{SecretAccessKey: "secret", Region: "us-east-1", Timestamp: now, PayloadHash: "UNSIGNED-PAYLOAD", SignedHeaders: names})
			if canonical := canonicalRequest(req, []string{"host", "x-test"}, "UNSIGNED-PAYLOAD"); canonical != tc.canonical {
				t.Fatalf("canonical = %q, want %q", canonical, tc.canonical)
			}
			if got.Signature != tc.signature || got.Scope != "20260821/us-east-1/s3/aws4_request" || got.SignedHeaders != "host;x-test" {
				t.Fatalf("result = %+v, want signature %s", got, tc.signature)
			}
			if !reflect.DeepEqual(names, original) || !reflect.DeepEqual(headers, req.Header) {
				t.Fatal("signing mutated inputs")
			}
		})
	}
}

func TestHostFallbackAndMultipleHeaderValues(t *testing.T) {
	req, _ := http.NewRequest(http.MethodGet, "https://url.test", nil)
	req.Host = ""
	req.Header.Add("X-Test", " a   b ")
	req.Header.Add("X-Test", "c\td")
	const want = "GET\n/\n\nhost:url.test\nx-test:a b,c d\n\nhost;x-test\nhash"
	if got := canonicalRequest(req, []string{"host", "x-test"}, "hash"); got != want {
		t.Fatalf("canonical = %q, want %q", got, want)
	}
}

func TestEscapePath(t *testing.T) {
	for _, tc := range []struct{ path, want string }{
		{"", "/"},
		{"/bucket/a b+c/雪?#:@&=", "/bucket/a%20b%2Bc/%E9%9B%AA%3F%23%3A%40%26%3D"},
		{"/bucket/a%2Fb%", "/bucket/a%252Fb%25"},
		{"/bucket//./a/../b/", "/bucket//./a/../b/"},
	} {
		if got := EscapePath(tc.path); got != tc.want {
			t.Errorf("EscapePath(%q) = %q, want %q", tc.path, got, tc.want)
		}
	}
}

func TestAWSKnownAnswer(t *testing.T) {
	req, _ := http.NewRequest(http.MethodGet, "https://examplebucket.s3.amazonaws.com/test.txt", nil)
	req.Header.Set("Range", "bytes=0-9")
	const payload = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
	req.Header.Set("X-Amz-Content-Sha256", payload)
	req.Header.Set("X-Amz-Date", "20130524T000000Z")
	got := Sign(req, Input{SecretAccessKey: "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", Region: "us-east-1", Timestamp: time.Date(2013, 5, 24, 0, 0, 0, 0, time.UTC), PayloadHash: payload, SignedHeaders: []string{"host", "range", "x-amz-content-sha256", "x-amz-date"}})
	const want = "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
	if got.Signature != want {
		t.Fatalf("signature = %s, want %s", got.Signature, want)
	}
}
