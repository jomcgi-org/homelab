// Package config loads fc-agentd configuration from the environment. The
// controller is a node-4 daemon; everything it needs is injected via env vars
// from the Helm values (Postgres DSN, node/arch identity, snapshot root, OTel).
package config

import (
	"fmt"
	"os"
	"runtime"
	"time"
)

// Config is the fully-resolved fc-agentd configuration.
type Config struct {
	// DatabaseURL is the monolith Postgres DSN holding the agent_threads
	// registry. Empty disables the Postgres-backed reconcile loop (dry run).
	DatabaseURL string
	// Node is the host this daemon manages; snapshots are node-affine so the
	// reconcile loop only touches threads pinned here.
	Node string
	// Arch is the CPU architecture of this node; FC snapshots are non-portable
	// and a mismatched restore fails closed (ADR 022).
	Arch string
	// SnapshotRoot is the directory holding per-thread snapshot bundles
	// (/disks/nvme-02 on node-4).
	SnapshotRoot string
	// ReconcileInterval is how often the loop polls desired vs actual state.
	ReconcileInterval time.Duration
}

// Load resolves configuration from the environment, applying defaults. It
// returns an error only for values that are present but malformed.
func Load() (Config, error) {
	c := Config{
		DatabaseURL:       os.Getenv("DATABASE_URL"),
		Node:              os.Getenv("FC_AGENTD_NODE"),
		Arch:              os.Getenv("FC_AGENTD_ARCH"),
		SnapshotRoot:      getenvDefault("FC_AGENTD_SNAPSHOT_ROOT", "/disks/nvme-02/agent-threads"),
		ReconcileInterval: 5 * time.Second,
	}

	if c.Node == "" {
		// Default to the pod's node via the downward API hostname fallback.
		c.Node = os.Getenv("NODE_NAME")
	}
	if c.Arch == "" {
		c.Arch = runtime.GOARCH
	}

	if v := os.Getenv("FC_AGENTD_RECONCILE_INTERVAL"); v != "" {
		d, err := time.ParseDuration(v)
		if err != nil {
			return Config{}, fmt.Errorf("invalid FC_AGENTD_RECONCILE_INTERVAL %q: %w", v, err)
		}
		c.ReconcileInterval = d
	}

	return c, nil
}

func getenvDefault(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}
