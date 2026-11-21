package main

import (
	"context"
	"errors"
	"flag"
	"io"
	"log"
	"net"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	authv3 "github.com/envoyproxy/go-control-plane/envoy/service/auth/v3"
	"github.com/spf13/viper"
	"github.com/zricethezav/gitleaks/v8/config"
	"github.com/zricethezav/gitleaks/v8/logging"
	"google.golang.org/grpc"
	"google.golang.org/grpc/health"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
)

func main() {
	addr := flag.String("listen", ":8080", "listen address")
	rules := flag.String("rules", "", "path to operator-supplied Gitleaks TOML")
	concurrency := flag.Int("concurrency", 8, "maximum simultaneous scans")
	flag.Parse()
	if *concurrency < 1 || *concurrency > 64 {
		log.Fatal("concurrency must be between 1 and 64")
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
	authv3.RegisterAuthorizationServer(server, &scanner{detector: detector, slots: make(chan struct{}, *concurrency)})
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
