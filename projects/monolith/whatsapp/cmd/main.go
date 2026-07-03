// Command whatsapp is the transport-only WhatsApp channel gateway (ADR 039). It
// pairs (or resumes) the bot number via whatsmeow, connects to WhatsApp, logs
// allow-listed group messages, and parks cleanly (with a Discord alert) on
// logout or ban. It holds no LLM access and no MCP tools: all agent behaviour
// lives in the monolith. This phase is transport-only plumbing with no inbound
// forwarding and no outbox drain yet.
package main

import (
	"context"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	whatsapp "github.com/jomcgi/homelab/projects/monolith/whatsapp"
)

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	slog.SetDefault(logger)

	if err := run(logger); err != nil {
		logger.Error("whatsapp gateway exited with error", "err", err)
		os.Exit(1)
	}
}

func run(logger *slog.Logger) error {
	// Cancel on SIGINT/SIGTERM so the health server drains gracefully.
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	cfg, err := whatsapp.LoadConfig()
	if err != nil {
		return err
	}
	logger.Info("whatsapp gateway starting",
		"bot_number", cfg.BotNumber,
		"groups", len(cfg.GroupJIDs),
		"health_addr", cfg.HealthAddr,
	)

	// The notifier and the whatsmeow store share the same Postgres; the notifier
	// targets chat.discord_outbox (schema-qualified), so its own search_path is
	// irrelevant and it opens the raw DSN.
	notifier, db, err := whatsapp.NewPGNotifier(cfg.DBDSN, cfg.AlertChannelID)
	if err != nil {
		return err
	}
	defer db.Close()

	session, err := whatsapp.NewWhatsmeowSession(ctx, cfg, logger)
	if err != nil {
		return err
	}

	gw := whatsapp.NewGateway(cfg, logger, session, notifier)
	if err := gw.Start(ctx); err != nil {
		return err
	}

	// Drain chat.whatsapp_outbox and send via whatsmeow. The drain reuses the
	// notifier's base-DSN db handle (its queries fully-qualify chat.whatsapp_outbox
	// so the session's search_path=whatsapp does not matter) and only sends while
	// the gateway is connected. It respects ctx, so it stops on shutdown.
	drain, err := whatsapp.NewOutboxDrain(db, session, gw.State, logger)
	if err != nil {
		return err
	}
	go drain.Run(ctx)

	srv := &http.Server{
		Addr:              cfg.HealthAddr,
		Handler:           gw.HealthHandler(),
		ReadHeaderTimeout: 10 * time.Second,
	}

	errCh := make(chan error, 1)
	go func() {
		logger.Info("health server listening", "addr", cfg.HealthAddr)
		errCh <- srv.ListenAndServe()
	}()

	select {
	case err := <-errCh:
		if err == http.ErrServerClosed {
			return nil
		}
		return err
	case <-ctx.Done():
		logger.Info("shutdown signal received; draining health server")
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		return srv.Shutdown(shutdownCtx)
	}
}
