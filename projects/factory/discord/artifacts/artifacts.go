// Package artifacts is Bosun's artifact store (HOME-909): an MCP server whose
// tools put, list and delete files in the bosun-artifacts Garage bucket.
// Garage's website endpoint serves the bucket at https://artifact.jomcgi.dev
// behind Authentik; the edge adds a CSP sandbox to every response, because
// the content is untrusted.
package artifacts

import (
	"bytes"
	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"path"
	"regexp"
	"strings"
	"unicode/utf8"
)

// MaxBytes caps one artifact after decoding.
const MaxBytes = 2 << 20

// maxList caps one list answer.
const maxList = 200

// contentTypes maps each allowed extension to the type the object is stored
// with. The type comes from the extension only, never from the caller.
// Markdown is stored as plain text: every browser shows it, none renders it.
var contentTypes = map[string]string{
	".html": "text/html; charset=utf-8",
	".md":   "text/plain; charset=utf-8",
	".txt":  "text/plain; charset=utf-8",
	".csv":  "text/csv; charset=utf-8",
	".json": "application/json",
	".svg":  "image/svg+xml",
	".png":  "image/png",
}

var (
	snowflake = regexp.MustCompile(`^[0-9]{15,21}$`)
	segment   = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$`)
)

// ParamError is a bad argument; its message goes back to the model.
type ParamError struct{ Param, Message string }

func (e *ParamError) Error() string { return e.Param + ": " + e.Message }

// Store holds artifacts for the allowed servers.
type Store struct {
	S3      *S3
	BaseURL string // https://artifact.jomcgi.dev, no trailing slash
	// Servers lists the Discord guild IDs an artifact may be filed under.
	Servers map[string]bool
}

// Private is the server value for a file only Joe can open.
const Private = "private"

// Place is the server and channel prefix a call works in. Server is the
// current message's Discord server ID or Private. The edge serves
// /<server>/ to that server's Authentik group and authentik Admins, and
// every other path, private/ included, to authentik Admins only.
type Place struct {
	Server  string `json:"server"`
	Channel string `json:"channel"`
}

func (s *Store) prefix(p Place) (string, error) {
	if p.Server != Private && (!snowflake.MatchString(p.Server) || !s.Servers[p.Server]) {
		return "", &ParamError{"server", "must be the ID of an allowlisted Discord server, or private"}
	}
	if !snowflake.MatchString(p.Channel) {
		return "", &ParamError{"channel", "must be a Discord channel ID"}
	}
	return p.Server + "/" + p.Channel + "/", nil
}

// CleanPath validates a relative artifact path and returns its content type.
func CleanPath(p string) (string, error) {
	if len(p) == 0 || len(p) > 200 {
		return "", &ParamError{"path", "must be 1 to 200 characters"}
	}
	parts := strings.Split(p, "/")
	if len(parts) > 5 {
		return "", &ParamError{"path", "may have at most 5 segments"}
	}
	for _, part := range parts {
		if !segment.MatchString(part) {
			return "", &ParamError{"path", "segments use letters, digits, '.', '_' and '-', and start with a letter or digit"}
		}
	}
	ct, ok := contentTypes[strings.ToLower(path.Ext(p))]
	if !ok {
		return "", &ParamError{"path", "extension must be one of .html .md .txt .csv .json .svg .png"}
	}
	return ct, nil
}

// PutArgs are the put tool's arguments.
type PutArgs struct {
	Place
	Path     string `json:"path"`
	Content  string `json:"content"`
	Encoding string `json:"encoding"`
}

// PutResult is what put returns.
type PutResult struct {
	URL         string `json:"url"`
	Path        string `json:"path"`
	Bytes       int    `json:"bytes"`
	ContentType string `json:"contentType"`
}

func (s *Store) Put(ctx context.Context, a PutArgs) (PutResult, error) {
	prefix, err := s.prefix(a.Place)
	if err != nil {
		return PutResult{}, err
	}
	ct, err := CleanPath(a.Path)
	if err != nil {
		return PutResult{}, err
	}
	var body []byte
	switch a.Encoding {
	case "", "utf-8":
		body = []byte(a.Content)
	case "base64":
		if body, err = base64.StdEncoding.DecodeString(a.Content); err != nil {
			return PutResult{}, &ParamError{"content", "is not valid base64"}
		}
	default:
		return PutResult{}, &ParamError{"encoding", "must be utf-8 or base64"}
	}
	if len(body) == 0 || len(body) > MaxBytes {
		return PutResult{}, &ParamError{"content", fmt.Sprintf("must be 1 to %d bytes", MaxBytes)}
	}
	if ct == "image/png" {
		if !bytes.HasPrefix(body, []byte("\x89PNG\r\n\x1a\n")) {
			return PutResult{}, &ParamError{"content", "is not a PNG image; send PNG bytes with encoding base64"}
		}
	} else if !utf8.Valid(body) {
		return PutResult{}, &ParamError{"content", "must be UTF-8 text for this extension"}
	}
	key := prefix + a.Path
	if err := s.S3.Put(ctx, key, body, ct); err != nil {
		return PutResult{}, err
	}
	return PutResult{URL: s.BaseURL + "/" + key, Path: a.Path, Bytes: len(body), ContentType: ct}, nil
}

// Listed is one artifact in a list answer.
type Listed struct {
	Path  string `json:"path"`
	URL   string `json:"url"`
	Bytes int64  `json:"bytes"`
}

// ListResult is what list returns.
type ListResult struct {
	Artifacts []Listed `json:"artifacts"`
	Truncated bool     `json:"truncated"`
}

func (s *Store) List(ctx context.Context, p Place) (ListResult, error) {
	prefix, err := s.prefix(p)
	if err != nil {
		return ListResult{}, err
	}
	objects, truncated, err := s.S3.List(ctx, prefix, maxList)
	if err != nil {
		return ListResult{}, err
	}
	out := ListResult{Artifacts: []Listed{}, Truncated: truncated}
	for _, o := range objects {
		if !strings.HasPrefix(o.Key, prefix) {
			return ListResult{}, errors.New("s3 listed a key outside the prefix")
		}
		out.Artifacts = append(out.Artifacts, Listed{Path: strings.TrimPrefix(o.Key, prefix), URL: s.BaseURL + "/" + o.Key, Bytes: o.Size})
	}
	return out, nil
}

// DeleteArgs are the delete tool's arguments.
type DeleteArgs struct {
	Place
	Path string `json:"path"`
}

func (s *Store) Delete(ctx context.Context, a DeleteArgs) (map[string]string, error) {
	prefix, err := s.prefix(a.Place)
	if err != nil {
		return nil, err
	}
	if _, err := CleanPath(a.Path); err != nil {
		return nil, err
	}
	if err := s.S3.Delete(ctx, prefix+a.Path); err != nil {
		return nil, err
	}
	return map[string]string{"deleted": s.BaseURL + "/" + prefix + a.Path}, nil
}
