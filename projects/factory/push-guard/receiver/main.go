// Command receiver sends authenticated factory pushes through the private hook.
package main

import (
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/hex"
	"errors"
	"flag"
	"io"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"time"
)

const (
	upstream       = "https://github.com/jomcgi-org/homelab.git"
	route          = "/jomcgi-org/homelab.git"
	gatewayID      = "spiffe://cluster.local/ns/ate-system/sa/atenet-egress"
	receiverBundle = "/run/servicedns.podcert.ate.dev/credential-bundle.pem"
	gatewayBundle  = "/run/podidentity.podcert.ate.dev/trust-bundle.pem"
)

func main() {
	repo := flag.String("repository", "/var/lib/factory-push-guard/homelab.git", "seeded bare repository")
	indexDigest := flag.String("index-sha256", "", "SHA-256 of the operator-provisioned index")
	listen := flag.String("listen", ":8443", "HTTPS listen address")
	flag.Parse()

	if _, err := os.Stat(filepath.Join(*repo, "HEAD")); err != nil {
		log.Fatal("staging repository unavailable")
	}
	index, err := verifiedIndex(filepath.Dir(*repo), *indexDigest)
	if err != nil {
		log.Fatal(err)
	}
	if _, err := loadTLS(receiverBundle, gatewayBundle); err != nil {
		log.Fatal(err)
	}
	server := &http.Server{
		Addr:              *listen,
		Handler:           newReceiver(*repo, index, upstream),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       40 * time.Second,
		WriteTimeout:      170 * time.Second,
		IdleTimeout:       30 * time.Second,
		MaxHeaderBytes:    16 << 10,
		TLSConfig: &tls.Config{
			MinVersion: tls.VersionTLS13,
			// Kubelet rotates the projected certificates and trust bundle.
			GetConfigForClient: func(*tls.ClientHelloInfo) (*tls.Config, error) {
				return loadTLS(receiverBundle, gatewayBundle)
			},
		},
	}
	log.Fatal(server.ListenAndServeTLS("", ""))
}

func verifiedIndex(volume, wanted string) (string, error) {
	if raw, err := hex.DecodeString(wanted); err != nil || len(raw) != sha256.Size {
		return "", errors.New("a pinned private index is required")
	}
	path := filepath.Join(volume, "indices", wanted+".json")
	file, err := os.Open(path)
	if err != nil {
		return "", errors.New("private index unavailable")
	}
	defer file.Close()
	hash := sha256.New()
	n, err := io.Copy(hash, io.LimitReader(file, (32<<20)+1))
	if err != nil || n == 0 || n > 32<<20 || hex.EncodeToString(hash.Sum(nil)) != wanted {
		return "", errors.New("private index did not match the pin or size limit")
	}
	return path, nil
}

func loadTLS(certPath, caPath string) (*tls.Config, error) {
	cert, err := tls.LoadX509KeyPair(certPath, certPath)
	if err != nil {
		return nil, errors.New("receiver certificate unavailable")
	}
	raw, err := os.ReadFile(caPath)
	pool := x509.NewCertPool()
	if err != nil || !pool.AppendCertsFromPEM(raw) {
		return nil, errors.New("gateway client trust bundle unavailable")
	}
	return &tls.Config{
		MinVersion:       tls.VersionTLS13,
		Certificates:     []tls.Certificate{cert},
		ClientAuth:       tls.RequireAndVerifyClientCert,
		ClientCAs:        pool,
		VerifyConnection: authorizeGateway,
	}, nil
}

func authorizeGateway(state tls.ConnectionState) error {
	for _, chain := range state.VerifiedChains {
		if len(chain) == 0 {
			continue
		}
		for _, uri := range chain[0].URIs {
			if uri.String() == gatewayID {
				return nil
			}
		}
	}
	return errors.New("client lacks the egress gateway identity")
}
