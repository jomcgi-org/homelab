// Package betterleaks is the Betterleaks detector of the egress scanner. It
// embeds the Betterleaks library (module pinned in go.mod), loads its default
// rules plus extra_rules.toml, and makes no network call: the scan packages do
// none, and the validation and analysis packages are not imported.
//
// Three settings matter for a deny path, and a test pins each:
//
//   - the allow markers ("betterleaks:allow", "gitleaks:allow") are off, because
//     the input is untrusted and a marker next to a secret must not hide it;
//   - the decode depth is 3 and the host caps the input size, so decoding cannot
//     grow memory without a bound;
//   - the call takes the request context, which the library checks between rules.
package betterleaks

import (
	"context"
	_ "embed"
	"errors"

	blconfig "github.com/betterleaks/betterleaks/v2/config"
	"github.com/betterleaks/betterleaks/v2/report"
	blscan "github.com/betterleaks/betterleaks/v2/scan"
	"github.com/betterleaks/betterleaks/v2/sources"
	"github.com/jomcgi/homelab/projects/platform-factory/substrate/egress-scan/detect"
)

// Name is the registered name of the detector.
const Name = "betterleaks"

// MaxDecodeDepth is the number of decoding passes (base64, hex, percent,
// Unicode) that the detector allows. Zero would turn decoding off.
const MaxDecodeDepth = 3

//go:embed extra_rules.toml
var extraRules string

func init() {
	detect.Register(Name, func(detect.Config) (detect.Detector, error) { return New() })
}

// New builds the detector. It loads the rules, compiles every regex up front
// and scans a warm-up text, so that the first request after start does not pay
// for the compile and the tokenizer inside its deadline (about 1 s in all).
func New() (detect.Detector, error) {
	cfg, err := blconfig.ParseTOMLString(extraRules, "")
	if err != nil {
		return nil, err
	}
	s, err := blscan.New(cfg,
		blscan.WithMaxDecodeDepth(MaxDecodeDepth),
		// No signature: nothing in the input can ask the scanner to skip a line.
		blscan.WithAllowSignatures(),
		blscan.WithPrecompile(),
	)
	if err != nil {
		return nil, err
	}
	a := &adapter{s: s}
	_ = a.s.ScanString(warmUp)
	return a, nil
}

// warmUp makes every keyword group match once, so the lazy parts of the library
// (the tokenizer, the keyword matcher) are ready before the first request.
const warmUp = "token secret password key auth bearer credential authorization postgres://u:p@h/d curl -H"

// reportTag on a rule makes its findings class report: counted, never denied.
const reportTag = "egress:report"

func classOf(tags []string) detect.Class {
	for _, t := range tags {
		if t == reportTag {
			return detect.ClassReport
		}
	}
	return detect.ClassBlock
}

type adapter struct{ s *blscan.Scanner }

// whole yields the input as one fragment. sources.Reader cuts a stream into
// chunks of about 100,000 bytes and scans each alone, so a secret that crosses a
// cut is missed, and a body of padding decides where the cuts fall. The input
// is already capped by the host, so one fragment is bounded.
type whole struct{ text string }

func (w whole) Fragments(ctx context.Context, yield sources.FragmentsFunc) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	return yield(sources.Fragment{Raw: w.text}, nil)
}

func (a *adapter) Name() string { return Name }

var errFound = errors.New("finding")

func (a *adapter) Detect(ctx context.Context, in detect.Input) ([]detect.Finding, error) {
	var found []detect.Finding
	_, err := a.s.Scan(ctx, whole{text: string(in.Bytes)}, func(f report.Finding) error {
		// Only the rule ID and the class leave this function: not the match, not
		// the line.
		class := classOf(f.Tags)
		found = append(found, detect.Finding{RuleID: f.RuleID, Class: class, Start: detect.NoOffset, End: detect.NoOffset})
		if class == detect.ClassBlock {
			return errFound // the host denies on this one: stop
		}
		return nil // a report finding: keep looking for a block finding
	})
	for _, f := range found {
		if f.Class == detect.ClassBlock {
			return found, nil // a block finding is a verdict, whatever else happened
		}
	}
	if err != nil {
		if ctxErr := ctx.Err(); ctxErr != nil {
			return found, ctxErr
		}
		return found, errors.New("betterleaks scan failed")
	}
	return found, nil
}
