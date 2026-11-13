// Package r2 reads single objects from Cloudflare R2 (or any S3-compatible
// store) with AWS Signature Version 4. It implements only the one call the
// connector needs, a conditional GET, so the service carries no SDK.
package r2

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/jomcgi/homelab/projects/shared/s3sigv4"
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
	u.RawPath = s3sigv4.EscapePath(u.Path)
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
	timestamp, _ := time.Parse("20060102T150405Z", amzDate)
	result := s3sigv4.Sign(req, s3sigv4.Input{
		SecretAccessKey: creds.SecretAccessKey, Region: region,
		Timestamp: timestamp, PayloadHash: emptyPayloadHash, SignedHeaders: signed,
	})
	return fmt.Sprintf("AWS4-HMAC-SHA256 Credential=%s/%s,SignedHeaders=%s,Signature=%s",
		creds.AccessKeyID, result.Scope, result.SignedHeaders, result.Signature)
}
