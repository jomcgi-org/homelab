package planner

import (
	"fmt"
	"regexp"
	"strings"
	"time"
)

// Schedule sets a planner job's reviewed limits and issue template.
type Schedule struct {
	Name         string        `yaml:"name"`
	Namespace    string        `yaml:"namespace"`
	Ceiling      string        `yaml:"ceiling"`
	MinInterval  time.Duration `yaml:"minInterval"`
	Cooldown     time.Duration `yaml:"cooldown"`
	PerRun       int           `yaml:"perRun"`
	IssueTitle   string        `yaml:"issueTitle"`
	Instructions string        `yaml:"instructions"`
	// Cron is the five-field UTC expression the bootstrap gives the itsaplan
	// schedule. Prompt is what the connector sends the planner Agent: reviewed
	// text on main, so no run takes its task from itsaplan. A schedule with no
	// prompt admits no run.
	Actions []string `yaml:"actions,omitempty"`
	Cron    string   `yaml:"cron"`
	Prompt  string   `yaml:"prompt"`
}

// MaxPromptBytes bounds a schedule's analysis prompt.
const MaxPromptBytes = 16 << 10

// RunPromptPrefix starts the prompt of each itsaplan cron schedule. The
// connector maps a claimed run to its reviewed schedule by the name after it
// and never sends the itsaplan prompt to a model.
const RunPromptPrefix = "factory-planner-schedule:"

// ScheduleName returns the schedule an itsaplan run prompt selects, or "".
func ScheduleName(runPrompt string) string {
	name, ok := strings.CutPrefix(strings.TrimSpace(runPrompt), RunPromptPrefix)
	if !ok || strings.TrimSpace(name) != name {
		return ""
	}
	return name
}

// Tracked assigns a fingerprint to a HOME issue that already covers it, such
// as one of the scheduled jobs HOME-4 to HOME-11. A proposal with that
// fingerprint is deduplicated while the issue is open.
type Tracked struct {
	Fingerprint string `yaml:"fingerprint"`
	Issue       string `yaml:"issue"`
}

var (
	fingerprintShape = regexp.MustCompile(`^[a-z0-9._/-]+(:[a-z0-9._/-]+){3}$`)
	issueKeyShape    = regexp.MustCompile(`^[A-Z][A-Z0-9]*-[0-9]+$`)
)

// Validate checks that a fingerprint is well formed and names an issue.
func (t Tracked) Validate() error {
	switch {
	case len(t.Fingerprint) > 256 || !fingerprintShape.MatchString(t.Fingerprint):
		return fmt.Errorf("fingerprint must be repo:area:object:failure-class")
	case !issueKeyShape.MatchString(t.Issue):
		return fmt.Errorf("issue must be a key such as HOME-4")
	}
	return nil
}

// Leave space for a proposal with a finding, fingerprint and evidence links.
const (
	reservedProposalBytes = 1024
	maxInstructionsBytes  = MaxDescriptionBytes - len(evidencePrefix) - len(evidenceSuffix) - reservedProposalBytes
)

var scheduleNamespace = regexp.MustCompile(`^[a-z0-9._/-]+:[a-z0-9._/-]+$`)

// Validate checks that a schedule can admit and file proposals.
func (s Schedule) Validate() error {
	switch {
	case strings.TrimSpace(s.Name) == "" || strings.TrimSpace(s.Name) != s.Name:
		return fmt.Errorf("name must be nonempty and have no surrounding whitespace")
	case !scheduleNamespace.MatchString(s.Namespace) || len(s.Namespace) > 252:
		return fmt.Errorf("namespace must be the first two fingerprint segments")
	case s.MinInterval < time.Microsecond || s.Cooldown < time.Microsecond:
		return fmt.Errorf("minInterval and cooldown must be positive PostgreSQL intervals")
	case s.PerRun < 1 || s.PerRun > 4:
		return fmt.Errorf("perRun must be between 1 and 4")
	case strings.TrimSpace(s.IssueTitle) == "":
		return fmt.Errorf("issueTitle is required")
	case strings.TrimSpace(s.Instructions) == "" || len(s.Instructions) > maxInstructionsBytes:
		return fmt.Errorf("instructions must be nonempty and at most %d bytes", maxInstructionsBytes)
	case len(s.Prompt) > MaxPromptBytes:
		return fmt.Errorf("prompt must be at most %d bytes", MaxPromptBytes)
	case s.Cron != "" && len(strings.Fields(s.Cron)) != 5:
		return fmt.Errorf("cron must have five fields")
	}
	return nil
}

// EffectiveCeiling limits unknown values to Backlog.
func (s Schedule) EffectiveCeiling() string {
	switch s.Ceiling {
	case "delegate", "joe":
		return s.Ceiling
	default:
		return "backlog"
	}
}
