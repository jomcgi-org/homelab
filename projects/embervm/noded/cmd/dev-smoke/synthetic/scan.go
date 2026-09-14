// Package synthetic implements the bounded, built-in development scan workload.
// It generates its own files and never accepts source code, paths or commands.
package synthetic

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"time"
)

type Request struct {
	Seed         string `json:"seed"`
	Files        int    `json:"files"`
	BytesPerFile int    `json:"bytes_per_file"`
	Passes       int    `json:"passes"`
	HoldMS       int    `json:"hold_ms"`
}

type Result struct {
	Synthetic bool    `json:"synthetic"`
	Files     int     `json:"files"`
	Bytes     int     `json:"bytes"`
	Passes    int     `json:"passes"`
	Findings  int     `json:"findings"`
	Checksum  string  `json:"checksum"`
	ScanMS    float64 `json:"scan_ms"`
	HoldMS    int     `json:"hold_ms"`
}

func Decode(r io.Reader) (Request, error) {
	q := Request{Seed: "demo", Files: 64, BytesPerFile: 32768, Passes: 64}
	d := json.NewDecoder(r)
	d.DisallowUnknownFields()
	value := &q
	if err := d.Decode(&value); err != nil {
		return q, err
	}
	if value == nil {
		return q, errors.New("expected a JSON object")
	}
	var extra any
	if err := d.Decode(&extra); err != io.EOF {
		return q, errors.New("expected one JSON object")
	}
	if len(q.Seed) > 80 || q.Files < 1 || q.Files > 256 || q.BytesPerFile < 1024 || q.BytesPerFile > 65536 || q.Passes < 1 || q.Passes > 1024 || q.HoldMS < 0 || q.HoldMS > 5000 {
		return q, errors.New("limits: seed <= 80 bytes, files 1..256, bytes_per_file 1024..65536, passes 1..1024, hold_ms 0..5000")
	}
	return q, nil
}

// Run writes generated fixtures to the fresh guest's tmpfs, then repeatedly
// reads, matches a fixed marker and hashes them. ScanMS excludes the optional
// hold, which exists only to make overload/cancellation checks reproducible.
func Run(ctx context.Context, dir string, q Request) (Result, error) {
	start := time.Now()
	result := Result{Synthetic: true, Files: q.Files, Bytes: q.Files * q.BytesPerFile, Passes: q.Passes, HoldMS: q.HoldMS}
	if err := os.Mkdir(dir, 0o700); err != nil {
		return result, fmt.Errorf("scan directory must be fresh: %w", err)
	}
	for i := 0; i < q.Files; i++ {
		line := []byte(fmt.Sprintf("// TODO synthetic finding %s %d\n", q.Seed, i))
		body := bytes.Repeat(line, q.BytesPerFile/len(line)+1)[:q.BytesPerFile]
		if err := os.WriteFile(filepath.Join(dir, fmt.Sprint(i)), body, 0o600); err != nil {
			return result, err
		}
	}
	hash := sha256.New()
	for pass := 0; pass < q.Passes; pass++ {
		for i := 0; i < q.Files; i++ {
			if err := ctx.Err(); err != nil {
				return result, err
			}
			body, err := os.ReadFile(filepath.Join(dir, fmt.Sprint(i)))
			if err != nil {
				return result, err
			}
			matches := bytes.Count(body, []byte("TODO"))
			if pass == 0 {
				result.Findings += matches
			}
			_, _ = hash.Write(body)
			_, _ = fmt.Fprintf(hash, ":%d:%d:%d", pass, i, matches)
		}
	}
	result.Checksum = hex.EncodeToString(hash.Sum(nil))
	result.ScanMS = float64(time.Since(start).Microseconds()) / 1000
	timer := time.NewTimer(time.Duration(q.HoldMS) * time.Millisecond)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return result, ctx.Err()
	case <-timer.C:
		return result, nil
	}
}
