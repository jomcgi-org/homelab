package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"github.com/jomcgi/homelab/projects/embervm/noded/sparse"
	"github.com/jomcgi/homelab/projects/embervm/noded/store"
)

const (
	exitFailure = 1
	exitUsage   = 2
	exitMiss    = 3

	checksumObjectName = "rootfs.ext4.sha256"
	maxMarkerBytes     = 1024

	defaultGetTimeout = 10 * time.Minute
	defaultPutTimeout = 20 * time.Minute
	connectTimeout    = 30 * time.Second
)

type completenessMarker struct {
	PayloadKey  string `json:"payloadKey"`
	SHA256      string `json:"sha256"`
	ImageRef    string `json:"imageRef"`
	ImageDigest string `json:"imageDigest"`
	RootfsSize  string `json:"rootfsSize"`
	BakeFormat  string `json:"bakeFormat"`
	UploadedAt  string `json:"uploadedAt"`
}

type cacheIdentity struct {
	Digest     string
	RootfsSize string
	BakeFormat string
}

var (
	rootfsSizePattern = regexp.MustCompile(`^[1-9][0-9]*[KMGTP]?$`)
	bakeFormatPattern = regexp.MustCompile(`^[a-z][a-z0-9-]*$`)
)

type putResult int

const (
	putUploaded putResult = iota
	putAlreadyPresent
	putOrphaned
)

type getenvFunc func(string) string

func main() {
	os.Exit(run(context.Background(), os.Args[1:], os.Getenv, os.Stdout, os.Stderr))
}

func run(ctx context.Context, args []string, getenv getenvFunc, stdout, stderr io.Writer) int {
	if len(args) == 0 {
		fmt.Fprintln(stderr, "usage: rootfs-store <get|put> [flags]")
		return exitUsage
	}

	switch args[0] {
	case "get", "put":
		if getenv("EMBERVM_NODED_STORE_ENDPOINT") == "" {
			return exitMiss
		}
	case "help", "-h", "--help":
		fmt.Fprintln(stdout, "usage: rootfs-store <get|put> [flags]")
		return 0
	default:
		fmt.Fprintf(stderr, "unknown subcommand %q\n", args[0])
		return exitUsage
	}

	s := store.New(
		getenv("EMBERVM_NODED_STORE_ENDPOINT"),
		getenv("EMBERVM_NODED_STORE_BUCKET"),
		false,
		store.WithHTTPClient(newHTTPClient()),
		store.WithCredentials(
			getenv("EMBERVM_NODED_STORE_ACCESS_KEY_ID"),
			getenv("EMBERVM_NODED_STORE_SECRET_ACCESS_KEY"),
		),
	)

	var err error
	switch args[0] {
	case "get":
		err = runGet(ctx, s, args[1:], stderr)
	case "put":
		err = runPut(ctx, s, args[1:], stdout, stderr)
	}
	if err == nil {
		return 0
	}
	if errors.Is(err, store.ErrNotPresent) {
		return exitMiss
	}
	fmt.Fprintf(stderr, "rootfs-store %s: %v\n", args[0], err)
	return exitFailure
}

func newHTTPClient() *http.Client {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.DialContext = (&net.Dialer{
		Timeout:   connectTimeout,
		KeepAlive: 30 * time.Second,
	}).DialContext
	return &http.Client{Transport: transport}
}

