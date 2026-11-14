package github

import "sync"

// DefaultETagEntries bounds an ETagCache whose Max is 0.
const DefaultETagEntries = 1024

// ETagCache keeps REST GET responses with their ETags. A client with one
// sends If-None-Match on every repeated read, and GitHub answers an
// unchanged resource with 304 Not Modified, which costs no rate limit. The
// least recently used entry goes first once Max entries are held.
type ETagCache struct {
	// Max bounds the entries kept, DefaultETagEntries when 0.
	Max int
	// Observe, when set, is told whether each conditional read was answered
	// 304 (true) or with a changed body (false).
	Observe func(notModified bool)

	mu      sync.Mutex
	entries map[string]*etagEntry
	clock   uint64
}

type etagEntry struct {
	etag string
	body []byte
	used uint64
}

func (c *ETagCache) get(key string) (string, []byte, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	e, ok := c.entries[key]
	if !ok {
		return "", nil, false
	}
	c.clock++
	e.used = c.clock
	return e.etag, e.body, true
}

func (c *ETagCache) put(key, etag string, body []byte) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.entries == nil {
		c.entries = map[string]*etagEntry{}
	}
	limit := c.Max
	if limit <= 0 {
		limit = DefaultETagEntries
	}
	if _, ok := c.entries[key]; !ok && len(c.entries) >= limit {
		var oldest string
		var at uint64
		for k, e := range c.entries {
			if oldest == "" || e.used < at {
				oldest, at = k, e.used
			}
		}
		delete(c.entries, oldest)
	}
	c.clock++
	c.entries[key] = &etagEntry{etag: etag, body: body, used: c.clock}
}

func (c *ETagCache) observe(notModified bool) {
	if c.Observe != nil {
		c.Observe(notModified)
	}
}

// Len is the number of cached responses.
func (c *ETagCache) Len() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return len(c.entries)
}
