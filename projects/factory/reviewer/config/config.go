// Package config loads the reviewer's settings from one YAML file, strictly:
// an unknown field is an error.
package config

import (
	"fmt"
	"os"
	"path"
	"strings"
	"time"

	"gopkg.in/yaml.v3"
)

// DefaultPath is where the file is read when FACTORY_REVIEWER_CONFIG is unset.
const DefaultPath = "/etc/factory-reviewer/config.yaml"

// DefaultCheckName is the check run the reviewer posts.
const DefaultCheckName = "factory review"

// Config is the reviewer's whole configuration.
type Config struct {
	// BrokerURL is the credential broker's admin port; the reviewer's GitHub
	// tokens come from its /github/reviewer-token, authenticated with the
	// projected token (audience factory-broker) in BrokerTokenFile.
	BrokerURL       string `yaml:"brokerURL"`
	BrokerTokenFile string `yaml:"brokerTokenFile"`
	// KagentURL is the kagent controller's A2A gateway, called with the
	// projected token (audience kagent-invocation) in KagentTokenFile.
	KagentURL       string `yaml:"kagentURL"`
	KagentTokenFile string `yaml:"kagentTokenFile"`
	GitHubURL       string `yaml:"githubURL"`
	MetricsAddr     string `yaml:"metricsAddr"`
	// CheckName is the check run posted on every head; AppID is the reviewer
	// GitHub App's ID, which only that App's check runs carry.
	CheckName string `yaml:"checkName"`
	AppID     int64  `yaml:"appId"`
	// Concurrency bounds the reviews in flight across all bindings.
	Concurrency int       `yaml:"concurrency"`
	Intervals   Intervals `yaml:"intervals"`
	// RuleIDs are the rule slugs findings are counted under; any other rule
	// counts as "other", which keeps the metric's labels bounded.
	RuleIDs  []string  `yaml:"ruleIds"`
	Bindings []Binding `yaml:"bindings"`
}

// Intervals are the loop timings.
type Intervals struct {
	// Poll spaces the listings of each binding's open PRs.
	Poll Duration `yaml:"poll"`
	// TaskPoll spaces the reads of a running review task.
	TaskPoll Duration `yaml:"taskPoll"`
	// Reissue spaces resends of a review message kagent has not taken.
	Reissue Duration `yaml:"reissue"`
	// Timeout bounds one review attempt.
	Timeout Duration `yaml:"timeout"`
	// RetryBackoff spaces the attempts of one review after one ends without
	// a report (a failed, rejected or timed-out task, or an unusable reply).
	RetryBackoff Duration `yaml:"retryBackoff"`
}

// Binding is one repository whose PRs into BaseBranch are reviewed by the
// kagent Agent KagentNamespace/KagentAgent. Unset lists take the defaults
// below; an empty list stays empty.
type Binding struct {
	Name            string   `yaml:"name"`
	Owner           string   `yaml:"owner"`
	Repo            string   `yaml:"repo"`
	BaseBranch      string   `yaml:"baseBranch"`
	KagentNamespace string   `yaml:"kagentNamespace"`
	KagentAgent     string   `yaml:"kagentAgent"`
	RequiredChecks  []Check  `yaml:"requiredChecks"`
	SkipLabels      []string `yaml:"skipLabels"`
	// EnqueueAuthors are the PR authors whose passing heads the reviewer
	// enqueues; other authors' PRs get the check only. The connector
	// enqueues the factory's own PRs, since it sees their stops.
	EnqueueAuthors []string `yaml:"enqueueAuthors"`
	// A PR touching HumanGatePaths (globs, ** for any directories) or
	// carrying HumanGateLabels needs an APPROVED review from one of
	// HumanApprovers on its exact head.
	HumanGatePaths  []string `yaml:"humanGatePaths"`
	HumanGateLabels []string `yaml:"humanGateLabels"`
	HumanApprovers  []string `yaml:"humanApprovers"`
	// TrustedCommenters are the logins whose PR comments reach the agent.
	TrustedCommenters []string `yaml:"trustedCommenters"`
	// ContextDocs are read from the base branch into every prompt.
	ContextDocs []string `yaml:"contextDocs"`
	// A PR whose diff, without GeneratedPaths, exceeds MaxDiffLines changed
	// lines or MaxDiffFiles files fails with a request to split it.
	GeneratedPaths []string `yaml:"generatedPaths"`
	MaxDiffLines   int      `yaml:"maxDiffLines"`
	MaxDiffFiles   int      `yaml:"maxDiffFiles"`
}

