// Command reviewer runs the factory reviewer: it gates every PR into each
// bound repository's base branch with the `factory review` check run, posted
// as the reviewer GitHub App, from the findings of a kagent reviewer agent,
// and enqueues heads that pass (projects/factory/ARCHITECTURE.md, "Merge
// authority").
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

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/collectors"
	"github.com/prometheus/client_golang/prometheus/promhttp"

	"github.com/jomcgi/homelab/projects/factory/connector/github"
	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
	"github.com/jomcgi/homelab/projects/factory/reviewer/config"
	"github.com/jomcgi/homelab/projects/factory/reviewer/metrics"
	"github.com/jomcgi/homelab/projects/factory/reviewer/review"
)

// reviewerTokenPath is the broker endpoint that mints the reviewer App's
// installation tokens.
const reviewerTokenPath = "/github/reviewer-token"

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stderr, nil))
	// A SIGTERM ends the loop with context.Canceled: a clean exit.
	if err := run(log); err != nil && !errors.Is(err, context.Canceled) {
		log.Error("reviewer stopped", "error", err)
		os.Exit(1)
	}
}

// fileToken reads a projected ServiceAccount token from its file on every
// call, so the kubelet's rotation needs no restart.
type fileToken string

func (f fileToken) Token() (string, error) {
	b, err := os.ReadFile(string(f))
	if err != nil {
		return "", fmt.Errorf("read broker token: %w", err)
	}
	t := strings.TrimSpace(string(b))
	if t == "" {
		return "", fmt.Errorf("broker token file %s is empty", f)
	}
	return t, nil
}

func run(log *slog.Logger) error {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()

	cfg, err := config.Load(os.Getenv)
	if err != nil {
		return err
	}

	reg := prometheus.NewRegistry()
	reg.MustRegister(collectors.NewGoCollector(), collectors.NewProcessCollector(collectors.ProcessCollectorOpts{}))
	m := metrics.New(reg)
	mux := http.NewServeMux()
	mux.Handle("/metrics", promhttp.HandlerFor(reg, promhttp.HandlerOpts{}))
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusOK) })
	srv := &http.Server{Addr: cfg.MetricsAddr, Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	go func() {
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Error("metrics server", "error", err)
		}
	}()
	defer func() {
		sctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = srv.Shutdown(sctx)
	}()

	gh := &github.Client{
		BaseURL: cfg.GitHubURL,
		Tokens: &github.BrokerTokens{
			URL: cfg.BrokerURL, Path: reviewerTokenPath, Bearer: fileToken(cfg.BrokerTokenFile),
		},
		ETags: &github.ETagCache{Observe: func(notModified bool) {
			if notModified {
				m.Conditional.WithLabelValues("not_modified").Inc()
			} else {
				m.Conditional.WithLabelValues("changed").Inc()
			}
		}},
	}
	a2a := &kagent.A2A{BaseURL: cfg.KagentURL, Tokens: &kagent.TokenSource{Path: cfg.KagentTokenFile}}

	var bindings []review.Binding
	var names []string
	for _, b := range cfg.Bindings {
		rb := review.Binding{
			Name: b.Name, Owner: b.Owner, Repo: b.Repo, BaseBranch: b.BaseBranch,
			Agent:      kagent.AgentRef{Namespace: b.KagentNamespace, Name: b.KagentAgent},
			SkipLabels: b.SkipLabels, EnqueueAuthors: b.EnqueueAuthors,
			HumanGatePaths: b.HumanGatePaths, HumanGateLabels: b.HumanGateLabels, HumanApprovers: b.HumanApprovers,
			TrustedCommenters: b.TrustedCommenters, ContextDocs: b.ContextDocs,
			GeneratedPaths: b.GeneratedPaths, MaxDiffLines: b.MaxDiffLines, MaxDiffFiles: b.MaxDiffFiles,
		}
		for _, c := range b.RequiredChecks {
			rb.RequiredChecks = append(rb.RequiredChecks, review.Check(c))
		}
		if len(rb.RequiredChecks) == 0 {
			log.Warn("binding requires no checks besides the review; a passing review enqueues alone", "binding", b.Name)
		}
		bindings = append(bindings, rb)
		names = append(names, b.Name)
	}
	eng := review.New(review.Config{
		CheckName:   cfg.CheckName,
		AppID:       cfg.AppID,
		Concurrency: cfg.Concurrency,
		TaskPoll:    time.Duration(cfg.Intervals.TaskPoll),
		Reissue:     time.Duration(cfg.Intervals.Reissue),
		Timeout:     time.Duration(cfg.Intervals.Timeout),
		RuleIDs:     cfg.RuleIDs,
		Bindings:    bindings,
	}, gh, a2a, m, log)
	log.Info("reviewer started", "bindings", names, "check", cfg.CheckName, "app_id", cfg.AppID,
		"concurrency", cfg.Concurrency, "poll", time.Duration(cfg.Intervals.Poll).String())
	return eng.Run(ctx, time.Duration(cfg.Intervals.Poll))
}
