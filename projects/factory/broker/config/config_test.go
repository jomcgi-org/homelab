package config

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func write(t *testing.T, name, content string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), name)
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestLoadFileAndEnv(t *testing.T) {
	appID := write(t, "app-id", "12345\n")
	path := write(t, "config.yaml", `
namespace: factory
codex:
  refreshCheckInterval: 5m
github:
  appIdFile: `+appID+`
  privateKeyFile: /keys/private-key
  agentPermissions: {contents: read}
bindings:
  - name: homelab
    repository: example-org/homelab
    namespace: factory-homelab
`)
	env := map[string]string{"GITHUB_INSTALLATION_ID": "678", "BROKER_LISTEN_ADDR": ":9000"}
	cfg, err := Load(path, func(k string) string { return env[k] })
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Namespace != "factory" || cfg.ListenAddr != ":9000" || cfg.GitHub.AppID != "12345" || cfg.GitHub.InstallationID != "678" {
		t.Fatalf("config = %+v", cfg)
	}
	if cfg.Codex.RefreshCheckInterval.Duration != 5*time.Minute || cfg.Codex.RefreshBeforeExpiry.Duration != 24*time.Hour || cfg.Codex.Grant != "codex-primary" {
		t.Fatalf("codex = %+v", cfg.Codex)
	}
	if len(cfg.GitHub.AgentPermissions) != 1 || cfg.GitHub.AgentPermissions["contents"] != "read" {
		t.Fatalf("agent permissions = %v, want the file's map to replace the default", cfg.GitHub.AgentPermissions)
	}
	if cfg.GitHub.ConnectorPermissions["pull_requests"] != "write" {
		t.Fatalf("connector permissions = %v, want defaults", cfg.GitHub.ConnectorPermissions)
	}
	b := cfg.Bindings[0]
	if b.Owner() != "example-org" || b.Repo() != "homelab" {
		t.Fatalf("binding split = %q %q", b.Owner(), b.Repo())
	}
}

func TestLoadRejects(t *testing.T) {
	for name, content := range map[string]string{
		"unknown field":      "namespace: f\nbogus: 1\n",
		"no namespace":       "listenAddr: ':1'\n",
		"bad repository":     "namespace: f\nbindings: [{name: a, namespace: n, repository: nope}]\n",
		"dup namespace":      "namespace: f\nbindings: [{name: a, namespace: n, repository: o/a}, {name: b, namespace: n, repository: o/b}]\n",
		"slow mint":          "namespace: f\ngithub: {mintInterval: 2h}\n",
		"http issuer":        "namespace: f\nactorJwt: {issuer: 'http://idp'}\n",
		"no app id":          "namespace: f\ngithub: {privateKeyFile: /k}\n",
		"shared listen":      "namespace: f\nadminListenAddr: ':8080'\n",
		"bad effort":         "namespace: f\ncodex: {reasoningEffort: huge}\n",
		"review without key": "namespace: f\ntokenReview: {reviewerSubjects: [a]}\n",
		"review same app":    "namespace: f\ngithub: {privateKeyFile: /k, appId: '1', installationId: '2'}\ngithubReview: {privateKeyFile: /r, appId: '1', installationId: '3'}\n",
		"review writes":      "namespace: f\ngithubReview: {privateKeyFile: /r, appId: '9', installationId: '3', agentPermissions: {contents: write}}\n",
		"review approves":    "namespace: f\ngithubReview: {privateKeyFile: /r, appId: '9', installationId: '3', connectorPermissions: {pull_requests: write}}\n",
		"review pushes":      "namespace: f\ngithubReview: {privateKeyFile: /r, appId: '9', installationId: '3', connectorPermissions: {contents: write, checks: write}}\n",
		"review namespace":   "namespace: f\nbindings: [{name: a, namespace: n, repository: o/a}]\ngithubReview: {privateKeyFile: /r, appId: '9', installationId: '3'}\nreviewBindings: [{name: a, namespace: n, repository: o/a}]\n",
		"review subject":     "namespace: f\ngithubReview: {privateKeyFile: /r, appId: '9', installationId: '3'}\ntokenReview: {connectorSubjects: [s], reviewerSubjects: [s]}\n",
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := Load(write(t, "c.yaml", content), func(string) string { return "" }); err == nil {
				t.Fatal("Load accepted invalid config")
			}
		})
	}
	review := "namespace: f\nbindings: [{name: a, namespace: n, repository: o/a}]\n" +
		"githubReview: {privateKeyFile: /r, appId: '9', installationId: '3'}\n" +
		"reviewBindings: [{name: a, namespace: nr, repository: o/a}]\ntokenReview: {reviewerSubjects: [s]}\n"
	if c, err := Load(write(t, "r.yaml", review), func(string) string { return "" }); err != nil || !c.GitHubReviewEnabled() ||
		c.GitHubReview.AgentPermissions["contents"] != "read" || c.GitHubReview.ConnectorPermissions["checks"] != "write" {
		t.Fatalf("reviewer config = %+v, %v", c.GitHubReview, err)
	}
	if c, err := Load(write(t, "e.yaml", "namespace: f\ncodex: {reasoningEffort: xhigh}\n"), func(string) string { return "" }); err != nil || c.Codex.ReasoningEffort != "xhigh" {
		t.Fatalf("xhigh effort = %q, %v", c.Codex.ReasoningEffort, err)
	}
	_, err := Load("", func(k string) string { return map[string]string{"POD_NAMESPACE": "factory"}[k] })
	if err != nil {
		t.Fatalf("defaults with POD_NAMESPACE: %v", err)
	}
	if _, err := Load("", func(string) string { return "" }); err == nil || !strings.Contains(err.Error(), "namespace") {
		t.Fatalf("missing namespace error = %v", err)
	}
}
