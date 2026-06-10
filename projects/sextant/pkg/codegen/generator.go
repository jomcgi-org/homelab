// Package codegen generates Go code from state machine definitions.
package codegen

import (
	"bytes"
	"embed"
	"fmt"
	"go/format"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"text/template"

	"github.com/jomcgi/homelab/projects/sextant/pkg/schema"
)

//go:embed templates/*.tmpl
var templates embed.FS

// Config configures code generation.
type Config struct {
	// OutputDir is the directory to write generated files
	OutputDir string

	// Package is the Go package name for generated code
	Package string

	// Module is the Go module path (e.g., "github.com/joe/operator")
	Module string

	// APIImportPath is the import path for the API types
	APIImportPath string
}

// Generator generates Go code from state machine definitions.
type Generator struct {
	config    Config
	templates *template.Template
}

// New creates a new Generator with the given configuration.
func New(config Config) (*Generator, error) {
	funcMap := template.FuncMap{
		"lower":           strings.ToLower,
		"upper":           strings.ToUpper,
		"title":           strings.Title,
		"camelToSnake":    camelToSnake,
		"toEventName":     toEventName,
		"goType":          goType,
		"defaultValue":    defaultValue,
		"hasRequeue":      hasRequeue,
		"durationLiteral": durationLiteral,
		"join":            strings.Join,
		"add":             func(a, b int) int { return a + b },
		"fieldGroupName":  fieldGroupName,
		"hasFieldInGroup": hasFieldInGroup,
		"contains":        contains,
	}

	tmpl, err := template.New("").Funcs(funcMap).ParseFS(templates, "templates/*.tmpl")
	if err != nil {
		return nil, fmt.Errorf("failed to parse templates: %w", err)
	}

	return &Generator{
		config:    config,
		templates: tmpl,
	}, nil
}

// Generate generates Go code for the given state machine.
func (g *Generator) Generate(sm *schema.StateMachine) error {
	// Ensure output directory exists
	if err := os.MkdirAll(g.config.OutputDir, 0o755); err != nil {
		return fmt.Errorf("failed to create output directory: %w", err)
	}

	// Build template data
	data := g.buildTemplateData(sm)

	// Generate each file
	files := []struct {
		name     string
		template string
	}{
		{fmt.Sprintf("%s_phases.go", camelToSnake(sm.Metadata.Name)), "phases.go.tmpl"},
		{fmt.Sprintf("%s_types.go", camelToSnake(sm.Metadata.Name)), "types.go.tmpl"},
		{fmt.Sprintf("%s_calculator.go", camelToSnake(sm.Metadata.Name)), "calculator.go.tmpl"},
		{fmt.Sprintf("%s_transitions.go", camelToSnake(sm.Metadata.Name)), "transitions.go.tmpl"},
		{fmt.Sprintf("%s_visit.go", camelToSnake(sm.Metadata.Name)), "visit.go.tmpl"},
		{fmt.Sprintf("%s_observability.go", camelToSnake(sm.Metadata.Name)), "observability.go.tmpl"},
		{fmt.Sprintf("%s_status.go", camelToSnake(sm.Metadata.Name)), "status.go.tmpl"},
	}

	for _, f := range files {
		if err := g.generateFile(f.name, f.template, data); err != nil {
			return fmt.Errorf("failed to generate %s: %w", f.name, err)
		}
	}

	// Conditionally generate metrics file when observability.metrics is enabled
	if sm.Observability.Metrics {
		metricsFile := fmt.Sprintf("%s_metrics.go", camelToSnake(sm.Metadata.Name))
		if err := g.generateFile(metricsFile, "metrics.go.tmpl", data); err != nil {
			return fmt.Errorf("failed to generate %s: %w", metricsFile, err)
		}
	}

	return nil
}

