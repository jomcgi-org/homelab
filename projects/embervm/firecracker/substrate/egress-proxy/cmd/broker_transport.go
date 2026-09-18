package main

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"time"

	"github.com/spiffe/go-spiffe/v2/bundle/x509bundle"
	"github.com/spiffe/go-spiffe/v2/spiffeid"
	"github.com/spiffe/go-spiffe/v2/spiffetls/tlsconfig"
	"github.com/spiffe/go-spiffe/v2/svid/x509svid"
	"github.com/spiffe/go-spiffe/v2/workloadapi"
)

// A single rotating source serves token fetches, refreshes and quota requests.
// It belongs to the sidecar, never to a guest or a claimed factory role.
type brokerX509Source interface {
	x509svid.Source
	x509bundle.Source
	Close() error
}

type brokerSourceFactory func(context.Context) (brokerX509Source, error)

func newBrokerX509Source(ctx context.Context) (brokerX509Source, error) {
	return workloadapi.NewX509Source(ctx)
}

func brokerTransport(rawURL, rawID string, create brokerSourceFactory) (http.RoundTripper, func(), error) {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	// Broker traffic is cluster-local and must not inherit HTTP(S)_PROXY.
	transport.Proxy = nil
	cleanup := func() { transport.CloseIdleConnections() }
	if rawURL == "" && rawID == "" {
		return transport, cleanup, nil
	}
	endpoint, err := url.Parse(normalizeBrokerURL(rawURL))
	if err != nil || endpoint.Host == "" || endpoint.User != nil || endpoint.RawQuery != "" || endpoint.Fragment != "" || (endpoint.Path != "" && endpoint.Path != "/") {
		return nil, nil, errors.New("token broker URL must be an HTTP(S) origin without credentials, path, query or fragment")
	}
	if rawID == "" {
		if endpoint.Scheme != "http" {
			return nil, nil, errors.New("HTTPS token broker requires EGRESS_TOKEN_BROKER_SPIFFE_ID")
		}
		return &brokerOriginTransport{origin: endpoint, transport: transport}, cleanup, nil
	}
	if endpoint.Scheme != "https" {
		return nil, nil, errors.New("SPIFFE token broker requires an explicit HTTPS URL")
	}
	serverID, err := spiffeid.FromString(rawID)
	if err != nil {
		return nil, nil, errors.New("invalid EGRESS_TOKEN_BROKER_SPIFFE_ID")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	source, err := create(ctx)
	if err != nil {
		return nil, nil, fmt.Errorf("token broker SPIFFE source unavailable: %w", err)
	}
	if _, err := source.GetX509SVID(); err != nil {
		_ = source.Close()
		return nil, nil, fmt.Errorf("token broker SPIFFE source has no SVID: %w", err)
	}
	transport.TLSClientConfig = tlsconfig.MTLSClientConfig(source, source, tlsconfig.AuthorizeID(serverID))
	return &brokerOriginTransport{origin: endpoint, transport: transport}, func() {
		transport.CloseIdleConnections()
		_ = source.Close()
	}, nil
}

// Reject redirects and accidental reuse against another origin, including a
// downgrade to HTTP. This transport is only for the configured token broker.
type brokerOriginTransport struct {
	origin    *url.URL
	transport *http.Transport
}

func (t *brokerOriginTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	if r.URL.Scheme != t.origin.Scheme || r.URL.Host != t.origin.Host || r.URL.User != nil {
		return nil, errors.New("token broker request changed origin")
	}
	return t.transport.RoundTrip(r)
}

func brokerHTTPClient(transport http.RoundTripper, timeout time.Duration) *http.Client {
	return &http.Client{
		Transport:     transport,
		Timeout:       timeout,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}
}
