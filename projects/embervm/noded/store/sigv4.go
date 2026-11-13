package store

import (
	"net/http"
	"strings"

	"github.com/jomcgi/homelab/projects/shared/s3sigv4"
)

const (
	sigV4Algorithm  = "AWS4-HMAC-SHA256"
	sigV4Region     = "us-east-1"
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
	result := s3sigv4.Sign(req, s3sigv4.Input{
		SecretAccessKey: s.credentials.secretAccessKey, Region: sigV4Region,
		Timestamp: now, PayloadHash: unsignedPayload, SignedHeaders: signedHeaders,
	})
	req.Header.Set("Authorization", sigV4Algorithm+" Credential="+s.credentials.accessKeyID+"/"+result.Scope+
		", SignedHeaders="+result.SignedHeaders+", Signature="+result.Signature)
	return nil
}
