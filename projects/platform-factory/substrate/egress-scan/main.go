package main

import (
	"context"
	"errors"
	"flag"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	authv3 "github.com/envoyproxy/go-control-plane/envoy/service/auth/v3"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"github.com/spf13/viper"
	"github.com/zricethezav/gitleaks/v8/config"
	"github.com/zricethezav/gitleaks/v8/logging"
	"google.golang.org/grpc"
	"google.golang.org/grpc/health"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
)

func main() {
	addr := flag.String("listen", ":8080", "listen address")
	metricsAddr := flag.String("metrics-listen", ":9090", "Prometheus metrics listen address")
	rules := flag.String("rules", "", "path to operator-supplied Gitleaks TOML")
	concurrency := flag.Int("concurrency", 8, "maximum simultaneous scans")
	semgrepEnabled := flag.Bool("semgrep-enabled", false, "send each request's scanned text to a Semgrep engine as a second scan tier")
	semgrep := semgrepConfig{}
	flag.StringVar(&semgrep.Endpoint, "semgrep-endpoint", "", "URL of the Semgrep engine (a cluster address; no TLS or credentials are added)")
	flag.DurationVar(&semgrep.Deadline, "semgrep-deadline", 300*time.Millisecond, "engine deadline per request; keep the gateway requestTimeout at least 10ms above it")
	flag.StringVar(&semgrep.FailureMode, "semgrep-failure-mode", failureDeny, "when the engine gives no verdict: deny or allow")
	flag.IntVar(&semgrep.MaxBody, "semgrep-max-body", maxBody, "largest request body in bytes that is sent to the engine")
	flag.IntVar(&semgrep.Concurrency, "semgrep-concurrency", 8, "maximum simultaneous engine calls; more are shed")
	flag.Int64Var(&semgrep.MaxResponse, "semgrep-max-response", 1<<20, "largest engine response in bytes")
	flag.IntVar(&semgrep.BreakerFailures, "semgrep-breaker-failures", 5, "consecutive engine failures that open the breaker; 0 turns it off")
	flag.DurationVar(&semgrep.BreakerCooldown, "semgrep-breaker-cooldown", 5*time.Second, "time the breaker stays open before one probe call")
	flag.Parse()
	if *concurrency < 1 || *concurrency > 64 {
		log.Fatal("concurrency must be between 1 and 64")
	}
	var tier *semgrepTier
	if *semgrepEnabled {
		engine, err := newHTTPEngine(semgrep.Endpoint, semgrep.MaxResponse)
		if err != nil {
			log.Fatal(err)
		}
		if tier, err = newSemgrepTier(semgrep, engine); err != nil {
			log.Fatal(err)
		}
	}

	// Library diagnostics can contain source-derived fields.
	logging.Logger = logging.Logger.Output(io.Discard)
	detector, err := newDetector(*rules)
	if err != nil {
		log.Fatal("cannot load secret scan rules")
	}

	sock, err := net.Listen("tcp", *addr)
	if err != nil {
		log.Fatal(err)
	}
	server := grpc.NewServer(grpc.MaxRecvMsgSize(maxCheckMessage))
	registry := prometheus.NewRegistry()
	authv3.RegisterAuthorizationServer(server, newMeteredScanner(&scanner{detector: detector, slots: make(chan struct{}, *concurrency), semgrep: tier}, registry))
	metricsListener, err := net.Listen("tcp", *metricsAddr)
	if err != nil {
		log.Fatal(err)
	}
	mux := http.NewServeMux()
	mux.Handle("GET /metrics", promhttp.HandlerFor(registry, promhttp.HandlerOpts{}))
	metricsServer := &http.Server{
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
		WriteTimeout:      10 * time.Second,
	}
	defer metricsServer.Close()
	go func() {
		if err := metricsServer.Serve(metricsListener); !errors.Is(err, http.ErrServerClosed) {
			log.Printf("metrics server stopped: %v", err)
		}
	}()

	healthServer := health.NewServer()
	healthpb.RegisterHealthServer(server, healthServer)
	healthServer.SetServingStatus("", healthpb.HealthCheckResponse_SERVING)

	signalCtx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	go func() {
		<-signalCtx.Done()
		healthServer.Shutdown()
		timer := time.AfterFunc(3*time.Second, server.Stop)
		server.GracefulStop()
		timer.Stop()
	}()
	if err := server.Serve(sock); err != nil {
		log.Fatal(err)
	}
}

func newDetector(path string) (*requestDetector, error) {
	loader := viper.New()
	loader.SetConfigType("toml")
	var err error
	if path == "" {
		err = loader.ReadConfig(strings.NewReader(config.DefaultConfig))
	} else {
		loader.SetConfigFile(path)
		err = loader.ReadInConfig()
	}
	if err != nil {
		return nil, err
	}
	var raw config.ViperConfig
	if err := loader.Unmarshal(&raw); err != nil {
		return nil, err
	}
	if raw.Extend.Path != "" || raw.Extend.URL != "" {
		return nil, errors.New("request rules can extend the embedded rules only")
	}
	for _, rule := range raw.Rules {
		if path != "" && rule.Path != "" {
			return nil, errors.New("operator request rules cannot require a file path")
		}
	}
	rules, err := raw.Translate()
	if err != nil {
		return nil, err
	}
	return compileRequestRules(rules)
}
