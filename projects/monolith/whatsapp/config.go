// Package whatsapp is the transport-only WhatsApp channel gateway (ADR 039).
// It speaks the WhatsApp multidevice protocol via go.mau.fi/whatsmeow, persists
// its device session in the monolith CNPG Postgres under a dedicated `whatsapp`
// schema, and (in this phase) pairs the bot number, connects, logs allow-listed
// group messages, and parks cleanly on logout or ban. It holds no LLM access
// and no MCP tools: all agent behaviour lives in the monolith.
package whatsapp

import (
	"fmt"
	"os"
	"strings"
)

// Config is the gateway's runtime configuration, loaded from the environment.
// Every field is injected by the chart (values.yaml) or a mounted secret; there
// are no hardcoded cluster URLs or credentials.
type Config struct {
	// DBDSN is the Postgres DSN for the whatsmeow sqlstore. The gateway appends
	// `search_path=whatsapp` so whatsmeow creates and reads its own tables inside
	// the dedicated schema rather than the public one.
	DBDSN string
	// BotNumber is the bot's own WhatsApp number in E.164 (e.g. +447700900123),
	// used to request a phone-number pairing code on first boot.
	BotNumber string
	// GroupJIDs is the allow-list of group JIDs the gateway will observe. Messages
	// from any chat not in this set are dropped without forwarding.
	GroupJIDs []string
	// AlertChannelID is the Discord channel id ops alerts are delivered to (the
	// pairing code and the logout/ban alert), via the shared chat.discord_outbox.
	AlertChannelID string
	// MonolithInboundURL is the in-cluster URL of the monolith backend inbound
	// endpoint the gateway POSTs allow-listed group messages to (spec section 2).
	// Injected from values (never hardcoded: a release rename changes the service
	// name prefix).
	MonolithInboundURL string
	// InboundToken is the bearer the gateway presents on the inbound POST; the
	// monolith validates it against the same 1Password-managed value.
	InboundToken string
	// HealthAddr is the listen address for the /healthz endpoint.
	HealthAddr string
}

// LoadConfig reads the gateway configuration from the environment and validates
// that the required fields are present. It returns a descriptive error naming
// every missing field so a misconfigured Deployment fails fast and legibly.
func LoadConfig() (Config, error) {
	cfg := Config{
		DBDSN:              os.Getenv("WHATSAPP_DB_DSN"),
		BotNumber:          os.Getenv("WHATSAPP_BOT_NUMBER"),
		GroupJIDs:          splitAndTrim(os.Getenv("WHATSAPP_GROUP_JIDS")),
		AlertChannelID:     os.Getenv("WHATSAPP_ALERT_CHANNEL_ID"),
		MonolithInboundURL: os.Getenv("MONOLITH_INBOUND_URL"),
		InboundToken:       os.Getenv("WHATSAPP_INBOUND_TOKEN"),
		HealthAddr:         os.Getenv("WHATSAPP_HEALTH_ADDR"),
	}
	if cfg.HealthAddr == "" {
		cfg.HealthAddr = ":8080"
	}
	return cfg, cfg.validate()
}

// validate reports the required fields that are missing. GroupJIDs is allowed to
// be empty (the gateway simply observes nothing until the registry is populated
// in a later phase), so it is not required here.
func (c Config) validate() error {
	var missing []string
	if c.DBDSN == "" {
		missing = append(missing, "WHATSAPP_DB_DSN")
	}
	if c.BotNumber == "" {
		missing = append(missing, "WHATSAPP_BOT_NUMBER")
	}
	if c.AlertChannelID == "" {
		missing = append(missing, "WHATSAPP_ALERT_CHANNEL_ID")
	}
	// Forwarding is Phase 3's whole purpose: without a destination and a bearer
	// the gateway cannot deliver a message, so fail fast rather than silently
	// dropping every inbound.
	if c.MonolithInboundURL == "" {
		missing = append(missing, "MONOLITH_INBOUND_URL")
	}
	if c.InboundToken == "" {
		missing = append(missing, "WHATSAPP_INBOUND_TOKEN")
	}
	if len(missing) > 0 {
		return fmt.Errorf("whatsapp: missing required config: %s", strings.Join(missing, ", "))
	}
	return nil
}

// DSNWithSchema returns the DSN with the whatsmeow tables scoped to the
// `whatsapp` schema via search_path. It appends the option with the correct
// separator whether or not the DSN already carries a query string, and leaves an
// explicit search_path already present in the DSN untouched.
func (c Config) DSNWithSchema() string {
	if strings.Contains(c.DBDSN, "search_path=") {
		return c.DBDSN
	}
	sep := "?"
	if strings.Contains(c.DBDSN, "?") {
		sep = "&"
	}
	return c.DBDSN + sep + "search_path=whatsapp"
}

// splitAndTrim splits a comma-separated env value into a slice, trimming
// whitespace and dropping empty entries.
func splitAndTrim(v string) []string {
	if v == "" {
		return nil
	}
	parts := strings.Split(v, ",")
	out := make([]string, 0, len(parts))
	for _, p := range parts {
		if t := strings.TrimSpace(p); t != "" {
			out = append(out, t)
		}
	}
	return out
}
