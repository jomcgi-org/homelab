// Command connector runs the factory connector's dispatch loop between
// itsaplan and kagent (projects/factory/ARCHITECTURE.md, "Connector").
//
//	connector                  run the loop
//	connector unblock SCOPE    clear a credential or quota block, for example
//	                           kagent:factory-homelab/coder
//	connector cancel ISSUE [--by NAME]
//	                           stop delivery for an issue, for example HL-7: the
//	                           running connector cancels its running turn and
//	                           dequeues its PR; --by defaults to $USER
package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"sync"
	"syscall"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/collectors"
	"github.com/prometheus/client_golang/prometheus/promhttp"

	"github.com/jomcgi/homelab/projects/factory/connector/config"
	"github.com/jomcgi/homelab/projects/factory/connector/dispatch"
	"github.com/jomcgi/homelab/projects/factory/connector/github"
	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
	"github.com/jomcgi/homelab/projects/factory/connector/metrics"
	"github.com/jomcgi/homelab/projects/factory/connector/migrations"
	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stderr, nil))
	var err error
	switch {
	case len(os.Args) == 3 && os.Args[1] == "unblock":
		err = unblock(os.Args[2])
	case len(os.Args) == 3 && os.Args[1] == "cancel":
		err = cancelIssue(os.Args[2], os.Getenv("USER"))
	case len(os.Args) == 5 && os.Args[1] == "cancel" && os.Args[3] == "--by":
		err = cancelIssue(os.Args[2], os.Args[4])
	default:
		err = run(log)
	}
	// A SIGTERM ends the loop with context.Canceled: a clean exit.
	if err != nil && !errors.Is(err, context.Canceled) {
		log.Error("connector stopped", "error", err)
		os.Exit(1)
	}
}

// operatorStore opens the database for an operator command.
func operatorStore() (*store.Store, error) {
	dsn := os.Getenv("FACTORY_CONNECTOR_DATABASE_URL")
	if dsn == "" {
		return nil, errors.New("FACTORY_CONNECTOR_DATABASE_URL is required")
	}
	return store.Open(dsn)
}

// cancelIssue is the operator's cancel: it stops delivery for the issue's
// work item. The running connector then cancels its review round through
// kagent and keeps it pending until kagent confirms.
func cancelIssue(issueKey, by string) error {
	if by == "" {
		return errors.New("name who cancels with --by")
	}
	st, err := operatorStore()
	if err != nil {
		return err
	}
	defer st.DB.Close()
	stopped, err := st.StopIssue(context.Background(), issueKey, "an operator ("+by+") canceled it with `connector cancel`", "operator:"+by)
	if err != nil {
		return err
	}
	if !stopped {
		return fmt.Errorf("%s is already stopped or done", issueKey)
	}
	fmt.Println("stopped", issueKey)
	return nil
}

// unblock is the operator's action that clears a grant block.
func unblock(scope string) error {
	st, err := operatorStore()
	if err != nil {
		return err
	}
	defer st.DB.Close()
	cleared, err := st.ClearGrantBlock(context.Background(), scope)
	if err != nil {
		return err
	}
	if !cleared {
		return fmt.Errorf("no active block for %s", scope)
	}
	fmt.Println("cleared", scope)
	return nil
}

