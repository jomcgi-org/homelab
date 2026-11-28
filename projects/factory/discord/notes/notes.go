// Package notes is Bosun's notes vault (HOME-918): Markdown notes in the
// bosun-notes Garage bucket, one prefix per Discord server, in the shape of
// an Obsidian vault. The MCP tools (mcp.go) take the server from the calling
// session, never from the model: the bridge records which server and channel
// each session it starts belongs to, and Places reads that record.
//
// Layout under <server>/:
//
//	<path>.md                 notes
//	_hermes/MEMORY.md         Hermes' memory file (memory_read, memory_write)
//	_hermes/USER.md           Hermes' user profile
//	_hermes/channels/<id>.md  a channel session's handoff note (handoff_read,
//	                          handoff_write): its latest compaction summary
//	.trash/<path>.md          deleted notes (notes_delete moves them here)
//	.history/<path>/<ts>.md   versions that a write replaced
//
// Nothing here deletes a note for good: a delete is a move to .trash/, and a
// replace keeps the old version under .history/.
package notes

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"
	"unicode/utf8"

	"github.com/jomcgi/homelab/projects/factory/discord/artifacts"
)

// Limits.
const (
	MaxNoteBytes   = 256 << 10
	MaxAppendBytes = 64 << 10
	MaxMemoryBytes = 64 << 10
	// MaxHandoffBytes bounds a channel's handoff note. A new session reads
	// it into its first turn, so it stays small.
	MaxHandoffBytes = 16 << 10
	maxList         = 500
	pageSize        = 1000
	// maxPages bounds one listing: .trash/ and .history/ sort before most
	// note names, so a listing pages past them.
	maxPages     = 20
	maxHits      = 20
	snippetRunes = 200
)

// Reserved top-level folders.
const (
	Trash   = ".trash"
	History = ".history"
	Hermes  = "_hermes"
)

// MemoryFiles are the Hermes files memory_read and memory_write serve.
var MemoryFiles = map[string]bool{"MEMORY.md": true, "USER.md": true}

const contentType = "text/markdown; charset=utf-8"

var (
	snowflake = regexp.MustCompile(`^[0-9]{15,21}$`)
	// A segment is a file or folder name: letters, digits, space, '.', '_',
	// '-', '(' and ')'. It starts with a letter, digit, '_' or '(' and ends
	// with a letter, digit or ')'.
	segment = regexp.MustCompile(`^[\p{L}\p{N}_(]([\p{L}\p{N} ._()-]{0,98}[\p{L}\p{N})])?$`)
)

// ParamError is a bad argument; its message goes back to the model.
type ParamError struct{ Param, Message string }

func (e *ParamError) Error() string { return e.Param + ": " + e.Message }

// Bucket is the S3 subset the vault uses (artifacts.S3).
type Bucket interface {
	Get(ctx context.Context, key string) ([]byte, error)
	Put(ctx context.Context, key string, body []byte, contentType string) error
	Delete(ctx context.Context, key string) error
	ListPage(ctx context.Context, prefix, from string, limit int) ([]artifacts.Object, string, error)
}

// Store holds the notes of the allowed servers. It runs as one replica: the
// per-key locks make append, delete and memory_write's version check atomic.
type Store struct {
	Bucket Bucket
	// Servers lists the Discord guild IDs a vault may exist for.
	Servers map[string]bool
	Now     func() time.Time

	mu    sync.Mutex
	locks map[string]*sync.Mutex
}

func (s *Store) now() time.Time {
	if s.Now != nil {
		return s.Now()
	}
	return time.Now()
}

// lock serializes the calls on one key.
func (s *Store) lock(key string) func() {
	s.mu.Lock()
	if s.locks == nil {
		s.locks = map[string]*sync.Mutex{}
	}
	l, ok := s.locks[key]
	if !ok {
		l = &sync.Mutex{}
		s.locks[key] = l
	}
	s.mu.Unlock()
	l.Lock()
	return l.Unlock
}

func (s *Store) prefix(server string) (string, error) {
	if !snowflake.MatchString(server) || !s.Servers[server] {
		return "", errors.New("notes: session's server is not allowlisted")
	}
	return server + "/", nil
}