// generateFile generates a single file from a template.
func (g *Generator) generateFile(name, tmplName string, data *TemplateData) error {
	var buf bytes.Buffer

	if err := g.templates.ExecuteTemplate(&buf, tmplName, data); err != nil {
		return fmt.Errorf("template execution failed: %w", err)
	}

	// Format the Go code
	formatted, err := format.Source(buf.Bytes())
	if err != nil {
		// Write unformatted for debugging
		path := filepath.Join(g.config.OutputDir, name+".unformatted")
		os.WriteFile(path, buf.Bytes(), 0o644)
		return fmt.Errorf("go format failed (unformatted written to %s): %w", path, err)
	}

	// Write the file
	path := filepath.Join(g.config.OutputDir, name)
	if err := os.WriteFile(path, formatted, 0o644); err != nil {
		return fmt.Errorf("failed to write file: %w", err)
	}

	return nil
}

// TemplateData contains all data needed for code generation templates.
type TemplateData struct {
	// Package name for generated code
	Package string

	// Resource name (e.g., "CloudflareTunnel")
	Name string

	// API group (e.g., "cloudflare.io")
	Group string

	// API version (e.g., "v1alpha1")
	Version string

	// Import path for API types
	APIImportPath string

	// States in the state machine
	States []StateData

	// Field groups
	FieldGroups []FieldGroupData

	// Transitions organized by source state
	TransitionsByState map[string][]TransitionData

	// All transitions
	Transitions []TransitionData

	// Guards
	Guards map[string]schema.Guard

	// Observability config
	Observability schema.Observability

	// ErrorHandling config
	ErrorHandling *schema.ErrorHandling

	// SpecChangeHandling config
	SpecChangeHandling *schema.SpecChangeHandling

	// Initial state name
	InitialState string

	// HasDeletionStates is true when the spec defines any deletion states.
	HasDeletionStates bool

	// Status field configuration
	PhaseField      string
	ConditionsField string
}

// StateData contains data for a single state.
type StateData struct {
	Name        string
	Initial     bool
	Terminal    bool
	Error       bool
	Deletion    bool
	Generated   bool
	Requeue     schema.Duration
	Fields      []FieldData
	FieldGroups []string

	// Requires lists optional field-group fields this state validates as set.
	Requires []string
}

// FieldData contains data for a single field.
type FieldData struct {
	Name string
	Type string

	// Optional fields (declared with a "?" type suffix, e.g. "string?") are
	// skipped by group-level Validate; states that need them list the field
	// in `requires` to get a state-scoped check.
	Optional bool
}

// FieldGroupData contains data for a field group.
type FieldGroupData struct {
	Name   string
	Fields []FieldData
}

// TransitionData contains data for a single transition.
type TransitionData struct {
	From   []string
	To     string
	Action string
	Params []FieldData
	Guard  string

	// Inits describes how to build the target state's struct literal for a
	// specific source state: params grouped into the field groups the target
	// embeds, field groups carried forward from the source state, and any
	// remaining direct fields. Populated per (from, to) pair in
	// TransitionsByState; empty in the flat Transitions slice.
	Inits []TransitionInit
}

// TransitionInit is one entry in a generated state struct literal.
type TransitionInit struct {
	// Group is the Go name of an embedded field group (e.g. "ResolveResult").
	// Empty for a direct field entry.
	Group string

	// Fields maps group (or direct) fields to the transition parameters that
	// populate them, in emission order.
	Fields []InitField

	// Carry means the whole group is copied from the source state
	// (the source embeds the same group and no params populate it).
	Carry bool
}

// InitField pairs a Go field name with the parameter variable that sets it.
type InitField struct {
	Field string
	Param string
}

