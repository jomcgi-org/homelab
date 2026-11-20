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
}

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
	case strings.TrimSpace(s.Instructions) == "" || len(s.Instructions) >= MaxDescriptionBytes:
		return fmt.Errorf("instructions must be nonempty and leave room for the evidence block")
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
