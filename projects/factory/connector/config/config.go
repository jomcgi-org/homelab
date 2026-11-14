// Package config loads the connector's settings: endpoints and secret paths
// from the environment, bindings and intervals from a YAML file.
package config

import (
	"context"
	"fmt"
	"os"
	"sort"
	"strings"
	"time"

	"gopkg.in/yaml.v3"
)

// Config is the connector's whole configuration.
type Config struct {
	// From the environment.
	DatabaseURL      string // FACTORY_CONNECTOR_DATABASE_URL
	ItsaplanURL      string // FACTORY_CONNECTOR_ITSAPLAN_URL
	KagentURL        string // FACTORY_CONNECTOR_KAGENT_URL (A2A over HTTP)
	KagentGRPCTarget string // FACTORY_CONNECTOR_KAGENT_GRPC (host:port)
	KagentTokenFile  string // FACTORY_CONNECTOR_KAGENT_TOKEN_FILE
	// BrokerURL turns delivery on: the connector's GitHub tokens come from
	// the broker, authenticated with the projected token in BrokerTokenFile.
	BrokerURL       string // FACTORY_CONNECTOR_BROKER_URL
	BrokerTokenFile string // FACTORY_CONNECTOR_BROKER_TOKEN_FILE
	GitHubURL       string // FACTORY_CONNECTOR_GITHUB_URL, default https://api.github.com
	MetricsAddr     string // FACTORY_CONNECTOR_METRICS_ADDR, default :9090
	Holder          string // POD_NAME, default hostname

	File File
}

// File is the YAML configuration file.
type File struct {
	Intervals   Intervals   `yaml:"intervals"`
	Coordinator Coordinator `yaml:"coordinator"`
	Agents      []Agent     `yaml:"agents"`
	Bindings    []Binding   `yaml:"bindings"`
	Links       Links       `yaml:"links"`
}

// Links are URL templates for PR bodies and Done comments; see dispatch.Links.
type Links struct {
	Issue   string `yaml:"issue"`
	Run     string `yaml:"run"`
	Session string `yaml:"session"`
}

// Intervals are the loop timings.
type Intervals struct {
	Poll            Duration `yaml:"poll"`
	Heartbeat       Duration `yaml:"heartbeat"`
	ReissueInterval Duration `yaml:"reissueInterval"`
	DispatchWindow  Duration `yaml:"dispatchWindow"`
	AcquireRetry    Duration `yaml:"acquireRetry"`
	ReviewPoll      Duration `yaml:"reviewPoll"`
}

// MinDispatchWindow covers the 60 s A2A call timeout (kagent.DefaultTimeout),
// kagent's two-minute dispatch reservation and 30 s of clock skew.
const MinDispatchWindow = 210 * time.Second

// Coordinator is the itsaplan agent that reads runs and comments.
type Coordinator struct {
	Key        string `yaml:"key"`
	APIKeyFile string `yaml:"apiKeyFile"`
}

// Agent is an itsaplan external agent whose runs the connector claims.
type Agent struct {
	Key         string `yaml:"key"`
	APIKeyFile  string `yaml:"apiKeyFile"`
	Concurrency int    `yaml:"concurrency"`
}

// Binding is one repository binding.
type Binding struct {
	Name                  string   `yaml:"name"`
	Owner                 string   `yaml:"owner"`
	Repo                  string   `yaml:"repo"`
	BaseBranch            string   `yaml:"baseBranch"`
	ItsaplanProject       string   `yaml:"itsaplanProject"`
	BranchPrefix          string   `yaml:"branchPrefix"`
	Agents                []string `yaml:"agents"`
	KagentNamespace       string   `yaml:"kagentNamespace"`
	KagentAgent           string   `yaml:"kagentAgent"`
	MaxEngineeringRetries *int     `yaml:"maxEngineeringRetries"`

	// Review policy: who and what the connector trusts on the binding's PRs.
	TrustedReviewers []string `yaml:"trustedReviewers"`
	RequiredChecks   []Check  `yaml:"requiredChecks"`
	ReviewerCheck    *Check   `yaml:"reviewerCheck"`
	MaxReviewRounds  *int     `yaml:"maxReviewRounds"`
	DoneColumn       string   `yaml:"doneColumn"`
}

// Check is a trusted check: a check run from the GitHub App appId, or a
// commit status set by the login statusCreator.
type Check struct {
	Name          string `yaml:"name"`
	AppID         int64  `yaml:"appId"`
	StatusCreator string `yaml:"statusCreator"`
}

