// Command guest is a tiny PID 1 for local snapshot and isolation checks.
package main

import (
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"sync"

	"github.com/jomcgi/homelab/projects/embervm/noded/vsockproto"
	"golang.org/x/sys/unix"
)

func main() {
	for _, m := range []struct{ source, target, kind string }{
		{"proc", "/proc", "proc"},
		{"tmpfs", "/tmp", "tmpfs"},
	} {
		if err := unix.Mount(m.source, m.target, m.kind, 0, ""); err != nil {
			log.Fatal(err)
		}
	}
	fd, err := unix.Socket(unix.AF_VSOCK, unix.SOCK_STREAM|unix.SOCK_CLOEXEC, 0)
	if err != nil {
		log.Fatal(err)
	}
	if err := unix.Bind(fd, &unix.SockaddrVM{CID: unix.VMADDR_CID_ANY, Port: vsockproto.GuestHTTPPort}); err != nil {
		log.Fatal(err)
	}
	if err := unix.Listen(fd, 16); err != nil {
		log.Fatal(err)
	}
	var mu sync.Mutex
	var memory string
	mux := http.NewServeMux()
	mux.HandleFunc("GET /shim/ready", func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(200) })
	mux.HandleFunc("/state", func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		defer mu.Unlock()
		if r.Method == http.MethodPut {
			body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, 4096))
			if err != nil {
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			if err := os.WriteFile("/tmp/marker", body, 0o600); err != nil {
				http.Error(w, err.Error(), 500)
				return
			}
			memory = string(body)
		} else if r.Method != http.MethodGet {
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		file, err := os.ReadFile("/tmp/marker")
		if err != nil && !os.IsNotExist(err) {
			http.Error(w, err.Error(), 500)
			return
		}
		_, _ = fmt.Fprintf(w, "%s\n%s", memory, file)
	})
	log.Fatal(http.Serve(&listener{fd: fd}, mux))
}

type listener struct{ fd int }

func (l *listener) Accept() (net.Conn, error) {
	fd, _, err := unix.Accept4(l.fd, unix.SOCK_CLOEXEC)
	if err != nil {
		return nil, err
	}
	return &connection{File: os.NewFile(uintptr(fd), "vsock")}, nil
}
func (l *listener) Close() error   { return unix.Close(l.fd) }
func (l *listener) Addr() net.Addr { return address{} }

type connection struct{ *os.File }

func (c *connection) LocalAddr() net.Addr  { return address{} }
func (c *connection) RemoteAddr() net.Addr { return address{} }

type address struct{}

func (address) Network() string { return "vsock" }
func (address) String() string  { return "vsock:1027" }
