package publish

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

// errNotFound reports a missing object.
var errNotFound = errors.New("object not found")

// store reads and writes one Garage bucket with path-style S3 requests,
// signed with AWS Signature Version 4. It implements only the calls the
// publisher needs, so the image carries no SDK.
type store struct {
	endpoint *url.URL
	region   string
	bucket   string
	keyID    string
	secret   string
	http     *http.Client
	now      func() time.Time
}

func newStore(endpoint, region, bucket, keyID, secret string) (*store, error) {
	u, err := url.Parse(endpoint)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		return nil, fmt.Errorf("invalid S3 endpoint %q", endpoint)
	}
	if bucket == "" || keyID == "" || secret == "" {
		return nil, errors.New("bucket, access key id and secret are required")
	}
	if region == "" {
		region = "garage"
	}
	return &store{
		endpoint: u, region: region, bucket: bucket, keyID: keyID, secret: secret,
		http: &http.Client{Timeout: time.Minute}, now: time.Now,
	}, nil
}

// do sends one signed request. The caller closes the response body.
func (s *store) do(ctx context.Context, method, key string, query url.Values, body []byte, header http.Header) (*http.Response, error) {
	u := *s.endpoint
	u.Path = "/" + s.bucket
	if key != "" {
		u.Path += "/" + key
	}
	u.RawPath = canonicalURI(u.Path)
	u.RawQuery = canonicalQuery(query)
	req, err := http.NewRequestWithContext(ctx, method, u.String(), bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.ContentLength = int64(len(body))
	for name, values := range header {
		req.Header[name] = values
	}
	digest := sha256.Sum256(body)
	date := s.now().UTC().Format("20060102T150405Z")
	req.Header.Set("X-Amz-Date", date)
	req.Header.Set("X-Amz-Content-Sha256", hex.EncodeToString(digest[:]))
	req.Header.Set("Authorization", authorization(req, []string{"host", "x-amz-content-sha256", "x-amz-date"}, date, s.region, s.keyID, s.secret))
	return s.http.Do(req)
}

func failure(resp *http.Response, method, key string) error {
	snippet, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
	return fmt.Errorf("%s %s: %s: %s", method, key, resp.Status, strings.TrimSpace(string(snippet)))
}

// get reads one object. It refuses a body larger than limit bytes.
func (s *store) get(ctx context.Context, key string, limit int64) ([]byte, error) {
	resp, err := s.do(ctx, http.MethodGet, key, nil, nil, nil)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusNotFound {
		return nil, fmt.Errorf("%w: %s", errNotFound, key)
	}
	if resp.StatusCode != http.StatusOK {
		return nil, failure(resp, "get", key)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, limit+1))
	if err != nil {
		return nil, err
	}
	if int64(len(body)) > limit {
		return nil, fmt.Errorf("get %s: larger than %d bytes", key, limit)
	}
	return body, nil
}

// put writes one JSON object with the given Cache-Control value.
func (s *store) put(ctx context.Context, key string, body []byte, cache string) error {
	header := http.Header{"Content-Type": {"application/json"}, "Cache-Control": {cache}}
	resp, err := s.do(ctx, http.MethodPut, key, nil, body, header)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode/100 != 2 {
		return failure(resp, "put", key)
	}
	return nil
}

// remove deletes one object. A missing object is not an error.
func (s *store) remove(ctx context.Context, key string) error {
	resp, err := s.do(ctx, http.MethodDelete, key, nil, nil, nil)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode/100 != 2 && resp.StatusCode != http.StatusNotFound {
		return failure(resp, "delete", key)
	}
	return nil
}

type listing struct {
	Contents []struct {
		Key          string
		LastModified time.Time
	}
	IsTruncated           bool
	NextContinuationToken string
}

// list returns the last-modified time of every object in the bucket.
func (s *store) list(ctx context.Context) (map[string]time.Time, error) {
	objects := map[string]time.Time{}
	token := ""
	for {
		query := url.Values{"list-type": {"2"}}
		if token != "" {
			query.Set("continuation-token", token)
		}
		resp, err := s.do(ctx, http.MethodGet, "", query, nil, nil)
		if err != nil {
			return nil, err
		}
		if resp.StatusCode != http.StatusOK {
			err = failure(resp, "list", s.bucket)
			resp.Body.Close()
			return nil, err
		}
		var page listing
		err = xml.NewDecoder(io.LimitReader(resp.Body, 64<<20)).Decode(&page)
		resp.Body.Close()
		if err != nil {
			return nil, fmt.Errorf("list %s: %w", s.bucket, err)
		}
		for _, c := range page.Contents {
			objects[c.Key] = c.LastModified
		}
		if !page.IsTruncated {
			return objects, nil
		}
		if page.NextContinuationToken == "" {
			return nil, fmt.Errorf("list %s: truncated page without a continuation token", s.bucket)
		}
		token = page.NextContinuationToken
	}
}

func authorization(req *http.Request, signed []string, amzDate, region, keyID, secret string) string {
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
		canonicalQuery(req.URL.Query()),
		headers.String(),
		signedHeaders,
		req.Header.Get("X-Amz-Content-Sha256"),
	}, "\n")
	date := amzDate[:8]
	scope := date + "/" + region + "/s3/aws4_request"
	hash := sha256.Sum256([]byte(canonical))
	toSign := strings.Join([]string{"AWS4-HMAC-SHA256", amzDate, scope, hex.EncodeToString(hash[:])}, "\n")
	key := []byte("AWS4" + secret)
	for _, part := range []string{date, region, "s3", "aws4_request"} {
		key = hmacSHA256(key, part)
	}
	return fmt.Sprintf("AWS4-HMAC-SHA256 Credential=%s/%s,SignedHeaders=%s,Signature=%s",
		keyID, scope, signedHeaders, hex.EncodeToString(hmacSHA256(key, toSign)))
}

func hmacSHA256(key []byte, data string) []byte {
	h := hmac.New(sha256.New, key)
	h.Write([]byte(data))
	return h.Sum(nil)
}

// canonicalURI percent-encodes each path segment and keeps the slashes.
func canonicalURI(path string) string {
	if path == "" {
		return "/"
	}
	segments := strings.Split(path, "/")
	for i, segment := range segments {
		segments[i] = uriEncode(segment)
	}
	return strings.Join(segments, "/")
}

// canonicalQuery sorts the encoded parameters by name, then by value.
func canonicalQuery(values url.Values) string {
	type pair struct{ key, value string }
	var pairs []pair
	for key, list := range values {
		for _, value := range list {
			pairs = append(pairs, pair{uriEncode(key), uriEncode(value)})
		}
	}
	sort.Slice(pairs, func(i, j int) bool {
		if pairs[i].key != pairs[j].key {
			return pairs[i].key < pairs[j].key
		}
		return pairs[i].value < pairs[j].value
	})
	parts := make([]string, len(pairs))
	for i, p := range pairs {
		parts[i] = p.key + "=" + p.value
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
