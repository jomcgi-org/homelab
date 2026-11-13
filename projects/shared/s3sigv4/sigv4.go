// Package s3sigv4 implements the S3 Signature Version 4 protocol.
// Callers own credential policy, request headers and Authorization rendering.
package s3sigv4

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"net/url"
	"sort"
	"strings"
	"time"
)

// Input supplies caller policy. SignedHeaders names are case insensitive;
// Sign sorts and deduplicates them without changing the input or request.
type Input struct {
	SecretAccessKey string
	Region          string
	Timestamp       time.Time
	PayloadHash     string
	SignedHeaders   []string
}

// Result contains the fields used by the caller's Authorization format.
type Result struct {
	Signature     string
	Scope         string
	SignedHeaders string
}

// Sign calculates an S3 signature without mutating req or input.
func Sign(req *http.Request, input Input) Result {
	names := make(map[string]struct{}, len(input.SignedHeaders))
	for _, name := range input.SignedHeaders {
		names[strings.ToLower(name)] = struct{}{}
	}
	signed := make([]string, 0, len(names))
	for name := range names {
		signed = append(signed, name)
	}
	sort.Strings(signed)
	now := input.Timestamp.UTC()
	date := now.Format("20060102")
	scope := date + "/" + input.Region + "/s3/aws4_request"
	canonical := canonicalRequest(req, signed, input.PayloadHash)
	stringToSign := "AWS4-HMAC-SHA256\n" + now.Format("20060102T150405Z") + "\n" + scope + "\n" + sha256Hex(canonical)
	return Result{Signature: calculateSignature(input.SecretAccessKey, date, input.Region, "s3", stringToSign), Scope: scope, SignedHeaders: strings.Join(signed, ";")}
}

// EscapePath encodes a decoded object path without cleaning slash or dot
// segments. A literal percent is encoded; an existing URL uses canonicalURI
// instead so that escaped slash segments remain distinct from separators.
func EscapePath(path string) string {
	if path == "" {
		return "/"
	}
	parts := strings.Split(path, "/")
	for i, part := range parts {
		parts[i] = awsEncode(part)
	}
	return strings.Join(parts, "/")
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
			values := req.Header.Values(name)
			normalized := make([]string, len(values))
			for i, v := range values {
				normalized[i] = strings.Join(strings.Fields(v), " ")
			}
			value = strings.Join(normalized, ",")
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
