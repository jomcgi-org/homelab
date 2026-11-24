// Package gitleaks is the Gitleaks detector of the egress scanner. It reads the
// Gitleaks rules pinned in go.mod (or an operator file) and matches them with
// the scanner's own matcher, which a context and a work budget bound. It does
// not use the Gitleaks "allow" markers: the input is untrusted.
package gitleaks

import (
	"context"
	"errors"
	"io"
	"strings"

	"github.com/jomcgi/homelab/projects/platform-factory/substrate/egress-scan/detect"
	"github.com/spf13/viper"
	"github.com/zricethezav/gitleaks/v8/config"
	"github.com/zricethezav/gitleaks/v8/logging"
)

// Name is the registered name of the detector.
const Name = "gitleaks"

// OptionRules is the Config.Options key of an operator rule file (TOML) that
// replaces the embedded rules. It must come from a trusted mount.
const OptionRules = "rules"

func init() {
	// Library diagnostics can contain source-derived fields.
	logging.Logger = logging.Logger.Output(io.Discard)
	detect.Register(Name, func(cfg detect.Config) (detect.Detector, error) {
		m, err := newMatcher(cfg.Options[OptionRules])
		if err != nil {
			return nil, errors.New("cannot load secret scan rules")
		}
		return &adapter{m: m}, nil
	})
}

type adapter struct{ m *requestDetector }

func (a *adapter) Name() string { return Name }

func (a *adapter) Detect(ctx context.Context, in detect.Input) ([]detect.Finding, error) {
	rule, err := a.m.find(ctx, in.Text())
	if errors.Is(err, errScanBudget) {
		return nil, detect.ErrBudget
	}
	if err != nil {
		return nil, err
	}
	if rule == "" {
		return nil, nil
	}
	return []detect.Finding{{RuleID: rule, Class: detect.ClassBlock, Start: detect.NoOffset, End: detect.NoOffset}}, nil
}

func newMatcher(path string) (*requestDetector, error) {
	loader := viper.New()
	loader.SetConfigType("toml")
	var err error
	if path == "" {
		err = loader.ReadConfig(strings.NewReader(config.DefaultConfig))
	} else {
		loader.SetConfigFile(path)
		err = loader.ReadInConfig()
	}
	if err != nil {
		return nil, err
	}
	var raw config.ViperConfig
	if err := loader.Unmarshal(&raw); err != nil {
		return nil, err
	}
	if raw.Extend.Path != "" || raw.Extend.URL != "" {
		return nil, errors.New("request rules can extend the embedded rules only")
	}
	for _, rule := range raw.Rules {
		if path != "" && rule.Path != "" {
			return nil, errors.New("operator request rules cannot require a file path")
		}
	}
	rules, err := raw.Translate()
	if err != nil {
		return nil, err
	}
	return compileRequestRules(rules)
}
