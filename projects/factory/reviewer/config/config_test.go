package config

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

const sample = `
brokerURL: http://factory-broker.factory.svc.cluster.local:8081/
kagentURL: http://kagent-controller.kagent.svc.cluster.local:8083
appId: 5758326
bindings:
  - name: homelab
    owner: jomcgi-org
    repo: homelab
    baseBranch: main
    kagentNamespace: factory-review
    kagentAgent: reviewer
    requiredChecks:
      - name: pr-checks
        statusCreator: buildbuddy-io[bot]
`

func TestParseAppliesDefaults(t *testing.T) {
	c, err := Parse([]byte(sample))
	if err != nil {
		t.Fatal(err)
	}
	if c.BrokerURL != "http://factory-broker.factory.svc.cluster.local:8081" || c.GitHubURL != "https://api.github.com" ||
		c.BrokerTokenFile != "/var/run/secrets/factory-broker/token" || c.KagentTokenFile != "/var/run/secrets/kagent/token" ||
		c.MetricsAddr != ":9090" {
		t.Fatalf("endpoints = %+v", c)
	}
	if c.CheckName != "factory review" || c.Concurrency != 2 {
		t.Fatalf("check %q, concurrency %d", c.CheckName, c.Concurrency)
	}
	iv := c.Intervals
	if time.Duration(iv.Poll) != 30*time.Second || time.Duration(iv.TaskPoll) != 15*time.Second ||
		time.Duration(iv.Reissue) != 15*time.Second || time.Duration(iv.Timeout) != 45*time.Minute ||
		time.Duration(iv.RetryBackoff) != 5*time.Minute {
		t.Fatalf("intervals = %+v", iv)
	}
	b := c.Bindings[0]
	if len(b.SkipLabels) != 0 || b.RequiredChecks[0].StatusCreator != "buildbuddy-io[bot]" {
		t.Fatalf("binding = %+v", b)
	}
	gated := "REVIEW.md,**/REVIEW.md,projects/factory/reviewer/**,projects/factory/connector/github/**," +
		"projects/factory/connector/kagent/**,projects/factory/broker/**,projects/factory/chart/templates/*reviewer*,.github/CODEOWNERS"
	if strings.Join(b.EnqueueAuthors, ",") != "jomcgi" || strings.Join(b.HumanGatePaths, ",") != gated ||
		strings.Join(b.HumanGateLabels, ",") != "human,needs-human" || strings.Join(b.HumanApprovers, ",") != "jomcgi" ||
		strings.Join(b.TrustedCommenters, ",") != "jomcgi" || strings.Join(b.ContextDocs, ",") != "AGENTS.md,docs/agents/reference.md" ||
		b.MaxDiffLines != 2500 || b.MaxDiffFiles != 150 || len(b.GeneratedPaths) == 0 {
		t.Fatalf("binding defaults = %+v", b)
	}
}

func TestParseKeepsExplicitEmptyLists(t *testing.T) {
	c, err := Parse([]byte(sample + "    humanGateLabels: []\n    enqueueAuthors: []\n    maxDiffLines: 10\nruleIds: [nil-deref]\n"))
	if err != nil {
		t.Fatal(err)
	}
	b := c.Bindings[0]
	if b.HumanGateLabels == nil || len(b.HumanGateLabels) != 0 || len(b.EnqueueAuthors) != 0 || b.MaxDiffLines != 10 || c.RuleIDs[0] != "nil-deref" {
		t.Fatalf("binding = %+v", b)
	}
}

func TestParseRejectsInvalidFiles(t *testing.T) {
	for name, file := range map[string]string{
		"unknown field":       sample + "extra: true\n",
		"unknown binding key": strings.Replace(sample, "    repo: homelab\n", "    repo: homelab\n    authors: [jomcgi]\n", 1),
		"no app id":           strings.Replace(sample, "appId: 5758326\n", "", 1),
		"no broker":           strings.Replace(sample, "brokerURL: http://factory-broker.factory.svc.cluster.local:8081/\n", "", 1),
		"no repo":             strings.Replace(sample, "    repo: homelab\n", "", 1),
		"no agent":            strings.Replace(sample, "    kagentAgent: reviewer\n", "", 1),
		"check with both":     strings.Replace(sample, "statusCreator: buildbuddy-io[bot]", "statusCreator: x\n        appId: 1", 1),
		"check with neither":  strings.Replace(sample, "        statusCreator: buildbuddy-io[bot]\n", "", 1),
		"review as required":  strings.Replace(sample, "name: pr-checks", "name: factory review", 1),
		"negative interval":   sample + "intervals:\n  poll: -1s\n",
		"bad duration":        sample + "intervals:\n  timeout: soon\n",
		"no bindings":         strings.Split(sample, "bindings:")[0],
		"duplicate binding":   sample + strings.SplitN(sample, "bindings:\n", 2)[1],
		"bad glob":            sample + "    humanGatePaths: [\"[\"]\n",
		"gate, no approvers":  sample + "    humanApprovers: []\n",
		"negative budget":     sample + "    maxDiffFiles: -1\n",
	} {
		if _, err := Parse([]byte(file)); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
}

func TestLoadReadsTheNamedFile(t *testing.T) {
	path := filepath.Join(t.TempDir(), "config.yaml")
	if err := os.WriteFile(path, []byte(sample+"concurrency: 3\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	c, err := Load(func(k string) string {
		if k == "FACTORY_REVIEWER_CONFIG" {
			return path
		}
		return ""
	})
	if err != nil || c.Concurrency != 3 {
		t.Fatalf("load = %+v, %v", c, err)
	}
}
