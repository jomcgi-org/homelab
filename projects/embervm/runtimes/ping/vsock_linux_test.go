//go:build linux

package main

import (
	"errors"
	"net"
	"testing"

	"github.com/jomcgi/homelab/projects/firecracker/substrate/guestvsock"
)

func TestListenVsockAdapter(t *testing.T) {
	original := listenGuestVsock
	t.Cleanup(func() { listenGuestVsock = original })
	want := guestvsock.Options{ErrorPrefix: "", FilePrefix: "vsock-http", WrapAcceptError: false}
	cause := errors.New("listener failure")
	sentinel := &adapterListener{}
	for _, port := range []uint32{0, 1024, 1025, 65535, ^uint32(0)} {
		for _, failure := range []bool{false, true} {
			called := false
			listenGuestVsock = func(gotPort uint32, options guestvsock.Options) (net.Listener, error) {
				called = true
				if gotPort != port || options != want {
					t.Fatalf("listen port=%d options=%+v, want port=%d options=%+v", gotPort, options, port, want)
				}
				if failure {
					return nil, cause
				}
				return sentinel, nil
			}
			ln, err := listenVsock(port)
			if !called {
				t.Fatal("shared listener was not called")
			}
			if failure {
				if ln != nil || err != cause {
					t.Fatalf("failure forwarding: %v, %v", ln, err)
				}
			} else if ln != sentinel || err != nil {
				t.Fatalf("listener forwarding: %v, %v", ln, err)
			}
		}
	}
}

type adapterListener struct{ net.Listener }
