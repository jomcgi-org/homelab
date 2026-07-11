package shim

import (
	"context"
	"fmt"
	"io"
	"net"
	"net/http"
	"strconv"
)

// Request carries the inbound /invoke call to a workload Handler. Path is the
// full URL path (e.g. "/invoke" or "/invoke/foo"), which the workload can use
// for sub-routing. Body is the opaque payload from the caller.
type Request struct {
	Path string
	Body io.Reader
}

// Response is returned by a Handler on a successful invocation. A zero Status
// is treated as 200 OK.
type Response struct {
	Status int
	Body   []byte
}

// Handler is the workload function that handles an /invoke call. Implementations
// must be safe for concurrent use.
type Handler func(ctx context.Context, r *Request) (*Response, error)

// Option configures a Server.
type Option func(*Server)

// WithChain installs a hook Chain that wraps every /invoke dispatch. The
// default is an empty chain, which runs the Handler directly.
func WithChain(c Chain) Option {
	return func(s *Server) {
		s.chain = c
	}
}

// WithReady sets the function polled by GET /shim/ready. The endpoint returns
// 200 when fn returns true and 503 when fn returns false. The default function
// always returns true. fc-invoke's warm-base readiness probe uses this: the
// guest flips it true once its workload is fully warmed.
func WithReady(fn func() bool) Option {
	return func(s *Server) {
		s.ready = fn
	}
}

// Server is an HTTP server that dispatches /invoke requests to a Handler and
// exposes a /shim/* control surface. It serves over any net.Listener (vsock
// in production, TCP/UDS in tests), making it fully testable without
// Firecracker.
type Server struct {
	h     Handler
	chain Chain
	ready func() bool
	srv   *http.Server
}

// NewServer builds a Server that dispatches invocations to h, applying any
// supplied options before wiring the mux.
func NewServer(h Handler, opts ...Option) *Server {
	s := &Server{
		h:     h,
		ready: func() bool { return true },
	}
	for _, o := range opts {
		o(s)
	}
	m := s.mux()
	s.srv = &http.Server{Handler: m}
	return s
}

// mux builds the HTTP routing table. It is a method (not a stored field) so
// that tests can drive individual requests via srv.mux() without starting a
// network listener.
//
// Routes:
//   - GET /shim/healthz   : liveness probe, always 200.
//   - GET /shim/ready     : readiness probe, 200 or 503 per WithReady.
//   - /invoke, /invoke/   : workload handler, wrapped by the hook chain.
//   - everything else     : 404 (ServeMux default).
func (s *Server) mux() *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /shim/healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	})
	mux.HandleFunc("GET /shim/ready", func(w http.ResponseWriter, _ *http.Request) {
		if s.ready() {
			w.WriteHeader(http.StatusOK)
		} else {
			w.WriteHeader(http.StatusServiceUnavailable)
		}
	})
	mux.HandleFunc("/invoke", s.invokeHandler)
	mux.HandleFunc("/invoke/", s.invokeHandler)
	return mux
}

// invokeHandler builds a Request from the incoming HTTP request, runs it
// through the hook chain and Handler, and writes the Response. On any error
// it responds 502 Bad Gateway.
func (s *Server) invokeHandler(w http.ResponseWriter, r *http.Request) {
	req := &Request{Path: r.URL.Path, Body: r.Body}
	resp, err := s.chain.Run(r.Context(), req, s.h)
	if err != nil {
		http.Error(w, fmt.Sprintf("bad gateway: %s", err), http.StatusBadGateway)
		return
	}
	status := http.StatusOK
	if resp != nil && resp.Status != 0 {
		status = resp.Status
	}
	// Set an explicit Content-Length so the response is fixed-length framed, not
	// chunked. WriteHeader commits the response before the body is written, so
	// without this Go falls back to chunked transfer-encoding; a large body (a
	// whole-repo scan result is hundreds of KiB) chunked over the vsock transport
	// surfaced as "malformed chunked encoding" resets on the daemon side. The body
	// is a known-size []byte, so a Content-Length is always available and correct.
	if resp != nil {
		w.Header().Set("Content-Length", strconv.Itoa(len(resp.Body)))
	}
	w.WriteHeader(status)
	if resp != nil {
		_, _ = w.Write(resp.Body)
	}
}

// Serve accepts and handles HTTP connections on ln until ln is closed. It
// returns the error from the underlying http.Server.Serve call (typically
// http.ErrServerClosed after Close).
func (s *Server) Serve(ln net.Listener) error {
	return s.srv.Serve(ln)
}

// Close immediately stops the server and closes all active connections.
func (s *Server) Close() error {
	return s.srv.Close()
}
