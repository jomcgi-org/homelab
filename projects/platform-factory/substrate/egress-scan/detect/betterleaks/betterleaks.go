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
	"bytes"
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

// New builds the detector. It compiles the rules, which takes about 60 ms.
func New() (detect.Detector, error) {
	cfg, err := blconfig.ParseTOMLString(extraRules, "")
	if err != nil {
		return nil, err
	}
	s, err := blscan.New(cfg,
		blscan.WithMaxDecodeDepth(MaxDecodeDepth),
		// No signature: nothing in the input can ask the scanner to skip a line.
		blscan.WithAllowSignatures(),
	)
	if err != nil {
		return nil, err
	}
	return &adapter{s: s}, nil
}

type adapter struct{ s *blscan.Scanner }

func (a *adapter) Name() string { return Name }

var errFound = errors.New("finding")

func (a *adapter) Detect(ctx context.Context, in detect.Input) ([]detect.Finding, error) {
	var found []detect.Finding
	_, err := a.s.Scan(ctx, &sources.Reader{Content: bytes.NewReader(in.Bytes)}, func(f report.Finding) error {
		// Only the rule ID leaves this function: not the match, not the line.
		found = append(found, detect.Finding{RuleID: f.RuleID, Class: detect.ClassBlock, Start: detect.NoOffset, End: detect.NoOffset})
		return errFound
	})
	if len(found) > 0 {
		return found, nil
	}
	if err != nil {
		if ctxErr := ctx.Err(); ctxErr != nil {
			return nil, ctxErr
		}
		return nil, errors.New("betterleaks scan failed")
	}
	return nil, nil
}
