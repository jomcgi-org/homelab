package main

import (
	"bytes"
	"context"
	"crypto/tls"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptrace"
	"net/url"
	"time"
)

const targetActorHeader = "ate-target-actor"

// Failure kinds of a router call. They say whether the body left the gate and
// whether the next child may take the request.
const (
	// sendUnsent: the body never left the gate (no connection, TLS failure).
	// The child is unspent.
	sendUnsent = "unsent"
	// sendNoReply: the body was written and no response header came back. The
	// child is gone or dead.
	sendNoReply = "no_reply"
	// sendStatus: the router or child answered with a status other than 200.
	sendStatus = "status"
	// sendRead: the response was cut off.
	sendRead = "read"
	// sendTooLarge: the response is over the limit.
	sendTooLarge = "too_large"
	// sendTimeout: the deadline passed.
	sendTimeout = "timeout"
)

// sendError describes a failed router call. It never holds the request body or
// the response body.
type sendError struct {
	Kind   string
	Status int
}

func (e *sendError) Error() string {
	if e.Status != 0 {
		return fmt.Sprintf("router call failed: %s (status %d)", e.Kind, e.Status)
	}
	return "router call failed: " + e.Kind
}

// retryable says whether the next child may take the request. The body went to
// a child that the gate created, and a spent or dead child holds nothing that
// the next one needs.
func (e *sendError) retryable() bool {
	switch e.Kind {
	case sendNoReply, sendRead:
		return true
	case sendStatus:
		switch e.Status {
		case http.StatusNotFound, http.StatusMisdirectedRequest, http.StatusBadGateway, http.StatusServiceUnavailable, http.StatusGatewayTimeout:
			return true
		}
	}
	return false
}

// sendResult carries the engine reply and the timings of the call.
type sendResult struct {
	Body    []byte
	Connect time.Duration // new connection, zero if one was reused
	Engine  time.Duration // request written to response read
}

// router sends one body to one child.
type router interface {
	// Send posts body to the child actor ("<atespace>/<name>") and returns the
	// response body of a 200 answer, at most the configured limit.
	Send(ctx context.Context, target string, body []byte) (sendResult, error)
}

// httpRouter reaches the child through the atenet-router HTTPS listener. The
// router picks the actor from ate-target-actor, resumes a stopped actor,
// checks the SPIFFE ID of this client, and sends the request to port 80 of the
// child. It never forwards a header from the scanner: the request is built
// here from fixed parts.
type httpRouter struct {
	url         string
	client      *http.Client
	maxResponse int64
}

func newHTTPRouter(rawURL string, tlsCfg *tls.Config, maxResponse int64) (*httpRouter, error) {
	u, err := url.Parse(rawURL)
	if err != nil || u.Scheme != "https" || u.Hostname() == "" || u.User != nil {
		return nil, errors.New("router url must be an https URL without credentials")
	}
	transport := &http.Transport{
		Proxy:                  nil, // a proxy would see the body
		DialContext:            (&net.Dialer{Timeout: 2 * time.Second}).DialContext,
		TLSClientConfig:        tlsCfg,
		TLSHandshakeTimeout:    2 * time.Second,
		MaxIdleConns:           16,
		MaxIdleConnsPerHost:    16,
		IdleConnTimeout:        30 * time.Second,
		MaxResponseHeaderBytes: 16 << 10,
		DisableCompression:     true,
	}
	return &httpRouter{
		url: u.String(),
		client: &http.Client{
			Transport: transport,
			// A redirect could send the body to another host.
			CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
		},
		maxResponse: maxResponse,
	}, nil
}

func (r *httpRouter) Send(ctx context.Context, target string, body []byte) (sendResult, error) {
	var res sendResult
	var connectStart, wrote time.Time
	wroteHeaders := false
	trace := &httptrace.ClientTrace{
		ConnectStart: func(string, string) { connectStart = time.Now() },
		TLSHandshakeDone: func(tls.ConnectionState, error) {
			if !connectStart.IsZero() {
				res.Connect = time.Since(connectStart)
			}
		},
		WroteHeaders: func() { wroteHeaders = true },
		WroteRequest: func(httptrace.WroteRequestInfo) { wrote = time.Now() },
	}
	req, err := http.NewRequestWithContext(httptrace.WithClientTrace(ctx, trace), http.MethodPost, r.url+"/scan", bytes.NewReader(body))
	if err != nil {
		return res, &sendError{Kind: sendUnsent}
	}
	req.Header.Set(targetActorHeader, target)
	req.Header.Set("Content-Type", "text/plain; charset=utf-8")
	req.Header.Set("User-Agent", "engine-gate")
	resp, err := r.client.Do(req)
	if err != nil {
		return res, classify(ctx, err, wroteHeaders)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return res, &sendError{Kind: sendStatus, Status: resp.StatusCode}
	}
	data, err := io.ReadAll(io.LimitReader(resp.Body, r.maxResponse+1))
	if !wrote.IsZero() {
		res.Engine = time.Since(wrote)
	}
	if err != nil {
		if ctx.Err() != nil {
			return res, &sendError{Kind: sendTimeout}
		}
		return res, &sendError{Kind: sendRead}
	}
	if int64(len(data)) > r.maxResponse {
		return res, &sendError{Kind: sendTooLarge}
	}
	res.Body = data
	return res, nil
}

// classify names a transport error without its text: the text can quote the
// address of the child.
func classify(ctx context.Context, err error, wroteHeaders bool) *sendError {
	switch {
	case ctx.Err() != nil:
		return &sendError{Kind: sendTimeout}
	case !wroteHeaders:
		return &sendError{Kind: sendUnsent}
	default:
		return &sendError{Kind: sendNoReply}
	}
}
