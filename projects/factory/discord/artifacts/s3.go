package artifacts

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/xml"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"sort"
	"strings"
	"time"
)

// emptySHA256 is the SHA-256 of an empty body.
const emptySHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

// S3 is a path-style S3 client for one bucket with AWS Signature Version 4.
// It has only the three calls the tools need, so the service has no SDK.
type S3 struct {
	endpoint  *url.URL
	region    string
	bucket    string
	keyID     string
	secretKey string
	http      *http.Client
	now       func() time.Time
}

// NewS3 builds a client. endpoint is an http or https URL with no path.
func NewS3(endpoint, region, bucket, keyID, secretKey string) (*S3, error) {
	u, err := url.Parse(endpoint)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		return nil, fmt.Errorf("s3 endpoint %q is not an http(s) URL", endpoint)
	}
	if region == "" || bucket == "" || keyID == "" || secretKey == "" {
		return nil, errors.New("s3 region, bucket and key pair are required")
	}
	return &S3{
		endpoint: u, region: region, bucket: bucket, keyID: keyID, secretKey: secretKey,
		http: &http.Client{Timeout: 30 * time.Second}, now: time.Now,
	}, nil
}

// Object is one listed object.
type Object struct {
	Key  string
	Size int64
}

// Put writes key with the given content type.
func (c *S3) Put(ctx context.Context, key string, body []byte, contentType string) error {
	sum := sha256.Sum256(body)
	req, err := c.request(ctx, http.MethodPut, key, nil, bytes.NewReader(body), hex.EncodeToString(sum[:]))
	if err != nil {
		return err
	}
	req.ContentLength = int64(len(body))
	req.Header.Set("Content-Type", contentType)
	// The edge adds the security headers; no-cache keeps a replaced
	// artifact from showing stale in the browser.
	req.Header.Set("Cache-Control", "no-cache")
	c.sign(req, "content-type", "cache-control")
	return c.do(req, nil)
}

// ErrNotFound is a Get of a key that does not exist.
var ErrNotFound = errors.New("s3: no such key")

// maxGet bounds one object read.
const maxGet = 8 << 20

// Get reads key. A missing key is ErrNotFound.
func (c *S3) Get(ctx context.Context, key string) ([]byte, error) {
	req, err := c.request(ctx, http.MethodGet, key, nil, nil, emptySHA256)
	if err != nil {
		return nil, err
	}
	c.sign(req)
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("s3 GET: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusNotFound {
		return nil, ErrNotFound
	}
	if resp.StatusCode/100 != 2 {
		snippet, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return nil, fmt.Errorf("s3 GET: %s: %s", resp.Status, strings.TrimSpace(string(snippet)))
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxGet+1))
	if err != nil {
		return nil, fmt.Errorf("s3 GET: %w", err)
	}
	if len(body) > maxGet {
		return nil, fmt.Errorf("s3 GET: object over %d bytes", maxGet)
	}
	return body, nil
}

// Delete removes key. S3 answers 204 for a key that does not exist.
func (c *S3) Delete(ctx context.Context, key string) error {
	req, err := c.request(ctx, http.MethodDelete, key, nil, nil, emptySHA256)
	if err != nil {
		return err
	}
	c.sign(req)
	return c.do(req, nil)
}

// List returns at most limit objects under prefix, in key order.
func (c *S3) List(ctx context.Context, prefix string, limit int) ([]Object, bool, error) {
	q := url.Values{"list-type": {"2"}, "prefix": {prefix}, "max-keys": {fmt.Sprint(limit)}}
	req, err := c.request(ctx, http.MethodGet, "", q, nil, emptySHA256)
	if err != nil {
		return nil, false, err
	}
	c.sign(req)
	var out struct {
		IsTruncated bool `xml:"IsTruncated"`
		Contents    []struct {
			Key  string `xml:"Key"`
			Size int64  `xml:"Size"`
		} `xml:"Contents"`
	}
	if err := c.do(req, &out); err != nil {
		return nil, false, err
	}
	objects := make([]Object, 0, len(out.Contents))
	for _, o := range out.Contents {
		objects = append(objects, Object{Key: o.Key, Size: o.Size})
	}
	return objects, out.IsTruncated, nil
}

