package config

import (
	"encoding/json"
	"flag"
	"os"
)

// Config holds operator configuration from flags and environment variables.
type Config struct {
	// Registry is the target OCI registry for cached models.
	Registry string

	// CopyImage is the container image used for sync Jobs (hf2oci).
	CopyImage string

	// SyncServiceAccount is the Kubernetes service account for sync Jobs.
	SyncServiceAccount string

	// Namespace is the operator's namespace (from POD_NAMESPACE).
	Namespace string

	// HFTokenSecret is the name of the Kubernetes Secret containing the HF token for copy Jobs.
	HFTokenSecret string

	// HFTokenSecretKey is the key within the Secret that holds the HF token.
	HFTokenSecretKey string

	// RegistryPushSecret is the name of a Secret containing .dockerconfigjson for pushing to the OCI registry.
	RegistryPushSecret string

	// SyncNodeSelector is applied to sync Job pods to control which nodes run model downloads.
	SyncNodeSelector map[string]string

	// SyncMemoryRequest is the Kubernetes memory request for sync Job containers (e.g. "1Gi").
	SyncMemoryRequest string

	// SyncMemoryLimit is the Kubernetes memory limit for sync Job containers (e.g. "2Gi").
	SyncMemoryLimit string

	// SyncMaxParallel caps concurrent layer uploads/downloads in sync Job
	// containers via the hf2oci --max-parallel flag (0 = auto from GOMEMLIMIT).
	SyncMaxParallel string
}

// BindFlags registers config flags on the given FlagSet.
func (c *Config) BindFlags(fs *flag.FlagSet) {
	fs.StringVar(&c.Registry, "oci-registry", envOrDefault("OCI_REGISTRY", "ghcr.io/jomcgi/models"),
		"Target OCI registry for cached models")
	fs.StringVar(&c.CopyImage, "copy-image", envOrDefault("COPY_IMAGE", "ghcr.io/jomcgi/homelab/bazel/tools/hf2oci:main"),
		"Container image for sync Jobs")
	fs.StringVar(&c.SyncServiceAccount, "sync-service-account", envOrDefault("SYNC_SERVICE_ACCOUNT", ""),
		"Service account for sync Jobs")
	c.Namespace = envOrDefault("POD_NAMESPACE", "oci-model-cache")
	c.HFTokenSecret = envOrDefault("HF_TOKEN_SECRET", "")
	c.HFTokenSecretKey = envOrDefault("HF_TOKEN_SECRET_KEY", "")
	c.RegistryPushSecret = envOrDefault("REGISTRY_PUSH_SECRET", "")
	if v := os.Getenv("SYNC_NODE_SELECTOR"); v != "" {
		_ = json.Unmarshal([]byte(v), &c.SyncNodeSelector)
	}
	c.SyncMemoryRequest = envOrDefault("SYNC_MEMORY_REQUEST", "")
	c.SyncMemoryLimit = envOrDefault("SYNC_MEMORY_LIMIT", "")
	c.SyncMaxParallel = envOrDefault("SYNC_MAX_PARALLEL", "")
}

func envOrDefault(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
