package main

import (
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"os"
	"sync"
	"time"
)

// fileCache re-reads a projected file when its modification time changes.
// kubelet rotates the pod certificate and the trust bundle in place, so a
// long-lived process must follow them without a restart.
type fileCache[T any] struct {
	path  string
	parse func([]byte) (T, error)

	mu    sync.Mutex
	mtime time.Time
	size  int64
	value T
	ok    bool
}

func (c *fileCache[T]) get() (T, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	info, err := os.Stat(c.path)
	if err != nil {
		var zero T
		return zero, err
	}
	if c.ok && info.ModTime().Equal(c.mtime) && info.Size() == c.size {
		return c.value, nil
	}
	data, err := os.ReadFile(c.path)
	if err != nil {
		var zero T
		return zero, err
	}
	v, err := c.parse(data)
	if err != nil {
		var zero T
		return zero, fmt.Errorf("%s: %w", c.path, err)
	}
	c.value, c.mtime, c.size, c.ok = v, info.ModTime(), info.Size(), true
	return v, nil
}

func parsePool(data []byte) (*x509.CertPool, error) {
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(data) {
		return nil, errors.New("no certificate in the trust bundle")
	}
	return pool, nil
}

func parseKeyPair(data []byte) (*tls.Certificate, error) {
	cert, err := tls.X509KeyPair(data, data)
	if err != nil {
		return nil, err
	}
	return &cert, nil
}

// tlsSource builds TLS configurations that follow the projected files.
type tlsSource struct {
	ca   *fileCache[*x509.CertPool]
	cert *fileCache[*tls.Certificate]
	now  func() time.Time
}

func newTLSSource(caFile, certFile string) *tlsSource {
	return &tlsSource{
		ca:   &fileCache[*x509.CertPool]{path: caFile, parse: parsePool},
		cert: &fileCache[*tls.Certificate]{path: certFile, parse: parseKeyPair},
		now:  time.Now,
	}
}

// config returns a client configuration that checks the server certificate
// against the current trust bundle and the name serverName. It presents the
// pod certificate when withClientCert is set. The standard verifier would pin
// the pool at start, so verification is done by hand on each handshake.
func (s *tlsSource) config(serverName string, withClientCert bool) *tls.Config {
	cfg := &tls.Config{
		MinVersion: tls.VersionTLS12,
		ServerName: serverName,
		// Verification happens in VerifyConnection with a fresh pool.
		InsecureSkipVerify: true, //nolint:gosec // see VerifyConnection below
		VerifyConnection: func(cs tls.ConnectionState) error {
			if len(cs.PeerCertificates) == 0 {
				return errors.New("server sent no certificate")
			}
			pool, err := s.ca.get()
			if err != nil {
				return err
			}
			inter := x509.NewCertPool()
			for _, c := range cs.PeerCertificates[1:] {
				inter.AddCert(c)
			}
			_, err = cs.PeerCertificates[0].Verify(x509.VerifyOptions{
				DNSName:       serverName,
				Roots:         pool,
				Intermediates: inter,
				CurrentTime:   s.now(),
				KeyUsages:     []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
			})
			return err
		},
	}
	if withClientCert {
		cfg.GetClientCertificate = func(*tls.CertificateRequestInfo) (*tls.Certificate, error) {
			return s.cert.get()
		}
	}
	return cfg
}
