package main

import (
	"crypto/tls"
	"net/http"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/runbookadmission"
	core "k8s.io/client-go/kubernetes/typed/core/v1"
	"k8s.io/client-go/rest"
)

func runDeadline() error {
	cfg, err := rest.InClusterConfig()
	if err != nil {
		return err
	}
	client, err := core.NewForConfig(cfg)
	if err != nil {
		return err
	}
	srv := http.Server{
		Addr:        ":8443",
		ReadTimeout: 5 * time.Second,
		Handler:     runbookadmission.Handler{Journals: client.ConfigMaps("grafana")},
		TLSConfig: &tls.Config{
			MinVersion: tls.VersionTLS12,
			GetCertificate: func(*tls.ClientHelloInfo) (*tls.Certificate, error) {
				cert, err := tls.LoadX509KeyPair("/tls/tls.crt", "/tls/tls.key")
				return &cert, err
			},
		},
	}
	return srv.ListenAndServeTLS("", "")
}
