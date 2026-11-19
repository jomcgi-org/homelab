// Package provenance describes the inputs selected by factory dispatchers.
package provenance

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"sort"
	"strings"

	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
)

// Runtime comes from the same Helm values as the Agent and the broker relay.
// Arm is descriptive; selecting experiment arms is a separate concern.
type Runtime struct {
	Model           string `json:"model" yaml:"model"`
	ReasoningEffort string `json:"reasoningEffort" yaml:"reasoningEffort"`
	Arm             string `json:"arm" yaml:"arm"`
	SystemPrompt    string `json:"-" yaml:"systemPrompt"`
}

func (r *Runtime) Validate() error {
	if r == nil || strings.TrimSpace(r.Model) == "" || strings.TrimSpace(r.ReasoningEffort) == "" || strings.TrimSpace(r.Arm) == "" {
		return errors.New("run provenance requires model, reasoningEffort and arm")
	}
	return nil
}

type Record struct {
	Model           string `json:"model"`
	ReasoningEffort string `json:"reasoningEffort"`
	Arm             string `json:"arm"`
	BaseSHA         string `json:"baseSHA"`
	PromptHash      string `json:"promptHash"`
	TaskClass       string `json:"taskClass"`
}

func New(r Runtime, base, prompt, class string) Record {
	// Include the rendered Agent system prompt as well as the exact task text.
	// A NUL separates the two, so moving text across the boundary changes the hash.
	sum := sha256.Sum256([]byte(r.SystemPrompt + "\x00" + prompt))
	return Record{r.Model, r.ReasoningEffort, r.Arm, base, hex.EncodeToString(sum[:]), class}
}

func (r Record) Validate() error {
	runtime := Runtime{Model: r.Model, ReasoningEffort: r.ReasoningEffort, Arm: r.Arm}
	if err := runtime.Validate(); err != nil {
		return err
	}
	sha, err := hex.DecodeString(r.BaseSHA)
	if err != nil || len(sha) != 20 {
		return errors.New("run provenance requires a commit SHA")
	}
	hash, err := hex.DecodeString(r.PromptHash)
	if err != nil || len(hash) != sha256.Size {
		return errors.New("run provenance requires a SHA-256 prompt hash")
	}
	if strings.TrimSpace(r.TaskClass) == "" {
		return errors.New("run provenance requires task class")
	}
	return nil
}

// TaskClass snapshots issue label names in sorted order. JSON preserves names
// containing separators and multiple labels without inventing a classification.
// Missing label definitions are a failed read, not an unclassified issue.
func TaskClass(issue *itsaplan.Issue, project *itsaplan.Project) (string, error) {
	if issue == nil || len(issue.LabelIDs) == 0 {
		return "unclassified", nil
	}
	names := map[int64]string{}
	if project != nil {
		for _, label := range project.Labels {
			names[label.ID] = label.Name
		}
	}
	labels := []string{}
	seen := map[int64]bool{}
	for _, id := range issue.LabelIDs {
		name := names[id]
		if name == "" {
			return "", errors.New("issue label definition unavailable")
		}
		if !seen[id] {
			labels = append(labels, name)
			seen[id] = true
		}
	}
	sort.Strings(labels)
	data, _ := json.Marshal(labels)
	return string(data), nil
}
