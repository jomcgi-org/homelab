// Package config loads the factory credential broker's configuration from a
// YAML (or JSON) file, with environment overrides for deployment values.
package config

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"os"
	"strings"
	"time"

	"sigs.k8s.io/yaml"
)

// Duration accepts Go duration strings such as "24h" or "30m".
type Duration struct{ time.Duration }

func (d *Duration) UnmarshalJSON(b []byte) error {
	var raw string
	if err := json.Unmarshal(b, &raw); err != nil {
		return fmt.Errorf("duration must be a string such as \"30m\": %w", err)
	}
	v, err := time.ParseDuration(raw)
	if err != nil {
		return err
	}
	d.Duration = v
	return nil
}

func (d Duration) MarshalJSON() ([]byte, error) { return json.Marshal(d.String()) }

type Config struct {
	// Namespace holds the grant Secret and the quota ConfigMap.
	Namespace  string `json:"namespace"`
	ListenAddr string `json:"listenAddr"`
	// AdminListenAddr, when set, serves the admin, connector-token and
	// metrics endpoints on their own listener, so network policy can admit
	// actors to the relay without reaching them. Empty serves everything on
	// ListenAddr.
	AdminListenAddr string `json:"adminListenAddr"`

	Codex       Codex       `json:"codex"`
	ActorJWT    ActorJWT    `json:"actorJwt"`
	TokenReview TokenReview `json:"tokenReview"`
	GitHub      GitHub      `json:"github"`
	Bindings    []Binding   `json:"bindings"`
	// GitHubReview is the reviewer App (jomcgi-factory-review). It mints a
	// read-only git token for each ReviewBindings namespace and serves
	// reviewer tokens (checks and merge queue) to ReviewerSubjects. It is
	// separate from GitHub so the identity that writes code can never post
	// the review or merge ("Merge authority").
	GitHubReview   GitHub    `json:"githubReview"`
	ReviewBindings []Binding `json:"reviewBindings"`
}

type Codex struct {
	// Grant is the one grant the relay serves.
	Grant        string `json:"grant"`
	SecretPrefix string `json:"secretPrefix"`
	// UpstreamURL is the Codex backend prefix; the relay appends
	// "/responses" and the other allowed paths.
	UpstreamURL string `json:"upstreamUrl"`
	// AuthIssuer is the OpenAI OAuth issuer for device login and refresh.
	AuthIssuer           string   `json:"authIssuer"`
	RefreshBeforeExpiry  Duration `json:"refreshBeforeExpiry"`
	MaxGrantAge          Duration `json:"maxGrantAge"`
	RefreshCheckInterval Duration `json:"refreshCheckInterval"`
	// QuotaConfigMap persists quota observations when set; otherwise they
	// live in memory.
	QuotaConfigMap string `json:"quotaConfigMap"`
	// ReasoningEffort, when set, is the reasoning effort of every relayed
	// /responses call. kagent's Codex harness sends none, so Codex uses the
	// model's default (low for the gpt-6 models). Empty leaves requests as
	// sent.
	ReasoningEffort string `json:"reasoningEffort"`
	// MaxRequestBytes bounds a buffered relay request body.
	MaxRequestBytes int64 `json:"maxRequestBytes"`
	// MaxConcurrentRequests caps relay requests in flight.
	MaxConcurrentRequests int `json:"maxConcurrentRequests"`
	// StreamIdleTimeout ends an upstream response silent for this long.
	StreamIdleTimeout Duration `json:"streamIdleTimeout"`
	// RequestReadTimeout bounds reading a whole request, body included.
	RequestReadTimeout Duration `json:"requestReadTimeout"`
}

type ActorJWT struct {
	Issuer   string `json:"issuer"`
	Audience string `json:"audience"`
	// CAFile verifies the issuer's TLS certificate (the Substrate signer's
	// ClusterTrustBundle). Empty uses the system roots.
	CAFile string `json:"caFile"`
	// KeyRefreshInterval refreshes the key set in the background.
	KeyRefreshInterval Duration `json:"keyRefreshInterval"`
	// MinKeyRefetch rate-limits refetches triggered by an unknown kid.
	MinKeyRefetch Duration `json:"minKeyRefetch"`
	// AllowedAtespaces restricts which actors may use the relay; empty
	// allows any atespace the issuer signs for.
	AllowedAtespaces []string `json:"allowedAtespaces"`
}

type TokenReview struct {
	// Audience is the projected ServiceAccount token audience callers present.
	Audience string `json:"audience"`
	// AdminSubjects may run device login and forced refresh.
	AdminSubjects []string `json:"adminSubjects"`
	// ReviewerSubjects may fetch reviewer GitHub tokens.
	ReviewerSubjects []string `json:"reviewerSubjects"`
	// ConnectorSubjects may fetch connector GitHub tokens.
	ConnectorSubjects []string `json:"connectorSubjects"`
}

