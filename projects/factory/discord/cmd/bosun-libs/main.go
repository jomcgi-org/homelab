// Command bosun-libs uploads the pinned browser libraries that artifact
// pages load from https://artifact.jomcgi.dev/_lib/ to the bosun-libs
// Garage bucket. The factory chart runs it as a PostSync Job; Bosun's
// artifact tools hold no key for this bucket.
package main

import (
	"context"
	"fmt"
	"io/fs"
	"log/slog"
	"mime"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/jomcgi/homelab/projects/factory/discord/artifacts"
)

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stderr, nil))
	if err := run(log); err != nil {
		log.Error("bosun-libs failed", "err", err)
		os.Exit(1)
	}
}

// contentType names the type Garage serves for a library file.
func contentType(name string) string {
	switch filepath.Ext(name) {
	case ".js":
		return "text/javascript; charset=utf-8"
	case ".html":
		return "text/html; charset=utf-8"
	}
	if t := mime.TypeByExtension(filepath.Ext(name)); t != "" {
		return t
	}
	return "application/octet-stream"
}

func run(log *slog.Logger) error {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
	defer cancel()
	dir := os.Getenv("LIB_DIR")
	if dir == "" {
		return fmt.Errorf("LIB_DIR is required")
	}
	s3, err := artifacts.NewS3(os.Getenv("AWS_ENDPOINT_URL"), os.Getenv("AWS_REGION"), os.Getenv("BUCKET"),
		os.Getenv("AWS_ACCESS_KEY_ID"), os.Getenv("AWS_SECRET_ACCESS_KEY"))
	if err != nil {
		return err
	}
	stored := map[string]int64{}
	objects, _, err := s3.List(ctx, "", 1000)
	if err != nil {
		return err
	}
	for _, o := range objects {
		stored[o.Key] = o.Size
	}
	shipped := map[string]bool{}
	err = filepath.WalkDir(dir, func(path string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return err
		}
		key := filepath.ToSlash(strings.TrimPrefix(path, filepath.Clean(dir)+string(filepath.Separator)))
		shipped[key] = true
		body, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		// A library's name carries its version, so the same name and size
		// is the same file. Pages are small and always rewritten.
		if size, ok := stored[key]; ok && size == int64(len(body)) && strings.HasSuffix(key, ".js") {
			log.Info("unchanged", "key", key)
			return nil
		}
		if err := s3.Put(ctx, key, body, contentType(key)); err != nil {
			return fmt.Errorf("%s: %w", key, err)
		}
		log.Info("uploaded", "key", key, "bytes", len(body))
		return nil
	})
	if err != nil {
		return err
	}
	// Old versions go, so the bucket's quota holds only what pages load.
	for key := range stored {
		if !shipped[key] {
			if err := s3.Delete(ctx, key); err != nil {
				return fmt.Errorf("%s: %w", key, err)
			}
			log.Info("deleted", "key", key)
		}
	}
	return nil
}
