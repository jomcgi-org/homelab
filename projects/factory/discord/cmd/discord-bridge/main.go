// discord-bridge connects Bosun to Discord's gateway (HOME-162): it answers
// messages that tag the bot in allowlisted servers, with memory from
// hindsight-discord and turns on a kagent Agent. In a server whose policy
// sets `ingest`, it also stores every human message (HOME-833). With
// BACKFILL_ENABLED, it also replays the history of each server with
// `history: true` into its bank in the background (HOME-164, HOME-836).
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
	"sync"
	"syscall"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"

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
	memory := &discord.Hindsight{BaseURL: strings.TrimSuffix(cfg.hindsightURL, "/"), Key: fileValue(cfg.hindsightKey), RecallTokens: 2000}
	bridge := &discord.Bridge{
		Config: policy,
		Agent: &discord.Kagent{
			A2A:      &kagent.A2A{BaseURL: strings.TrimSuffix(cfg.kagentURL, "/"), Tokens: tokens},
			Sessions: sessions,
			Agent:    kagent.AgentRef{Namespace: ns, Name: name},
			Log:      log,
		},
		Memory:  memory,
		Replies: rest,
		Log:     log,
		// Only allowlisted servers are cached: anyone can add the bot.
		Places: &discord.Places{Keep: func(id string) bool { _, ok := policy.Guilds[id]; return ok }},
	}
	if u := os.Getenv("DISCORD_RENDER_URL"); u != "" {
		bridge.Render = &discord.HTTPRenderer{BaseURL: u}
		log.Info("renderer on", "url", u)
	}
	wireOperatorUser(log, bridge)
	wireServerAgents(log, bridge, &kagent.A2A{BaseURL: strings.TrimSuffix(cfg.kagentURL, "/"), Tokens: tokens}, sessions)
	wireExplorer(log, bridge, &kagent.A2A{BaseURL: strings.TrimSuffix(cfg.kagentURL, "/"), Tokens: tokens}, sessions)
	if err := wireOperator(log, bridge, rest, &kagent.A2A{BaseURL: strings.TrimSuffix(cfg.kagentURL, "/"), Tokens: tokens}, sessions); err != nil {
		return err
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	// The bridge registers no slash commands. Clear any the old hub bot left,
	// so the picker lists none. A failure only logs: the bridge works without it.
	if err := rest.ClearCommands(ctx); err != nil {
		log.Warn("clear stale slash commands", "error", err)
	}

	// A server with `ingest` stores every human message (HOME-833). The
	// ingestor outlives the gateway so the last turns' lines are stored.
	intents := discord.IntentsFor(policy)
	ingestDone := make(chan struct{})
	stopIngest := func() {}
	if intents&discord.IntentMessageContent != 0 {
		bridge.Ingest = &discord.Ingestor{Memory: memory, Log: log}
		ictx, cancel := context.WithCancel(context.Background())
		stopIngest = cancel
		go func() {
			defer close(ingestDone)
			bridge.Ingest.Run(ictx)
		}()
	} else {
		close(ingestDone)
	}

	reg := prometheus.NewRegistry()
	backfill, err := newBackfill(log, policy, rest, cfg.hindsightURL, fileValue(cfg.hindsightKey), reg)
	if err != nil {
		return err
	}
	// The backfill starts once the gateway is ready, with Bosun's user ID
	// from READY. It stops with ctx.
	ready := make(chan struct{})
	var readyOnce sync.Once
	onReady := func(u discord.User) {
		bridge.SetBot(u)
		readyOnce.Do(func() {
			if backfill != nil {
				backfill.Importer.BotID = u.ID
			}
			close(ready)
		})
	}
	// Explorer sessions past the TTL or the cap are deleted each hour.
	go bridge.RunExploreSweep(ctx, time.Hour)
	backfillDone := make(chan struct{})
	if backfill != nil {
		go func() {
			defer close(backfillDone)
			backfill.Run(ctx, ready)
		}()
	} else {
		close(backfillDone)
	}

	mux := http.NewServeMux()
	mux.Handle("GET /metrics", promhttp.HandlerFor(reg, promhttp.HandlerOpts{}))
	mux.Handle("POST /notify", discord.NotificationHandler(policy.NotificationChannels, fileValue("/var/run/secrets/factory-notify/app-password"), rest))
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
		OnReady:   onReady,
		OnMessage: func(m discord.Message) { bridge.Handle(ctx, m) },
		Places:    bridge.Places,
		Log:       log,
		Intents:   intents,
	}
	log.Info("discord bridge starting", "guilds", len(policy.Guilds), "agent", cfg.agent, "intents", intents,
		"backfill", backfill != nil)
	err = gateway.Run(ctx)
	stop()
	bridge.Wait()
	<-backfillDone
	stopIngest()
	<-ingestDone
	if errors.Is(err, context.Canceled) {
		return nil
	}
	return err
}
