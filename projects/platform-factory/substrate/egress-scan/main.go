package main

import (
	"context"
	"errors"
	"flag"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	authv3 "github.com/envoyproxy/go-control-plane/envoy/service/auth/v3"
	"github.com/jomcgi/homelab/projects/platform-factory/substrate/egress-scan/detect"
	"github.com/jomcgi/homelab/projects/platform-factory/substrate/egress-scan/detect/gitleaks"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"google.golang.org/grpc"
	"google.golang.org/grpc/health"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
)

func main() {
	addr := flag.String("listen", ":8080", "listen address")
	metricsAddr := flag.String("metrics-listen", ":9090", "Prometheus metrics listen address")
	rules := flag.String("rules", "", "path to operator-supplied Gitleaks TOML (the gitleaks detector only)")
	detectorName := flag.String("detector", gitleaks.Name, "detector name: one of "+strings.Join(detect.Names(), ", "))
	detectorMode := flag.String("detector-mode", string(detect.ModeEnforce), "enforce: a finding denies; observe: counted, never denies")
	detectorFailure := flag.String("detector-failure-mode", string(detect.FailDeny), "when the detector gives no verdict: deny or allow")
	detectorDeadline := flag.Duration("detector-deadline", maxScanTime, "detector time for one request; keep the gateway requestTimeout at least 5ms above it")
	detectorMaxInput := flag.Int("detector-max-input-bytes", maxBody, "largest input in bytes that a detector call takes; larger is a failure")
	detectorData := flag.String("detector-data-path", "", "mounted path of the private data of a detector that holds any")
	concurrency := flag.Int("concurrency", 8, "maximum simultaneous scans")
	flag.Parse()
	if *concurrency < 1 || *concurrency > 64 {
		log.Fatal("concurrency must be between 1 and 64")
	}
	options := map[string]string{}
	if *rules != "" {
		options[gitleaks.OptionRules] = *rules
	}
	host, err := detect.New(detect.Config{
		Name:        *detectorName,
		Mode:        detect.Mode(*detectorMode),
		FailureMode: detect.FailureMode(*detectorFailure),
		// Abandoned calls count: 4 for each scan slot bounds the CPU that calls past
		// their deadline can hold.
		Limits:   detect.Limits{MaxInputBytes: *detectorMaxInput, Deadline: *detectorDeadline, MaxConcurrent: 4 * *concurrency},
		DataPath: *detectorData,
		Options:  options,
	})
	if err != nil {
		log.Fatal("cannot load the secret detector: ", err)
	}

	sock, err := net.Listen("tcp", *addr)
	if err != nil {
		log.Fatal(err)
	}
	server := grpc.NewServer(grpc.MaxRecvMsgSize(maxCheckMessage))
	registry := prometheus.NewRegistry()
	authv3.RegisterAuthorizationServer(server, newMeteredScanner(&scanner{host: host, slots: make(chan struct{}, *concurrency)}, registry))
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
