// Command ping is the minimal HTTP guest used to prove the EmberVM serving
// path. It has no outbound dependencies and serves only health and identity.
//
// One handler set, two listeners:
//
//   - AF_VSOCK on vsockproto.GuestHTTPPort: the readiness contract BuildBase
//     probes over HTTP-over-vsock before cutting a base snapshot (the tap NIC
//     does not exist during the build boot).
//   - TCP :8080 on 0.0.0.0: live serving traffic, DNAT'd by noded onto the
//     tap interface once the VM is published.
package main

import (
	"context"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/jomcgi/homelab/projects/firecracker/substrate/vsockproto"
)

const (
	listenAddress   = ":8080"
	vsockHTTPPort   = vsockproto.GuestHTTPPort
	shutdownGraceMs = 2000
)

func newMux(hostname string) *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	})
	mux.HandleFunc("GET /{$}", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		_, _ = fmt.Fprintf(w, "embervm ping from %s at %s\n", hostname, time.Now().UTC().Format(time.RFC3339Nano))
	})
	return mux
}

func main() {
	hostname, err := os.Hostname()
	if err != nil {
		log.Fatalf("read hostname: %v", err)
	}
	mux := newMux(hostname)

	vln, err := listenVsock(vsockHTTPPort)
	if err != nil {
		log.Fatalf("vsock listen port=%d: %v", vsockHTTPPort, err)
	}
	tln, err := net.Listen("tcp", listenAddress)
	if err != nil {
		log.Fatalf("tcp listen %s: %v", listenAddress, err)
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	srv := &http.Server{Handler: mux}
	errCh := make(chan error, 2)
	go func() { errCh <- srv.Serve(vln) }()
	go func() { errCh <- srv.Serve(tln) }()

	log.Printf("embervm ping listening on tcp:%s and vsock:%d", listenAddress, vsockHTTPPort)
	select {
	case err := <-errCh:
		if err != nil && err != http.ErrServerClosed {
			log.Fatalf("serve: %v", err)
		}
	case <-ctx.Done():
	}

	shutdownCtx, cancel := context.WithTimeout(context.Background(), shutdownGraceMs*time.Millisecond)
	defer cancel()
	_ = srv.Shutdown(shutdownCtx)
}
