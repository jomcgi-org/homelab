package gitleaks

import (
	"context"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"

	"github.com/jomcgi/homelab/projects/platform-factory/substrate/egress-scan/detect"
	"github.com/jomcgi/homelab/projects/platform-factory/substrate/egress-scan/detect/detecttest"
)

// fragments builds the fake credential at test time: the repository holds no
// token-shaped text.
func fakeToken() string { return "gh" + "p_" + "8Jdm9u1Qa7bWdF2p7XZ9sWy7Qnf2gXMbRFqa" }

func TestConformance(t *testing.T) {
	token := fakeToken()
	detecttest.Conformance(t, Name, func(cfg detect.Config) (detect.Detector, error) {
		return detect.Detector(&adapter{m: mustMatcher(t, "")}), nil
	}, detecttest.Fixture{
		Clean: []string{
			"Remember to book the dentist on Tuesday and ask Sam about the garden plan.",
			"Authorization: Bearer $TOKEN",
			"export GITHUB_TOKEN=<your-token-here>",
		},
		Secret:      "token=" + token,
		SecretValue: token,
		Markers:     []string{"gitleaks:allow", "betterleaks:allow"},
	})
}

func mustMatcher(t *testing.T, path string) *requestDetector {
	t.Helper()
	m, err := newMatcher(path)
	if err != nil {
		t.Fatal(err)
	}
	return m
}

func TestOperatorRules(t *testing.T) {
	path := filepath.Join(t.TempDir(), "gitleaks.toml")
	const config = `title = "Operator test rules"

[[rules]]
id = "test-credential"
description = "Synthetic pattern added by operator"
regex = 'TEST_CREDENTIAL_[A-Z0-9]{12}'
`
	if err := os.WriteFile(path, []byte(config), 0o600); err != nil {
		t.Fatal(err)
	}
	detector, err := newMatcher(path)
	if err != nil {
		t.Fatal(err)
	}
	if rule, err := detector.find(context.Background(), "TEST_CREDENTIAL_AB12CD34EF56"); rule != "test-credential" || err != nil {
		t.Fatalf("configured rule did not match: %v", err)
	}
	if err := os.WriteFile(path, []byte(config+`path = '\.go$'`), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := newMatcher(path); err == nil {
		t.Fatal("configured a rule that requires a repository path")
	}
	if err := os.WriteFile(path, []byte("rules = []"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := newMatcher(path); err == nil {
		t.Fatal("configured an empty ruleset")
	}
}

func TestLineAllowlistUsesMatchedLines(t *testing.T) {
	path := filepath.Join(t.TempDir(), "gitleaks.toml")
	const config = `[[rules]]
id = 'synthetic-marker'
regex = '(?m)^MARK:[^\r\n]+\r?\n'
[[rules.allowlists]]
regexTarget = 'line'
regexes = ['approved-line']
`
	if err := os.WriteFile(path, []byte(config), 0o600); err != nil {
		t.Fatal(err)
	}
	detector, err := newMatcher(path)
	if err != nil {
		t.Fatal(err)
	}
	for _, endings := range []struct {
		name string
		text string
	}{{"LF", "\n"}, {"CRLF", "\r\n"}} {
		t.Run(endings.name, func(t *testing.T) {
			rule, err := detector.find(context.Background(), "MARK:example"+endings.text+"approved-line")
			if err != nil || rule == "" {
				t.Fatalf("a marker on the next line suppressed the match: %v", err)
			}
			rule, err = detector.find(context.Background(), "MARK:example approved-line"+endings.text+"next line")
			if err != nil || rule != "" {
				t.Fatalf("a marker on the matched line did not suppress the match: %v", err)
			}
		})
	}
}

func TestRegexReaderStopsAfterCancellation(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	reader := newScanReader(ctx, strings.Repeat("abcd", 1024)+"MARK:other")
	if _, _, err := reader.ReadRune(); err != nil {
		cancel()
		t.Fatal(err)
	}
	cancel()
	match := regexp.MustCompile(`MARK:[a-z]+`).FindReaderIndex(reader)
	if match != nil || reader.count > 256 {
		t.Fatal("regex kept reading input after cancellation")
	}
}
