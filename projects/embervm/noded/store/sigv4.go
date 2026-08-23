package store

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"net/url"
	"sort"
	"strings"
)

const (
	sigV4Algorithm  = "AWS4-HMAC-SHA256"
	sigV4Region     = "us-east-1"
	sigV4Service    = "s3"
	unsignedPayload = "UNSIGNED-PAYLOAD"
)

type credentials struct {
	accessKeyID     string
	secretAccessKey string
}

// SignRequest adds an S3 SigV4 signature when credentials were configured.
// With no credentials it is a strict no-op, including leaving headers alone.
func (s *Store) SignRequest(req *http.Request) error {
	return s.sign(req)
}

func (s *Store) sign(req *http.Request) error {
	if s == nil || s.credentials.accessKeyID == "" || s.credentials.secretAccessKey == "" {
		return nil
	}
	now := s.now().UTC()
	amzDate := now.Format("20060102T150405Z")
	date := now.Format("20060102")
	req.Header.Set("x-amz-content-sha256", unsignedPayload)
	req.Header.Set("x-amz-date", amzDate)

	signedSet := map[string]struct{}{"host": {}}
	for name := range req.Header {
		lower := strings.ToLower(name)
		if lower != "authorization" {
			signedSet[lower] = struct{}{}
		}
	}
	signedHeaders := make([]string, 0, len(signedSet))
	for name := range signedSet {
		signedHeaders = append(signedHeaders, name)
	}
	sort.Strings(signedHeaders)
	canonical := canonicalRequest(req, signedHeaders, unsignedPayload)
	scope := date + "/" + sigV4Region + "/" + sigV4Service + "/aws4_request"
	stringToSign := sigV4Algorithm + "\n" + amzDate + "\n" + scope + "\n" + sha256Hex(canonical)
	signature := calculateSignature(s.credentials.secretAccessKey, date, sigV4Region, sigV4Service, stringToSign)
	req.Header.Set("Authorization", sigV4Algorithm+" Credential="+s.credentials.accessKeyID+"/"+scope+
		", SignedHeaders="+strings.Join(signedHeaders, ";")+", Signature="+signature)
	return nil
}

func canonicalRequest(req *http.Request, signedHeaders []string, payloadHash string) string {
	var headers strings.Builder
	for _, name := range signedHeaders {
		value := ""
		if name == "host" {
			value = req.Host
			if value == "" {
				value = req.URL.Host
			}
		} else {
			value = req.Header.Get(name)
		}
		headers.WriteString(name)
		headers.WriteByte(':')
		headers.WriteString(strings.Join(strings.Fields(value), " "))
		headers.WriteByte('\n')
	}
	return req.Method + "\n" + canonicalURI(req.URL) + "\n" + canonicalQuery(req.URL) + "\n" +
		headers.String() + "\n" + strings.Join(signedHeaders, ";") + "\n" + payloadHash
}

func canonicalURI(u *url.URL) string {
	path := u.EscapedPath()
	if path == "" {
		return "/"
	}
	parts := strings.Split(path, "/")
	for i, part := range parts {
		decoded, err := url.PathUnescape(part)
		if err == nil {
			parts[i] = awsEncode(decoded)
		}
	}
	return strings.Join(parts, "/")
}

func canonicalQuery(u *url.URL) string {
	values := u.Query()
	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	sort.Slice(keys, func(i, j int) bool { return awsEncode(keys[i]) < awsEncode(keys[j]) })
	parts := make([]string, 0, len(keys))
	for _, key := range keys {
		encodedKey := awsEncode(key)
		encodedValues := append([]string(nil), values[key]...)
		if len(encodedValues) == 0 {
			encodedValues = []string{""}
		}
		sort.Slice(encodedValues, func(i, j int) bool { return awsEncode(encodedValues[i]) < awsEncode(encodedValues[j]) })
		for _, value := range encodedValues {
			parts = append(parts, encodedKey+"="+awsEncode(value))
		}
	}
	return strings.Join(parts, "&")
}

func awsEncode(value string) string {
	return strings.ReplaceAll(url.QueryEscape(value), "+", "%20")
}

func sha256Hex(value string) string {
	sum := sha256.Sum256([]byte(value))
	return hex.EncodeToString(sum[:])
}

func calculateSignature(secret, date, region, service, stringToSign string) string {
	kDate := hmacSHA256([]byte("AWS4"+secret), date)
	kRegion := hmacSHA256(kDate, region)
	kService := hmacSHA256(kRegion, service)
	kSigning := hmacSHA256(kService, "aws4_request")
	return hex.EncodeToString(hmacSHA256(kSigning, stringToSign))
}

func hmacSHA256(key []byte, value string) []byte {
	h := hmac.New(sha256.New, key)
	_, _ = h.Write([]byte(value))
	return h.Sum(nil)
}
