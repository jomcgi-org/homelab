package config

import (
	"context"
	"errors"
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
	if *b.MaxReviewRounds != 3 || len(b.RequiredChecks) != 2 || b.ReviewerCheck.AppID != 4242 || b.TrustedReviewers[0] != "reviewer" {
		t.Fatalf("binding = %+v", b)
	}
	if time.Duration(f.Intervals.ReviewPoll) != time.Minute || f.Links.Issue == "" {
		t.Fatalf("intervals = %+v links = %+v", f.Intervals, f.Links)
	}
	if _, err := Parse([]byte(strings.Replace(review, "        statusCreator: lint-bot\n", "", 1))); err == nil {
		t.Fatal("check without appId or statusCreator accepted")
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