func run(log *slog.Logger) error {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()

	cfg, err := config.Load(os.Getenv)
	if err != nil {
		return err
	}
	st, err := store.Open(cfg.DatabaseURL)
	if err != nil {
		return err
	}
	defer st.DB.Close()
	if err := migrations.Apply(ctx, st.DB); err != nil {
		return err
	}

	keys := map[string]string{}
	coordKey, err := config.ReadKey(cfg.File.Coordinator.APIKeyFile)
	if err != nil {
		return err
	}
	keys[cfg.File.Coordinator.Key] = coordKey
	var agents []dispatch.Agent
	for _, a := range cfg.File.Agents {
		k, err := config.ReadKey(a.APIKeyFile)
		if err != nil {
			return err
		}
		keys[a.Key] = k
		agents = append(agents, dispatch.Agent{Key: a.Key, Concurrency: a.Concurrency})
	}
	var bindings []dispatch.Binding
	for _, b := range cfg.File.Bindings {
		review := dispatch.ReviewPolicy{
			TrustedReviewers: b.TrustedReviewers, MaxReviewRounds: *b.MaxReviewRounds, DoneColumn: b.DoneColumn,
		}
		for _, c := range b.RequiredChecks {
			review.RequiredChecks = append(review.RequiredChecks, dispatch.Check(c))
		}
		if b.ReviewerCheck != nil {
			c := dispatch.Check(*b.ReviewerCheck)
			review.ReviewerCheck = &c
		}
		if review.ReviewerCheck == nil && len(review.TrustedReviewers) == 0 {
			log.Warn("binding has no reviewer check or trusted reviewers; its PRs are never enqueued", "binding", b.Name)
		}
		bindings = append(bindings, dispatch.Binding{
			Binding: store.Binding{
				Name: b.Name, Owner: b.Owner, Repo: b.Repo, BaseBranch: b.BaseBranch,
				ItsaplanProject: b.ItsaplanProject, KagentNamespace: b.KagentNamespace,
				KagentAgent: b.KagentAgent, BranchPrefix: b.BranchPrefix,
				MaxEngineeringRetries: *b.MaxEngineeringRetries,
			},
			Agents: b.Agents,
			Review: review,
		})
	}

	its := &itsaplan.Client{BaseURL: cfg.ItsaplanURL, Keys: keys, Coordinator: cfg.File.Coordinator.Key}
	tokens := &kagent.TokenSource{Path: cfg.KagentTokenFile}
	a2a := &kagent.A2A{BaseURL: cfg.KagentURL, Tokens: tokens}
	sessions, err := kagent.DialSessions(cfg.KagentGRPCTarget, tokens)
	if err != nil {
		return err
	}
	defer sessions.Close()

	// Delivery (PRs, review rounds, merge) needs the broker's GitHub tokens.
	var gh dispatch.GitHub
	if cfg.BrokerURL != "" {
		gh = &github.Client{BaseURL: cfg.GitHubURL, Tokens: &github.BrokerTokens{
			URL: cfg.BrokerURL, Bearer: &kagent.TokenSource{Path: cfg.BrokerTokenFile},
		}}
	} else {
		log.Warn("FACTORY_CONNECTOR_BROKER_URL is unset; delivery (PRs, review and merge) is off")
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
	defer srv.Close()

	eng := dispatch.New(dispatch.Config{
		Holder:          cfg.Holder,
		Agents:          agents,
		Bindings:        bindings,
		Heartbeat:       time.Duration(cfg.File.Intervals.Heartbeat),
		ReissueInterval: time.Duration(cfg.File.Intervals.ReissueInterval),
		DispatchWindow:  time.Duration(cfg.File.Intervals.DispatchWindow),
		ReviewPoll:      time.Duration(cfg.File.Intervals.ReviewPoll),
		Links:           dispatch.Links(cfg.File.Links),
	}, st, its, a2a, sessions, gh, m, log)

	// Wait for the writer lock: during a Recreate rollout the old pod may
	// still hold it.
	for {
		ok, err := eng.Acquire(ctx)
		if err != nil {
			// Acquire released the lock; exit and let the pod restart.
			return fmt.Errorf("acquire writer: %w", err)
		}
		if ok {
			break
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(time.Duration(cfg.File.Intervals.AcquireRetry)):
		}
	}
	defer eng.Release()
	log.Info("writer acquired", "epoch", eng.Epoch(), "holder", cfg.Holder)

	// Leases are kept by their own loop so a slow call in a tick cannot let
	// itsaplan expire a run. Either loop ending stops the process.
	// Deferred in this order, the heartbeat loop is stopped and waited for
	// before the deferred Release closes the writer's connection.
	ctx, cancel := context.WithCancelCause(ctx)
	var wg sync.WaitGroup
	defer wg.Wait()
	defer cancel(nil)
	wg.Add(1)
	go func() {
		defer wg.Done()
		err := eng.RunHeartbeats(ctx, time.Duration(cfg.File.Intervals.Heartbeat)/4)
		cancel(err)
	}()

	tick := time.NewTicker(time.Duration(cfg.File.Intervals.Poll))
	defer tick.Stop()
	for {
		if err := eng.Tick(ctx); err != nil {
			if errors.Is(err, store.ErrNotWriter) {
				// Another process took over; exit so nothing writes twice.
				return err
			}
			if ctx.Err() == nil {
				log.Error("tick", "error", err)
			}
		}
		select {
		case <-ctx.Done():
			if cause := context.Cause(ctx); cause != nil && !errors.Is(cause, context.Canceled) {
				return cause
			}
			return context.Canceled
		case <-tick.C:
		}
	}
}