// CleanPath validates a note path relative to the server's vault and adds
// .md unless it ends in .md. With reserved, a path may start with .trash/
// or .history/ (reads only).
func CleanPath(p string, reserved bool) (string, error) {
	p = strings.TrimSpace(strings.TrimPrefix(p, "/"))
	// A trash or history copy adds a folder, a time and up to two
	// segments to a note's path, so reads of those allow more.
	maxLen, maxSegments := 200, 8
	if reserved {
		maxLen, maxSegments = 260, 10
	}
	if p == "" || len(p) > maxLen {
		return "", &ParamError{"path", fmt.Sprintf("must be 1 to %d characters", maxLen)}
	}
	if strings.HasSuffix(strings.ToLower(p), ".md") {
		p = p[:len(p)-3] + ".md"
	} else {
		p += ".md"
	}
	parts := strings.Split(p, "/")
	if len(parts) > maxSegments {
		return "", &ParamError{"path", fmt.Sprintf("may have at most %d segments", maxSegments)}
	}
	for i, part := range parts {
		if i == 0 && reserved && (part == Trash || part == History) && len(parts) > 1 {
			continue
		}
		if !segment.MatchString(part) || strings.Contains(part, "..") {
			return "", &ParamError{"path", "segments use letters, digits, spaces, '.', '_', '-', '(' and ')', and start with a letter or digit"}
		}
	}
	return p, nil
}

// writable refuses the folders only the bridge, the runner or a move write.
func writable(p string) error {
	top, _, _ := strings.Cut(p, "/")
	if top == Hermes || top == Trash || top == History {
		return &ParamError{"path", "folders " + Hermes + "/, " + Trash + "/ and " + History + "/ are read-only"}
	}
	return nil
}

func hidden(rel string) bool {
	return strings.HasPrefix(rel, Trash+"/") || strings.HasPrefix(rel, History+"/")
}

func checkText(body string, limit int) error {
	if len(body) > limit {
		return &ParamError{"content", fmt.Sprintf("must be at most %d bytes", limit)}
	}
	if !utf8.ValidString(body) {
		return &ParamError{"content", "must be UTF-8 text"}
	}
	return nil
}

func (s *Store) get(ctx context.Context, key string) (string, bool, error) {
	b, err := s.Bucket.Get(ctx, key)
	if errors.Is(err, artifacts.ErrNotFound) {
		return "", false, nil
	}
	if err != nil {
		return "", false, err
	}
	return string(b), true, nil
}

func stamp(t time.Time) string { return t.UTC().Format("20060102T150405.000Z") }

// keep copies the current version of rel to .history/ before a replace.
func (s *Store) keep(ctx context.Context, prefix, rel, old string) error {
	return s.Bucket.Put(ctx, prefix+History+"/"+strings.TrimSuffix(rel, ".md")+"/"+stamp(s.now())+".md", []byte(old), contentType)
}

// Note is one note in a list answer.
type Note struct {
	Path  string `json:"path"`
	Bytes int64  `json:"bytes"`
}

// ListResult is what notes_list returns.
type ListResult struct {
	Notes     []Note `json:"notes"`
	Truncated bool   `json:"truncated"`
}

// List lists the notes under folder ("" for all). .trash/ and .history/
// appear only with includeTrash.
func (s *Store) List(ctx context.Context, server, folder string, includeTrash bool) (ListResult, error) {
	prefix, err := s.prefix(server)
	if err != nil {
		return ListResult{}, err
	}
	folder = strings.Trim(strings.TrimSpace(folder), "/")
	if folder != "" {
		if _, err := CleanPath(folder+"/x", true); err != nil {
			return ListResult{}, &ParamError{"folder", "is not a valid folder"}
		}
		folder += "/"
	}
	out := ListResult{Notes: []Note{}}
	from := ""
	for page := 0; ; page++ {
		if page == maxPages {
			out.Truncated = true
			return out, nil
		}
		objects, next, err := s.Bucket.ListPage(ctx, prefix+folder, from, pageSize)
		if err != nil {
			return ListResult{}, err
		}
		for _, o := range objects {
			rel, ok := strings.CutPrefix(o.Key, prefix)
			if !ok {
				return ListResult{}, errors.New("s3 listed a key outside the prefix")
			}
			if hidden(rel) && !includeTrash {
				continue
			}
			if len(out.Notes) == maxList {
				out.Truncated = true
				return out, nil
			}
			out.Notes = append(out.Notes, Note{Path: rel, Bytes: o.Size})
		}
		if next == "" {
			return out, nil
		}
		from = next
	}
}

