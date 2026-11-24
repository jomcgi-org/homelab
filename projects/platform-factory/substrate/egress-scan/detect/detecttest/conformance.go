// Package detecttest is the conformance test every detector must pass. A new
// detector calls Conformance from its own test with a fixture of its own.
package detecttest

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"log"
	"log/slog"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/platform-factory/substrate/egress-scan/detect"
)

// Fixture holds the inputs a detector is tested with. Build every secret-shaped
// value at test time from fragments: the repository must not hold a token.
type Fixture struct {
	// Clean inputs must give no ClassBlock finding. Include prose, code and a
	// placeholder such as "Authorization: Bearer $TOKEN".
	Clean []string
	// Secret is an input that must give a ClassBlock finding, and SecretValue is
	// the part of it that must never appear in a finding, a log or an error.
	Secret      string
	SecretValue string
	// Markers are strings that a text can carry to ask a detector to skip a
	// finding ("gitleaks:allow"). A secret next to any of them must still be
	// found: the input is untrusted.
	Markers []string
}

// Build makes a detector from a Config. Conformance sets the Name.
type Build func(cfg detect.Config) (detect.Detector, error)

func config(name string) detect.Config {
	return detect.Config{
		Name:        name,
		Mode:        detect.ModeEnforce,
		FailureMode: detect.FailDeny,
		Limits:      detect.Limits{MaxInputBytes: 512 << 10, Deadline: 5 * time.Second},
	}
}

func input(s string) detect.Input {
	return detect.Input{Bytes: []byte(s), Source: "body"}
}

