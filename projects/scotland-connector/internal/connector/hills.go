package connector

import (
	"fmt"
	"strings"
	"unicode/utf8"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/walks"
)

type HillFilter struct {
	HillClass string `json:"hill_class,omitempty"`
	HillName  string `json:"hill_name,omitempty"`
}

func hillFilter(f HillFilter) (HillFilter, error) {
	out := HillFilter{}
	if f.HillClass != "" {
		for _, c := range walks.HillClasses {
			if strings.EqualFold(strings.TrimSpace(f.HillClass), c) {
				out.HillClass = c
			}
		}
		if out.HillClass == "" {
			return out, &ParamError{"hill_class", "one of Munro, Munro Top, Corbett, Graham, Donald"}
		}
	}
	if f.HillName != "" {
		out.HillName = walks.Normalize(f.HillName)
		if utf8.RuneCountInString(f.HillName) > MaxNameLength || out.HillName == "" {
			return out, &ParamError{"hill_name", "1 to 100 characters with letters or digits"}
		}
	}
	return out, nil
}

func (f HillFilter) active() bool { return f.HillClass != "" || f.HillName != "" }
func (f HillFilter) matches(w *walks.Walk) bool {
	if !f.active() {
		return true
	}
	for _, h := range w.Hills {
		if (f.HillClass == "" || f.HillClass == h.Class) && (f.HillName == "" || walks.Normalize(h.Name) == f.HillName) {
			return true
		}
	}
	return false
}

func (f HillFilter) available(wk *walks.Dataset) error {
	if f.active() && (wk == nil || wk.HillSource == nil) {
		return fmt.Errorf("%w: hill snapshot disabled or not loaded", ErrUnavailable)
	}
	return nil
}

func (f HillFilter) filter(wk *walks.Dataset) *walks.Dataset {
	if !f.active() || wk == nil {
		return wk
	}
	out := *wk
	out.Walks = nil
	for _, w := range wk.Walks {
		if f.matches(&w) {
			out.Walks = append(out.Walks, w)
		}
	}
	return &out
}

func hillMeta(meta *Meta, wk *walks.Dataset, options []WalkOption) {
	if wk == nil || wk.HillSource == nil {
		return
	}
	src := wk.HillSource
	meta.Sources = append(meta.Sources, Source{Name: src.Name, Attribution: src.Attribution, License: src.License, URL: src.URL, Use: "Hill classifications and summit matches", Version: src.Version, Modifications: src.Modifications})
	for _, o := range options {
		if len(o.Hills) > 0 && o.hillsInferred {
			meta.Notes = append(meta.Notes, walks.HillInferenceNote)
			break
		}
	}
}
