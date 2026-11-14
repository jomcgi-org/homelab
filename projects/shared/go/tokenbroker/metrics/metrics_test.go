package metrics

import (
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/shared/go/tokenbroker/quota"
	"github.com/prometheus/client_golang/prometheus"
)

func familyNames(t *testing.T, m *Metrics, collector prometheus.Collector) []string {
	t.Helper()
	registry := prometheus.NewPedanticRegistry()
	m.Register(registry)
	registry.MustRegister(collector)
	m.Refreshes.Inc()
	m.RefreshFailures.Inc()
	m.ReusedRetries.Inc()
	m.CodexUsage.WithLabelValues("g", "ok").Inc()
	families, err := registry.Gather()
	if err != nil {
		t.Fatal(err)
	}
	var names []string
	for _, family := range families {
		names = append(names, family.GetName())
	}
	sort.Strings(names)
	return names
}

func observedStore(t *testing.T) *quota.Store {
	t.Helper()
	now := time.Now().UTC().Truncate(time.Second)
	store := quota.NewStore()
	obs := quota.Observation{ObservedAt: now.Format(time.RFC3339), Status: "allowed", Windows: []quota.Window{{Name: "primary", UsedPercent: 5, ResetsAt: now.Add(time.Hour).Format(time.RFC3339)}}}
	if err := store.PutGrant("g", "codex", obs, now); err != nil {
		t.Fatal(err)
	}
	return store
}

// EmberVM's dashboards and alerts depend on these exact names.
func TestDefaultNamesUnchanged(t *testing.T) {
	got := familyNames(t, New(), NewQuotaCollector(observedStore(t), []string{"codex"}))
	want := []string{
		"tokenbroker_codex_usage_total",
		"tokenbroker_grant_quota_exhausted",
		"tokenbroker_grant_quota_used_percent",
		"tokenbroker_quota_exhausted",
		"tokenbroker_quota_observed_at_seconds",
		"tokenbroker_quota_resets_at_seconds",
		"tokenbroker_quota_used_percent",
		"tokenbroker_refresh_failures_total",
		"tokenbroker_refresh_token_reused_total",
		"tokenbroker_refresh_total",
	}
	if strings.Join(got, ",") != strings.Join(want, ",") {
		t.Fatalf("metric families = %v, want %v", got, want)
	}
}

func TestPrefixedNames(t *testing.T) {
	got := familyNames(t, NewWithPrefix("factory_broker"), NewQuotaCollectorWithPrefix("factory_broker", observedStore(t), []string{"codex"}))
	for _, name := range got {
		if !strings.HasPrefix(name, "factory_broker_") {
			t.Fatalf("metric %q lacks the configured prefix", name)
		}
	}
	if len(got) != 10 {
		t.Fatalf("metric families = %v, want 10", got)
	}
}