// Conformance runs the table every detector must pass.
func Conformance(t *testing.T, name string, build Build, fx Fixture) {
	t.Helper()
	cfg := config(name)
	det, err := build(cfg)
	if err != nil {
		t.Fatalf("build %s: %v", name, err)
	}
	if det.Name() != name {
		t.Fatalf("Name() = %q, want the registered name %q", det.Name(), name)
	}
	host := func(c detect.Config) *detect.Host { return detect.NewHost(det, c) }

	t.Run("clean input is allowed", func(t *testing.T) {
		for _, text := range fx.Clean {
			v := host(cfg).Scan(context.Background(), input(text))
			if v.Outcome != detect.OutcomeClean || v.Deny {
				t.Errorf("clean input %.40q: %s", text, v)
			}
		}
	})

	t.Run("a finding denies and names no text", func(t *testing.T) {
		var logs bytes.Buffer
		restore := captureLogs(&logs)
		defer restore()
		v := host(cfg).Scan(context.Background(), input(fx.Secret))
		if v.Outcome != detect.OutcomeBlocked || !v.Deny || len(v.Findings) == 0 {
			t.Fatalf("secret: %s", v)
		}
		for _, f := range v.Findings {
			if f.Detector != name || f.RuleID == "" || (f.Class != detect.ClassBlock && f.Class != detect.ClassReport) {
				t.Errorf("finding has no detector, rule or class: %+v", f)
			}
		}
		if strings.Contains(fmt.Sprintf("%+v %v", v, v), fx.SecretValue) {
			t.Error("the verdict holds the matched text")
		}
		if strings.Contains(logs.String(), fx.SecretValue) {
			t.Error("a log line holds the matched text")
		}
	})

	t.Run("an allow marker does not suppress a finding", func(t *testing.T) {
		for _, marker := range fx.Markers {
			for _, text := range []string{fx.Secret + " # " + marker, "# " + marker + "\n" + fx.Secret} {
				if v := host(cfg).Scan(context.Background(), input(text)); !v.Deny {
					t.Errorf("marker %q suppressed the finding: %s", marker, v)
				}
			}
		}
	})

	t.Run("observe mode never denies", func(t *testing.T) {
		c := cfg
		c.Mode = detect.ModeObserve
		v := host(c).Scan(context.Background(), input(fx.Secret))
		if v.Outcome != detect.OutcomeBlocked || v.Deny {
			t.Errorf("observe mode: %s", v)
		}
	})

	t.Run("input over the limit is a failure, never a skip", func(t *testing.T) {
		c := cfg
		c.Limits.MaxInputBytes = len(fx.Secret) - 1
		v := host(c).Scan(context.Background(), input(fx.Secret))
		if v.Outcome != detect.OutcomeFailed || v.Failure != detect.FailureOversize || !v.Deny {
			t.Errorf("oversize with failure mode deny: %s", v)
		}
		c.FailureMode = detect.FailAllow
		v = host(c).Scan(context.Background(), input(fx.Secret))
		if v.Outcome != detect.OutcomeFailed || v.Deny {
			t.Errorf("oversize with failure mode allow: %s", v)
		}
	})

	t.Run("the deadline is enforced", func(t *testing.T) {
		c := cfg
		c.Limits.Deadline = time.Millisecond
		start := time.Now()
		v := host(c).Scan(context.Background(), input(heavy(1<<20)))
		if v.Outcome == detect.OutcomeClean {
			t.Skip("the detector finished a 1 MiB input inside 1ms; no deadline to test")
		}
		if v.Outcome != detect.OutcomeFailed || !v.Deny || time.Since(start) > time.Second {
			t.Errorf("deadline: %s after %s", v, time.Since(start))
		}
	})

	t.Run("a canceled caller is no verdict", func(t *testing.T) {
		ctx, cancel := context.WithCancel(context.Background())
		cancel()
		v := host(cfg).Scan(ctx, input(fx.Secret))
		if v.Canceled == nil || v.Deny {
			t.Errorf("canceled caller: %s", v)
		}
	})

	t.Run("the detector stops when its context ends", func(t *testing.T) {
		big := input(heavy(1 << 20))
		start := time.Now()
		_, _ = det.Detect(context.Background(), big)
		full := time.Since(start)
		if full < 20*time.Millisecond {
			t.Skip("the detector scans 1 MiB too fast to test cancellation")
		}
		ctx, cancel := context.WithTimeout(context.Background(), full/10)
		defer cancel()
		start = time.Now()
		_, err := det.Detect(ctx, big)
		if took := time.Since(start); took > full/2 {
			t.Errorf("Detect kept working after its context ended: %s of %s (err %v)", took, full, err)
		}
		if err != nil && !errors.Is(err, context.DeadlineExceeded) && !errors.Is(err, context.Canceled) && !errors.Is(err, detect.ErrBudget) {
			t.Errorf("Detect returned an unrelated error: %v", err)
		}
	})

	t.Run("concurrent calls agree", func(t *testing.T) {
		var wg sync.WaitGroup
		for i := range 16 {
			wg.Add(1)
			go func() {
				defer wg.Done()
				text, deny := fx.Secret, true
				if i%2 == 1 && len(fx.Clean) > 0 {
					text, deny = fx.Clean[i%len(fx.Clean)], false
				}
				if v := host(cfg).Scan(context.Background(), input(text)); v.Deny != deny {
					t.Errorf("concurrent call %d: %s", i, v)
				}
			}()
		}
		wg.Wait()
	})
}

// heavy builds keyword-rich text that makes a detector run many rules.
func heavy(n int) string {
	const line = "export API_KEY=${API_KEY} # token secret password key auth bearer credential\nsecret_name: app-token, password_hash: 5f4dcc3b5aa765d61d8327deb882cf99, key_id=a1b2c3\n"
	return strings.Repeat(line, n/len(line)+1)[:n]
}

func captureLogs(buf *bytes.Buffer) (restore func()) {
	oldLog, oldFlags := log.Writer(), log.Flags()
	oldSlog := slog.Default()
	log.SetOutput(buf)
	slog.SetDefault(slog.New(slog.NewTextHandler(buf, &slog.HandlerOptions{Level: slog.LevelDebug})))
	return func() {
		log.SetOutput(oldLog)
		log.SetFlags(oldFlags)
		slog.SetDefault(oldSlog)
	}
}