func (g *Generator) buildTemplateData(sm *schema.StateMachine) *TemplateData {
	data := &TemplateData{
		Package:            g.config.Package,
		Name:               sm.Metadata.Name,
		Group:              sm.Metadata.Group,
		Version:            sm.Metadata.Version,
		APIImportPath:      g.config.APIImportPath,
		Guards:             sm.Guards,
		Observability:      sm.Observability,
		ErrorHandling:      sm.ErrorHandling,
		SpecChangeHandling: sm.SpecChangeHandling,
		PhaseField:         sm.Status.PhaseField,
		ConditionsField:    sm.Status.ConditionsField,
		TransitionsByState: make(map[string][]TransitionData),
	}

	// ErrorHandling is intentionally NOT defaulted: when the spec omits it,
	// templates emit requeue-based retry helpers instead of retry-count
	// exponential backoff. See transitions.go.tmpl.

	// Apply defaults for SpecChangeHandling when enabled but field not specified
	if data.SpecChangeHandling != nil && data.SpecChangeHandling.Enabled {
		if data.SpecChangeHandling.ObservedGenerationField == "" {
			data.SpecChangeHandling.ObservedGenerationField = "observedGeneration"
		}
	}

	if data.PhaseField == "" {
		data.PhaseField = "phase"
	}
	if data.ConditionsField == "" {
		data.ConditionsField = "conditions"
	}

	// Convert field groups. Maps have no iteration order, so sort groups and
	// their fields by name — codegen must be byte-stable across runs for the
	// committed-output drift check to work.
	for name, group := range sm.FieldGroups {
		fg := FieldGroupData{Name: name}
		for fieldName, fieldType := range group {
			fg.Fields = append(fg.Fields, newFieldData(fieldName, fieldType))
		}
		sortFields(fg.Fields)
		data.FieldGroups = append(data.FieldGroups, fg)
	}
	sort.Slice(data.FieldGroups, func(i, j int) bool {
		return data.FieldGroups[i].Name < data.FieldGroups[j].Name
	})

	// Convert states
	for _, s := range sm.States {
		if s.Initial {
			data.InitialState = s.Name
		}

		// Build a set of fields that are in the field groups THIS STATE embeds
		embeddedFields := make(map[string]bool)
		for _, groupName := range s.FieldGroups {
			if group, ok := sm.FieldGroups[groupName]; ok {
				for fieldName := range group {
					embeddedFields[fieldName] = true
				}
			}
		}

		sd := StateData{
			Name:        s.Name,
			Initial:     s.Initial,
			Terminal:    s.Terminal,
			Error:       s.Error,
			Deletion:    s.Deletion,
			Generated:   s.Generated,
			Requeue:     s.Requeue,
			FieldGroups: s.FieldGroups,
			Requires:    s.Requires,
		}
		if s.Deletion {
			data.HasDeletionStates = true
		}

		// Only add fields that are not in embedded field groups for THIS state
		for fieldName, fieldType := range s.Fields {
			if !embeddedFields[fieldName] {
				sd.Fields = append(sd.Fields, newFieldData(fieldName, fieldType))
			}
		}
		sortFields(sd.Fields)

		data.States = append(data.States, sd)
	}

	// Add Unknown state if not defined
	hasUnknown := false
	for _, s := range data.States {
		if s.Name == "Unknown" {
			hasUnknown = true
			break
		}
	}
	if !hasUnknown {
		data.States = append(data.States, StateData{
			Name:      "Unknown",
			Error:     true,
			Generated: true,
			Fields: []FieldData{
				{Name: "observedPhase", Type: "string"},
			},
		})
	}

	// Convert transitions
	statesByName := make(map[string]StateData, len(data.States))
	for _, s := range data.States {
		statesByName[s.Name] = s
	}
	groupsByName := make(map[string]FieldGroupData, len(data.FieldGroups))
	for _, g := range data.FieldGroups {
		groupsByName[g.Name] = g
	}

	for _, t := range sm.Transitions {
		td := TransitionData{
			From:   t.From.States,
			To:     t.To,
			Action: t.Action,
			Guard:  t.Guard,
		}

		for _, p := range t.Params {
			// newFieldData strips a "?" suffix so an optional group field
			// used as a param still yields a valid Go type in the signature.
			td.Params = append(td.Params, newFieldData(p.Name, p.Type))
		}

		data.Transitions = append(data.Transitions, td)

		// Organize by source state, computing the target struct literal for
		// each (from, to) pair — params grouped into the target's embedded
		// field groups, shared groups carried forward from the source.
		for _, from := range t.From.States {
			td := td
			td.Inits = buildTransitionInits(td, statesByName[from], statesByName[td.To], groupsByName)
			data.TransitionsByState[from] = append(data.TransitionsByState[from], td)
		}
	}

	return data
}

