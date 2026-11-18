package config

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

const sample = `
intervals:
  poll: 2s
coordinator:
  key: factory-coordinator
  apiKeyFile: /secrets/coordinator
agents:
  - key: coder
    apiKeyFile: /secrets/coder
bindings:
  - name: homelab
    owner: jomcgi-org
    repo: homelab
    baseBranch: main
    itsaplanProject: HL
    agents: [coder]
    kagentNamespace: factory-homelab
    kagentAgent: coder
`

func TestLoadRequiresTheControllersAuthority(t *testing.T) {
	path := filepath.Join(t.TempDir(), "config.yaml")
	if err := os.WriteFile(path, []byte(sample), 0o600); err != nil {
		t.Fatal(err)
	}
	env := map[string]string{
		"FACTORY_CONNECTOR_CONFIG":       path,
		"FACTORY_CONNECTOR_DATABASE_URL": "postgres://localhost/factory",
		"FACTORY_CONNECTOR_ITSAPLAN_URL": "http://itsaplan",
		"FACTORY_CONNECTOR_KAGENT_URL":   "http://kagent",
		"FACTORY_CONNECTOR_KAGENT_GRPC":  "kagent:8083",
	}
	getenv := func(name string) string { return env[name] }
	if _, err := Load(getenv); err == nil || !strings.Contains(err.Error(), "FACTORY_CONNECTOR_KAGENT_AUTHORITY") {
		t.Fatalf("missing authority = %v", err)
	}
	env["FACTORY_CONNECTOR_KAGENT_AUTHORITY"] = "kept-controller"
	c, err := Load(getenv)
	if err != nil || c.KagentAuthority != "kept-controller" {
		t.Fatalf("authority = %+v, %v", c, err)
	}
}

func TestParseAppliesDefaults(t *testing.T) {
	f, err := Parse([]byte(sample))
	if err != nil {
		t.Fatal(err)
	}
	if time.Duration(f.Intervals.Poll) != 2*time.Second || time.Duration(f.Intervals.Heartbeat) != time.Minute {
		t.Fatalf("intervals = %+v", f.Intervals)
	}
	b := f.Bindings[0]
	if b.BranchPrefix != "factory/" || *b.MaxEngineeringRetries != 2 || f.Agents[0].Concurrency != 1 {
		t.Fatalf("binding = %+v agents = %+v", b, f.Agents)
	}
}

// The tool lease bounds a turn whose tool call stays open (HOME-159): it is
// 60 minutes unless set, and must be positive.
func TestParseToolLease(t *testing.T) {
	for value, want := range map[string]time.Duration{"": time.Hour, "  toolLease: 45m\n": 45 * time.Minute} {
		f, err := Parse([]byte(strings.Replace(sample, "  poll: 2s\n", "  poll: 2s\n"+value, 1)))
		if err != nil || time.Duration(f.Intervals.ToolLease) != want {
			t.Fatalf("toolLease %q: config = %+v, error = %v; want %s", value, f, err, want)
		}
	}
	if _, err := Parse([]byte(strings.Replace(sample, "  poll: 2s\n", "  poll: 2s\n  toolLease: -1m\n", 1))); err == nil {
		t.Fatal("negative toolLease accepted")
	}
}

func TestHumanGateAssigneeDefaultsToJoeOnlyForHOME(t *testing.T) {
	for _, tc := range []struct{ input, want string }{
		{sample, ""},
		{strings.Replace(sample, "itsaplanProject: HL", "itsaplanProject: HOME", 1), "joe"},
		{strings.Replace(sample, "itsaplanProject: HL", "itsaplanProject: HOME", 1) + "    humanGateAssignee: ''\n", ""},
		{sample + "    humanGateAssignee: other\n", "other"},
	} {
		f, err := Parse([]byte(tc.input))
		if err != nil {
			t.Fatal(err)
		}
		if got := *f.Bindings[0].HumanGateAssignee; got != tc.want {
			t.Fatalf("assignee = %q, want %q", got, tc.want)
		}
	}
}

func TestParseRejectsUnknownAgentAndFields(t *testing.T) {
	if _, err := Parse([]byte(strings.Replace(sample, "agents: [coder]", "agents: [other]", 1))); err == nil {
		t.Fatal("unknown agent accepted")
	}
	if _, err := Parse([]byte(sample + "extra: true\n")); err == nil {
		t.Fatal("unknown field accepted")
	}
	if _, err := Parse([]byte(strings.Replace(sample, "    repo: homelab\n", "", 1))); err == nil {
		t.Fatal("binding without repo accepted")
	}
	if _, err := Parse([]byte(strings.Replace(sample, "poll: 2s", "poll: 2s\n  dispatchWindow: 150s", 1))); err == nil {
		t.Fatal("dispatch window shorter than timeout plus reservation accepted")
	}
}