func runGet(ctx context.Context, s *store.Store, args []string, stderr io.Writer) error {
	flags := flag.NewFlagSet("get", flag.ContinueOnError)
	flags.SetOutput(stderr)
	digest := flags.String("digest", "", "full SHA-256 guest image digest")
	rootfsSize := flags.String("rootfs-size", "", "mkfs rootfs size (for example 4G)")
	bakeFormat := flags.String("bake-format", "", "rootfs bake-format version")
	out := flags.String("out", "", "destination rootfs path")
	timeout := flags.Duration("timeout", defaultGetTimeout, "maximum time for the store download")
	if err := flags.Parse(args); err != nil {
		return err
	}
	if flags.NArg() != 0 || *digest == "" || *rootfsSize == "" || *bakeFormat == "" || *out == "" {
		return errors.New("get requires --digest, --rootfs-size, --bake-format, and --out")
	}
	if *timeout <= 0 {
		return errors.New("get --timeout must be greater than zero")
	}
	identity, err := validateCacheIdentity(*digest, *rootfsSize, *bakeFormat)
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(ctx, *timeout)
	defer cancel()
	return getRootfs(ctx, s, identity, *out)
}

func runPut(ctx context.Context, s *store.Store, args []string, stdout, stderr io.Writer) error {
	flags := flag.NewFlagSet("put", flag.ContinueOnError)
	flags.SetOutput(stderr)
	digest := flags.String("digest", "", "full SHA-256 guest image digest")
	rootfsSize := flags.String("rootfs-size", "", "mkfs rootfs size (for example 4G)")
	bakeFormat := flags.String("bake-format", "", "rootfs bake-format version")
	file := flags.String("file", "", "rootfs file to upload")
	imageRef := flags.String("image-ref", "", "guest image reference used to bake the rootfs")
	timeout := flags.Duration("timeout", defaultPutTimeout, "maximum time for the store upload")
	if err := flags.Parse(args); err != nil {
		return err
	}
	if flags.NArg() != 0 || *digest == "" || *rootfsSize == "" || *bakeFormat == "" || *file == "" || strings.TrimSpace(*imageRef) == "" {
		return errors.New("put requires --digest, --rootfs-size, --bake-format, --file, and --image-ref")
	}
	if *timeout <= 0 {
		return errors.New("put --timeout must be greater than zero")
	}
	identity, err := validateCacheIdentity(*digest, *rootfsSize, *bakeFormat)
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(ctx, *timeout)
	defer cancel()
	result, payloadKey, err := putRootfs(ctx, s, identity, *file, strings.TrimSpace(*imageRef))
	if err != nil {
		return err
	}
	switch result {
	case putUploaded:
		fmt.Fprintln(stdout, "uploaded")
	case putAlreadyPresent:
		fmt.Fprintln(stdout, "already present")
	case putOrphaned:
		fmt.Fprintf(stdout, "already present; orphan payload %s is eligible for retention sweep\n", payloadKey)
	}
	return nil
}

func validateDigest(value string) (string, error) {
	value = strings.TrimPrefix(value, "sha256:")
	if len(value) != sha256.Size*2 {
		return "", fmt.Errorf("digest must contain 64 hexadecimal characters")
	}
	if _, err := hex.DecodeString(value); err != nil {
		return "", fmt.Errorf("invalid digest: %w", err)
	}
	return strings.ToLower(value), nil
}

func validateCacheIdentity(digest, rootfsSize, bakeFormat string) (cacheIdentity, error) {
	cleanDigest, err := validateDigest(digest)
	if err != nil {
		return cacheIdentity{}, err
	}
	if len(rootfsSize) > 16 || !rootfsSizePattern.MatchString(rootfsSize) {
		return cacheIdentity{}, errors.New("rootfs size must match ^[1-9][0-9]*[KMGTP]?$")
	}
	if len(bakeFormat) > 32 || !bakeFormatPattern.MatchString(bakeFormat) {
		return cacheIdentity{}, errors.New("bake format must start with a lowercase letter and contain only lowercase letters, digits, and hyphens")
	}
	return cacheIdentity{Digest: cleanDigest, RootfsSize: rootfsSize, BakeFormat: bakeFormat}, nil
}

func cacheObjectPrefix(identity cacheIdentity) string {
	return "rootfs/" + identity.Digest + "/size-" + identity.RootfsSize + "/format-" + identity.BakeFormat
}

func checksumObjectKey(identity cacheIdentity) string {
	return cacheObjectPrefix(identity) + "/" + checksumObjectName
}