// Duration parses Go duration strings in YAML.
type Duration time.Duration

// UnmarshalYAML implements yaml.Unmarshaler.
func (d *Duration) UnmarshalYAML(n *yaml.Node) error {
	v, err := time.ParseDuration(n.Value)
	if err != nil {
		return err
	}
	*d = Duration(v)
	return nil
}

// Load reads the environment and the file it names.
func Load(getenv func(string) string) (*Config, error) {
	c := &Config{
		DatabaseURL:      getenv("FACTORY_CONNECTOR_DATABASE_URL"),
		ItsaplanURL:      strings.TrimSuffix(getenv("FACTORY_CONNECTOR_ITSAPLAN_URL"), "/"),
		KagentURL:        strings.TrimSuffix(getenv("FACTORY_CONNECTOR_KAGENT_URL"), "/"),
		KagentGRPCTarget: getenv("FACTORY_CONNECTOR_KAGENT_GRPC"),
		KagentTokenFile:  getenv("FACTORY_CONNECTOR_KAGENT_TOKEN_FILE"),
		BrokerURL:        strings.TrimSuffix(getenv("FACTORY_CONNECTOR_BROKER_URL"), "/"),
		BrokerTokenFile:  getenv("FACTORY_CONNECTOR_BROKER_TOKEN_FILE"),
		GitHubURL:        strings.TrimSuffix(getenv("FACTORY_CONNECTOR_GITHUB_URL"), "/"),
		MetricsAddr:      getenv("FACTORY_CONNECTOR_METRICS_ADDR"),
		Holder:           getenv("POD_NAME"),
	}
	if c.MetricsAddr == "" {
		c.MetricsAddr = ":9090"
	}
	if c.KagentTokenFile == "" {
		c.KagentTokenFile = "/var/run/secrets/kagent/token"
	}
	if c.BrokerTokenFile == "" {
		c.BrokerTokenFile = "/var/run/secrets/factory-broker/token"
	}
	if c.GitHubURL == "" {
		c.GitHubURL = "https://api.github.com"
	}
	if c.Holder == "" {
		c.Holder, _ = os.Hostname()
	}
	path := getenv("FACTORY_CONNECTOR_CONFIG")
	if path == "" {
		path = "/etc/factory-connector/config.yaml"
	}
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	f, err := Parse(b)
	if err != nil {
		return nil, err
	}
	c.File = *f
	for name, v := range map[string]string{
		"FACTORY_CONNECTOR_DATABASE_URL": c.DatabaseURL,
		"FACTORY_CONNECTOR_ITSAPLAN_URL": c.ItsaplanURL,
		"FACTORY_CONNECTOR_KAGENT_URL":   c.KagentURL,
		"FACTORY_CONNECTOR_KAGENT_GRPC":  c.KagentGRPCTarget,
	} {
		if v == "" {
			return nil, fmt.Errorf("config: %s is required", name)
		}
	}
	return c, nil
}

