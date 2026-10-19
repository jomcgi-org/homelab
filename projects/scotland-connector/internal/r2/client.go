// Package r2 reads single objects from Cloudflare R2 (or any S3-compatible
// store) with AWS Signature Version 4. It implements only the one call the
// connector needs, a conditional GET, so the service carries no SDK.
package r2

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"sort"
	"strings"
	"time"
)

// emptyPayloadHash is the SHA-256 of an empty body, which is what a GET signs.
const emptyPayloadHash = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

// ErrNotFound is returned when the bucket or key does not exist.
var ErrNotFound = errors.New("r2: object not found")

// Credentials are the S3 access key pair.
type Credentials struct {
	AccessKeyID     string
	SecretAccessKey string
}

// Client reads objects path-style from one endpoint.
type Client struct {
	endpoint *url.URL
	region   string
	creds    Credentials
	http     *http.Client
	now      func() time.Time
}

// New builds a client. endpoint must be an absolute https (or, for tests,
// http) URL with no path.
func New(endpoint, region string, creds Credentials, httpClient *http.Client) (*Client, error) {
	u, err := url.Parse(endpoint)
	if err != nil {
		return nil, fmt.Errorf("r2: parse endpoint: %w", err)
	}
	if u.Scheme != "https" && u.Scheme != "http" {
		return nil, fmt.Errorf("r2: endpoint %q needs an http or https scheme", endpoint)
	}
	if u.Host == "" {
		return nil, fmt.Errorf("r2: endpoint %q has no host", endpoint)
	}
	if region == "" {
		region = "auto"
	}
	if creds.AccessKeyID == "" || creds.SecretAccessKey == "" {
		return nil, errors.New("r2: access key id and secret are required")
	}
	if httpClient == nil {
		httpClient = &http.Client{Timeout: 30 * time.Second}
	}
	return &Client{endpoint: u, region: region, creds: creds, http: httpClient, now: time.Now}, nil
}

// Object is the result of a conditional GET. When NotModified is true, Body is
// nil and ETag echoes the caller's value.
type Object struct {
	Body        []byte
	ETag        string
	NotModified bool
}

// Get fetches bucket/key. A non-empty ifNoneMatch makes the request
// conditional, and a matching object returns NotModified. Bodies larger than
// maxBytes are refused rather than truncated.
func (c *Client) Get(ctx context.Context, bucket, key, ifNoneMatch string, maxBytes int64) (Object, error) {
	u := *c.endpoint
	u.Path = "/" + bucket + "/" + strings.TrimPrefix(key, "/")
	u.RawPath = canonicalURI(u.Path)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u.String(), nil)
	if err != nil {
		return Object{}, fmt.Errorf("r2: build request: %w", err)
	}
	if ifNoneMatch != "" {
		req.Header.Set("If-None-Match", ifNoneMatch)
	}
	c.sign(req, c.now().UTC())

	resp, err := c.http.Do(req)
	if err != nil {
		return Object{}, fmt.Errorf("r2: get %s/%s: %w", bucket, key, err)
	}
	defer resp.Body.Close()

	switch {
	case resp.StatusCode == http.StatusNotModified:
		return Object{ETag: ifNoneMatch, NotModified: true}, nil
	case resp.StatusCode == http.StatusNotFound:
		return Object{}, fmt.Errorf("%w: %s/%s", ErrNotFound, bucket, key)
	case resp.StatusCode != http.StatusOK:
		snippet, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return Object{}, fmt.Errorf("r2: get %s/%s: status %d: %s", bucket, key, resp.StatusCode, strings.TrimSpace(string(snippet)))
	}

	body, err := io.ReadAll(io.LimitReader(resp.Body, maxBytes+1))
	if err != nil {
		return Object{}, fmt.Errorf("r2: read %s/%s: %w", bucket, key, err)
	}
	if int64(len(body)) > maxBytes {
		return Object{}, fmt.Errorf("r2: %s/%s exceeds %d bytes", bucket, key, maxBytes)
	}
	etag := resp.Header.Get("ETag")
	if etag == "" {
		return Object{}, fmt.Errorf("r2: %s/%s has no ETag", bucket, key)
	}
	return Object{Body: body, ETag: etag}, nil
}