// Binding defaults.
var (
	DefaultEnqueueAuthors = []string{"jomcgi"}
	// DefaultHumanGatePaths are the rules, the reviewer, the code its
	// GitHub, kagent and token calls go through, its chart templates and
	// CODEOWNERS.
	DefaultHumanGatePaths = []string{
		"REVIEW.md", "**/REVIEW.md", "projects/factory/reviewer/**",
		"projects/factory/connector/github/**", "projects/factory/connector/kagent/**", "projects/factory/broker/**",
		"projects/factory/chart/templates/*reviewer*", ".github/CODEOWNERS",
	}
	DefaultHumanGateLabels = []string{"human", "needs-human"}
	DefaultHumanApprovers  = []string{"jomcgi"}
	DefaultContextDocs     = []string{"AGENTS.md", "docs/agents/reference.md"}
	DefaultGeneratedPaths  = []string{
		"**/*.lock", "**/*.lock.json", "**/*-lock.json", "**/*-lock.yaml", "**/go.sum",
		"**/render.sha256", "**/BUILD", "**/BUILD.bazel", "**/charts/*.tgz",
	}
)

const (
	DefaultMaxDiffLines = 2500
	DefaultMaxDiffFiles = 150
)

// Check must succeed on a head before it is enqueued: a check run named
// Name from the GitHub App AppID, or a commit status with context Name set
// by the login StatusCreator.
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

// Load reads the file FACTORY_REVIEWER_CONFIG names, or DefaultPath.
func Load(getenv func(string) string) (*Config, error) {
	path := getenv("FACTORY_REVIEWER_CONFIG")
	if path == "" {
		path = DefaultPath
	}
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	return Parse(b)
}

