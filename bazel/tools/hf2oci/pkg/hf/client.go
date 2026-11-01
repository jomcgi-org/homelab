package hf

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"sync"
	"time"
)

const defaultBaseURL = "https://huggingface.co"

// Client is a HuggingFace API client.
type Client struct {
	baseURL    string
	token      string
	httpClient *http.Client
	cacheTTL   time.Duration
	cache      sync.Map // key → cacheEntry

	// Parallel download tuning.
	parallelConfigured  bool
	parallelChunkSize   int64
	parallelWorkers     int
	parallelMinFileSize int64
}

type cacheEntry struct {
	data      any
	expiresAt time.Time
}

// Option configures a Client.
type Option func(*Client)

// WithToken sets the HuggingFace API token.
func WithToken(token string) Option {
	return func(c *Client) { c.token = token }
}

// WithBaseURL overrides the base URL (useful for tests).
func WithBaseURL(u string) Option {
	return func(c *Client) { c.baseURL = u }
}

// WithCacheTTL enables in-memory caching of Tree and ModelInfo responses.
// Cached entries expire after the given duration. Zero disables caching.
func WithCacheTTL(ttl time.Duration) Option {
	return func(c *Client) { c.cacheTTL = ttl }
}

// NewClient creates a new HuggingFace API client.
func NewClient(opts ...Option) *Client {
	c := &Client{
		baseURL: defaultBaseURL,
	}
	for _, o := range opts {
		o(c)
	}
	if c.httpClient == nil {
		// Use a client that strips auth on cross-domain redirects (CDN).
		c.httpClient = &http.Client{
			CheckRedirect: c.checkRedirect,
		}
	}
	return c
}

// checkRedirect strips the Authorization header when redirected to a different host.
// HuggingFace redirects file downloads to a CDN, and sending the HF token to the
// CDN is unnecessary and could leak credentials.
func (c *Client) checkRedirect(req *http.Request, via []*http.Request) error {
	if len(via) == 0 {
		return nil
	}
	if req.URL.Host != via[0].URL.Host {
		req.Header.Del("Authorization")
	}
	return nil
}

// Tree lists all files in a HuggingFace model repository.
func (c *Client) Tree(ctx context.Context, repo, revision string) ([]TreeEntry, error) {
	cacheKey := "tree\x00" + repo + "\x00" + revision
	if cached, ok := c.loadCache(cacheKey); ok {
		return cached.([]TreeEntry), nil
	}

	u := fmt.Sprintf("%s/api/models/%s/tree/%s", c.baseURL, repo, url.PathEscape(revision))

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return nil, fmt.Errorf("creating request: %w", err)
	}
	c.setAuth(req)

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("fetching tree: %w", err)
	}
	defer resp.Body.Close()

	if err := checkResponse(resp); err != nil {
		return nil, err
	}

	var entries []TreeEntry
	if err := json.NewDecoder(resp.Body).Decode(&entries); err != nil {
		return nil, fmt.Errorf("decoding tree response: %w", err)
	}

	c.storeCache(cacheKey, entries)
	return entries, nil
}

// ModelInfo fetches model metadata including base model relationships.
// Use expand[]=baseModels to get lineage information for smart OCI naming.
func (c *Client) ModelInfo(ctx context.Context, repo string) (*ModelInfo, error) {
	cacheKey := "info\x00" + repo
	if cached, ok := c.loadCache(cacheKey); ok {
		return cached.(*ModelInfo), nil
	}

	u := fmt.Sprintf("%s/api/models/%s?expand[]=baseModels", c.baseURL, repo)

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return nil, fmt.Errorf("creating request: %w", err)
	}
	c.setAuth(req)

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("fetching model info: %w", err)
	}
	defer resp.Body.Close()

	if err := checkResponse(resp); err != nil {
		return nil, err
	}

	var info ModelInfo
	if err := json.NewDecoder(resp.Body).Decode(&info); err != nil {
		return nil, fmt.Errorf("decoding model info: %w", err)
	}

	c.storeCache(cacheKey, &info)
	return &info, nil
}

