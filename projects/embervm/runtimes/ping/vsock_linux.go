//go:build linux

package main

import (
	"net"

	"github.com/jomcgi/homelab/projects/firecracker/substrate/guestvsock"
)

var listenGuestVsock = guestvsock.Listen

func listenVsock(port uint32) (net.Listener, error) {
	return listenGuestVsock(port, guestvsock.Options{FilePrefix: "vsock-http"})
}