// Parse decodes and validates a configuration file, applying defaults.
func Parse(b []byte) (*Config, error) {
	var c Config
	dec := yaml.NewDecoder(strings.NewReader(string(b)))
	dec.KnownFields(true)
	if err := dec.Decode(&c); err != nil {
		return nil, fmt.Errorf("config: %w", err)
	}
	c.BrokerURL = strings.TrimSuffix(c.BrokerURL, "/")
	c.KagentURL = strings.TrimSuffix(c.KagentURL, "/")
	c.GitHubURL = strings.TrimSuffix(c.GitHubURL, "/")
	for field, v := range map[string]string{"brokerURL": c.BrokerURL, "kagentURL": c.KagentURL} {
		if v == "" {
			return nil, fmt.Errorf("config: %s is required", field)
		}
	}
	if c.AppID <= 0 {
		return nil, fmt.Errorf("config: appId is required: only the reviewer App's check runs count")
	}
	if c.BrokerTokenFile == "" {
		c.BrokerTokenFile = "/var/run/secrets/factory-broker/token"
	}
	if c.KagentTokenFile == "" {
		c.KagentTokenFile = "/var/run/secrets/kagent/token"
	}
	if c.GitHubURL == "" {
		c.GitHubURL = "https://api.github.com"
	}
	if c.MetricsAddr == "" {
		c.MetricsAddr = ":9090"
	}
	if c.CheckName == "" {
		c.CheckName = DefaultCheckName
	}
	if c.Concurrency == 0 {
		c.Concurrency = 2
	}
	if c.Concurrency < 0 {
		return nil, fmt.Errorf("config: concurrency must be positive")
	}
	iv := &c.Intervals
	for _, d := range []struct {
		v   *Duration
		def time.Duration
	}{
		{&iv.Poll, 30 * time.Second},
		{&iv.TaskPoll, 15 * time.Second},
		{&iv.Reissue, 15 * time.Second},
		{&iv.Timeout, 45 * time.Minute},
		{&iv.RetryBackoff, 5 * time.Minute},
	} {
		if *d.v == 0 {
			*d.v = Duration(d.def)
		}
		if *d.v < 0 {
			return nil, fmt.Errorf("config: intervals must be positive")
		}
	}
	if len(c.Bindings) == 0 {
		return nil, fmt.Errorf("config: no bindings")
	}
	names, repos := map[string]bool{}, map[string]bool{}
	for i := range c.Bindings {
		b := &c.Bindings[i]
		for field, v := range map[string]string{
			"name": b.Name, "owner": b.Owner, "repo": b.Repo, "baseBranch": b.BaseBranch,
			"kagentNamespace": b.KagentNamespace, "kagentAgent": b.KagentAgent,
		} {
			if v == "" {
				return nil, fmt.Errorf("config: binding %d: %s is required", i, field)
			}
		}
		if names[b.Name] {
			return nil, fmt.Errorf("config: duplicate binding %q", b.Name)
		}
		names[b.Name] = true
		key := strings.ToLower(b.Owner + "/" + b.Repo + ":" + b.BaseBranch)
		if repos[key] {
			return nil, fmt.Errorf("config: binding %s: %s/%s %s is bound twice", b.Name, b.Owner, b.Repo, b.BaseBranch)
		}
		repos[key] = true
		for _, d := range []struct {
			v   *[]string
			def []string
		}{
			{&b.EnqueueAuthors, DefaultEnqueueAuthors},
			{&b.HumanGatePaths, DefaultHumanGatePaths},
			{&b.HumanGateLabels, DefaultHumanGateLabels},
			{&b.HumanApprovers, DefaultHumanApprovers},
			{&b.TrustedCommenters, DefaultHumanApprovers},
			{&b.ContextDocs, DefaultContextDocs},
			{&b.GeneratedPaths, DefaultGeneratedPaths},
		} {
			if *d.v == nil {
				*d.v = append([]string(nil), d.def...)
			}
		}
		for _, g := range append(append([]string(nil), b.HumanGatePaths...), b.GeneratedPaths...) {
			for _, seg := range strings.Split(g, "/") {
				if _, err := path.Match(seg, ""); err != nil || g == "" {
					return nil, fmt.Errorf("config: binding %s: bad path glob %q", b.Name, g)
				}
			}
		}
		if len(b.HumanGatePaths)+len(b.HumanGateLabels) > 0 && len(b.HumanApprovers) == 0 {
			return nil, fmt.Errorf("config: binding %s: a human gate needs humanApprovers", b.Name)
		}
		if b.MaxDiffLines == 0 {
			b.MaxDiffLines = DefaultMaxDiffLines
		}
		if b.MaxDiffFiles == 0 {
			b.MaxDiffFiles = DefaultMaxDiffFiles
		}
		if b.MaxDiffLines < 0 || b.MaxDiffFiles < 0 {
			return nil, fmt.Errorf("config: binding %s: diff budgets must be positive", b.Name)
		}
		for _, ch := range b.RequiredChecks {
			if ch.Name == "" || (ch.AppID == 0) == (ch.StatusCreator == "") {
				return nil, fmt.Errorf("config: binding %s: a required check needs a name and exactly one of appId and statusCreator", b.Name)
			}
			if strings.EqualFold(ch.Name, c.CheckName) {
				return nil, fmt.Errorf("config: binding %s: the review check %q cannot also be a required check", b.Name, c.CheckName)
			}
		}
	}
	return &c, nil
}