// buildTransitionInits computes the struct literal entries for a transition's
// target state. Group entries come first in the target's declared group
// order, then any params that are direct (non-group) fields, in param order.
func buildTransitionInits(t TransitionData, from, to StateData, groups map[string]FieldGroupData) []TransitionInit {
	paramNames := make(map[string]bool, len(t.Params))
	for _, p := range t.Params {
		paramNames[p.Name] = true
	}
	usedParams := make(map[string]bool, len(t.Params))

	var inits []TransitionInit
	for _, groupName := range to.FieldGroups {
		group := groups[groupName]

		var fields []InitField
		for _, f := range group.Fields {
			if paramNames[f.Name] {
				fields = append(fields, InitField{Field: f.Name, Param: f.Name})
				usedParams[f.Name] = true
			}
		}

		switch {
		case len(fields) > 0:
			// Params populate the group; uncovered fields stay zero.
			inits = append(inits, TransitionInit{Group: groupName, Fields: fields})
		case contains(from.FieldGroups, groupName):
			// No params for a group both states embed: carry it forward so
			// data like resolve results survives intermediate transitions.
			inits = append(inits, TransitionInit{Group: groupName, Carry: true})
		}
		// Otherwise the group starts zero-valued.
	}

	for _, p := range t.Params {
		if !usedParams[p.Name] {
			inits = append(inits, TransitionInit{Fields: []InitField{{Field: p.Name, Param: p.Name}}})
		}
	}

	return inits
}

// newFieldData parses a spec field declaration. A "?" type suffix
// (e.g. "string?") marks the field optional for group-level validation.
func newFieldData(name, fieldType string) FieldData {
	optional := strings.HasSuffix(fieldType, "?")
	return FieldData{
		Name:     name,
		Type:     strings.TrimSuffix(fieldType, "?"),
		Optional: optional,
	}
}

// sortFields orders fields by name so generated output is deterministic.
func sortFields(fields []FieldData) {
	sort.Slice(fields, func(i, j int) bool { return fields[i].Name < fields[j].Name })
}

// Helper functions for templates

func camelToSnake(s string) string {
	var result strings.Builder
	for i, r := range s {
		if i > 0 && r >= 'A' && r <= 'Z' {
			result.WriteRune('_')
		}
		result.WriteRune(r)
	}
	return strings.ToLower(result.String())
}

func toEventName(action string) string {
	var result strings.Builder
	for i, r := range action {
		if i > 0 && r >= 'A' && r <= 'Z' {
			result.WriteRune('_')
		}
		if r >= 'a' && r <= 'z' {
			result.WriteRune(r - 32)
		} else {
			result.WriteRune(r)
		}
	}
	return result.String()
}

func goType(t string) string {
	switch t {
	case "string", "int", "int32", "int64", "bool", "float32", "float64":
		return t
	default:
		return t
	}
}

func defaultValue(t string) string {
	switch t {
	case "string":
		return `""`
	case "int", "int32", "int64":
		return "0"
	case "bool":
		return "false"
	case "float32", "float64":
		return "0.0"
	default:
		return "nil"
	}
}

func hasRequeue(s StateData) bool {
	return s.Requeue.Duration > 0
}

func durationLiteral(d schema.Duration) string {
	if d.Duration == 0 {
		return "0"
	}
	return fmt.Sprintf("time.Duration(%d)", d.Duration)
}

func fieldGroupName(name string) string {
	return strings.Title(name)
}

// hasFieldInGroup checks if a field is already defined in any of the field groups
func hasFieldInGroup(field FieldData, groups []FieldGroupData) bool {
	for _, g := range groups {
		for _, f := range g.Fields {
			if f.Name == field.Name {
				return true
			}
		}
	}
	return false
}

// contains checks if a string is in a slice
func contains(slice []string, str string) bool {
	for _, s := range slice {
		if s == str {
			return true
		}
	}
	return false
}
