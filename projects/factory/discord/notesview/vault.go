// Package notesview is the members' read-only viewer of Bosun's notes vault
// (HOME-920) at notes.jomcgi.dev/<server-id>/: a file tree, rendered
// Markdown with [[wikilinks]] and backlinks, search and a link graph. It
// holds a read-only key on bucket bosun-notes and reads only the prefix of
// the server named by the URL's first path segment. .trash/ and .history/
// never appear. Who may open a server's pages is the edge's decision
// (projects/platform-factory/edge/chart/templates/notes-route.yaml).
package notesview

import (
	"context"
	"errors"
	"fmt"
	"path"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"
	"unicode/utf8"

	"github.com/jomcgi/homelab/projects/factory/discord/artifacts"
	"github.com/jomcgi/homelab/projects/factory/discord/notes"
)

// Limits. A server's index holds every note's text and HTML in memory.
const (
	maxNotes     = 2000
	maxPages     = 20
	pageSize     = 1000
	maxVaultSize = 32 << 20
	fetchWorkers = 8
)

var snowflake = regexp.MustCompile(`^[0-9]{15,21}$`)

// Bucket is the read-only S3 subset the viewer uses (artifacts.S3).
type Bucket interface {
	Get(ctx context.Context, key string) ([]byte, error)
	ListPage(ctx context.Context, prefix, from string, limit int) ([]artifacts.Object, string, error)
}

// Note is one note in a server's index.
type Note struct {
	Path      string // relative to <server>/, with .md
	Name      string // file name without .md
	Body      string
	HTML      string
	Links     []string // resolved note paths, sorted, no duplicates
	Backlinks []string
}

// Index is one server's vault at one time.
type Index struct {
	Server    string
	Notes     map[string]*Note
	Paths     []string // sorted
	Truncated bool
	Built     time.Time
	byName    map[string][]string
	byPath    map[string]string
}

// hidden reports whether a vault-relative key is in .trash/ or .history/,
// or in any other folder or file whose name starts with '.'.
func hidden(rel string) bool {
	for _, seg := range strings.Split(rel, "/") {
		if strings.HasPrefix(seg, ".") {
			return true
		}
	}
	return false
}

func key(s string) string { return strings.ToLower(strings.TrimSuffix(strings.TrimSpace(s), ".md")) }

// Resolve maps a wikilink target to a note path, like Obsidian: a target
// with a '/' names a path from the vault root, otherwise a file name; case
// does not matter, and ".md" is optional. With several notes of one name,
// the shortest path wins, then the first in order.
func (x *Index) Resolve(target string) string {
	k := key(strings.TrimPrefix(target, "/"))
	if k == "" {
		return ""
	}
	if strings.Contains(k, "/") {
		return x.byPath[k]
	}
	if c := x.byName[k]; len(c) > 0 {
		return c[0]
	}
	return ""
}

// Vault builds and caches one Index per server.
type Vault struct {
	Bucket  Bucket
	Servers map[string]bool
	TTL     time.Duration
	Now     func() time.Time

	mu    sync.Mutex
	cache map[string]*entry
}

type entry struct {
	mu  sync.Mutex
	idx *Index
}

// ErrUnknownServer is a server that is not allowlisted.
var ErrUnknownServer = errors.New("notesview: server is not allowlisted")

func (v *Vault) now() time.Time {
	if v.Now != nil {
		return v.Now()
	}
	return time.Now()
}

// Allowed reports whether server is an allowlisted guild ID.
func (v *Vault) Allowed(server string) bool {
	return snowflake.MatchString(server) && v.Servers[server]
}

// Index returns the server's index, rebuilt when older than TTL. Only one
// build per server runs at a time; a failed build keeps no stale index.
func (v *Vault) Index(ctx context.Context, server string) (*Index, error) {
	if !v.Allowed(server) {
		return nil, ErrUnknownServer
	}
	v.mu.Lock()
	if v.cache == nil {
		v.cache = map[string]*entry{}
	}
	e, ok := v.cache[server]
	if !ok {
		e = &entry{}
		v.cache[server] = e
	}
	v.mu.Unlock()
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.idx != nil && v.now().Sub(e.idx.Built) < v.TTL {
		return e.idx, nil
	}
	idx, err := v.build(ctx, server)
	if err != nil {
		e.idx = nil
		return nil, err
	}
	e.idx = idx
	return idx, nil
}

