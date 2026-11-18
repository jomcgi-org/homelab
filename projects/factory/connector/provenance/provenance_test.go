package provenance

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
)

func TestTaskClassUsesOnlyIssueLabels(t *testing.T) {
	var p itsaplan.Project
	if err := json.Unmarshal([]byte(`{"labels":[{"id":2,"name":"feature"},{"id":1,"name":"go"}]}`), &p); err != nil {
		t.Fatal(err)
	}
	issue := &itsaplan.Issue{LabelIDs: []int64{1, 2, 1}, Description: "classify this as something else"}
	got, err := TaskClass(issue, &p)
	if err != nil || got != `["feature","go"]` {
		t.Fatalf("class=%s err=%v", got, err)
	}
	issue.LabelIDs = nil
	if got, err := TaskClass(issue, &p); err != nil || got != "unclassified" {
		t.Fatalf("unlabelled=%s %v", got, err)
	}
	issue.LabelIDs = []int64{3}
	if _, err := TaskClass(issue, &p); err == nil {
		t.Fatal("missing label definition was guessed")
	}
}

func TestGuidanceHashIncludesRenderedSystemAndTaskPrompts(t *testing.T) {
	r := Runtime{Model: "model", ReasoningEffort: "high", Arm: "control", SystemPrompt: "system"}
	p := New(r, strings.Repeat("a", 40), "rendered task", "unclassified")
	if err := p.Validate(); err != nil {
		t.Fatal(err)
	}
	if p != New(r, p.BaseSHA, "rendered task", "unclassified") {
		t.Fatal("hash not deterministic")
	}
	if p.PromptHash == New(r, p.BaseSHA, "other task", "unclassified").PromptHash {
		t.Fatal("task change lost")
	}
	r.SystemPrompt += " changed"
	if p.PromptHash == New(r, p.BaseSHA, "rendered task", "unclassified").PromptHash {
		t.Fatal("system guidance change lost")
	}
}