// sign adds SigV4 headers for an empty-payload request. Only host,
// x-amz-content-sha256, x-amz-date and any range header are signed; other
// headers (If-None-Match) travel unsigned, which S3 permits.
func (c *Client) sign(req *http.Request, now time.Time) {
	amzDate := now.Format("20060102T150405Z")
	req.Header.Set("X-Amz-Date", amzDate)
	req.Header.Set("X-Amz-Content-Sha256", emptyPayloadHash)
	signed := []string{"host", "x-amz-content-sha256", "x-amz-date"}
	if req.Header.Get("Range") != "" {
		signed = append(signed, "range")
	}
	req.Header.Set("Authorization", authorization(req, signed, amzDate, c.region, c.creds))
}

func authorization(req *http.Request, signed []string, amzDate, region string, creds Credentials) string {
	sort.Strings(signed)
	var headers strings.Builder
	for _, name := range signed {
		value := req.Header.Get(name)
		if name == "host" {
			value = req.URL.Host
		}
		headers.WriteString(name)
		headers.WriteByte(':')
		headers.WriteString(strings.TrimSpace(value))
		headers.WriteByte('\n')
	}
	signedHeaders := strings.Join(signed, ";")
	canonicalRequest := strings.Join([]string{
		req.Method,
		canonicalURI(req.URL.Path),
		canonicalQuery(req.URL.Query()),
		headers.String(),
		signedHeaders,
		emptyPayloadHash,
	}, "\n")

	date := amzDate[:8]
	scope := date + "/" + region + "/s3/aws4_request"
	stringToSign := strings.Join([]string{
		"AWS4-HMAC-SHA256",
		amzDate,
		scope,
		hexSHA256(canonicalRequest),
	}, "\n")

	key := hmacSHA256([]byte("AWS4"+creds.SecretAccessKey), date)
	key = hmacSHA256(key, region)
	key = hmacSHA256(key, "s3")
	key = hmacSHA256(key, "aws4_request")
	signature := hex.EncodeToString(hmacSHA256(key, stringToSign))

	return fmt.Sprintf("AWS4-HMAC-SHA256 Credential=%s/%s,SignedHeaders=%s,Signature=%s",
		creds.AccessKeyID, scope, signedHeaders, signature)
}

// canonicalURI percent-encodes every byte outside the SigV4 unreserved set,
// segment by segment, keeping the slashes.
func canonicalURI(path string) string {
	if path == "" {
		return "/"
	}
	segments := strings.Split(path, "/")
	for i, s := range segments {
		segments[i] = uriEncode(s)
	}
	return strings.Join(segments, "/")
}

func canonicalQuery(values url.Values) string {
	if len(values) == 0 {
		return ""
	}
	keys := make([]string, 0, len(values))
	for k := range values {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var parts []string
	for _, k := range keys {
		vs := append([]string(nil), values[k]...)
		sort.Strings(vs)
		for _, v := range vs {
			parts = append(parts, uriEncode(k)+"="+uriEncode(v))
		}
	}
	return strings.Join(parts, "&")
}

func uriEncode(s string) string {
	const hexDigits = "0123456789ABCDEF"
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		c := s[i]
		if ('A' <= c && c <= 'Z') || ('a' <= c && c <= 'z') || ('0' <= c && c <= '9') ||
			c == '-' || c == '_' || c == '.' || c == '~' {
			b.WriteByte(c)
			continue
		}
		b.WriteByte('%')
		b.WriteByte(hexDigits[c>>4])
		b.WriteByte(hexDigits[c&0x0f])
	}
	return b.String()
}

func hexSHA256(s string) string {
	sum := sha256.Sum256([]byte(s))
	return hex.EncodeToString(sum[:])
}

func hmacSHA256(key []byte, data string) []byte {
	h := hmac.New(sha256.New, key)
	h.Write([]byte(data))
	return h.Sum(nil)
}
