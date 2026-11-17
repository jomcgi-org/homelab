// discord-bridge connects Bosun to Discord's gateway (HOME-162): it answers
// messages that tag the bot in allowlisted servers, with memory from
// hindsight-discord and turns on a kagent Agent.
package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
	"github.com/jomcgi/homelab/projects/factory/discord"
)

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stderr, nil))
	if err := run(log); err != nil {
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
	var cfg struct{ policy, token, hindsightURL, hindsightKey, kagentURL, kagentGRPC, kagentToken, agent, health string }
	for name, dst := range map[string]*string{
		"DISCORD_POLICY_FILE": &cfg.policy, "DISCORD_TOKEN_FILE": &cfg.token,
		"HINDSIGHT_URL": &cfg.hindsightURL, "HINDSIGHT_KEY_FILE": &cfg.hindsightKey,
		"KAGENT_URL": &cfg.kagentURL, "KAGENT_GRPC": &cfg.kagentGRPC, "KAGENT_TOKEN_FILE": &cfg.kagentToken,
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
	sessions, err := kagent.DialSessions(cfg.kagentGRPC, tokens)
	if err != nil {
		return err
	}
	defer sessions.Close()

	botToken := fileValue(cfg.token)
	rest := &discord.REST{Token: botToken}
	bridge := &discord.Bridge{
		Config: policy,
		Agent: &discord.Kagent{
			A2A:      &kagent.A2A{BaseURL: strings.TrimSuffix(cfg.kagentURL, "/"), Tokens: tokens},
			Sessions: sessions,
			Agent:    kagent.AgentRef{Namespace: ns, Name: name},
			Log:      log,
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