type GitHub struct {
	APIURL             string `json:"apiUrl"`
	AppID              string `json:"appId"`
	AppIDFile          string `json:"appIdFile"`
	InstallationID     string `json:"installationId"`
	InstallationIDFile string `json:"installationIdFile"`
	PrivateKeyFile     string `json:"privateKeyFile"`
	// MintInterval is how often agent tokens are reminted.
	MintInterval Duration `json:"mintInterval"`
	// AgentSecretName is written in each binding's namespace.
	AgentSecretName      string            `json:"agentSecretName"`
	AgentPermissions     map[string]string `json:"agentPermissions"`
	ConnectorPermissions map[string]string `json:"connectorPermissions"`
}

// Binding is one repository binding: the access boundary for an agent.
type Binding struct {
	Name       string `json:"name"`
	Repository string `json:"repository"` // owner/name
	Namespace  string `json:"namespace"`
}

// Owner and Repo split Repository.
func (b Binding) Owner() string { o, _, _ := strings.Cut(b.Repository, "/"); return o }
func (b Binding) Repo() string  { _, r, _ := strings.Cut(b.Repository, "/"); return r }

// Defaults returns the configuration before the file and environment apply.
func Defaults() Config {
	return Config{
		ListenAddr: ":8080",
		Codex: Codex{
			Grant:                "codex-primary",
			SecretPrefix:         "factory-broker-grant-",
			UpstreamURL:          "https://chatgpt.com/backend-api/codex",
			AuthIssuer:           "https://auth.openai.com",
			RefreshBeforeExpiry:  Duration{24 * time.Hour},
			MaxGrantAge:          Duration{7 * 24 * time.Hour},
			RefreshCheckInterval: Duration{10 * time.Minute},
			// Codex sends the whole conversation each turn; images are
			// inlined as base64.
			MaxRequestBytes:       32 << 20,
			MaxConcurrentRequests: 64,
			StreamIdleTimeout:     Duration{5 * time.Minute},
			RequestReadTimeout:    Duration{2 * time.Minute},
		},
		ActorJWT: ActorJWT{
			Issuer:             "https://idp.ate-system.svc",
			Audience:           "factory-broker",
			KeyRefreshInterval: Duration{5 * time.Minute},
			MinKeyRefetch:      Duration{30 * time.Second},
		},
		TokenReview: TokenReview{Audience: "factory-broker"},
		GitHub: GitHub{
			APIURL:           "https://api.github.com",
			MintInterval:     Duration{30 * time.Minute},
			AgentSecretName:  "github-git",
			AgentPermissions: map[string]string{"contents": "write"},
			// merge_queues is the GitHub App permission for merge queues
			// [I]: confirm the name and that enqueuePullRequest needs it.
			ConnectorPermissions: map[string]string{
				"contents": "write", "pull_requests": "write",
				"checks": "read", "statuses": "read", "merge_queues": "write",
			},
		},
		GitHubReview: GitHub{
			APIURL:           "https://api.github.com",
			MintInterval:     Duration{30 * time.Minute},
			AgentSecretName:  "github-git",
			AgentPermissions: map[string]string{"contents": "read"},
			// The reviewer service's token: post the check, read the PR,
			// and enqueue. Never pull_requests write, so it cannot approve.
			ConnectorPermissions: map[string]string{
				"checks": "write", "pull_requests": "read", "contents": "read",
				"statuses": "read", "merge_queues": "write",
			},
		},
	}
}

