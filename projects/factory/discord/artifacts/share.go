package artifacts

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// Share link lifetimes. SigV4 query auth allows at most seven days.
const (
	DefaultShareTTL = 24 * time.Hour
	MaxShareTTL     = 7 * 24 * time.Hour
	MinShareTTL     = time.Minute
)

// SharePrefix is the edge path that forwards a signed GET to Garage's S3 API
// as /<bucket>/<key>, with Host kept, so the signature checks
// (edge artifact-route.yaml, route artifact-share).
const SharePrefix = "/s/"

// Sharer signs share links: S3 presigned GET URLs for one bucket, signed with
// a read-only key that only the bridge holds. Garage checks the signature
// and the expiry; rotating the key revokes every link. The key files are
// read at each call, so a rotated Secret takes effect without a restart.
type Sharer struct {
	BaseURL string // https://artifact.jomcgi.dev, no trailing slash
	Region  string
	Bucket  string
	// KeyDir holds AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY files.
	KeyDir string
	Now    func() time.Time
}

// Link returns a URL that serves key until it expires.
func (s *Sharer) Link(key string, ttl time.Duration) (string, time.Time, error) {
	if ttl < MinShareTTL || ttl > MaxShareTTL {
		return "", time.Time{}, fmt.Errorf("share: ttl %s is outside %s to %s", ttl, MinShareTTL, MaxShareTTL)
	}
	id, err := os.ReadFile(filepath.Join(s.KeyDir, "AWS_ACCESS_KEY_ID"))
	if err != nil {
		return "", time.Time{}, err
	}
	secret, err := os.ReadFile(filepath.Join(s.KeyDir, "AWS_SECRET_ACCESS_KEY"))
	if err != nil {
		return "", time.Time{}, err
	}
	keyID, secretKey := strings.TrimSpace(string(id)), strings.TrimSpace(string(secret))
	if keyID == "" || secretKey == "" {
		return "", time.Time{}, errors.New("share: the share key is not filled yet")
	}
	base, err := url.Parse(s.BaseURL)
	if err != nil || base.Scheme != "https" || base.Host == "" {
		return "", time.Time{}, fmt.Errorf("share: base URL %q is not https", s.BaseURL)
	}
	now := time.Now
	if s.Now != nil {
		now = s.Now
	}
	at := now().UTC()
	query := presign(base.Host, "/"+s.Bucket+"/"+key, s.Region, keyID, secretKey, at, ttl)
	return s.BaseURL + SharePrefix + canonicalURI(key) + "?" + query, at.Add(ttl), nil
}

// presign gives the SigV4 query string, signature included, for a GET of
// path on host. Only host is signed and the payload is unsigned, as for any
// presigned GET.
func presign(host, path, region, keyID, secretKey string, at time.Time, ttl time.Duration) string {
	amzDate := at.Format("20060102T150405Z")
	date := amzDate[:8]
	scope := date + "/" + region + "/s3/aws4_request"
	q := url.Values{
		"X-Amz-Algorithm":     {"AWS4-HMAC-SHA256"},
		"X-Amz-Credential":    {keyID + "/" + scope},
		"X-Amz-Date":          {amzDate},
		"X-Amz-Expires":       {fmt.Sprint(int64(ttl / time.Second))},
		"X-Amz-SignedHeaders": {"host"},
	}
	query := canonicalQuery(q)
	canonical := strings.Join([]string{
		"GET", canonicalURI(path), query, "host:" + host + "\n", "host", "UNSIGNED-PAYLOAD",
	}, "\n")
	sum := sha256.Sum256([]byte(canonical))
	toSign := strings.Join([]string{"AWS4-HMAC-SHA256", amzDate, scope, hex.EncodeToString(sum[:])}, "\n")
	k := hmacSHA256([]byte("AWS4"+secretKey), date)
	for _, part := range []string{region, "s3", "aws4_request"} {
		k = hmacSHA256(k, part)
	}
	return query + "&X-Amz-Signature=" + hex.EncodeToString(hmacSHA256(k, toSign))
}

// ShareKey turns what Bosun names, a file path in the current channel, an
// artifact.jomcgi.dev URL or a bucket key, into the bucket key. Only a file
// of the current channel can be shared: <guild>/<channel>/<path> or
// private/<channel>/<path>.
func ShareKey(baseURL, guild, channel, ref string) (string, error) {
	ref = strings.TrimPrefix(ref, strings.TrimRight(baseURL, "/")+"/")
	if !snowflake.MatchString(guild) || !snowflake.MatchString(channel) {
		return "", errors.New("share: not a server channel")
	}
	for _, folder := range []string{guild, Private} {
		if rest, ok := strings.CutPrefix(ref, folder+"/"+channel+"/"); ok {
			if _, err := CleanPath(rest); err != nil {
				return "", err
			}
			return folder + "/" + channel + "/" + rest, nil
		}
	}
	if strings.Contains(ref, "://") || strings.HasPrefix(ref, "/") {
		return "", &ParamError{"path", "must be a file of this channel"}
	}
	if _, err := CleanPath(ref); err != nil {
		return "", err
	}
	return guild + "/" + channel + "/" + ref, nil
}
