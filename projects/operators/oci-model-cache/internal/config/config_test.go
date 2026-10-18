package config

import (
	"flag"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// newBoundConfig creates a Config, binds its flags to a fresh FlagSet, and
// parses the given args. It also clears any environment variables that would
// interfere, so tests are hermetic.
func newBoundConfig(t *testing.T, args []string, env map[string]string) Config {
	t.Helper()

	// Clear all env vars that BindFlags reads, then restore them after the test.
	envKeys := []string{
		"OCI_REGISTRY", "COPY_IMAGE", "SYNC_SERVICE_ACCOUNT",
		"POD_NAMESPACE", "HF_TOKEN_SECRET", "HF_TOKEN_SECRET_KEY",
		"REGISTRY_PUSH_SECRET", "SYNC_NODE_SELECTOR",
		"SYNC_MEMORY_REQUEST", "SYNC_MEMORY_LIMIT", "SYNC_MAX_PARALLEL",
	}
	for _, k := range envKeys {
		t.Setenv(k, "") // clears key; t.Cleanup restores it automatically
	}

	// Apply test-specific env overrides.
	for k, v := range env {
		t.Setenv(k, v)
	}

	var cfg Config
	fs := flag.NewFlagSet("test", flag.ContinueOnError)
	cfg.BindFlags(fs)
	require.NoError(t, fs.Parse(args))
	return cfg
}

// TestBindFlags_Defaults verifies built-in defaults are used when no flags or
// env vars are set.
func TestBindFlags_Defaults(t *testing.T) {
	cfg := newBoundConfig(t, nil, nil)

	assert.Equal(t, "ghcr.io/jomcgi/models", cfg.Registry)
	assert.Equal(t, "ghcr.io/jomcgi/homelab/bazel/tools/hf2oci:main", cfg.CopyImage)
	assert.Equal(t, "oci-model-cache", cfg.Namespace, "namespace falls back to literal default")
}

// TestBindFlags_FlagOverrides verifies that explicit flag values override defaults.
func TestBindFlags_FlagOverrides(t *testing.T) {
	args := []string{
		"--oci-registry", "registry.example.com/models",
		"--copy-image", "registry.example.com/hf2oci:v1.2.3",
		"--sync-service-account", "model-sync-sa",
	}

	cfg := newBoundConfig(t, args, nil)

	assert.Equal(t, "registry.example.com/models", cfg.Registry)
	assert.Equal(t, "registry.example.com/hf2oci:v1.2.3", cfg.CopyImage)
	assert.Equal(t, "model-sync-sa", cfg.SyncServiceAccount)
}

// TestBindFlags_EnvVarOverridesDefault verifies that environment variables
// override the compiled-in defaults.
func TestBindFlags_EnvVarOverridesDefault(t *testing.T) {
	cfg := newBoundConfig(t, nil, map[string]string{
		"OCI_REGISTRY": "env-registry.example.com/models",
		"COPY_IMAGE":   "env-registry.example.com/hf2oci:latest",
	})

	assert.Equal(t, "env-registry.example.com/models", cfg.Registry)
	assert.Equal(t, "env-registry.example.com/hf2oci:latest", cfg.CopyImage)
}

// TestBindFlags_FlagTakesPrecedenceOverEnv verifies that an explicit flag value
// overrides the corresponding environment variable.
func TestBindFlags_FlagTakesPrecedenceOverEnv(t *testing.T) {
	cfg := newBoundConfig(t,
		[]string{"--oci-registry", "flag-registry.example.com/models"},
		map[string]string{"OCI_REGISTRY": "env-registry.example.com/models"},
	)

	// flag wins over env
	assert.Equal(t, "flag-registry.example.com/models", cfg.Registry)
}

// TestBindFlags_NamespaceFromEnv verifies POD_NAMESPACE is read directly (not
// a flag) and wired into Config.Namespace.
func TestBindFlags_NamespaceFromEnv(t *testing.T) {
	cfg := newBoundConfig(t, nil, map[string]string{
		"POD_NAMESPACE": "custom-namespace",
	})

	assert.Equal(t, "custom-namespace", cfg.Namespace)
}

// TestBindFlags_HFTokenSecret verifies HF_TOKEN_SECRET and HF_TOKEN_SECRET_KEY
// env vars are read correctly.
func TestBindFlags_HFTokenSecret(t *testing.T) {
	cfg := newBoundConfig(t, nil, map[string]string{
		"HF_TOKEN_SECRET":     "hf-token-secret",
		"HF_TOKEN_SECRET_KEY": "token",
	})

	assert.Equal(t, "hf-token-secret", cfg.HFTokenSecret)
	assert.Equal(t, "token", cfg.HFTokenSecretKey)
}

// TestBindFlags_RegistryPushSecret verifies REGISTRY_PUSH_SECRET is read.
func TestBindFlags_RegistryPushSecret(t *testing.T) {
	cfg := newBoundConfig(t, nil, map[string]string{
		"REGISTRY_PUSH_SECRET": "registry-credentials",
	})

	assert.Equal(t, "registry-credentials", cfg.RegistryPushSecret)
}

// TestBindFlags_SyncNodeSelector verifies SYNC_NODE_SELECTOR is parsed as JSON.
func TestBindFlags_SyncNodeSelector(t *testing.T) {
	cfg := newBoundConfig(t, nil, map[string]string{
		"SYNC_NODE_SELECTOR": `{"kubernetes.io/arch":"amd64","workload-type":"gpu"}`,
	})

	require.NotNil(t, cfg.SyncNodeSelector)
	assert.Equal(t, "amd64", cfg.SyncNodeSelector["kubernetes.io/arch"])
	assert.Equal(t, "gpu", cfg.SyncNodeSelector["workload-type"])
}

// TestBindFlags_InvalidSyncNodeSelector verifies that invalid JSON is silently
// ignored (no panic, nil map).
func TestBindFlags_InvalidSyncNodeSelector(t *testing.T) {
	cfg := newBoundConfig(t, nil, map[string]string{
		"SYNC_NODE_SELECTOR": "not-json",
	})

	assert.Nil(t, cfg.SyncNodeSelector, "invalid JSON should result in nil node selector")
}

// TestBindFlags_EmptySyncNodeSelector verifies an empty env var leaves the
// node selector nil.
func TestBindFlags_EmptySyncNodeSelector(t *testing.T) {
	cfg := newBoundConfig(t, nil, nil)

	assert.Nil(t, cfg.SyncNodeSelector)
}

// TestBindFlags_MemorySettings verifies SYNC_MEMORY_REQUEST / LIMIT env vars.
func TestBindFlags_MemorySettings(t *testing.T) {
	cfg := newBoundConfig(t, nil, map[string]string{
		"SYNC_MEMORY_REQUEST": "512Mi",
		"SYNC_MEMORY_LIMIT":   "2Gi",
	})

	assert.Equal(t, "512Mi", cfg.SyncMemoryRequest)
	assert.Equal(t, "2Gi", cfg.SyncMemoryLimit)
}

// TestBindFlags_MaxParallel verifies SYNC_MAX_PARALLEL is read.
func TestBindFlags_MaxParallel(t *testing.T) {
	cfg := newBoundConfig(t, nil, map[string]string{
		"SYNC_MAX_PARALLEL": "8",
	})

	assert.Equal(t, "8", cfg.SyncMaxParallel)
}

// TestBindFlags_EmptyOptionalFields verifies that fields not backed by a flag
// are empty strings by default.
func TestBindFlags_EmptyOptionalFields(t *testing.T) {
	cfg := newBoundConfig(t, nil, nil)

	assert.Empty(t, cfg.HFTokenSecret)
	assert.Empty(t, cfg.HFTokenSecretKey)
	assert.Empty(t, cfg.RegistryPushSecret)
	assert.Empty(t, cfg.SyncServiceAccount)
	assert.Empty(t, cfg.SyncMemoryRequest)
	assert.Empty(t, cfg.SyncMemoryLimit)
	assert.Empty(t, cfg.SyncMaxParallel)
}