// Load reads path (when set) over Defaults, then applies the environment.
func Load(path string, getenv func(string) string) (Config, error) {
	cfg := Defaults()
	if path != "" {
		data, err := os.ReadFile(path)
		if err != nil {
			return Config{}, fmt.Errorf("read config: %w", err)
		}
		// Permission maps replace the defaults instead of merging into them.
		cfg.GitHub.AgentPermissions, cfg.GitHub.ConnectorPermissions = nil, nil
		cfg.GitHubReview.AgentPermissions, cfg.GitHubReview.ConnectorPermissions = nil, nil
		if err := yaml.UnmarshalStrict(data, &cfg); err != nil {
			return Config{}, fmt.Errorf("parse config: %w", err)
		}
		defaults := Defaults().GitHub
		if cfg.GitHub.AgentPermissions == nil {
			cfg.GitHub.AgentPermissions = defaults.AgentPermissions
		}
		if cfg.GitHub.ConnectorPermissions == nil {
			cfg.GitHub.ConnectorPermissions = defaults.ConnectorPermissions
		}
		review := Defaults().GitHubReview
		if cfg.GitHubReview.AgentPermissions == nil {
			cfg.GitHubReview.AgentPermissions = review.AgentPermissions
		}
		if cfg.GitHubReview.ConnectorPermissions == nil {
			cfg.GitHubReview.ConnectorPermissions = review.ConnectorPermissions
		}
	}
	for key, target := range map[string]*string{
		"BROKER_NAMESPACE":          &cfg.Namespace,
		"BROKER_LISTEN_ADDR":        &cfg.ListenAddr,
		"BROKER_ADMIN_LISTEN_ADDR":  &cfg.AdminListenAddr,
		"BROKER_ACTOR_JWT_ISSUER":   &cfg.ActorJWT.Issuer,
		"BROKER_ACTOR_JWT_AUDIENCE": &cfg.ActorJWT.Audience,
		"BROKER_ACTOR_JWT_CA_FILE":  &cfg.ActorJWT.CAFile,
		"BROKER_CODEX_UPSTREAM_URL": &cfg.Codex.UpstreamURL,
		"GITHUB_APP_ID":             &cfg.GitHub.AppID,
		"GITHUB_INSTALLATION_ID":    &cfg.GitHub.InstallationID,
		"GITHUB_PRIVATE_KEY_FILE":   &cfg.GitHub.PrivateKeyFile,
	} {
		if v := strings.TrimSpace(getenv(key)); v != "" {
			*target = v
		}
	}
	if cfg.Namespace == "" {
		cfg.Namespace = strings.TrimSpace(getenv("POD_NAMESPACE"))
	}
	if err := cfg.resolveFiles(); err != nil {
		return Config{}, err
	}
	return cfg, cfg.Validate()
}

func (c *Config) resolveFiles() error {
	for _, f := range []struct {
		value *string
		file  string
	}{
		{&c.GitHub.AppID, c.GitHub.AppIDFile},
		{&c.GitHub.InstallationID, c.GitHub.InstallationIDFile},
		{&c.GitHubReview.AppID, c.GitHubReview.AppIDFile},
		{&c.GitHubReview.InstallationID, c.GitHubReview.InstallationIDFile},
	} {
		if *f.value != "" || f.file == "" {
			continue
		}
		data, err := os.ReadFile(f.file)
		if err != nil {
			return fmt.Errorf("read %s: %w", f.file, err)
		}
		*f.value = strings.TrimSpace(string(data))
	}
	return nil
}

// GitHubEnabled reports whether App credentials are configured.
func (c Config) GitHubEnabled() bool { return c.GitHub.PrivateKeyFile != "" }

// GitHubReviewEnabled reports whether the reviewer App is configured.
func (c Config) GitHubReviewEnabled() bool { return c.GitHubReview.PrivateKeyFile != "" }

func (c Config) Validate() error {
	var errs []error
	if c.Namespace == "" {
		errs = append(errs, errors.New("namespace is required (or POD_NAMESPACE)"))
	}
	if c.AdminListenAddr != "" && c.AdminListenAddr == c.ListenAddr {
		errs = append(errs, errors.New("adminListenAddr must differ from listenAddr"))
	}
	if c.Codex.Grant == "" || c.Codex.SecretPrefix == "" {
		errs = append(errs, errors.New("codex.grant and codex.secretPrefix are required"))
	}
	for _, u := range []string{c.Codex.UpstreamURL, c.Codex.AuthIssuer, c.ActorJWT.Issuer} {
		if parsed, err := url.Parse(u); err != nil || parsed.Scheme != "https" || parsed.Host == "" {
			errs = append(errs, fmt.Errorf("%q must be an https URL", u))
		}
	}
	if c.ActorJWT.Audience == "" || c.TokenReview.Audience == "" {
		errs = append(errs, errors.New("actorJwt.audience and tokenReview.audience are required"))
	}
	for name, d := range map[string]Duration{
		"codex.refreshBeforeExpiry": c.Codex.RefreshBeforeExpiry, "codex.maxGrantAge": c.Codex.MaxGrantAge,
		"codex.refreshCheckInterval": c.Codex.RefreshCheckInterval, "actorJwt.keyRefreshInterval": c.ActorJWT.KeyRefreshInterval,
		"actorJwt.minKeyRefetch": c.ActorJWT.MinKeyRefetch, "github.mintInterval": c.GitHub.MintInterval,
		"codex.streamIdleTimeout": c.Codex.StreamIdleTimeout, "codex.requestReadTimeout": c.Codex.RequestReadTimeout,
	} {
		if d.Duration <= 0 {
			errs = append(errs, fmt.Errorf("%s must be positive", name))
		}
	}
	switch c.Codex.ReasoningEffort {
	case "", "minimal", "low", "medium", "high", "xhigh", "max", "ultra":
	default:
		errs = append(errs, fmt.Errorf("codex.reasoningEffort %q is not a reasoning effort", c.Codex.ReasoningEffort))
	}
	if c.Codex.MaxRequestBytes <= 0 || c.Codex.MaxConcurrentRequests <= 0 {
		errs = append(errs, errors.New("codex.maxRequestBytes and codex.maxConcurrentRequests must be positive"))
	}
	// Installation tokens live one hour; reminting less often than that
	// leaves agents with expired credentials.
	if c.GitHub.MintInterval.Duration > 50*time.Minute {
		errs = append(errs, errors.New("github.mintInterval must be at most 50m"))
	}
	if c.GitHubEnabled() && (c.GitHub.AppID == "" || c.GitHub.InstallationID == "") {
		errs = append(errs, errors.New("github appId and installationId are required with a private key"))
	}
	errs = append(errs, c.validateReview()...)
	seen, namespaces := map[string]bool{}, map[string]bool{}
	for i, b := range c.Bindings {
		owner, repo, ok := strings.Cut(b.Repository, "/")
		if b.Name == "" || b.Namespace == "" || !ok || owner == "" || repo == "" || strings.Contains(repo, "/") {
			errs = append(errs, fmt.Errorf("bindings[%d] needs name, namespace and repository owner/name", i))
		}
		if seen[b.Name] {
			errs = append(errs, fmt.Errorf("bindings[%d]: duplicate name %q", i, b.Name))
		}
		seen[b.Name] = true
		// Each binding's git Secret has a fixed name, so namespaces are
		// one per binding.
		if namespaces[b.Namespace] {
			errs = append(errs, fmt.Errorf("bindings[%d]: namespace %q is already bound", i, b.Namespace))
		}
		namespaces[b.Namespace] = true
	}
	return errors.Join(errs...)
}