func payloadObjectKey(identity cacheIdentity, checksum string) string {
	return cacheObjectPrefix(identity) + "/" + checksum + ".ext4"
}

func getRootfs(ctx context.Context, s *store.Store, identity cacheIdentity, out string) error {
	marker, err := getCompletenessMarker(ctx, s, identity)
	if err != nil {
		return err
	}

	body, _, err := s.Get(ctx, marker.PayloadKey)
	if err != nil {
		return err
	}
	defer body.Close()

	dir := filepath.Dir(out)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return fmt.Errorf("create output directory: %w", err)
	}
	// Stage as <out>.tmp.<random> so the builder's ENOSPC orphan sweep matches
	// an interrupted download at the same depth as its other rootfs temporaries.
	tmp, err := os.CreateTemp(dir, filepath.Base(out)+".tmp.*")
	if err != nil {
		return fmt.Errorf("create temporary output: %w", err)
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName)

	hash := sha256.New()
	if _, err := io.Copy(io.MultiWriter(tmp, hash), body); err != nil {
		tmp.Close()
		return fmt.Errorf("download %q: %w", marker.PayloadKey, err)
	}
	if err := tmp.Close(); err != nil {
		return fmt.Errorf("close temporary output: %w", err)
	}
	got := hex.EncodeToString(hash.Sum(nil))
	if got != marker.SHA256 {
		return fmt.Errorf("checksum mismatch for %q: got %s, want %s", marker.PayloadKey, got, marker.SHA256)
	}
	sparse.BestEffort(tmpName, "rootfs-store-download")
	if err := os.Rename(tmpName, out); err != nil {
		return fmt.Errorf("publish output: %w", err)
	}
	return nil
}

func getCompletenessMarker(ctx context.Context, s *store.Store, identity cacheIdentity) (completenessMarker, error) {
	key := checksumObjectKey(identity)
	body, _, err := s.Get(ctx, key)
	if err != nil {
		return completenessMarker{}, err
	}
	defer body.Close()
	contents, err := io.ReadAll(io.LimitReader(body, maxMarkerBytes+1))
	if err != nil {
		return completenessMarker{}, fmt.Errorf("download %q: %w", key, err)
	}
	if len(contents) > maxMarkerBytes {
		return completenessMarker{}, fmt.Errorf("completeness marker %q exceeds %d bytes", key, maxMarkerBytes)
	}
	var marker completenessMarker
	if err := json.Unmarshal(bytes.TrimSpace(contents), &marker); err != nil {
		return completenessMarker{}, fmt.Errorf("decode completeness marker %q: %w", key, err)
	}
	want, err := validateDigest(marker.SHA256)
	if err != nil {
		return completenessMarker{}, fmt.Errorf("completeness marker %q: %w", key, err)
	}
	marker.SHA256 = want
	markerDigest, err := validateDigest(marker.ImageDigest)
	if err != nil {
		return completenessMarker{}, fmt.Errorf("completeness marker %q has invalid image digest: %w", key, err)
	}
	if markerDigest != identity.Digest || marker.RootfsSize != identity.RootfsSize || marker.BakeFormat != identity.BakeFormat {
		return completenessMarker{}, fmt.Errorf(
			"completeness marker %q identity is digest=%s rootfs-size=%s bake-format=%s, want digest=%s rootfs-size=%s bake-format=%s",
			key, markerDigest, marker.RootfsSize, marker.BakeFormat,
			identity.Digest, identity.RootfsSize, identity.BakeFormat,
		)
	}
	wantPayloadKey := payloadObjectKey(identity, want)
	if marker.PayloadKey != wantPayloadKey {
		return completenessMarker{}, fmt.Errorf("completeness marker %q names payload %q, want %q", key, marker.PayloadKey, wantPayloadKey)
	}
	return marker, nil
}

