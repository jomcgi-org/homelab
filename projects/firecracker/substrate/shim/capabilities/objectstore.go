// Package capabilities provides workload-agnostic building blocks that guest
// shim handlers compose into hook implementations (ADR 030, decision 5).
// Nothing in this package names a specific workload (goose, sessions.db, etc.);
// that composition lives in the workload-specific handler layer.
package capabilities

import (
	"context"
	"fmt"
)

// ObjectStore is a simple key-value blob store used by shim hooks to persist
// and retrieve arbitrary byte payloads across microVM invocations.
type ObjectStore interface {
	// Pull fetches the blob stored at key. It returns an error if the key does
	// not exist.
	Pull(ctx context.Context, key string) ([]byte, error)

	// Push stores data at key, overwriting any previous value.
	Push(ctx context.Context, key string, data []byte) error
}

// S3Config holds the configuration needed to reach an S3-compatible object
// store (such as SeaweedFS or MinIO). It is passed to NewS3ObjectStore.
type S3Config struct {
	// Endpoint is the S3-compatible API base URL, e.g. "http://seaweedfs-s3:8333".
	Endpoint string
	// Bucket is the bucket name for all reads and writes.
	Bucket string
	// AccessKeyID and SecretAccessKey are the S3 credentials.
	AccessKeyID     string
	SecretAccessKey string
}

// S3ObjectStore is an ObjectStore backed by an S3-compatible endpoint.
//
// NOTE: the ObjectStore interface and the MapObjectStore test fake (see
// objectstore_test.go) are the wave-1 deliverable. The real S3 wire-up lands
// in the PR that vendors an S3 client (aws-sdk-go-v2 or minio-go); until then
// every method body returns a clear placeholder error so callers fail loudly
// rather than silently.
type S3ObjectStore struct {
	cfg S3Config
}

// NewS3ObjectStore constructs an S3ObjectStore from the supplied configuration.
// The struct is ready to receive a real implementation once an S3 client is
// vendored into the module.
func NewS3ObjectStore(cfg S3Config) *S3ObjectStore {
	return &S3ObjectStore{cfg: cfg}
}

// Pull returns the bytes stored at key in the configured S3 bucket.
//
// TODO: replace this placeholder body when an S3 client is vendored.
func (s *S3ObjectStore) Pull(_ context.Context, key string) ([]byte, error) {
	return nil, fmt.Errorf("objectstore: real S3 impl pending; wire in PR that adds the client (key=%q, endpoint=%q)", key, s.cfg.Endpoint)
}

// Push stores data under key in the configured S3 bucket.
//
// TODO: replace this placeholder body when an S3 client is vendored.
func (s *S3ObjectStore) Push(_ context.Context, _ string, _ []byte) error {
	return fmt.Errorf("objectstore: real S3 impl pending; wire in PR that adds the client (endpoint=%q)", s.cfg.Endpoint)
}

// Compile-time check: S3ObjectStore satisfies ObjectStore.
var _ ObjectStore = (*S3ObjectStore)(nil)
