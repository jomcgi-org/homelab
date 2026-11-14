package config

import (
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
