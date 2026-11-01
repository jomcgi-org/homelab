package main

import (
	"bytes"
	"encoding/base64"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// These tests pin the local setter's application semantics. No test relies on
// map iteration order, and every environment change is restored by testing.T.
func TestSetMmdsEnvDuplicatesAndFailures(t *testing.T) {
	const key = "BOOTENV_TEST_SECRET"
	const other = "BOOTENV_TEST_CONTINUATION"
	const first = "synthetic-first-secret"
	const last = "synthetic-last-secret"
	const nul = "synthetic-nul-secret\x00suffix"
	const continued = "synthetic-continuation-secret"
	enc := func(s string) string { return base64.RawURLEncoding.EncodeToString([]byte(s)) }
	token := func(s string) string { return "ember.env." + key + "=" + enc(s) }
	for _, tc := range []struct {
		name, cmdline, want string
		failure, malformed  bool
	}{
		{name: "successful duplicate", cmdline: token(first) + " " + token(last), want: last},
		{name: "malformed duplicate", cmdline: token(first) + " ember.env." + key + "=%%%", want: first, malformed: true},
		{name: "empty duplicate", cmdline: token(first) + " ember.env." + key + "=", want: first},
		{name: "valid then NUL duplicate", cmdline: token(first) + " " + token(nul), want: "prior", failure: true},
		{name: "NUL then valid duplicate", cmdline: token(nul) + " " + token(last), want: last, failure: false},
		{name: "failed Setenv continues", cmdline: token(nul), want: "prior", failure: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv(key, "prior")
			t.Setenv(other, "prior-other")
			path := filepath.Join(t.TempDir(), "cmdline")
			cmdline := tc.cmdline + " ember.env." + other + "=" + enc(continued) + " ember.env.bad-key=" + enc(last)
			if err := os.WriteFile(path, []byte(cmdline), 0o600); err != nil {
				t.Fatal(err)
			}
			previous := procCmdlinePath
			procCmdlinePath = path
			t.Cleanup(func() { procCmdlinePath = previous })
			var logs bytes.Buffer
			setMmdsEnv(slog.New(slog.NewTextHandler(&logs, nil)))
			if got := os.Getenv(key); got != tc.want {
				t.Fatalf("setter result = %q, want %q", got, tc.want)
			}
			if got := os.Getenv(other); got != continued {
				t.Fatalf("setter did not continue: %q", got)
			}
			for _, secret := range []string{first, last, nul, "synthetic-nul-secret", continued, enc(first), enc(last), enc(nul), enc(continued)} {
				if strings.Contains(logs.String(), secret) {
					t.Error("synthetic secret leaked in logs")
				}
			}
			if got := strings.Contains(logs.String(), "could not set mmds env var"); got != tc.failure {
				t.Errorf("failure diagnostic present = %v, want %v", got, tc.failure)
			}
			if !strings.Contains(logs.String(), "stateful first-boot env: set from mmds_env boot-args") {
				t.Error("runtime success diagnostic changed")
			}
		})
	}
}