func TestParseReviewPolicy(t *testing.T) {
	review := sample + `    trustedReviewers: [reviewer]
    requiredChecks:
      - name: test
        appId: 15368
      - name: ci/lint
        statusCreator: lint-bot
    reviewerCheck:
      name: semgrep-review
      appId: 4242
links:
  issue: https://plan.example/issue/{issue}
`
	f, err := Parse([]byte(review))
	if err != nil {
		t.Fatal(err)
	}
	b := f.Bindings[0]
	if *b.MaxReviewRounds != 3 || *b.MaxRebaseRounds != 3 || *b.MaxSplitRounds != 1 || len(b.RequiredChecks) != 2 || b.ReviewerCheck.AppID != 4242 || b.TrustedReviewers[0] != "reviewer" {
		t.Fatalf("binding = %+v", b)
	}
	if time.Duration(f.Intervals.ReviewPoll) != time.Minute || time.Duration(f.Intervals.QuotaStall) != 10*time.Minute ||
		time.Duration(f.Intervals.TaskLease) != 5*time.Minute || time.Duration(f.Intervals.ProgressLease) != 20*time.Minute ||
		time.Duration(f.Intervals.ForceDelete) != 10*time.Minute || f.Links.Issue == "" {
		t.Fatalf("intervals = %+v links = %+v", f.Intervals, f.Links)
	}
	if _, err := Parse([]byte(strings.Replace(review, "        statusCreator: lint-bot\n", "", 1))); err == nil {
		t.Fatal("check without appId or statusCreator accepted")
	}
	for _, bound := range []int{0, 5, -1} {
		f, err := Parse([]byte(sample + fmt.Sprintf("    maxRebaseRounds: %d\n", bound)))
		if bound < 0 {
			if err == nil {
				t.Fatal("negative rebase bound accepted")
			}
		} else if err != nil || *f.Bindings[0].MaxRebaseRounds != bound {
			t.Fatalf("maxRebaseRounds %d: config = %+v, error = %v", bound, f, err)
		}
		f, err = Parse([]byte(sample + fmt.Sprintf("    maxSplitRounds: %d\n", bound)))
		if bound < 0 {
			if err == nil {
				t.Fatal("negative split bound accepted")
			}
		} else if err != nil || *f.Bindings[0].MaxSplitRounds != bound {
			t.Fatalf("maxSplitRounds %d: config = %+v, error = %v", bound, f, err)
		}
	}
}

func TestKeyFilesWaitAndReread(t *testing.T) {
	dir := t.TempDir()
	coord, coder := filepath.Join(dir, "coord"), filepath.Join(dir, "coder")
	keys := KeyFiles{"coord": coord, "coder": coder}
	if err := os.WriteFile(coord, []byte("k1\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	// An empty projected file is a key not written yet.
	if err := os.WriteFile(coder, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	if got := keys.Missing(); len(got) != 1 || got[0] != coder {
		t.Fatalf("missing = %v", got)
	}
	reports := 0
	err := keys.WaitForKeys(context.Background(), time.Second, func(missing []string) {
		reports++
		if err := os.WriteFile(coder, []byte("k2"), 0o600); err != nil {
			t.Error(err)
		}
	})
	if err != nil || reports != 1 {
		t.Fatalf("wait = %v after %d reports", err, reports)
	}
	if k, err := keys.Key("coder"); err != nil || k != "k2" {
		t.Fatalf("key = %q, %v", k, err)
	}
	// A rotated key is read on the next lookup.
	if err := os.WriteFile(coder, []byte("k3"), 0o600); err != nil {
		t.Fatal(err)
	}
	if k, _ := keys.Key("coder"); k != "k3" {
		t.Fatalf("rotated key = %q", k)
	}
	if _, err := keys.Key("nobody"); err == nil {
		t.Fatal("unknown agent has a key")
	}
}

func TestWaitForKeysStopsWithContext(t *testing.T) {
	keys := KeyFiles{"coord": filepath.Join(t.TempDir(), "absent")}
	ctx, cancel := context.WithCancel(context.Background())
	err := keys.WaitForKeys(ctx, time.Second, func([]string) { cancel() })
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("err = %v", err)
	}
}

func TestParseBuildBuddy(t *testing.T) {
	const good = `
buildbuddy:
  apiURL: https://example.buildbuddy.io
  invocationPrefix: https://example.buildbuddy.io/invocation/
  apiKeyFile: /secrets/buildbuddy/api-key
`
	f, err := Parse([]byte(sample + good))
	if err != nil {
		t.Fatal(err)
	}
	if f.BuildBuddy == nil || f.BuildBuddy.APIKeyFile != "/secrets/buildbuddy/api-key" {
		t.Fatalf("buildbuddy = %+v", f.BuildBuddy)
	}
	if f, err := Parse([]byte(sample)); err != nil || f.BuildBuddy != nil {
		t.Fatalf("without buildbuddy: %+v, %v", f, err)
	}
	for _, bad := range []string{
		strings.Replace(good, "https://example.buildbuddy.io\n", "http://example.buildbuddy.io\n", 1),
		strings.Replace(good, "https://example.buildbuddy.io\n", "https://example.buildbuddy.io/api\n", 1),
		strings.Replace(good, "https://example.buildbuddy.io\n", "https://user:pw@example.buildbuddy.io\n", 1),
		strings.Replace(good, "/invocation/\n", "/invocation\n", 1),
		strings.Replace(good, "  apiKeyFile: /secrets/buildbuddy/api-key\n", "", 1),
		good + "  extra: true\n",
	} {
		if _, err := Parse([]byte(sample + bad)); err == nil {
			t.Errorf("accepted:\n%s", bad)
		}
	}
}

func TestParseReviewerGoalURL(t *testing.T) {
	f, err := Parse([]byte(sample + "reviewerGoalURL: http://factory-reviewer.factory.svc:8091\n"))
	if err != nil || f.ReviewerGoalURL != "http://factory-reviewer.factory.svc:8091" {
		t.Fatalf("reviewerGoalURL = %+v, %v", f, err)
	}
	for _, bad := range []string{
		"https://factory-reviewer.factory.svc:8091",
		"http://reviewer.example.com:8091",
		"http://factory-reviewer.factory.svc:8091/v1",
		"http://u:p@factory-reviewer.factory.svc:8091",
		"http://factory-reviewer.factory.svc:8091?x=1",
	} {
		if _, err := Parse([]byte(sample + "reviewerGoalURL: " + bad + "\n")); err == nil {
			t.Errorf("accepted %s", bad)
		}
	}
}
