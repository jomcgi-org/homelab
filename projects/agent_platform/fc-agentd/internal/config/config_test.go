package config

import "testing"

// TestTierEnvParsesNestedTierMaps verifies FC_AGENTD_TIER_<TIER>__<NAME> vars
// are grouped by tier, and that env names containing underscores (the reason for
// the "__" separator) survive intact.
func TestTierEnvParsesNestedTierMaps(t *testing.T) {
	t.Setenv("FC_AGENTD_TIER_default__OPENAI_HOST", "http://qwen:8080")
	t.Setenv("FC_AGENTD_TIER_default__OPENAI_API_KEY", "sk-noauth")
	t.Setenv("FC_AGENTD_TIER_artifact__OPENAI_HOST", "https://openrouter.ai/api")
	t.Setenv("FC_AGENTD_TIER_artifact__GOOSE_MODEL", "google/gemini-3.5-flash")

	got := tierEnv()
	if got == nil {
		t.Fatal("tierEnv() = nil, want two tiers")
	}
	if v := got["default"]["OPENAI_API_KEY"]; v != "sk-noauth" {
		t.Errorf("default OPENAI_API_KEY = %q, want sk-noauth", v)
	}
	if v := got["artifact"]["OPENAI_HOST"]; v != "https://openrouter.ai/api" {
		t.Errorf("artifact OPENAI_HOST = %q, want the openrouter host", v)
	}
	if v := got["artifact"]["GOOSE_MODEL"]; v != "google/gemini-3.5-flash" {
		t.Errorf("artifact GOOSE_MODEL = %q, want the gemini id", v)
	}
}

// TestTierEnvSkipsMalformed verifies vars without the "__" separator (or with an
// empty tier/name) are ignored rather than mis-parsed.
func TestTierEnvSkipsMalformed(t *testing.T) {
	t.Setenv("FC_AGENTD_TIER_noseparator", "x")
	t.Setenv("FC_AGENTD_TIER___EMPTYTIER", "y")
	got := tierEnv()
	if _, ok := got["noseparator"]; ok {
		t.Error("a var without the __ separator should be skipped")
	}
	if _, ok := got[""]; ok {
		t.Error("an empty tier name should be skipped")
	}
}

// TestTierEnvEmptyWhenUnset verifies no tier vars yields nil (the dry-run path).
func TestTierEnvEmptyWhenUnset(t *testing.T) {
	if got := tierEnv(); got != nil {
		t.Errorf("tierEnv() = %v, want nil when no FC_AGENTD_TIER_* set", got)
	}
}
