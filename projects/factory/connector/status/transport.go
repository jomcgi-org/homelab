package status

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io"
	"net/http"

	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

// Avoid net/http's implicit retries on reused connections and HTTP/2 streams:
// each wire attempt must pass through RoundTrip and the durable journal,
// including auth retries.
var readTransport = func() *http.Transport {
	t := http.DefaultTransport.(*http.Transport).Clone()
	t.DisableKeepAlives = true
	t.Protocols = new(http.Protocols)
	t.Protocols.SetHTTP1(true)
	return t
}()

// WriterTransport journals and fences each itsaplan, broker and GitHub read.
// Begin checks the writer epoch, commits the attempt, then checks the writer
// again. A failure in any of those steps prevents the HTTP request.
type WriterTransport struct {
	Begin func(context.Context, string) error
}

func (t *WriterTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	if err := r.Context().Err(); err != nil {
		return nil, err
	}
	if t.Begin == nil {
		return nil, store.ErrNotWriter
	}
	id, err := readMessageID(r)
	if err != nil {
		return nil, err
	}
	if err := t.Begin(r.Context(), id); err != nil {
		return nil, err
	}
	request := r.Clone(r.Context())
	request.Header.Set("X-Request-ID", id)
	return readTransport.RoundTrip(request)
}

// Read attempts share an ID across token rotation, retries and writer takeover.
// Reads always fetch current state; the journal deduplicates identity, not data.
// Store only the digest, never a URL, credential, query body or response.
func readMessageID(r *http.Request) (string, error) {
	h := sha256.New()
	_, _ = io.WriteString(h, r.Method+"\n"+r.URL.String()+"\n")
	if r.Body != nil && r.Body != http.NoBody {
		if r.GetBody == nil {
			return "", errors.New("status: request body is not replayable")
		}
		body, err := r.GetBody()
		if err != nil {
			return "", err
		}
		defer body.Close()
		n, err := io.Copy(h, io.LimitReader(body, (1<<20)+1))
		if err != nil {
			return "", err
		}
		if n > 1<<20 {
			return "", errors.New("status: request body exceeds limit")
		}
	}
	return "status-read/" + hex.EncodeToString(h.Sum(nil)), nil
}