func (c *S3) request(ctx context.Context, method, key string, q url.Values, body io.Reader, digest string) (*http.Request, error) {
	u := *c.endpoint
	u.Path = "/" + c.bucket + "/" + key
	if key == "" {
		u.Path = "/" + c.bucket
	}
	u.RawQuery = canonicalQuery(q)
	req, err := http.NewRequestWithContext(ctx, method, u.String(), body)
	if err != nil {
		return nil, err
	}
	req.Header.Set("X-Amz-Content-Sha256", digest)
	return req, nil
}

func (c *S3) do(req *http.Request, out any) error {
	resp, err := c.http.Do(req)
	if err != nil {
		return fmt.Errorf("s3 %s: %w", req.Method, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode/100 != 2 {
		snippet, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return fmt.Errorf("s3 %s: %s: %s", req.Method, resp.Status, strings.TrimSpace(string(snippet)))
	}
	if out == nil {
		return nil
	}
	return xml.NewDecoder(io.LimitReader(resp.Body, 4<<20)).Decode(out)
}

// sign adds the SigV4 Authorization header. It signs host, the payload hash,
// the date and the extra headers named.
func (c *S3) sign(req *http.Request, extra ...string) {
	amzDate := c.now().UTC().Format("20060102T150405Z")
	req.Header.Set("X-Amz-Date", amzDate)
	signed := append([]string{"host", "x-amz-content-sha256", "x-amz-date"}, extra...)
	sort.Strings(signed)
	var headers strings.Builder
	for _, name := range signed {
		value := req.Header.Get(name)
		if name == "host" {
			value = req.URL.Host
		}
		headers.WriteString(name + ":" + strings.TrimSpace(value) + "\n")
	}
	signedHeaders := strings.Join(signed, ";")
	canonical := strings.Join([]string{
		req.Method,
		canonicalURI(req.URL.Path),
		req.URL.RawQuery,
		headers.String(),
		signedHeaders,
		req.Header.Get("X-Amz-Content-Sha256"),
	}, "\n")
	date := amzDate[:8]
	scope := date + "/" + c.region + "/s3/aws4_request"
	sum := sha256.Sum256([]byte(canonical))
	toSign := strings.Join([]string{"AWS4-HMAC-SHA256", amzDate, scope, hex.EncodeToString(sum[:])}, "\n")
	key := hmacSHA256([]byte("AWS4"+c.secretKey), date)
	for _, part := range []string{c.region, "s3", "aws4_request"} {
		key = hmacSHA256(key, part)
	}
	req.Header.Set("Authorization", fmt.Sprintf("AWS4-HMAC-SHA256 Credential=%s/%s,SignedHeaders=%s,Signature=%s",
		c.keyID, scope, signedHeaders, hex.EncodeToString(hmacSHA256(key, toSign))))
}

func canonicalURI(path string) string {
	segments := strings.Split(path, "/")
	for i, s := range segments {
		segments[i] = uriEncode(s)
	}
	return strings.Join(segments, "/")
}

// canonicalQuery is sorted and SigV4-encoded, so the same string is both the
// request's query and the signed one.
func canonicalQuery(q url.Values) string {
	keys := make([]string, 0, len(q))
	for k := range q {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		for _, v := range q[k] {
			parts = append(parts, uriEncode(k)+"="+uriEncode(v))
		}
	}
	return strings.Join(parts, "&")
}

func uriEncode(s string) string {
	const hexDigits = "0123456789ABCDEF"
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		ch := s[i]
		if ('A' <= ch && ch <= 'Z') || ('a' <= ch && ch <= 'z') || ('0' <= ch && ch <= '9') ||
			ch == '-' || ch == '_' || ch == '.' || ch == '~' {
			b.WriteByte(ch)
			continue
		}
		b.WriteByte('%')
		b.WriteByte(hexDigits[ch>>4])
		b.WriteByte(hexDigits[ch&0x0f])
	}
	return b.String()
}

func hmacSHA256(key []byte, data string) []byte {
	h := hmac.New(sha256.New, key)
	h.Write([]byte(data))
	return h.Sum(nil)
}
