package main

import (
	"bytes"
	"container/list"
	"fmt"
	"net/http"
	"sync"
	"time"
)

const (
	maxBody  = 4 << 20
	capacity = 64 << 20
)

type (
	cacheKey struct{ path, query, accept string }
	entry    struct {
		key     cacheKey
		expires time.Time
		header  http.Header
		body    []byte
		size    int
	}
	flight struct {
		done   chan struct{}
		result *entry
	}
	readCache struct {
		sync.Mutex
		entries                 map[cacheKey]*list.Element
		flights                 map[cacheKey]*flight
		lru                     list.List
		bytes                   int
		hits, misses, coalesced uint64
	}
)

func (c *readCache) remove(el *list.Element) {
	e := el.Value.(*entry)
	delete(c.entries, e.key)
	c.bytes -= e.size
	c.lru.Remove(el)
}

func (e *entry) write(w http.ResponseWriter) {
	for k, v := range e.header {
		w.Header()[k] = append([]string(nil), v...)
	}
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(e.body)
}

// Stream the leader's response while keeping a bounded copy for reuse.
type capture struct {
	http.ResponseWriter
	body   bytes.Buffer
	header http.Header
	status int
}

func (w *capture) Unwrap() http.ResponseWriter { return w.ResponseWriter }
func (w *capture) WriteHeader(code int) {
	if code >= 200 && w.status == 0 {
		w.status = code
		if code == 200 {
			w.header = w.Header().Clone()
			w.header.Del("X-Explore-Window-End")
		}
	}
	w.ResponseWriter.WriteHeader(code)
}

func (w *capture) Write(b []byte) (int, error) {
	if w.status == 0 {
		w.WriteHeader(200)
	}
	n, err := w.ResponseWriter.Write(b)
	if w.header != nil {
		if err != nil || n != len(b) || len(b) > maxBody-w.body.Len() {
			w.header = nil
			w.body = bytes.Buffer{}
		} else {
			w.body.Write(b)
		}
	}
	return n, err
}

func (c *readCache) serve(w http.ResponseWriter, r *http.Request, now func() time.Time, forward func(http.ResponseWriter)) {
	k := cacheKey{r.URL.Path, r.URL.RawQuery, r.Header.Get("Accept")}
	c.Lock()
	if el := c.entries[k]; el != nil {
		e := el.Value.(*entry)
		if now().Before(e.expires) {
			c.lru.MoveToFront(el)
			c.hits++
			c.Unlock()
			e.write(w)
			return
		}
		c.remove(el)
	}
	if f := c.flights[k]; f != nil {
		c.coalesced++
		c.Unlock()
		select {
		case <-f.done:
			if r.Context().Err() != nil {
				return
			}
			if f.result != nil {
				f.result.write(w)
			} else {
				forward(w)
			}
		case <-r.Context().Done():
		}
		return
	}
	if c.entries == nil {
		c.entries = make(map[cacheKey]*list.Element)
		c.flights = make(map[cacheKey]*flight)
	}
	f := &flight{done: make(chan struct{})}
	c.flights[k] = f
	c.misses++
	c.Unlock()
	defer func() { c.Lock(); delete(c.flights, k); close(f.done); c.Unlock() }()
	out := &capture{ResponseWriter: w}
	forward(out)
	if out.status == 0 {
		out.WriteHeader(200)
	}
	if out.header == nil || r.Context().Err() != nil {
		return
	}
	e := &entry{key: k, header: out.header, body: bytes.Clone(out.body.Bytes()), expires: now().Add(2 * time.Second)}
	e.size = 1 + len(k.path) + len(k.query) + len(k.accept) + len(e.body)
	for name, values := range e.header {
		e.size += len(name)
		for _, v := range values {
			e.size += len(v)
		}
	}
	c.Lock()
	defer c.Unlock()
	if e.size <= capacity {
		for c.bytes+e.size > capacity {
			c.remove(c.lru.Back())
		}
		c.entries[k] = c.lru.PushFront(e)
		c.bytes += e.size
		f.result = e
	}
}

func (c *readCache) metrics(w http.ResponseWriter) {
	c.Lock()
	h, m, f, b := c.hits, c.misses, c.coalesced, c.bytes
	c.Unlock()
	fmt.Fprintf(w, "explore_proxy_cache_hits_total %d\nexplore_proxy_cache_misses_total %d\nexplore_proxy_cache_coalesced_total %d\nexplore_proxy_cached_bytes %d\n", h, m, f, b)
}
