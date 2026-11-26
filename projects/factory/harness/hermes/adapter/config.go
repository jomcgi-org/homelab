// Package adapter runs one Hermes Agent gateway behind an A2A endpoint.
//
// The adapter renders Hermes' configuration, starts `hermes gateway` with only
// the API server on loopback, and sends each A2A turn to Hermes' non-streaming
// /v1/responses endpoint.
package adapter

import (
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"net/url"
	"os"
	"path/filepath"
	"strings"

	"gopkg.in/yaml.v3"
)

// DefaultBaseURL is the OpenRouter API. The egress gateway injects the real
// key, so the rendered key is a placeholder.
const DefaultBaseURL = "https://openrouter.ai/api/v1"

// PlaceholderAPIKey is the model key Hermes sends. The Substrate egress
// gateway replaces the Authorization header with the real credential.
const PlaceholderAPIKey = "kagent-egress-gateway-placeholder"

// DisabledToolsets names every built-in Hermes v0.23.4 toolset. The empty
// api_server platform list already enables none; this list also strips them
// if a later config adds a platform list.
var DisabledToolsets = []string{
	"a2a", "browser", "catalog", "clarify", "code_execution", "computer_use",
	"connections", "cronjob", "delegation", "desktop_ui", "discord",
	"discord_admin", "feishu_doc", "feishu_drive", "file", "image_gen",
	"kanban", "memory", "project", "search", "session_search", "skills",
	"terminal", "todo", "tts", "video", "video_gen", "vision", "web", "x_search",
}

// Settings are the values the harness renders into Hermes' configuration.
type Settings struct {
	// Model is the OpenRouter model ID, such as deepseek/deepseek-v4.1-flash-1017:online.
	Model string
	// BaseURL is an OpenAI-compatible endpoint. Empty means DefaultBaseURL.
	BaseURL string
	// APIKey is the key Hermes sends. Empty means PlaceholderAPIKey.
	APIKey string
}

func (s Settings) withDefaults() (Settings, error) {
	if strings.TrimSpace(s.Model) == "" {
		return s, fmt.Errorf("hermes: model is required")
	}
	if s.BaseURL == "" {
		s.BaseURL = DefaultBaseURL
	}
	u, err := url.Parse(s.BaseURL)
	if err != nil || (u.Scheme != "https" && u.Scheme != "http") || u.Host == "" {
		return s, fmt.Errorf("hermes: base URL %q is not an absolute HTTP(S) URL", s.BaseURL)
	}
	if s.APIKey == "" {
		s.APIKey = PlaceholderAPIKey
	}
	return s, nil
}

type modelConfig struct {
	Default  string `yaml:"default"`
	Provider string `yaml:"provider"`
	BaseURL  string `yaml:"base_url"`
	APIKey   string `yaml:"api_key"`
}

type agentConfig struct {
	DisabledToolsets []string `yaml:"disabled_toolsets"`
}

type memoryConfig struct {
	MemoryEnabled      bool   `yaml:"memory_enabled"`
	UserProfileEnabled bool   `yaml:"user_profile_enabled"`
	Provider           string `yaml:"provider"`
}

type approvalsConfig struct {
	Mode            string `yaml:"mode"`
	Timeout         int    `yaml:"timeout"`
	CronMode        string `yaml:"cron_mode"`
	SingleQueryMode string `yaml:"single_query_mode"`
	UnattendedMode  string `yaml:"unattended_mode"`
}

type securityConfig struct {
	AllowLazyInstalls bool `yaml:"allow_lazy_installs"`
}

type titleConfig struct {
	Enabled bool `yaml:"enabled"`
}

type auxiliaryConfig struct {
	TitleGeneration titleConfig `yaml:"title_generation"`
}

type hermesConfig struct {
	Model            modelConfig         `yaml:"model"`
	Toolsets         []string            `yaml:"toolsets"`
	PlatformToolsets map[string][]string `yaml:"platform_toolsets"`
	Agent            agentConfig         `yaml:"agent"`
	Memory           memoryConfig        `yaml:"memory"`
	Approvals        approvalsConfig     `yaml:"approvals"`
	Security         securityConfig      `yaml:"security"`
	Auxiliary        auxiliaryConfig     `yaml:"auxiliary"`
}

// hindsightConfig is $HERMES_HOME/hindsight/config.json. It stays inert
// until memory.provider names hindsight (HOME-905 slice 3).
type hindsightConfig struct {
	MemoryMode string `json:"memory_mode"`
}

// RenderConfig returns config.yaml for Hermes.
func RenderConfig(s Settings) ([]byte, error) {
	s, err := s.withDefaults()
	if err != nil {
		return nil, err
	}
	return yaml.Marshal(hermesConfig{
		Model: modelConfig{Default: s.Model, Provider: "custom", BaseURL: s.BaseURL, APIKey: s.APIKey},
		// An explicit empty list enables no toolset and merges no MCP server
		// (hermes_cli/tools_config.py _get_platform_tools at v0.23.4).
		Toolsets:         []string{},
		PlatformToolsets: map[string][]string{"api_server": {}},
		Agent:            agentConfig{DisabledToolsets: DisabledToolsets},
		Memory:           memoryConfig{},
		// api_server sessions are unattended: unattended_mode deny refuses a
		// flagged action at once instead of parking the turn.
		Approvals: approvalsConfig{Mode: "manual", Timeout: 10, CronMode: "deny", SingleQueryMode: "deny", UnattendedMode: "deny"},
		Security:  securityConfig{AllowLazyInstalls: false},
		Auxiliary: auxiliaryConfig{TitleGeneration: titleConfig{Enabled: false}},
	})
}

// RenderHindsightConfig returns hindsight/config.json for Hermes.
func RenderHindsightConfig() ([]byte, error) {
	return json.MarshalIndent(hindsightConfig{MemoryMode: "context"}, "", "  ")
}

// WriteHome writes the rendered configuration into home. It replaces files
// from an earlier start, so values in git own the configuration. It also
// removes $HERMES_HOME/.env: Hermes loads it over the process environment,
// so a durable file could re-enable what the environment allowlist drops.
func WriteHome(home string, s Settings) error {
	cfg, err := RenderConfig(s)
	if err != nil {
		return err
	}
	hindsight, err := RenderHindsightConfig()
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Join(home, "hindsight"), 0o700); err != nil {
		return err
	}
	if err := os.Remove(filepath.Join(home, ".env")); err != nil && !errors.Is(err, fs.ErrNotExist) {
		return fmt.Errorf("hermes: remove %s/.env: %w", home, err)
	}
	if err := writeFile(filepath.Join(home, "config.yaml"), cfg); err != nil {
		return err
	}
	return writeFile(filepath.Join(home, "hindsight", "config.json"), hindsight)
}

func writeFile(path string, data []byte) error {
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}
