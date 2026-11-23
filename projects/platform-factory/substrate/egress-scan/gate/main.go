// engine-gate is the service behind the Semgrep tier of the egress scanner. It
// keeps a small pool of waiting, single-use gVisor fork children of the
// engine template, sends each scan request to one child through the
// atenet-router, and returns the engine reply. See README.md.
package main

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"sync"
	"syscall"
	"time"

	"github.com/prometheus/client_golang/prometheus/promhttp"
)

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stderr, nil))
	cfg, err := parseConfig(os.Args[1:])
	if err != nil {
		logger.Error("invalid configuration", "error", err.Error())
		os.Exit(2)
	}
	if err := run(cfg, logger); err != nil {
		logger.Error("engine-gate stopped", "error", err.Error())
		os.Exit(1)
	}
}

func run(cfg config, logger *slog.Logger) error {
	stop, cancelStop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancelStop()
	loopCtx, cancelLoop := context.WithCancel(context.Background())
	defer cancelLoop()

	m := newMetrics()
	log := newLimitedLog(logger)
	tlsSrc := newTLSSource(cfg.CAFile, cfg.RouterCert)
	conn, err := dialAPI(cfg, tlsSrc)
	if err != nil {
		return err
	}
	defer conn.Close()
	rt, err := newHTTPRouter(cfg.RouterURL, tlsSrc.config(cfg.RouterName, true), cfg.MaxResponse)
	if err != nil {
		return err
	}
	p := newPool(loopCtx, cfg, newGRPCAPI(conn, cfg.Atespace, cfg.Template), m, log)
	g := newGate(cfg, p, rt, m, log)

	var loop sync.WaitGroup
	loop.Add(1)
	go func() { defer loop.Done(); p.run(loopCtx) }()

	scan := &http.Server{
		Addr: cfg.Listen, Handler: g.handler(),
		ReadHeaderTimeout: 2 * time.Second, ReadTimeout: 10 * time.Second, WriteTimeout: 10 * time.Second,
		IdleTimeout: 30 * time.Second, MaxHeaderBytes: 16 << 10,
	}
	admin := &http.Server{
		Addr:              cfg.AdminListen,
		Handler:           adminHandler(p, m, promhttp.HandlerFor(m.reg, promhttp.HandlerOpts{})),
		ReadHeaderTimeout: 2 * time.Second, ReadTimeout: 5 * time.Second, WriteTimeout: 10 * time.Second,
	}
	errc := make(chan error, 2)
	for _, s := range []*http.Server{scan, admin} {
		go func() { errc <- s.ListenAndServe() }()
	}
	logger.Info("engine-gate started", "pod", cfg.PodName, "atespace", cfg.Atespace, "template", cfg.Template, "prefix", cfg.prefix())

	select {
	case <-stop.Done():
		logger.Info("shutting down")
	case err := <-errc:
		if !errors.Is(err, http.ErrServerClosed) {
			cancelLoop()
			loop.Wait()
			return err
		}
	}
	shutdownInOrder(cfg, p, scan, admin, func() { cancelLoop(); loop.Wait() })
	return nil
}

// server is the part of http.Server that the shutdown uses.
type server interface{ Shutdown(context.Context) error }

// shutdownInOrder is the stop sequence. /readyz fails first and the gate waits
// DrainDelay, so that the Service drops the pod before the listener closes.
// Then it stops the scan listener and finishes the requests in flight, stops
// the pool loop, deletes every child, and last closes the admin port, so
// probes and metrics work to the end. The whole sequence takes about
// DrainDelay + ShutdownGrace and the cleanup, so terminationGracePeriodSeconds
// must stay at 30 s or more.
func shutdownInOrder(cfg config, p *pool, scan, admin server, stopLoop func()) {
	p.drain()
	time.Sleep(cfg.DrainDelay)
	grace, cancel := context.WithTimeout(context.Background(), cfg.ShutdownGrace)
	defer cancel()
	_ = scan.Shutdown(grace)
	stopLoop()
	p.shutdown(grace)
	_ = admin.Shutdown(grace)
}
