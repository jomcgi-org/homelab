//go:build linux

package main

import (
	"context"
	"log/slog"
	"net"

	"github.com/jomcgi/homelab/projects/embervm/noded/guestagent"
	"github.com/jomcgi/homelab/projects/firecracker/substrate/guestvsock"
)

// startGuestAgent binds the guest control agent's vsock listener on the frozen
// port 1024 and serves it in a supervised goroutine for the guest's lifetime, so
// a post-resume clock resync (standing decision 7) works. A bind/listen failure
// is logged, not fatal: the clock agent is only exercised on a RELIGHT resume,
// and a fresh boot (the spike) must still bring k3s up. The context cancels the
// listener on shutdown.
func startGuestAgent(ctx context.Context, logger *slog.Logger) {
	ln, err := listenVsock(guestagent.GroupGuestAgentVsockPort)
	if err != nil {
		logger.Warn("guest control agent: vsock listen unavailable; no clock resync channel", "err", err)
		return
	}
	logger.Info("guest control agent: listening", "port", guestagent.GroupGuestAgentVsockPort)
	agent := guestagent.New(guestagent.RealClock(), logger)
	go func() {
		if err := agent.Serve(ln); err != nil {
			logger.Warn("guest control agent: serve stopped", "err", err)
		}
	}()
	go func() {
		<-ctx.Done()
		_ = ln.Close()
	}()
}

var listenGuestVsock = guestvsock.Listen

func listenVsock(port uint32) (net.Listener, error) {
	return listenGuestVsock(port, guestvsock.Options{
		ErrorPrefix: "ember-k3s-init: vsock ", FilePrefix: "vsock", WrapAcceptError: true,
	})
}
