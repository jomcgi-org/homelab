//go:build linux

package main

import (
	"net"

	"github.com/jomcgi/homelab/projects/firecracker/substrate/guestvsock"
)

var listenGuestVsock = guestvsock.Listen

// listenVsock serves the shim readiness contract used by warm-base snapshots.
func listenVsock(port uint32) (net.Listener, error) {
	return listenGuestVsock(port, guestvsock.Options{
		ErrorPrefix: "ember-postgres-init: vsock ", FilePrefix: "vsock-http", WrapAcceptError: true,
	})
}