// Download fetches a file from a HuggingFace model repository.
// The caller must close the returned ReadCloser.
func (c *Client) Download(ctx context.Context, repo, revision, path string) (io.ReadCloser, int64, error) {
	u := fmt.Sprintf("%s/%s/resolve/%s/%s", c.baseURL, repo, url.PathEscape(revision), path)

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return nil, 0, fmt.Errorf("creating request: %w", err)
	}
	c.setAuth(req)

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return nil, 0, fmt.Errorf("downloading %s: %w", path, err)
	}

	if err := checkResponse(resp); err != nil {
		resp.Body.Close()
		return nil, 0, err
	}

	return resp.Body, resp.ContentLength, nil
}

// DownloadRange fetches a byte range of a file from a HuggingFace model repository.
// Start and end are inclusive byte offsets. The caller must close the returned ReadCloser.
// If the server does not support range requests (returns 200 instead of 206), the
// fallback bool is true and the full body is returned — the caller should handle this case.
func (c *Client) DownloadRange(ctx context.Context, repo, revision, path string, start, end int64) (body io.ReadCloser, size int64, fallback bool, err error) {
	u := fmt.Sprintf("%s/%s/resolve/%s/%s", c.baseURL, repo, url.PathEscape(revision), path)

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return nil, 0, false, fmt.Errorf("creating request: %w", err)
	}
	c.setAuth(req)
	req.Header.Set("Range", fmt.Sprintf("bytes=%d-%d", start, end))

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return nil, 0, false, fmt.Errorf("downloading range %s: %w", path, err)
	}

	switch resp.StatusCode {
	case http.StatusPartialContent:
		return resp.Body, resp.ContentLength, false, nil
	case http.StatusOK:
		// Server ignored the Range header; return full body and signal fallback.
		return resp.Body, resp.ContentLength, true, nil
	default:
		if err := checkResponse(resp); err != nil {
			resp.Body.Close()
			return nil, 0, false, err
		}
		// Shouldn't reach here, but be safe.
		return resp.Body, resp.ContentLength, false, nil
	}
}

func (c *Client) loadCache(key string) (any, bool) {
	if c.cacheTTL <= 0 {
		return nil, false
	}
	v, ok := c.cache.Load(key)
	if !ok {
		return nil, false
	}
	e := v.(cacheEntry)
	if time.Now().After(e.expiresAt) {
		c.cache.Delete(key)
		return nil, false
	}
	return e.data, true
}

func (c *Client) storeCache(key string, data any) {
	if c.cacheTTL <= 0 {
		return
	}
	c.cache.Store(key, cacheEntry{data: data, expiresAt: time.Now().Add(c.cacheTTL)})
}

func (c *Client) setAuth(req *http.Request) {
	if c.token != "" {
		req.Header.Set("Authorization", "Bearer "+c.token)
	}
}

// APIError is returned when the HuggingFace API responds with a non-2xx status.
type APIError struct {
	StatusCode int
	Body       string
}

func (e *APIError) Error() string {
	switch e.StatusCode {
	case http.StatusUnauthorized:
		return fmt.Sprintf("unauthorized (HTTP 401): set HF_TOKEN for private repos. %s", e.Body)
	case http.StatusNotFound:
		return fmt.Sprintf("not found (HTTP 404): %s", e.Body)
	default:
		return fmt.Sprintf("unexpected status %d: %s", e.StatusCode, e.Body)
	}
}

// IsNotFound reports whether the error is a 404.
func (e *APIError) IsNotFound() bool { return e.StatusCode == 404 }

// IsClientError reports whether the status code is in the 4xx range.
func (e *APIError) IsClientError() bool {
	return e.StatusCode >= 400 && e.StatusCode < 500
}

// IsRetryable reports whether the error is a retryable status (429, 408).
func (e *APIError) IsRetryable() bool {
	return e.StatusCode == http.StatusTooManyRequests || e.StatusCode == http.StatusRequestTimeout
}

func checkResponse(resp *http.Response) error {
	if resp.StatusCode >= 200 && resp.StatusCode < 300 {
		return nil
	}
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 1024))
	return &APIError{StatusCode: resp.StatusCode, Body: string(body)}
}
