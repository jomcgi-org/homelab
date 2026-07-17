//go:build linux

package main

import (
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// writePgHba writes a minimal pg_hba.conf (the lines initdb produces) into a
// fresh PGDATA dir and returns the file path.
func writePgHba(t *testing.T, dir string) string {
	t.Helper()
	hba := filepath.Join(dir, "pg_hba.conf")
	initdbDefault := "" +
		"local   all             all                                     trust\n" +
		"host    all             all             127.0.0.1/32            scram-sha-256\n" +
		"host    all             all             ::1/128                 scram-sha-256\n"
	if err := os.WriteFile(hba, []byte(initdbDefault), 0o600); err != nil {
		t.Fatalf("seed pg_hba.conf: %v", err)
	}
	return hba
}

// TestEnsureHostAuthAppendsRule: a freshly-initdb'd pg_hba (no cluster rule) gets
// the cluster-internal scram block appended.
func TestEnsureHostAuthAppendsRule(t *testing.T) {
	dir := t.TempDir()
	hba := writePgHba(t, dir)

	if err := ensureHostAuth(slog.Default(), dir); err != nil {
		t.Fatalf("ensureHostAuth: %v", err)
	}

	got, err := os.ReadFile(hba)
	if err != nil {
		t.Fatalf("read pg_hba.conf: %v", err)
	}
	if !strings.Contains(string(got), hbaRuleMarker) {
		t.Errorf("marker not appended; pg_hba.conf:\n%s", got)
	}
	if !strings.Contains(string(got), "host all all 0.0.0.0/0 scram-sha-256") {
		t.Errorf("IPv4 rule not appended; pg_hba.conf:\n%s", got)
	}
}

// TestEnsureHostAuthIdempotent: running it twice (the every-boot call path) does
// NOT duplicate the block, so a healthy volume that already has the rule is left
// with exactly one copy across repeated cold boots.
func TestEnsureHostAuthIdempotent(t *testing.T) {
	dir := t.TempDir()
	hba := writePgHba(t, dir)

	for i := 0; i < 3; i++ {
		if err := ensureHostAuth(slog.Default(), dir); err != nil {
			t.Fatalf("ensureHostAuth pass %d: %v", i, err)
		}
	}

	got, err := os.ReadFile(hba)
	if err != nil {
		t.Fatalf("read pg_hba.conf: %v", err)
	}
	if n := strings.Count(string(got), hbaRuleMarker); n != 1 {
		t.Errorf("marker appears %d times, want exactly 1; pg_hba.conf:\n%s", n, got)
	}
}

// TestEnsureHostAuthSelfHealsPoisonedVolume is the regression this fix exists
// for: a volume whose first boot was interrupted after initdb wrote PG_VERSION
// but before the cluster rule was appended (so pg_hba has only the initdb
// defaults and no marker). The next cold boot's unconditional call must add the
// rule so the cluster can connect again, rather than skipping config forever
// because PG_VERSION already exists.
func TestEnsureHostAuthSelfHealsPoisonedVolume(t *testing.T) {
	dir := t.TempDir()
	hba := writePgHba(t, dir) // initdb defaults only: the poisoned-volume state

	if strings.Contains(mustRead(t, hba), hbaRuleMarker) {
		t.Fatalf("precondition failed: seed should not contain the marker")
	}

	if err := ensureHostAuth(slog.Default(), dir); err != nil {
		t.Fatalf("ensureHostAuth: %v", err)
	}

	if !strings.Contains(mustRead(t, hba), "host all all 0.0.0.0/0 scram-sha-256") {
		t.Errorf("poisoned volume was not healed; pg_hba.conf:\n%s", mustRead(t, hba))
	}
}

func mustRead(t *testing.T, path string) string {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read %s: %v", path, err)
	}
	return string(b)
}
