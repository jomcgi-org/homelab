package whatsapp

import (
	"strings"
	"testing"
)

func TestLoadConfigValidation(t *testing.T) {
	tests := []struct {
		name        string
		env         map[string]string
		wantErr     bool
		wantMissing []string
	}{
		{
			name: "all required present",
			env: map[string]string{
				"WHATSAPP_DB_DSN":           "postgres://u:p@h:5432/db",
				"WHATSAPP_BOT_NUMBER":       "+447700900123",
				"WHATSAPP_ALERT_CHANNEL_ID": "123",
				"MONOLITH_INBOUND_URL":      "http://monolith:8000/internal/whatsapp/inbound",
				"WHATSAPP_INBOUND_TOKEN":    "tok",
			},
			wantErr: false,
		},
		{
			name: "missing dsn and bot number",
			env: map[string]string{
				"WHATSAPP_ALERT_CHANNEL_ID": "123",
			},
			wantErr:     true,
			wantMissing: []string{"WHATSAPP_DB_DSN", "WHATSAPP_BOT_NUMBER"},
		},
		{
			name:        "all missing",
			env:         map[string]string{},
			wantErr:     true,
			wantMissing: []string{"WHATSAPP_DB_DSN", "WHATSAPP_BOT_NUMBER", "WHATSAPP_ALERT_CHANNEL_ID"},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			for _, k := range []string{"WHATSAPP_DB_DSN", "WHATSAPP_BOT_NUMBER", "WHATSAPP_GROUP_JIDS", "WHATSAPP_ALERT_CHANNEL_ID", "MONOLITH_INBOUND_URL", "WHATSAPP_INBOUND_TOKEN", "WHATSAPP_HEALTH_ADDR"} {
				t.Setenv(k, "")
			}
			for k, v := range tt.env {
				t.Setenv(k, v)
			}
			cfg, err := LoadConfig()
			if tt.wantErr {
				if err == nil {
					t.Fatalf("expected error, got nil")
				}
				for _, m := range tt.wantMissing {
					if !strings.Contains(err.Error(), m) {
						t.Errorf("error %q should name missing field %q", err.Error(), m)
					}
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if cfg.HealthAddr != ":8080" {
				t.Errorf("HealthAddr default = %q, want :8080", cfg.HealthAddr)
			}
		})
	}
}

func TestGroupJIDsParsing(t *testing.T) {
	for _, k := range []string{"WHATSAPP_DB_DSN", "WHATSAPP_BOT_NUMBER", "WHATSAPP_ALERT_CHANNEL_ID", "MONOLITH_INBOUND_URL", "WHATSAPP_INBOUND_TOKEN"} {
		t.Setenv(k, "x")
	}
	t.Setenv("WHATSAPP_GROUP_JIDS", " a@g.us , b@g.us ,, c@g.us ")
	cfg, err := LoadConfig()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	want := []string{"a@g.us", "b@g.us", "c@g.us"}
	if len(cfg.GroupJIDs) != len(want) {
		t.Fatalf("GroupJIDs = %v, want %v", cfg.GroupJIDs, want)
	}
	for i := range want {
		if cfg.GroupJIDs[i] != want[i] {
			t.Errorf("GroupJIDs[%d] = %q, want %q", i, cfg.GroupJIDs[i], want[i])
		}
	}
}

func TestDSNWithSchema(t *testing.T) {
	tests := []struct {
		name string
		in   string
		want string
	}{
		{"no query", "postgres://h/db", "postgres://h/db?search_path=whatsapp"},
		{"existing query", "postgres://h/db?sslmode=require", "postgres://h/db?sslmode=require&search_path=whatsapp"},
		{"already scoped", "postgres://h/db?search_path=whatsapp", "postgres://h/db?search_path=whatsapp"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := Config{DBDSN: tt.in}.DSNWithSchema()
			if got != tt.want {
				t.Errorf("DSNWithSchema() = %q, want %q", got, tt.want)
			}
		})
	}
}