// validateReview keeps the reviewer apart from the author: its own App, its
// own agent namespaces (both App's agent Secrets are named github-git) and
// its own token subjects.
func (c Config) validateReview() []error {
	var errs []error
	if !c.GitHubReviewEnabled() {
		if len(c.ReviewBindings) > 0 || len(c.TokenReview.ReviewerSubjects) > 0 {
			errs = append(errs, errors.New("reviewBindings and reviewerSubjects need githubReview.privateKeyFile"))
		}
		return errs
	}
	r := c.GitHubReview
	if r.AppID == "" || r.InstallationID == "" {
		errs = append(errs, errors.New("githubReview appId and installationId are required with a private key"))
	}
	if r.AppID != "" && r.AppID == c.GitHub.AppID {
		errs = append(errs, errors.New("githubReview must be a different App from github"))
	}
	if r.MintInterval.Duration <= 0 || r.MintInterval.Duration > 50*time.Minute {
		errs = append(errs, errors.New("githubReview.mintInterval must be positive and at most 50m"))
	}
	if r.AgentSecretName == "" {
		errs = append(errs, errors.New("githubReview.agentSecretName is required"))
	}
	for k, v := range r.AgentPermissions {
		if v != "read" {
			errs = append(errs, fmt.Errorf("githubReview.agentPermissions %s: the reviewer agent's token is read-only", k))
		}
	}
	// The reviewer may write only its check and the merge queue: any other
	// write (contents, workflows, pull_requests approval) would let it
	// author or approve what it reviews.
	for k, v := range r.ConnectorPermissions {
		if v == "write" && k != "checks" && k != "merge_queues" {
			errs = append(errs, fmt.Errorf("githubReview.connectorPermissions %s: the reviewer may write only checks and merge_queues", k))
		}
	}
	author := map[string]bool{}
	for _, b := range c.Bindings {
		author[b.Namespace] = true
	}
	seen, namespaces := map[string]bool{}, map[string]bool{}
	for i, b := range c.ReviewBindings {
		owner, repo, ok := strings.Cut(b.Repository, "/")
		if b.Name == "" || b.Namespace == "" || !ok || owner == "" || repo == "" || strings.Contains(repo, "/") {
			errs = append(errs, fmt.Errorf("reviewBindings[%d] needs name, namespace and repository owner/name", i))
		}
		if seen[b.Name] {
			errs = append(errs, fmt.Errorf("reviewBindings[%d]: duplicate name %q", i, b.Name))
		}
		seen[b.Name] = true
		if namespaces[b.Namespace] || author[b.Namespace] || b.Namespace == c.Namespace {
			errs = append(errs, fmt.Errorf("reviewBindings[%d]: namespace %q is already bound", i, b.Namespace))
		}
		namespaces[b.Namespace] = true
	}
	for _, s := range c.TokenReview.ReviewerSubjects {
		for _, other := range append(append([]string(nil), c.TokenReview.ConnectorSubjects...), c.TokenReview.AdminSubjects...) {
			if s == other {
				errs = append(errs, fmt.Errorf("tokenReview: %q may not be both a reviewer and a connector or admin subject", s))
			}
		}
	}
	return errs
}
