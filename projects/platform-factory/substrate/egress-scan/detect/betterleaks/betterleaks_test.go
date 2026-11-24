package betterleaks

import (
	"context"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/platform-factory/substrate/egress-scan/detect"
	"github.com/jomcgi/homelab/projects/platform-factory/substrate/egress-scan/detect/detecttest"
)

func fakeToken() string { return "gh" + "p_" + "8Jdm9u1Qa7bWdF2p7XZ9sWy7Qnf2gXMbRFqa" }

func build(t *testing.T) detect.Detector {
	t.Helper()
	d, err := New()
	if err != nil {
		t.Fatal(err)
	}
	return d
}

func TestConformance(t *testing.T) {
	token := fakeToken()
	detecttest.Conformance(t, Name, func(detect.Config) (detect.Detector, error) { return New() }, detecttest.Fixture{
		Clean: []string{
			"Remember to book the dentist on Tuesday and ask Sam about the garden plan.",
			"Authorization: Bearer $TOKEN",
			"export GITHUB_TOKEN=<your-token-here>",
		},
		Secret:      "token=" + token,
		SecretValue: token,
		Markers:     []string{"betterleaks:allow", "gitleaks:allow"},
	})
}

// A marker next to a secret must not hide it. WithAllowSignatures with no
// argument turns the markers off: without it the library skips the line.
func TestAllowMarkersDoNotHideASecret(t *testing.T) {
	d := build(t)
	token := fakeToken()
	for _, marker := range []string{"betterleaks:allow", "gitleaks:allow"} {
		for _, text := range []string{
			"token=" + token + " # " + marker,
			"// " + marker + "\ntoken=" + token,
			"token=" + token + "\n# " + marker,
		} {
			found, err := d.Detect(context.Background(), detect.Input{Bytes: []byte(text)})
			if err != nil || len(found) == 0 {
				t.Errorf("marker %q hid the finding in %.30q (err %v)", marker, text, err)
			}
		}
	}
}

// The default rules and the extra rules find the fake corpus. Encoded samples
// need the decoder, so they pin MaxDecodeDepth too.
func TestCorpus(t *testing.T) {
	d := build(t)
	// What the detector is known to miss (DESIGN-detectors.md).
	gaps := map[string]bool{"gap-split-over-lines": true, "gap-gzip-base64": true}
	for _, s := range detecttest.Corpus(20261124) {
		found, err := d.Detect(context.Background(), detect.Input{Bytes: []byte(s.Text)})
		if err != nil {
			t.Fatalf("%s: %v", s.Name, err)
		}
		switch {
		case gaps[s.Name]:
			if len(found) > 0 {
				t.Errorf("%s: a known gap is now found; move it out of the gap list", s.Name)
			}
		case s.Secret && len(found) == 0:
			t.Errorf("%s: secret not found", s.Name)
		case !s.Secret && len(found) > 0:
			t.Errorf("%s: false positive %s", s.Name, found[0].RuleID)
		}
	}
}

func TestDecodingIsOnAndBounded(t *testing.T) {
	if MaxDecodeDepth != 3 {
		t.Fatalf("decode depth is %d, the design says 3", MaxDecodeDepth)
	}
	d := build(t)
	for _, s := range detecttest.Corpus(7) {
		if s.Secret && s.Encoded && s.Name != "gap-gzip-base64" {
			if found, _ := d.Detect(context.Background(), detect.Input{Bytes: []byte(s.Text)}); len(found) == 0 {
				t.Errorf("%s: an encoded secret was not decoded", s.Name)
			}
		}
	}
}

// A body at the size cap that makes every rule run cannot hold the call past
// the deadline: the host fails it (deny with failure mode deny).
func TestHeavyBodyAtTheCapFailsAtTheDeadline(t *testing.T) {
	d := build(t)
	cfg := detect.Config{Name: Name, Mode: detect.ModeEnforce, FailureMode: detect.FailDeny,
		Limits: detect.Limits{MaxInputBytes: 512 << 10, Deadline: 75 * time.Millisecond}}
	h := detect.NewHost(d, cfg)
	text := make([]byte, 0, 512<<10)
	const line = "export API_KEY=${API_KEY} # token secret password key auth bearer credential\nsecret_name: app-token, password_hash: 5f4dcc3b5aa765d61d8327deb882cf99, key_id=a1b2c3\n"
	for len(text) < 512<<10 {
		text = append(text, line...)
	}
	start := time.Now()
	v := h.Scan(context.Background(), detect.Input{Bytes: text[:512<<10]})
	if took := time.Since(start); took > 400*time.Millisecond {
		t.Errorf("the host held the call for %s", took)
	}
	if v.Outcome == detect.OutcomeClean {
		t.Skip("the machine scanned 512 KiB of keyword-heavy text inside 75 ms")
	}
	if v.Outcome != detect.OutcomeFailed || !v.Deny {
		t.Errorf("heavy body: %s", v)
	}
}
