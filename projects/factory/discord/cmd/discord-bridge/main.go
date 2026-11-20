// discord-bridge connects Bosun to Discord's gateway (HOME-162): it answers
// messages that tag the bot in allowlisted servers, with memory from
// hindsight-discord and turns on a kagent Agent. "discord-bridge import"
// replays allowlisted servers' history into their banks instead (HOME-164).
package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
	"github.com/jomcgi/homelab/projects/factory/discord"
)

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stderr, nil))
	command := run
	if len(os.Args) > 1 && os.Args[1] == "import" {
		command = runImport
	}
	if err := command(log); err != nil {
		log.Error("discord bridge stopped", "error", err)
		os.Exit(1)
	}
}

// fileValue reads a mounted Secret file on each use, so a rotated value
// applies without a restart.
func fileValue(path string) func() (string, error) {
	return func() (string, error) {
		b, err := os.ReadFile(path)
		if err != nil {
			return "", err
		}
		v := strings.TrimSpace(string(b))
		if v == "" {
			return "", fmt.Errorf("%s is empty", path)
		}
		return v, nil
	}
}

func env(name string) (string, error) {
	v := os.Getenv(name)
	if v == "" {
		return "", fmt.Errorf("%s is not set", name)
	}
	return v, nil
}

func run(log *slog.Logger) error {
	var cfg struct{ policy, token, hindsightURL, hindsightKey, kagentURL, kagentToken, agent, health string }
	for name, dst := range map[string]*string{
		"DISCORD_POLICY_FILE": &cfg.policy, "DISCORD_TOKEN_FILE": &cfg.token,
		"HINDSIGHT_URL": &cfg.hindsightURL, "HINDSIGHT_KEY_FILE": &cfg.hindsightKey,
		"KAGENT_URL": &cfg.kagentURL, "KAGENT_TOKEN_FILE": &cfg.kagentToken,
		"KAGENT_AGENT": &cfg.agent, "HEALTH_ADDR": &cfg.health,
	} {
		v, err := env(name)
		if err != nil {
			return err
		}
		*dst = v
	}
	data, err := os.ReadFile(cfg.policy)
	if err != nil {
		return err
	}
	policy, err := discord.ParsePolicy(data)
	if err != nil {
		return err
	}
	ns, name, ok := strings.Cut(cfg.agent, "/")
	if !ok || ns == "" || name == "" {
		return fmt.Errorf("KAGENT_AGENT %q is not namespace/name", cfg.agent)
	}
	tokens := &kagent.TokenSource{Path: cfg.kagentToken}

	botToken := fileValue(cfg.token)
	rest := &discord.REST{Token: botToken}
	bridge := &discord.Bridge{
		Config: policy,
		Agent: &discord.Kagent{
			A2A:   &kagent.A2A{BaseURL: strings.TrimSuffix(cfg.kagentURL, "/"), Tokens: tokens},
			Agent: kagent.AgentRef{Namespace: ns, Name: name},
		},
		Memory:  &discord.Hindsight{BaseURL: strings.TrimSuffix(cfg.hindsightURL, "/"), Key: fileValue(cfg.hindsightKey), RecallTokens: 2000},
		Replies: rest,
		Log:     log,
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusNoContent) })
	server := &http.Server{Addr: cfg.health, Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	go func() {
		if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Error("health server", "error", err)
		}
	}()
	defer server.Close()

	gateway := &discord.Gateway{
		REST:      rest,
		Token:     botToken,
		OnReady:   bridge.SetBot,
		OnMessage: func(m discord.Message) { bridge.Handle(ctx, m) },
		Log:       log,
	}
	log.Info("discord bridge starting", "guilds", len(policy.Guilds), "agent", cfg.agent)
	err = gateway.Run(ctx)
	stop()
	bridge.Wait()
	if errors.Is(err, context.Canceled) {
		return nil
	}
	return err
}

// runImport replays history once and exits. A run that reaches its document
// cap succeeds; the next run continues where it stopped.
func runImport(log *slog.Logger) error {
	var cfg struct{ policy, token, hindsightURL, hindsightKey string }
	for name, dst := range map[string]*string{
		"DISCORD_POLICY_FILE": &cfg.policy, "DISCORD_TOKEN_FILE": &cfg.token,
		"HINDSIGHT_URL": &cfg.hindsightURL, "HINDSIGHT_KEY_FILE": &cfg.hindsightKey,
	} {
		v, err := env(name)
		if err != nil {
			return err
		}
		*dst = v
	}
	data, err := os.ReadFile(cfg.policy)
	if err != nil {
		return err
	}
	policy, err := discord.ParsePolicy(data)
	if err != nil {
		return err
	}
	number := func(name string, fallback int) (int, error) {
		v := os.Getenv(name)
		if v == "" {
			return fallback, nil
		}
		return strconv.Atoi(v)
	}
	maxDocs, err := number("IMPORT_MAX_DOCUMENTS", 500)
	if err != nil {
		return err
	}
	workers, err := number("IMPORT_WORKERS", 2)
	if err != nil {
		return err
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	rest := &discord.REST{Token: fileValue(cfg.token)}
	me, err := rest.Me(ctx)
	if err != nil {
		return fmt.Errorf("bot user: %w", err)
	}
	im := &discord.Importer{
		Source:       rest,
		Store:        &discord.Hindsight{BaseURL: strings.TrimSuffix(cfg.hindsightURL, "/"), Key: fileValue(cfg.hindsightKey)},
		Config:       policy,
		BotID:        me.ID,
		IncludeBots:  os.Getenv("IMPORT_INCLUDE_BOTS") == "true",
		MaxDocuments: maxDocs,
		Workers:      workers,
		Log:          log,
	}
	stats, err := im.Run(ctx)
	log.Info("history import finished", "channels", stats.Channels, "retained", stats.Retained,
		"skipped", stats.Skipped, "capped", stats.Capped)
	if errors.Is(err, discord.ErrCapped) {
		return nil
	}
	return err
}
