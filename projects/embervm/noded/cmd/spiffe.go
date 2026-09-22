package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"sync"
	"time"

	"github.com/spiffe/go-spiffe/v2/bundle/x509bundle"
	"github.com/spiffe/go-spiffe/v2/spiffetls/tlsconfig"
	"github.com/spiffe/go-spiffe/v2/svid/x509svid"
	"github.com/spiffe/go-spiffe/v2/workloadapi"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/keepalive"

	"github.com/jomcgi/homelab/projects/embervm/noded/config"
	nodev1 "github.com/jomcgi/homelab/projects/embervm/proto/embervm/node/v1"
)

const (
	nodedX509SourceTimeout = 60 * time.Second
	// ADR embervm/041 decision 7 bounds established channels so rotated SVIDs
	// are consumed on a new handshake.
	nodedMaxConnectionAge      = time.Hour
	nodedMaxConnectionAgeGrace = 5 * time.Minute
)

type spiffeX509Source interface {
	x509svid.Source
	x509bundle.Source
	io.Closer
}

type x509SourceFactory func(context.Context) (spiffeX509Source, error)

func newWorkloadX509Source(ctx context.Context) (spiffeX509Source, error) {
	return workloadapi.NewX509Source(ctx)
}

func waitForX509Source(ctx context.Context, timeout time.Duration, create x509SourceFactory) (spiffeX509Source, error) {
	sourceCtx, cancelSource := context.WithTimeout(ctx, timeout)
	defer cancelSource()
	source, err := create(sourceCtx)
	if err != nil {
		return nil, fmt.Errorf("SPIFFE X509 source did not deliver an SVID within %s: %w", timeout, err)
	}
	if _, err := source.GetX509SVID(); err != nil {
		_ = source.Close()
		return nil, fmt.Errorf("SPIFFE X509 source has no SVID: %w", err)
	}
	return source, nil
}

type spiffeKeepaliveConfig struct {
	maxConnectionAge      time.Duration
	maxConnectionAgeGrace time.Duration
}

var defaultSPIFFEKeepalive = spiffeKeepaliveConfig{
	maxConnectionAge:      nodedMaxConnectionAge,
	maxConnectionAgeGrace: nodedMaxConnectionAgeGrace,
}

func spiffeServerOptions(source spiffeX509Source, cfg config.Config, connectionAge spiffeKeepaliveConfig) []grpc.ServerOption {
	tlsConfig := tlsconfig.MTLSServerConfig(
		source,
		source,
		tlsconfig.AuthorizeOneOf(cfg.SPIFFEClientIDs...),
	)
	return []grpc.ServerOption{
		grpc.Creds(credentials.NewTLS(tlsConfig)),
		grpc.KeepaliveParams(keepalive.ServerParameters{
			MaxConnectionAge:      connectionAge.maxConnectionAge,
			MaxConnectionAgeGrace: connectionAge.maxConnectionAgeGrace,
		}),
		grpc.ChainUnaryInterceptor(unaryRecoveryInterceptor(), unaryTraceContextInterceptor()),
	}
}

type nodedGRPCListener struct {
	name     string
	address  string
	listener net.Listener
	server   *grpc.Server
}

type nodedGRPCServers struct {
	listeners []nodedGRPCListener
	source    spiffeX509Source
	closeOnce sync.Once
}

func newNodedGRPCServers(
	ctx context.Context,
	cfg config.Config,
	service nodev1.NodeServiceServer,
	createSource x509SourceFactory,
	connectionAge spiffeKeepaliveConfig,
) (_ *nodedGRPCServers, err error) {
	servers := &nodedGRPCServers{}
	defer func() {
		if err != nil {
			servers.Close()
		}
	}()

	// Build and bind the explicitly requested TLS listener first. Plaintext is
	// not started, or even bound, if credentials or the TLS address are invalid.
	if cfg.SPIFFEEnabled {
		servers.source, err = waitForX509Source(ctx, nodedX509SourceTimeout, createSource)
		if err != nil {
			return nil, err
		}
		listener, listenErr := net.Listen("tcp", cfg.TLSListenAddr)
		if listenErr != nil {
			return nil, fmt.Errorf("SPIFFE mTLS gRPC listen on %s: %w", cfg.TLSListenAddr, listenErr)
		}
		server := grpc.NewServer(spiffeServerOptions(servers.source, cfg, connectionAge)...)
		nodev1.RegisterNodeServiceServer(server, service)
		servers.listeners = append(servers.listeners, nodedGRPCListener{
			name: "SPIFFE mTLS", address: cfg.TLSListenAddr, listener: listener, server: server,
		})
	}

	if cfg.PlaintextGRPCEnabled {
		listener, listenErr := net.Listen("tcp", cfg.ListenAddr)
		if listenErr != nil {
			return nil, fmt.Errorf("plaintext gRPC listen on %s: %w", cfg.ListenAddr, listenErr)
		}
		server := grpc.NewServer(grpcServerOptions(cfg.BearerToken)...)
		nodev1.RegisterNodeServiceServer(server, service)
		servers.listeners = append(servers.listeners, nodedGRPCListener{
			name: "plaintext", address: cfg.ListenAddr, listener: listener, server: server,
		})
	}
	if len(servers.listeners) == 0 {
		return nil, errors.New("at least one noded gRPC listener must be enabled")
	}
	return servers, nil
}

func (s *nodedGRPCServers) Serve(logger *slog.Logger) <-chan error {
	errors := make(chan error, len(s.listeners))
	for index := range s.listeners {
		listener := &s.listeners[index]
		go func() {
			logger.Info("gRPC NodeService listening", "transport", listener.name, "addr", listener.address)
			if err := listener.server.Serve(listener.listener); err != nil {
				errors <- fmt.Errorf("%s gRPC listener stopped: %w", listener.name, err)
			}
		}()
	}
	return errors
}

func (s *nodedGRPCServers) GracefulStop(timeout time.Duration, logger *slog.Logger) {
	done := make(chan struct{})
	go func() {
		var wait sync.WaitGroup
		wait.Add(len(s.listeners))
		for index := range s.listeners {
			server := s.listeners[index].server
			go func() {
				defer wait.Done()
				server.GracefulStop()
			}()
		}
		wait.Wait()
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(timeout):
		logger.Warn("graceful stop budget exceeded; forcing all gRPC listeners to stop")
		for index := range s.listeners {
			s.listeners[index].server.Stop()
		}
		<-done
	}
}

func (s *nodedGRPCServers) Close() {
	s.closeOnce.Do(func() {
		for index := range s.listeners {
			s.listeners[index].server.Stop()
			_ = s.listeners[index].listener.Close()
		}
		if s.source != nil {
			_ = s.source.Close()
		}
	})
}