func putRootfs(ctx context.Context, s *store.Store, identity cacheIdentity, path, imageRef string) (putResult, string, error) {
	checksumKey := checksumObjectKey(identity)
	// The sidecar is the completeness marker. A payload without it can only be
	// an interrupted upload and must not suppress a repair upload.
	exists, err := s.Head(ctx, checksumKey)
	if err != nil {
		return putAlreadyPresent, "", err
	}
	if exists {
		if _, err := getCompletenessMarker(ctx, s, identity); err != nil {
			return putAlreadyPresent, "", fmt.Errorf("verify existing completeness marker: %w", err)
		}
		return putAlreadyPresent, "", nil
	}

	file, err := os.Open(path)
	if err != nil {
		return putAlreadyPresent, "", fmt.Errorf("open rootfs: %w", err)
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return putAlreadyPresent, "", fmt.Errorf("stat rootfs: %w", err)
	}
	if !info.Mode().IsRegular() {
		return putAlreadyPresent, "", errors.New("rootfs is not a regular file")
	}

	hash := sha256.New()
	// Compute the payload identity in one pass. The object key must be known before
	// the upload begins, so rewind the regular file after hashing it.
	if _, err := io.Copy(hash, file); err != nil {
		return putAlreadyPresent, "", fmt.Errorf("hash rootfs: %w", err)
	}
	checksum := hex.EncodeToString(hash.Sum(nil))
	payloadKey := payloadObjectKey(identity, checksum)
	if _, err := file.Seek(0, io.SeekStart); err != nil {
		return putAlreadyPresent, "", fmt.Errorf("rewind rootfs: %w", err)
	}

	// Recheck immediately before PUT so a later writer can become a no-op before
	// uploading. Writers that still race upload to distinct content-addressed keys.
	exists, err = s.Head(ctx, checksumKey)
	if err != nil {
		return putAlreadyPresent, "", err
	}
	if exists {
		if _, err := getCompletenessMarker(ctx, s, identity); err != nil {
			return putAlreadyPresent, "", fmt.Errorf("verify existing completeness marker: %w", err)
		}
		return putAlreadyPresent, "", nil
	}

	// Reading a sparse file streams zero bytes for every hole. GCS stores those
	// holes as zeros, so the object has the file's nominal, logical size.
	if err := s.Put(ctx, payloadKey, file, info.Size()); err != nil {
		return putAlreadyPresent, "", err
	}

	// A winner may have published its sidecar while this payload was uploading.
	// If it names different content, leave this payload as harmless retention-sweep
	// work rather than replacing the winner's completeness marker.
	exists, err = s.Head(ctx, checksumKey)
	if err != nil {
		return putAlreadyPresent, "", err
	}
	if exists {
		winner, err := getCompletenessMarker(ctx, s, identity)
		if err != nil {
			return putAlreadyPresent, "", err
		}
		if winner.PayloadKey == payloadKey {
			return putAlreadyPresent, "", nil
		}
		return putOrphaned, payloadKey, nil
	}

	sidecar, err := json.Marshal(completenessMarker{
		PayloadKey:  payloadKey,
		SHA256:      checksum,
		ImageRef:    imageRef,
		ImageDigest: identity.Digest,
		RootfsSize:  identity.RootfsSize,
		BakeFormat:  identity.BakeFormat,
		UploadedAt:  time.Now().UTC().Format(time.RFC3339Nano),
	})
	if err != nil {
		return putAlreadyPresent, "", fmt.Errorf("encode completeness marker: %w", err)
	}
	sidecar = append(sidecar, '\n')
	if len(sidecar) > maxMarkerBytes {
		return putAlreadyPresent, "", fmt.Errorf("completeness marker exceeds %d bytes", maxMarkerBytes)
	}
	if err := s.Put(ctx, checksumKey, strings.NewReader(string(sidecar)), int64(len(sidecar))); err != nil {
		return putAlreadyPresent, "", err
	}
	return putUploaded, payloadKey, nil
}