// ReadResult is what notes_read returns.
type ReadResult struct {
	Path    string `json:"path"`
	Content string `json:"content"`
}

// Read returns one note.
func (s *Store) Read(ctx context.Context, server, path string) (ReadResult, error) {
	prefix, err := s.prefix(server)
	if err != nil {
		return ReadResult{}, err
	}
	rel, err := CleanPath(path, true)
	if err != nil {
		return ReadResult{}, err
	}
	body, ok, err := s.get(ctx, prefix+rel)
	if err != nil {
		return ReadResult{}, err
	}
	if !ok {
		return ReadResult{}, &ParamError{"path", "no note " + rel}
	}
	return ReadResult{Path: rel, Content: body}, nil
}

// Hit is one search match.
type Hit struct {
	Path    string `json:"path"`
	Snippet string `json:"snippet"`
	Matches int    `json:"matches"`
}

// SearchResult is what notes_search returns.
type SearchResult struct {
	Hits      []Hit `json:"hits"`
	Truncated bool  `json:"truncated"`
}

// Search finds notes whose path or text holds query, ignoring case.
func (s *Store) Search(ctx context.Context, server, query string, includeTrash bool) (SearchResult, error) {
	q := strings.ToLower(strings.TrimSpace(query))
	if q == "" || len(q) > 200 {
		return SearchResult{}, &ParamError{"query", "must be 1 to 200 characters"}
	}
	list, err := s.List(ctx, server, "", includeTrash)
	if err != nil {
		return SearchResult{}, err
	}
	out := SearchResult{Hits: []Hit{}, Truncated: list.Truncated}
	prefix := server + "/"
	for _, n := range list.Notes {
		body, ok, err := s.get(ctx, prefix+n.Path)
		if err != nil {
			return SearchResult{}, err
		}
		if !ok {
			continue
		}
		lower := strings.ToLower(body)
		count := strings.Count(lower, q)
		inPath := strings.Contains(strings.ToLower(n.Path), q)
		if count == 0 && !inPath {
			continue
		}
		if len(out.Hits) == maxHits {
			out.Truncated = true
			break
		}
		out.Hits = append(out.Hits, Hit{Path: n.Path, Snippet: snippet(body, lower, q), Matches: count})
	}
	sort.SliceStable(out.Hits, func(i, j int) bool { return out.Hits[i].Matches > out.Hits[j].Matches })
	return out, nil
}

// snippet is about snippetRunes runes around the first match. lower is
// strings.ToLower(body); byte offsets can differ for some scripts, so it
// cuts on rune boundaries of lower and maps by rune count.
func snippet(body, lower, q string) string {
	runes := []rune(body)
	at := 0
	if i := strings.Index(lower, q); i >= 0 {
		at = utf8.RuneCountInString(lower[:i])
	}
	start := max(at-snippetRunes/2, 0)
	end := min(start+snippetRunes, len(runes))
	start = min(start, end)
	s := strings.Join(strings.Fields(string(runes[start:end])), " ")
	if start > 0 {
		s = "…" + s
	}
	if end < len(runes) {
		s += "…"
	}
	return s
}

// WriteResult is what notes_write and notes_append return.
type WriteResult struct {
	Path     string `json:"path"`
	Bytes    int    `json:"bytes"`
	Created  bool   `json:"created"`
	Replaced bool   `json:"replaced,omitempty"`
}

// Write creates a note, or replaces one with replace. A replaced version
// stays under .history/.
func (s *Store) Write(ctx context.Context, server, path, content string, replace bool) (WriteResult, error) {
	prefix, err := s.prefix(server)
	if err != nil {
		return WriteResult{}, err
	}
	rel, err := CleanPath(path, false)
	if err != nil {
		return WriteResult{}, err
	}
	if err := writable(rel); err != nil {
		return WriteResult{}, err
	}
	if err := checkText(content, MaxNoteBytes); err != nil {
		return WriteResult{}, err
	}
	defer s.lock(prefix + rel)()
	old, exists, err := s.get(ctx, prefix+rel)
	if err != nil {
		return WriteResult{}, err
	}
	if exists && !replace {
		return WriteResult{}, &ParamError{"path", rel + " exists: choose a new path, or set replace when Joe asks to replace it"}
	}
	if exists && old == content {
		return WriteResult{Path: rel, Bytes: len(content)}, nil
	}
	if exists {
		if err := s.keep(ctx, prefix, rel, old); err != nil {
			return WriteResult{}, err
		}
	}
	if err := s.Bucket.Put(ctx, prefix+rel, []byte(content), contentType); err != nil {
		return WriteResult{}, err
	}
	return WriteResult{Path: rel, Bytes: len(content), Created: !exists, Replaced: exists}, nil
}

