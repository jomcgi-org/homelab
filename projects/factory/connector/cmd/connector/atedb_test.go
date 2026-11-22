package main

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"database/sql"
	"encoding/pem"
	"io"
	"math/big"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

type testCert struct {
	der []byte
	key *ecdsa.PrivateKey
}

func issue(t *testing.T, tmpl *x509.Certificate, parent *testCert) *testCert {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	tmpl.SerialNumber = big.NewInt(time.Now().UnixNano())
	tmpl.NotBefore, tmpl.NotAfter = time.Now().Add(-time.Hour), time.Now().Add(time.Hour)
	parentTmpl, signer := tmpl, key
	if parent != nil {
		parentTmpl, err = x509.ParseCertificate(parent.der)
		if err != nil {
			t.Fatal(err)
		}
		signer = parent.key
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, parentTmpl, &key.PublicKey, signer)
	if err != nil {
		t.Fatal(err)
	}
	return &testCert{der: der, key: key}
}

func (c *testCert) certPEM() []byte {
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: c.der})
}

func (c *testCert) keyPEM(t *testing.T) []byte {
	b, err := x509.MarshalECPrivateKey(c.key)
	if err != nil {
		t.Fatal(err)
	}
	return pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: b})
}

// fakePostgres answers each connection's SSLRequest, completes a TLS
// handshake that requires a client certificate from ca, and reports the
// client's CN. A connection that ends before a client certificate reports
// nothing.
func fakePostgres(t *testing.T, ca, server *testCert) (string, <-chan string) {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { ln.Close() })
	pool := x509.NewCertPool()
	caCert, _ := x509.ParseCertificate(ca.der)
	pool.AddCert(caCert)
	cn := make(chan string, 4)
	serve := func(conn net.Conn) {
		defer conn.Close()
		if _, err := io.ReadFull(conn, make([]byte, 8)); err != nil {
			return
		}
		if _, err := conn.Write([]byte{'S'}); err != nil {
			return
		}
		tc := tls.Server(conn, &tls.Config{
			Certificates: []tls.Certificate{{Certificate: [][]byte{server.der}, PrivateKey: server.key}},
			ClientAuth:   tls.RequireAndVerifyClientCert,
			ClientCAs:    pool,
		})
		if tc.Handshake() == nil {
			cn <- tc.ConnectionState().PeerCertificates[0].Subject.CommonName
		}
	}
	go func() {
		for {
			conn, err := ln.Accept()
			if err != nil {
				return
			}
			go serve(conn)
		}
	}()
	return ln.Addr().(*net.TCPAddr).AddrPort().String(), cn
}

// A projected pod certificate under a pod fsGroup is 0640 and owned by the
// pod user. lib/pq's sslkey file path refuses it; the inline connector
// presents it.
func TestPEMConnectorPresentsGroupReadableKey(t *testing.T) {
	ca := issue(t, &x509.Certificate{Subject: pkix.Name{CommonName: "ca"}, IsCA: true, BasicConstraintsValid: true,
		KeyUsage: x509.KeyUsageCertSign}, nil)
	server := issue(t, &x509.Certificate{Subject: pkix.Name{CommonName: "postgres"}, IPAddresses: []net.IP{net.ParseIP("127.0.0.1")},
		ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}}, ca)
	client := issue(t, &x509.Certificate{Subject: pkix.Name{CommonName: "factory_reaper"},
		ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth}}, ca)

	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "ca.pem"), ca.certPEM(), 0o640); err != nil {
		t.Fatal(err)
	}
	// The kubelet credential bundle: the key, then the certificate chain.
	bundle := append(client.keyPEM(t), client.certPEM()...)
	if err := os.WriteFile(filepath.Join(dir, "client.pem"), bundle, 0o640); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(filepath.Join(dir, "client.pem"), 0o640); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	addr, cn := fakePostgres(t, ca, server)
	if os.Getuid() != 0 {
		// The file path DSN fails this way on the cluster.
		file := filepath.Join(dir, "client.pem")
		old, err := sql.Open("postgres", "postgresql://factory_reaper@"+addr+"/atepg?sslmode=verify-full&sslrootcert="+
			filepath.Join(dir, "ca.pem")+"&sslcert="+file+"&sslkey="+file)
		if err != nil {
			t.Fatal(err)
		}
		defer old.Close()
		if err := old.PingContext(ctx); err == nil || !strings.Contains(err.Error(), "private key has world access") {
			t.Fatalf("file path DSN error = %v, want the key permission refusal", err)
		}
	}

	db := sql.OpenDB(pemConnector{dsn: "postgresql://factory_reaper@" + addr + "/atepg?sslmode=verify-full", dir: dir})
	defer db.Close()
	_ = db.PingContext(ctx) // the fake server closes after the handshake
	select {
	case got := <-cn:
		if got != "factory_reaper" {
			t.Fatalf("server saw client %q, want factory_reaper", got)
		}
	case <-ctx.Done():
		t.Fatal("no TLS handshake reached the server")
	}
}

func TestPEMConnectorReportsMissingBundle(t *testing.T) {
	db := sql.OpenDB(pemConnector{dsn: actorDB, dir: t.TempDir()})
	defer db.Close()
	if err := db.PingContext(context.Background()); err == nil || !strings.Contains(err.Error(), "ca.pem") {
		t.Fatalf("error = %v, want the missing CA file", err)
	}
}
