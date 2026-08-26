package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"
)

func main() {
	cfg, err := loadConfig()
	if err != nil {
		slog.Error("invalid conformance runner configuration", "error", err)
		os.Exit(2)
	}

	store := newVerdictStore(cfg.chartVersion)
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok\n"))
	})
	mux.HandleFunc("GET /verdict", store.verdictHandler)
	mux.HandleFunc("GET /metrics", store.metricsHandler)
	server := &http.Server{Addr: cfg.listenAddr, Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	client := &controlPlaneClient{
		baseURL:   strings.TrimRight(cfg.baseURL, "/"),
		tokenFile: cfg.tokenFile,
		http:      &http.Client{Timeout: 2 * time.Minute},
	}

	go runLoop(context.Background(), cfg, client, store)
	slog.Info("conformance runner listening", "address", cfg.listenAddr, "chart_version", cfg.chartVersion)
	if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		slog.Error("conformance HTTP server stopped", "error", err)
		os.Exit(1)
	}
}

func runLoop(ctx context.Context, cfg config, client *controlPlaneClient, store *verdictStore) {
	for {
		started := time.Now().UTC()
		store.start(started)
		readyCtx, cancelReady := context.WithTimeout(ctx, cfg.readyWait)
		err := waitForReady(readyCtx, client, cfg.taskWorkload, cfg.sessionWorkload)
		cancelReady()
		if err != nil {
			result := scenarioVerdict{ID: "S0", Verdict: verdictFail, Detail: fmt.Sprintf("workload readiness failed within READY_WAIT: %v", err), MS: time.Since(started).Milliseconds()}
			logScenario(result)
			store.finish(started, []scenarioVerdict{result})
		} else {
			totalBudget := cfg.budgets["S1"] + cfg.budgets["S2"] + cfg.budgets["S3"] + cfg.budgets["S4"]
			suiteCtx, cancelSuite := context.WithTimeout(ctx, totalBudget)
			scenarios := runScenarios(suiteCtx, cfg, client, started)
			cancelSuite()
			store.finish(started, scenarios)
		}

		if err := waitContext(ctx, cfg.runInterval); err != nil {
			return
		}
	}
}

func loadConfig() (config, error) {
	cfg := config{
		baseURL:              os.Getenv("EMBERVM_URL"),
		tokenFile:            envOr("EMBERVM_TOKEN_FILE", "/var/run/secrets/kubernetes.io/serviceaccount/token"),
		chartVersion:         os.Getenv("CHART_VERSION"),
		listenAddr:           envOr("LISTEN_ADDR", ":8080"),
		taskWorkload:         envOr("TASK_WORKLOAD", "sandbox-python"),
		sessionWorkload:      envOr("SESSION_WORKLOAD", "pi-runtime"),
		idleBankSeconds:      20,
		sweepGraceSeconds:    15,
		minPassingInvariants: 4,
		budgets:              make(map[string]time.Duration),
	}
	if cfg.baseURL == "" {
		return config{}, fmt.Errorf("EMBERVM_URL is required")
	}
	if cfg.chartVersion == "" {
		return config{}, fmt.Errorf("CHART_VERSION is required")
	}

	var err error
	if cfg.runInterval, err = durationEnv("RUN_INTERVAL", 30*time.Minute); err != nil {
		return config{}, err
	}
	if cfg.readyWait, err = durationEnv("READY_WAIT", 15*time.Minute); err != nil {
		return config{}, err
	}
	for key, fallback := range map[string]time.Duration{"S1": time.Minute, "S2": 2 * time.Minute, "S3": time.Minute, "S4": 10 * time.Second} {
		cfg.budgets[key], err = durationEnv(key+"_BUDGET", fallback)
		if err != nil {
			return config{}, err
		}
	}
	if cfg.idleBankSeconds, err = intEnv("IDLE_BANK_SECONDS", cfg.idleBankSeconds); err != nil {
		return config{}, err
	}
	if cfg.sweepGraceSeconds, err = intEnv("SWEEP_GRACE_SECONDS", cfg.sweepGraceSeconds); err != nil {
		return config{}, err
	}
	if cfg.minPassingInvariants, err = intEnv("MIN_PASSING_INVARIANTS", cfg.minPassingInvariants); err != nil {
		return config{}, err
	}
	return cfg, nil
}

func envOr(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}

func durationEnv(name string, fallback time.Duration) (time.Duration, error) {
	value := os.Getenv(name)
	if value == "" {
		return fallback, nil
	}
	parsed, err := time.ParseDuration(value)
	if err != nil || parsed <= 0 {
		return 0, fmt.Errorf("%s must be a positive duration", name)
	}
	return parsed, nil
}

func intEnv(name string, fallback int) (int, error) {
	value := os.Getenv(name)
	if value == "" {
		return fallback, nil
	}
	parsed, err := strconv.Atoi(value)
	if err != nil || parsed < 0 {
		return 0, fmt.Errorf("%s must be a non-negative integer", name)
	}
	return parsed, nil
}