// Append adds text to the end of a note, on a new line, and creates the note
// when it does not exist.
func (s *Store) Append(ctx context.Context, server, path, content string) (WriteResult, error) {
	prefix, err := s.prefix(server)
	if err != nil {
		return WriteResult{}, err
	}
	rel, err := CleanPath(path, false)
	if err != nil {
		return WriteResult{}, err
	}
	if err := writable(rel); err != nil {
		return WriteResult{}, err
	}
	if strings.TrimSpace(content) == "" {
		return WriteResult{}, &ParamError{"content", "is empty"}
	}
	if err := checkText(content, MaxAppendBytes); err != nil {
		return WriteResult{}, err
	}
	defer s.lock(prefix + rel)()
	old, exists, err := s.get(ctx, prefix+rel)
	if err != nil {
		return WriteResult{}, err
	}
	body := content
	if exists {
		body = old
		if body != "" && !strings.HasSuffix(body, "\n") {
			body += "\n"
		}
		body += content
	}
	if err := checkText(body, MaxNoteBytes); err != nil {
		return WriteResult{}, &ParamError{"content", fmt.Sprintf("the note would pass %d bytes; start a new note", MaxNoteBytes)}
	}
	if err := s.Bucket.Put(ctx, prefix+rel, []byte(body), contentType); err != nil {
		return WriteResult{}, err
	}
	return WriteResult{Path: rel, Bytes: len(body), Created: !exists}, nil
}

// DeleteResult is what notes_delete returns.
type DeleteResult struct {
	Path    string `json:"path"`
	TrashAt string `json:"trashedAs"`
}

// Delete moves a note to .trash/<path>, or to .trash/<path>.<time>.md when
// that name is taken. The note's text is never lost.
func (s *Store) Delete(ctx context.Context, server, path string) (DeleteResult, error) {
	prefix, err := s.prefix(server)
	if err != nil {
		return DeleteResult{}, err
	}
	rel, err := CleanPath(path, false)
	if err != nil {
		return DeleteResult{}, err
	}
	if err := writable(rel); err != nil {
		return DeleteResult{}, err
	}
	defer s.lock(prefix + rel)()
	body, ok, err := s.get(ctx, prefix+rel)
	if err != nil {
		return DeleteResult{}, err
	}
	if !ok {
		return DeleteResult{}, &ParamError{"path", "no note " + rel}
	}
	trash := Trash + "/" + rel
	defer s.lock(prefix + trash)()
	if _, taken, err := s.get(ctx, prefix+trash); err != nil {
		return DeleteResult{}, err
	} else if taken {
		trash = Trash + "/" + strings.TrimSuffix(rel, ".md") + "." + stamp(s.now()) + ".md"
	}
	// Copy first: a failure in between leaves two copies, never none.
	if err := s.Bucket.Put(ctx, prefix+trash, []byte(body), contentType); err != nil {
		return DeleteResult{}, err
	}
	if err := s.Bucket.Delete(ctx, prefix+rel); err != nil {
		return DeleteResult{}, err
	}
	return DeleteResult{Path: rel, TrashAt: trash}, nil
}

// Version names a memory file's content: the hex SHA-256, or "" for a file
// that does not exist.
func Version(content string, exists bool) string {
	if !exists {
		return ""
	}
	sum := sha256.Sum256([]byte(content))
	return hex.EncodeToString(sum[:])
}

// MemoryResult is what memory_read and memory_write return.
type MemoryResult struct {
	File    string `json:"file"`
	Content string `json:"content"`
	Version string `json:"version"`
	// Conflict is true when base_version was not the current version: the
	// file is unchanged, and Content and Version are the current copy to
	// merge into.
	Conflict bool `json:"conflict,omitempty"`
}

