package main

import (
	"log/slog"
	"net/http"
	"os"
	"time"
)

func newQuotaReporter(rawURL string, logger *slog.Logger) *quotaReporter {
	return newQuotaReporterWithClient(rawURL, logger, brokerHTTPClient(nil, 5*time.Second))
}

func newTokenBroker(rawURL string) *tokenBroker {
	return newTokenBrokerWithClient(rawURL, brokerHTTPClient(nil, 10*time.Second))
}

func (e *secretEntry) headerValue() string {
	return e.headerValueFor(e.resolvedValue())
}

func (e *secretEntry) resolvedValue() string {
	if e.mu != nil {
		e.mu.RLock()
		defer e.mu.RUnlock()
	}
	return e.value
}

func loadSecrets(logger *slog.Logger) []secretEntry {
	return loadSecretsWithBroker(logger, os.Getenv("EGRESS_TOKEN_BROKER_URL"))
}

func loadSecretsWithBroker(logger *slog.Logger, brokerURL string) []secretEntry {
	return loadSecretsWithBrokerClient(logger, brokerURL, brokerHTTPClient(nil, 10*time.Second))
}

func injectRequest(req *http.Request, sec *secretEntry) bool {
	return injectRequestWith(req, sec, sec.credential())
}