// Parse decodes and validates a configuration file, applying defaults.
func Parse(b []byte) (*File, error) {
	var f File
	dec := yaml.NewDecoder(strings.NewReader(string(b)))
	dec.KnownFields(true)
	if err := dec.Decode(&f); err != nil {
		return nil, fmt.Errorf("config: %w", err)
	}
	if f.Intervals.Poll == 0 {
		f.Intervals.Poll = Duration(5 * time.Second)
	}
	if f.Intervals.Heartbeat == 0 {
		f.Intervals.Heartbeat = Duration(60 * time.Second)
	}
	if f.Intervals.DispatchWindow == 0 {
		f.Intervals.DispatchWindow = Duration(4 * time.Minute)
	}
	if time.Duration(f.Intervals.DispatchWindow) < MinDispatchWindow {
		return nil, fmt.Errorf("config: dispatchWindow must be at least %s", MinDispatchWindow)
	}
	if f.Intervals.ReissueInterval == 0 {
		f.Intervals.ReissueInterval = Duration(15 * time.Second)
	}
	if hb := time.Duration(f.Intervals.Heartbeat); hb <= 0 || hb > 2*time.Minute {
		return nil, fmt.Errorf("config: heartbeat must be positive and at most 2m against itsaplan's 300 s lease")
	}
	if f.Intervals.AcquireRetry == 0 {
		f.Intervals.AcquireRetry = Duration(5 * time.Second)
	}
	if f.Intervals.ReviewPoll == 0 {
		f.Intervals.ReviewPoll = Duration(60 * time.Second)
	}
	if f.Coordinator.Key == "" || f.Coordinator.APIKeyFile == "" {
		return nil, fmt.Errorf("config: coordinator key and apiKeyFile are required")
	}
	agents := map[string]bool{}
	for i := range f.Agents {
		a := &f.Agents[i]
		if a.Key == "" || a.APIKeyFile == "" {
			return nil, fmt.Errorf("config: agent %d needs key and apiKeyFile", i)
		}
		if agents[a.Key] {
			return nil, fmt.Errorf("config: duplicate agent %q", a.Key)
		}
		agents[a.Key] = true
		if a.Concurrency <= 0 {
			a.Concurrency = 1
		}
	}
	projects := map[string]bool{}
	for i := range f.Bindings {
		b := &f.Bindings[i]
		for field, v := range map[string]string{
			"name": b.Name, "owner": b.Owner, "repo": b.Repo, "baseBranch": b.BaseBranch,
			"itsaplanProject": b.ItsaplanProject, "kagentNamespace": b.KagentNamespace, "kagentAgent": b.KagentAgent,
		} {
			if v == "" {
				return nil, fmt.Errorf("config: binding %d: %s is required", i, field)
			}
		}
		if projects[b.ItsaplanProject] {
			return nil, fmt.Errorf("config: project %s has two bindings", b.ItsaplanProject)
		}
		projects[b.ItsaplanProject] = true
		if b.BranchPrefix == "" {
			b.BranchPrefix = "factory/"
		}
		if b.MaxEngineeringRetries == nil {
			two := 2
			b.MaxEngineeringRetries = &two
		}
		if b.MaxReviewRounds == nil {
			three := 3
			b.MaxReviewRounds = &three
		}
		checks := append([]Check(nil), b.RequiredChecks...)
		if b.ReviewerCheck != nil {
			checks = append(checks, *b.ReviewerCheck)
		}
		for _, c := range checks {
			if c.Name == "" || (c.AppID == 0) == (c.StatusCreator == "") {
				return nil, fmt.Errorf("config: binding %s: a check needs a name and exactly one of appId and statusCreator", b.Name)
			}
		}
		if len(b.Agents) == 0 {
			return nil, fmt.Errorf("config: binding %s names no agents", b.Name)
		}
		for _, a := range b.Agents {
			if !agents[a] {
				return nil, fmt.Errorf("config: binding %s names unknown agent %q", b.Name, a)
			}
		}
	}
	return &f, nil
}

// ReadKey reads an API key file. An empty file is an error: a Secret whose
// key is not written yet projects no file or an empty one.
func ReadKey(path string) (string, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	k := strings.TrimSpace(string(b))
	if k == "" {
		return "", fmt.Errorf("config: %s is empty", path)
	}
	return k, nil
}

// KeyFiles maps itsaplan agent keys to the files holding their API keys. It
// reads a file on every lookup, so a key rotated in its Secret takes effect
// without a restart.
type KeyFiles map[string]string

// KeyFiles returns the coordinator's and every agent's key file.
func (f *File) KeyFiles() KeyFiles {
	k := KeyFiles{f.Coordinator.Key: f.Coordinator.APIKeyFile}
	for _, a := range f.Agents {
		k[a.Key] = a.APIKeyFile
	}
	return k
}

// Key reads the API key of agent.
func (k KeyFiles) Key(agent string) (string, error) {
	path, ok := k[agent]
	if !ok {
		return "", fmt.Errorf("config: no API key file for agent %q", agent)
	}
	return ReadKey(path)
}

// Missing lists the key files that cannot be read yet, sorted.
func (k KeyFiles) Missing() []string {
	var out []string
	for _, path := range k {
		if _, err := ReadKey(path); err != nil {
			out = append(out, path)
		}
	}
	sort.Strings(out)
	return out
}

// WaitForKeys returns once every key file is readable. The itsaplan bootstrap
// writes the keys after the connector first starts, so on a fresh install or
// a restore the connector waits for them, retrying with backoff from 1 s up
// to maxWait and reporting what it still waits for.
func (k KeyFiles) WaitForKeys(ctx context.Context, maxWait time.Duration, report func(missing []string)) error {
	wait := time.Second
	for {
		missing := k.Missing()
		if len(missing) == 0 {
			return nil
		}
		report(missing)
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(wait):
		}
		wait = min(2*wait, maxWait)
	}
}