func memoryKey(prefix, file string) (string, error) {
	if !MemoryFiles[file] {
		return "", &ParamError{"file", "must be MEMORY.md or USER.md"}
	}
	return prefix + Hermes + "/" + file, nil
}

// MemoryRead returns a Hermes memory file and its version. A missing file is
// empty, with version "".
func (s *Store) MemoryRead(ctx context.Context, server, file string) (MemoryResult, error) {
	prefix, err := s.prefix(server)
	if err != nil {
		return MemoryResult{}, err
	}
	key, err := memoryKey(prefix, file)
	if err != nil {
		return MemoryResult{}, err
	}
	body, ok, err := s.get(ctx, key)
	if err != nil {
		return MemoryResult{}, err
	}
	return MemoryResult{File: file, Content: body, Version: Version(body, ok)}, nil
}

// MemoryWrite replaces a Hermes memory file when its current version is
// base. Otherwise it changes nothing and returns the current copy with
// Conflict set, so the caller can merge and try again. The replaced version
// stays under .history/_hermes/.
func (s *Store) MemoryWrite(ctx context.Context, server, file, content, base string) (MemoryResult, error) {
	prefix, err := s.prefix(server)
	if err != nil {
		return MemoryResult{}, err
	}
	key, err := memoryKey(prefix, file)
	if err != nil {
		return MemoryResult{}, err
	}
	if err := checkText(content, MaxMemoryBytes); err != nil {
		return MemoryResult{}, err
	}
	defer s.lock(key)()
	old, exists, err := s.get(ctx, key)
	if err != nil {
		return MemoryResult{}, err
	}
	current := Version(old, exists)
	if current != base {
		return MemoryResult{File: file, Content: old, Version: current, Conflict: true}, nil
	}
	if exists && old == content {
		return MemoryResult{File: file, Content: content, Version: current}, nil
	}
	if exists {
		if err := s.keep(ctx, prefix, Hermes+"/"+file, old); err != nil {
			return MemoryResult{}, err
		}
	}
	if err := s.Bucket.Put(ctx, key, []byte(content), contentType); err != nil {
		return MemoryResult{}, err
	}
	return MemoryResult{File: file, Content: content, Version: Version(content, true)}, nil
}

// HandoffResult is what handoff_read and handoff_write return.
type HandoffResult struct {
	Channel string `json:"channel_id"`
	Content string `json:"content"`
}

// handoffKey is the key of a channel's handoff note (HOME-944). The channel
// comes from the bridge's record of the session, never from the caller.
func handoffKey(prefix, channel string) (string, error) {
	if !snowflake.MatchString(channel) {
		return "", errors.New("notes: session's channel is not a Discord ID")
	}
	return prefix + Hermes + "/channels/" + channel + ".md", nil
}

// HandoffRead returns the handoff note of the session's channel; a missing
// note is empty.
func (s *Store) HandoffRead(ctx context.Context, server, channel string) (HandoffResult, error) {
	prefix, err := s.prefix(server)
	if err != nil {
		return HandoffResult{}, err
	}
	key, err := handoffKey(prefix, channel)
	if err != nil {
		return HandoffResult{}, err
	}
	body, _, err := s.get(ctx, key)
	if err != nil {
		return HandoffResult{}, err
	}
	return HandoffResult{Channel: channel, Content: body}, nil
}

// HandoffWrite replaces the handoff note of the session's channel. The note
// is the session's latest compaction summary, so the newest write wins and
// no old version is kept.
func (s *Store) HandoffWrite(ctx context.Context, server, channel, content string) (HandoffResult, error) {
	prefix, err := s.prefix(server)
	if err != nil {
		return HandoffResult{}, err
	}
	key, err := handoffKey(prefix, channel)
	if err != nil {
		return HandoffResult{}, err
	}
	if err := checkText(content, MaxHandoffBytes); err != nil {
		return HandoffResult{}, err
	}
	if strings.TrimSpace(content) == "" {
		return HandoffResult{}, &ParamError{"content", "must not be empty"}
	}
	defer s.lock(key)()
	if err := s.Bucket.Put(ctx, key, []byte(content), contentType); err != nil {
		return HandoffResult{}, err
	}
	return HandoffResult{Channel: channel, Content: content}, nil
}
