package main

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"
)

type canaryRoundTripper func(*http.Request) (*http.Response, error)

func (f canaryRoundTripper) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestGitHubCanaryScopeAndDenial(t *testing.T) {
	for _, tc := range []struct {
		name                    string
		denied, allowed, github int
		tokenBody, repos        string
		ok                      bool
	}{
		{name: "success", ok: true},
		{name: "publisher allowed", denied: 200},
		{name: "publisher missing", denied: 404},
		{name: "publisher redirect", denied: 307},
		{name: "token denied", allowed: 403},
		{name: "token expired", tokenBody: `{"access_token":"secret","expires_at":"2020-01-01T00:00:00Z"}`},
		{name: "token malformed", tokenBody: "secret"},
		{name: "token oversized", tokenBody: strings.Repeat("x", (1<<20)+1)},
		{name: "github denied", github: 403},
		{name: "github redirect", github: 302},
		{name: "wrong repo", repos: `{"total_count":1,"repositories":[{"id":456}]}`},
		{name: "extra repo", repos: `{"total_count":2,"repositories":[{"id":123},{"id":456}]}`},
		{name: "more pages", repos: `{"total_count":101,"repositories":[{"id":123}]}`},
		{name: "invalid JSON", repos: "secret"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if tc.denied == 0 {
				tc.denied = 403
			}
			if tc.allowed == 0 {
				tc.allowed = 200
			}
			if tc.github == 0 {
				tc.github = 200
			}
			if tc.tokenBody == "" {
				tc.tokenBody = fmt.Sprintf(`{"access_token":"secret","expires_at":%q}`, time.Now().Add(time.Hour).Format(time.RFC3339))
			}
			if tc.repos == "" {
				tc.repos = `{"total_count":1,"repositories":[{"id":123}]}`
			}
			brokerCalls, githubCalls := 0, 0
			response := func(status int, body string) *http.Response {
				return &http.Response{StatusCode: status, Body: io.NopCloser(strings.NewReader(body)), Header: make(http.Header)}
			}
			brokerClient := &http.Client{Transport: canaryRoundTripper(func(r *http.Request) (*http.Response, error) {
				brokerCalls++
				if r.Header.Get("Authorization") != "" {
					t.Fatal("GitHub token sent to broker")
				}
				if r.Method != "GET" || r.URL.Host != "broker" {
					t.Fatal("unexpected broker request")
				}
				switch r.URL.Path {
				case "/github/grants/publisher/token":
					return response(tc.denied, "secret"), nil
				case "/github/grants/reader/token":
					return response(tc.allowed, tc.tokenBody), nil
				default:
					t.Fatal("unexpected grant")
					return nil, nil
				}
			})}
			githubClient := &http.Client{Transport: canaryRoundTripper(func(r *http.Request) (*http.Response, error) {
				githubCalls++
				if r.URL.String() != "https://api.github.com/installation/repositories?per_page=100" || r.Method != "GET" || r.Header.Get("Authorization") != "Bearer secret" {
					t.Fatal("unexpected GitHub request")
				}
				return response(tc.github, tc.repos), nil
			})}
			err := checkGitHubCanary(context.Background(), githubCanaryConfig{brokerURL: "https://broker", grant: "reader", deniedGrant: "publisher", repositoryID: 123}, brokerClient, githubClient)
			if (err == nil) != tc.ok {
				t.Fatalf("error=%v, success=%v", err, tc.ok)
			}
			if err != nil && strings.Contains(err.Error(), "secret") {
				t.Fatal("credential leaked in error")
			}
			if tc.denied != 403 && (brokerCalls != 1 || githubCalls != 0) {
				t.Fatal("continued after failed denial check")
			}
			if tc.ok && (brokerCalls != 2 || githubCalls != 1) {
				t.Fatal("missing canary operation")
			}
		})
	}
}

func TestConfiguredGitHubCanaryRejectsUnsafeInput(t *testing.T) {
	defaults := map[string]string{"CANARY_BROKER_URL": "https://broker:8443", "CANARY_BROKER_SPIFFE_ID": "spiffe://test.example/broker", "CANARY_GRANT": "reader", "CANARY_DENIED_GRANT": "publisher", "CANARY_REPOSITORY_ID": "123"}
	for key, value := range defaults {
		t.Setenv(key, value)
	}
	if _, err := configuredGitHubCanary(); err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct{ key, value string }{
		{"CANARY_BROKER_URL", "http://broker"},
		{"CANARY_BROKER_URL", "https://user:secret@broker"},
		{"CANARY_BROKER_URL", "https://broker/path"},
		{"CANARY_BROKER_URL", "https://broker?query=x"},
		{"CANARY_BROKER_SPIFFE_ID", "invalid"},
		{"CANARY_GRANT", "../publisher"},
		{"CANARY_DENIED_GRANT", "reader"},
		{"CANARY_REPOSITORY_ID", "0"},
	} {
		t.Run(tc.key+tc.value, func(t *testing.T) {
			t.Setenv(tc.key, tc.value)
			if _, err := configuredGitHubCanary(); err == nil {
				t.Fatal("accepted unsafe config")
			}
		})
	}
}
