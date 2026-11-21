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
//	connector recovery ...    inspect holds or attest operator fencing
package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"sync"
	"syscall"
	"time"

	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/collectors"
	"github.com/prometheus/client_golang/prometheus/promhttp"

	"github.com/jomcgi/homelab/projects/factory/broker/auth"
	"github.com/jomcgi/homelab/projects/factory/connector/buildbuddy"
	"github.com/jomcgi/homelab/projects/factory/connector/config"
	"github.com/jomcgi/homelab/projects/factory/connector/dispatch"
	"github.com/jomcgi/homelab/projects/factory/connector/github"
	"github.com/jomcgi/homelab/projects/factory/connector/internal/tracing"
	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
	"github.com/jomcgi/homelab/projects/factory/connector/metrics"
	"github.com/jomcgi/homelab/projects/factory/connector/migrations"
	"github.com/jomcgi/homelab/projects/factory/connector/notify"
	"github.com/jomcgi/homelab/projects/factory/connector/status"
	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stderr, nil))
	var err error
	switch {
	case len(os.Args) == 2 && os.Args[1] == "runbook-deadline":
		err = runDeadline()
	case len(os.Args) == 2 && os.Args[1] == "runbook-kagent-revert":
		err = runRevert(log)
	case len(os.Args) == 2 && os.Args[1] == "reaper":
		err = runReaper(log)
	case len(os.Args) > 1 && os.Args[1] == "recovery":
		err = recoveryCLI(os.Args[2:])
	case len(os.Args) == 3 && os.Args[1] == "unblock":
		err = unblock(os.Args[2])
	case len(os.Args) == 3 && os.Args[1] == "cancel":
		err = cancelIssue(os.Args[2], os.Getenv("USER"))
	case len(os.Args) == 5 && os.Args[1] == "cancel" && os.Args[3] == "--by":
		err = cancelIssue(os.Args[2], os.Args[4])
	case len(os.Args) == 1:
		err = run(log)
	default:
		err = errors.New("usage: connector [unblock SCOPE | cancel ISSUE [--by NAME] | recovery ...]")
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
// work item and its held runs. The running connector then cancels its review round through
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
	if !stopped.Any() {
		return fmt.Errorf("%s is already stopped or done and has no held run", issueKey)
	}
	fmt.Printf("stopped %s: work item %t, held runs %d\n", issueKey, stopped.WorkItem, stopped.HeldRuns)
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
	tp, err := tracing.Init(ctx)
	if err != nil {
		return err
	}
	if tp != nil {
		defer func() {
			flush, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			_ = tp.Shutdown(flush)
		}()
	}

	cfg, err := config.Load(os.Getenv)
	if err != nil {
		return err
	}

	// Health and metrics come up first, so the pod is ready while it waits for
	// its keys and runs migrations: on a fresh install the itsaplan bootstrap
	// writes the keys only once the rest of the release is healthy.
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

	keys := cfg.File.KeyFiles()
	if err := keys.WaitForKeys(ctx, 30*time.Second, func(missing []string) {
		log.Info("waiting for itsaplan API keys", "missing", missing)
	}); err != nil {
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

	var agents []dispatch.Agent
	for _, a := range cfg.File.Agents {
		agents = append(agents, dispatch.Agent{Key: a.Key, Concurrency: a.Concurrency})
	}
	var bindings []dispatch.Binding
	for _, b := range cfg.File.Bindings {
		review := dispatch.ReviewPolicy{
			TrustedReviewers: b.TrustedReviewers, MaxReviewRounds: *b.MaxReviewRounds, DoneColumn: b.DoneColumn,
			MaxRebaseRounds: *b.MaxRebaseRounds, MaxSplitRounds: *b.MaxSplitRounds,
			HoldLabels: dispatch.DefaultHoldLabels,
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
			Arms:              b.Arms,
			Runtime:           b.Runtime,
			Agents:            b.Agents,
			Review:            review,
			HumanGateAssignee: *b.HumanGateAssignee,
		})
	}

	// Keys are read per call, so a key the bootstrap rotates takes effect
	// without a restart.
	limits := &itsaplan.RequestLimits{}
	its := &itsaplan.Client{Limits: limits, BaseURL: cfg.ItsaplanURL, KeyFunc: keys.Key, Coordinator: cfg.File.Coordinator.Key}
	tokens := &kagent.TokenSource{Path: cfg.KagentTokenFile}
	a2a := &kagent.A2A{BaseURL: cfg.KagentURL, Tokens: tokens}
	sessions, err := kagent.DialSessions(cfg.KagentGRPCTarget, tokens)
	if err != nil {
		return err
	}
	defer sessions.Close()

	// Delivery (PRs, review rounds, merge) needs the broker's GitHub tokens.
	// The broker's quota reading resumes work a quota failure paused.
	var gh dispatch.GitHub
	var quota dispatch.QuotaSource
	if cfg.BrokerURL != "" {
		bearer := &kagent.TokenSource{Path: cfg.BrokerTokenFile}
		gh = &github.Client{BaseURL: cfg.GitHubURL, Tokens: &github.BrokerTokens{URL: cfg.BrokerURL, Bearer: bearer}}
		quota = &github.BrokerQuota{URL: cfg.BrokerURL, Bearer: bearer}
	} else {
		log.Warn("FACTORY_CONNECTOR_BROKER_URL is unset; delivery (PRs, review and merge) is off")
	}

	var ciLogs dispatch.CILogs
	if bb := cfg.File.BuildBuddy; bb != nil {
		ciLogs = &buildbuddy.Client{
			APIURL: bb.APIURL, InvocationPrefix: bb.InvocationPrefix, KeyFile: bb.APIKeyFile,
			HTTP: &http.Client{Timeout: 30 * time.Second},
		}
	}
	var goalReasons dispatch.GoalReasonsSource
	if u := cfg.File.ReviewerGoalURL; u != "" {
		goalReasons = &dispatch.GoalReasonsClient{BaseURL: u, HTTP: &http.Client{Timeout: 10 * time.Second}}
	}
	eng := dispatch.New(dispatch.Config{
		// Native Codex artifact text reaches itsaplan run events (#8017). The
		// rollback target is chart 0.29.10, which resumes admitted journals
		// but admits no new ones (ARCHITECTURE.md "Rollback transition").
		ArtifactTranscripts: true,

		Caller: &kagent.InvocationCredentials{
			Tokens: tokens, Authority: cfg.KagentAuthority,
			A2AEndpoint: cfg.KagentURL, GRPCTarget: cfg.KagentGRPCTarget,
		},
		Holder:          cfg.Holder,
		Agents:          agents,
		Bindings:        bindings,
		Heartbeat:       time.Duration(cfg.File.Intervals.Heartbeat),
		ReissueInterval: time.Duration(cfg.File.Intervals.ReissueInterval),
		DispatchWindow:  time.Duration(cfg.File.Intervals.DispatchWindow),
		ReviewPoll:      time.Duration(cfg.File.Intervals.ReviewPoll),
		Quota:           quota,
		QuotaStall:      time.Duration(cfg.File.Intervals.QuotaStall),
		TaskLease:       time.Duration(cfg.File.Intervals.TaskLease),
		ProgressLease:   time.Duration(cfg.File.Intervals.ProgressLease),
		ToolLease:       time.Duration(cfg.File.Intervals.ToolLease),
		ForceDelete:     time.Duration(cfg.File.Intervals.ForceDelete),
		Links:           dispatch.Links(cfg.File.Links),
		CILogs:          ciLogs,
		GoalReasons:     goalReasons,

		PlannerSchedules: cfg.File.PlannerSchedules,
	}, st, its, a2a, sessions, gh, m, log)
	notifier, err := notify.New(notify.Config{
		URL: cfg.NotifyURL, TokenURL: cfg.NotifyTokenURL, AppPasswordFile: cfg.NotifyPasswordFile,
		BeforeRequest: eng.RecordNotifyAttempt,
	})
	if err != nil {
		return err
	}

	eng.NotifyOutage = func(ctx context.Context, phase string) error {
		return notifier.Notify(ctx, "error", notify.Text{Key: "itsaplan-outage/" + phase, Title: "Factory itsaplan outage", Status: "failed"})
	}

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

	// Status starts only after Acquire. Each outbound request checks the same
	// writer, and loss of it cancels every loop and in-progress read. Wait for
	// the status loop as well as heartbeats before releasing the lock.
	beginAttempt := func(ctx context.Context, messageID string) error {
		err := eng.BeginStatusRead(ctx, messageID)
		if errors.Is(err, store.ErrNotWriter) {
			cancel(err)
		}
		return err
	}
	statusHTTP := &http.Client{Timeout: status.ReadTimeout, Transport: &status.WriterTransport{Begin: beginAttempt}}
	statusPlan := &itsaplan.Client{
		Limits:  limits,
		BaseURL: cfg.ItsaplanURL, KeyFunc: keys.Key,
		Coordinator: cfg.File.Coordinator.Key, HTTP: statusHTTP,
	}
	var statusGitHub status.GitHub
	if cfg.BrokerURL != "" {
		statusGitHub = &github.Client{
			BaseURL: cfg.GitHubURL, HTTP: statusHTTP,
			Tokens: &github.BrokerTokens{
				URL: cfg.BrokerURL, HTTP: statusHTTP,
				Bearer: &kagent.TokenSource{Path: cfg.BrokerTokenFile},
			},
		}
	}
	statusSource := &status.Source{
		Store: st, Plan: statusPlan, GitHub: statusGitHub,
		Bindings: cfg.File.Bindings, IssueURL: cfg.File.Links.Issue,
	}
	statusHandler := &status.Handler{Load: statusSource.Load, Log: log}
	wg.Add(1)
	go func() {
		defer wg.Done()
		statusHandler.Run(ctx)
	}()
	clusterConfig, err := rest.InClusterConfig()
	if err != nil {
		return err
	}
	clusterConfig.NextProtos = []string{"http/1.1"}
	clusterConfig.Wrap(func(next http.RoundTripper) http.RoundTripper {
		return tokenReviewTransport{next: next, begin: beginAttempt}
	})
	kube, err := kubernetes.NewForConfig(clusterConfig)
	if err != nil {
		return err
	}

	decision := &itsaplan.DecisionHandler{
		WithFence: st.WithDecisionFence,
		Plan:      statusPlan,
		Origin:    os.Getenv("ITSAPLAN_APP_URL"),
		Password:  func() (string, error) { return config.ReadKey("/var/run/secrets/joe-sessions/password") },
	}
	for _, b := range cfg.File.Bindings {
		decision.Projects = append(decision.Projects, b.ItsaplanProject)
	}
	statusMux := http.NewServeMux()
	statusMux.Handle("/status", statusHandler)
	statusMux.Handle("POST /decide", decisionAccess(kube, decision))
	statusServer := &http.Server{
		Addr: cfg.StatusAddr, Handler: statusMux,
		ReadHeaderTimeout: 5 * time.Second, WriteTimeout: 35 * time.Second, IdleTimeout: 30 * time.Second,
	}
	go func() {
		if err := statusServer.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Error("status server", "error", err)
		}
	}()
	defer statusServer.Close()

	phaseMux := http.NewServeMux()
	phaseMux.HandleFunc("GET /run-phases", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		issueID, err := strconv.ParseInt(r.URL.Query().Get("issueId"), 10, 64)
		if err != nil || issueID < 1 {
			http.Error(w, "Invalid issue ID", http.StatusBadRequest)
			return
		}
		readCtx, cancel := context.WithTimeout(r.Context(), 1500*time.Millisecond)
		defer cancel()
		items, err := eng.RunPhases(readCtx, issueID)
		if err != nil {
			http.Error(w, "Run phases unavailable", http.StatusServiceUnavailable)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(struct {
			Items []dispatch.RunPhase `json:"items"`
		}{Items: items})
	})
	if reviewer := cfg.File.ReviewerGoalURL; reviewer != "" {
		client := &http.Client{CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
		phaseMux.HandleFunc("GET /issue-review", func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Cache-Control", "no-store")
			issueID, err := strconv.ParseInt(r.URL.Query().Get("issueId"), 10, 64)
			if err != nil || issueID < 1 {
				http.Error(w, "Invalid issue ID", http.StatusBadRequest)
				return
			}
			readCtx, cancel := context.WithTimeout(r.Context(), 1500*time.Millisecond)
			defer cancel()
			review, err := eng.IssueReviewQueue(readCtx, issueID, reviewer, client)
			if err != nil {
				http.Error(w, "Review queue unavailable", http.StatusServiceUnavailable)
				return
			}
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(review)
		})
	}

	phaseServer := &http.Server{
		Addr: ":8085", Handler: phaseMux, ReadHeaderTimeout: 5 * time.Second,
		WriteTimeout: 5 * time.Second, IdleTimeout: 30 * time.Second,
	}
	go func() {
		if err := phaseServer.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Error("run phase server", "error", err)
		}
	}()
	defer phaseServer.Close()

	wg.Add(1)
	go func() {
		defer wg.Done()
		err := eng.RunHeartbeats(ctx, time.Duration(cfg.File.Intervals.Heartbeat)/4)
		cancel(err)
	}()

	tick := time.NewTicker(time.Duration(cfg.File.Intervals.Poll))
	defer tick.Stop()
	smokeDone := false
	for {
		if !smokeDone {
			if err := notifier.Notify(ctx, "info", notify.Text{Key: notify.SmokeKey, Title: "Factory notify sink is live"}); err != nil {
				log.Warn("notify smoke pending", "error", err)
			} else {
				smokeDone = true
			}
		}
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

func decisionAccess(kube kubernetes.Interface, handler http.Handler) http.Handler {
	reviewer := auth.Reviewer{Client: kube, Audience: "factory-decide"}
	return reviewer.Require([]string{"system:serviceaccount:factory-landing:factory-landing", "system:serviceaccount:factory-discord:discord-bridge"}, handler)
}

type tokenReviewTransport struct {
	next  http.RoundTripper
	begin func(context.Context, string) error
}

func (t tokenReviewTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	const id = "decision/tokenreview"
	if err := t.begin(req.Context(), id); err != nil {
		return nil, err
	}
	req = req.Clone(req.Context())
	req.Close = true // Prevent transport retries on a reused connection.
	req.Header.Set("X-Request-ID", id)
	return t.next.RoundTrip(req)
}
