package driver

import (
	"os"
	"strings"
	"testing"
)

// TestProcessEnvDefaultCarriesNoDaemonSecrets pins the launch environment: with no
// Env configured the VMM gets PATH and nothing else, so none of the daemon's
// EMBERVM_NODED_* secrets (bearer token, store credentials, restore capability
// key) can be read out of the Firecracker process by an escaped guest.
func TestProcessEnvDefaultCarriesNoDaemonSecrets(t *testing.T) {
	t.Setenv("EMBERVM_NODED_BEARER_TOKEN", "fleet-bearer")
	t.Setenv("EMBERVM_NODED_STORE_SECRET_ACCESS_KEY", "store-secret")
	t.Setenv("EMBERVM_NODED_RESTORE_CAPABILITY_KEY", "cap-key")

	env := (&ExecLauncher{}).processEnv()
	if len(env) != 1 || !strings.HasPrefix(env[0], "PATH=") {
		t.Fatalf("default launch env = %q, want exactly one PATH entry", env)
	}
	for _, kv := range env {
		if strings.Contains(kv, "EMBERVM_") || strings.Contains(kv, "fleet-bearer") ||
			strings.Contains(kv, "store-secret") || strings.Contains(kv, "cap-key") {
			t.Fatalf("default launch env leaks a daemon secret: %q", kv)
		}
	}
	// os.Environ still carries them: the point is that the child does not.
	if !strings.Contains(strings.Join(os.Environ(), "\n"), "EMBERVM_NODED_BEARER_TOKEN=fleet-bearer") {
		t.Fatal("test setup: os.Environ does not carry the bearer token")
	}
}

// TestProcessEnvConfiguredIsCopied asserts an explicit Env is passed through as a
// copy, so a caller mutating its slice after Launch cannot change the child's view.
func TestProcessEnvConfiguredIsCopied(t *testing.T) {
	configured := []string{"PATH=/bin", "EMBER_JAILER_TEST_BINARY=/x"}
	l := &ExecLauncher{Env: configured}
	got := l.processEnv()
	if strings.Join(got, ",") != strings.Join(configured, ",") {
		t.Fatalf("processEnv = %q, want %q", got, configured)
	}
	got[0] = "PATH=/mutated"
	if l.Env[0] != "PATH=/bin" {
		t.Fatal("processEnv returned the configured slice itself, not a copy")
	}
}