// list returns the server's note objects, skipping hidden keys. ".history/"
// and ".trash/" sort before most note names, so a listing pages past them.
func (v *Vault) list(ctx context.Context, prefix string) ([]artifacts.Object, bool, error) {
	var out []artifacts.Object
	from := ""
	for page := 0; page < maxPages; page++ {
		objects, next, err := v.Bucket.ListPage(ctx, prefix, from, pageSize)
		if err != nil {
			return nil, false, err
		}
		for _, o := range objects {
			rel, ok := strings.CutPrefix(o.Key, prefix)
			if !ok {
				return nil, false, errors.New("s3 listed a key outside the prefix")
			}
			if hidden(rel) || !strings.HasSuffix(rel, ".md") || o.Size > notes.MaxNoteBytes {
				continue
			}
			if len(out) == maxNotes {
				return out, true, nil
			}
			out = append(out, o)
		}
		if next == "" {
			return out, false, nil
		}
		from = next
	}
	return out, true, nil
}

func (v *Vault) build(ctx context.Context, server string) (*Index, error) {
	prefix := server + "/"
	objects, truncated, err := v.list(ctx, prefix)
	if err != nil {
		return nil, err
	}
	var total int64
	keep := objects[:0]
	for _, o := range objects {
		if total+o.Size > maxVaultSize {
			truncated = true
			break
		}
		total += o.Size
		keep = append(keep, o)
	}
	bodies := make([]string, len(keep))
	errs := make([]error, len(keep))
	work := make(chan int)
	var wg sync.WaitGroup
	for range fetchWorkers {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := range work {
				b, err := v.Bucket.Get(ctx, keep[i].Key)
				if errors.Is(err, artifacts.ErrNotFound) {
					continue // deleted since the list
				}
				if err != nil {
					errs[i] = err
					continue
				}
				if !utf8.Valid(b) {
					b = []byte(strings.ToValidUTF8(string(b), "�"))
				}
				bodies[i] = string(b)
			}
		}()
	}
	for i := range keep {
		work <- i
	}
	close(work)
	wg.Wait()
	if err := errors.Join(errs...); err != nil {
		return nil, fmt.Errorf("read notes: %w", err)
	}

	idx := &Index{
		Server:    server,
		Notes:     map[string]*Note{},
		Truncated: truncated,
		Built:     v.now(),
		byName:    map[string][]string{},
		byPath:    map[string]string{},
	}
	for i, o := range keep {
		rel := strings.TrimPrefix(o.Key, prefix)
		n := &Note{Path: rel, Name: strings.TrimSuffix(path.Base(rel), ".md"), Body: bodies[i]}
		idx.Notes[rel] = n
		idx.Paths = append(idx.Paths, rel)
		idx.byPath[key(rel)] = rel
		idx.byName[key(n.Name)] = append(idx.byName[key(n.Name)], rel)
	}
	sort.Strings(idx.Paths)
	for _, c := range idx.byName {
		sort.Slice(c, func(i, j int) bool {
			if len(c[i]) != len(c[j]) {
				return len(c[i]) < len(c[j])
			}
			return c[i] < c[j]
		})
	}
	href := func(p string) string { return NoteURL(server, p) }
	back := map[string]map[string]bool{}
	for _, p := range idx.Paths {
		n := idx.Notes[p]
		r, err := Render([]byte(n.Body), idx.Resolve, href)
		if err != nil {
			return nil, fmt.Errorf("render %s: %w", p, err)
		}
		n.HTML = r.HTML
		seen := map[string]bool{}
		for _, t := range r.Targets {
			if to := idx.Resolve(t); to != "" && to != p && !seen[to] {
				seen[to] = true
				n.Links = append(n.Links, to)
				if back[to] == nil {
					back[to] = map[string]bool{}
				}
				back[to][p] = true
			}
		}
		sort.Strings(n.Links)
	}
	for to, from := range back {
		for p := range from {
			idx.Notes[to].Backlinks = append(idx.Notes[to].Backlinks, p)
		}
		sort.Strings(idx.Notes[to].Backlinks)
	}
	return idx, nil
}
