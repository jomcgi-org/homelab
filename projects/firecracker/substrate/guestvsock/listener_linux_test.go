//go:build linux

package guestvsock

import (
	"errors"
	"fmt"
	"io"
	"os"
	"reflect"
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

var runtimeOptions = []struct {
	name string
	opts Options
}{
	{"ping", Options{FilePrefix: "vsock-http"}},
	{"postgres", Options{ErrorPrefix: "ember-postgres-init: vsock ", FilePrefix: "vsock-http", WrapAcceptError: true}},
	{"k3s", Options{ErrorPrefix: "ember-k3s-init: vsock ", FilePrefix: "vsock", WrapAcceptError: true}},
	{"shotter", Options{ErrorPrefix: "ember-shotter-init: vsock ", FilePrefix: "vsock-http", WrapAcceptError: true}},
	{"sandbox", Options{ErrorPrefix: "sandbox-guest-init: vsock ", FilePrefix: "vsock-http", WrapAcceptError: true}},
}

// These fakes validate every argument and reject calls after the failing step.
func recordingOps(t *testing.T, fail string, cause error, calls *[]string) socketOps {
	t.Helper()
	step := func(name string) error {
		*calls = append(*calls, name)
		if name == fail {
			return cause
		}
		return nil
	}
	return socketOps{
		socket: func(domain, typ, protocol int) (int, error) {
			if domain != unix.AF_VSOCK || typ != unix.SOCK_STREAM || protocol != 0 {
				t.Fatalf("socket(%d, %d, %d)", domain, typ, protocol)
			}
			return 41, step("socket")
		},
		bind: func(fd int, sa unix.Sockaddr) error {
			want := &unix.SockaddrVM{CID: unix.VMADDR_CID_ANY, Port: 1024}
			if fd != 41 || !reflect.DeepEqual(sa, want) {
				t.Fatalf("bind(%d, %+v), want %+v", fd, sa, want)
			}
			return step("bind")
		},
		listen: func(fd, backlog int) error {
			if fd != 41 || backlog != 16 {
				t.Fatalf("listen(%d, %d)", fd, backlog)
			}
			return step("listen")
		},
		accept: func(fd int) (int, unix.Sockaddr, error) {
			if fd != 41 {
				t.Fatalf("accept(%d)", fd)
			}
			return -1, nil, step("accept")
		},
		close: func(fd int) error {
			if fd != 41 {
				t.Fatalf("close(%d), accepted FD must belong to os.File", fd)
			}
			_ = step("close")
			return unix.EBADF // Cleanup errors must not replace the original error.
		},
	}
}

func TestLifecycleFailures(t *testing.T) {
	cause := errors.New("injected")
	for _, runtime := range runtimeOptions {
		for _, stage := range []string{"socket", "bind", "listen", "accept"} {
			t.Run(runtime.name+"/"+stage, func(t *testing.T) {
				var calls []string
				ops := recordingOps(t, stage, cause, &calls)
				ln, err := listen(1024, runtime.opts, ops)
				wantCalls := []string{"socket"}
				message := "socket: injected"
				if stage != "socket" {
					wantCalls = append(wantCalls, "bind")
					message = "bind port=1024: injected"
					if stage != "bind" {
						wantCalls = append(wantCalls, "listen")
						message = "listen port=1024: injected"
					}
					if stage != "accept" {
						wantCalls = append(wantCalls, "close")
					}
				}
				if stage == "accept" {
					if err != nil || ln == nil {
						t.Fatalf("listen: %v", err)
					}
					c, acceptErr := ln.Accept()
					err = acceptErr
					if c != nil {
						t.Fatal("connection returned on accept failure")
					}
					wantCalls = append(wantCalls, "accept")
					message = "accept: injected"
					if !runtime.opts.WrapAcceptError {
						message = "injected"
						if err != cause {
							t.Fatalf("ping accept lost error identity: %v", err)
						}
					}
				} else if ln != nil {
					t.Fatal("listener returned on setup failure")
				}
				if stage != "accept" || runtime.opts.WrapAcceptError {
					message = runtime.opts.ErrorPrefix + message
					if errors.Unwrap(err) != cause {
						t.Fatalf("lost wrapping: %v", err)
					}
				}
				if !errors.Is(err, cause) || err.Error() != message {
					t.Fatalf("got %v, want %q wrapping cause", err, message)
				}
				if !reflect.DeepEqual(calls, wantCalls) {
					t.Fatalf("calls %v, want %v", calls, wantCalls)
				}
				if stage == "accept" {
					// Failed accept leaves the listener open for another attempt.
					_, _ = ln.Accept()
					if closeErr := ln.Close(); closeErr != unix.EBADF {
						t.Fatalf("close error identity: %v", closeErr)
					}
					wantCalls = append(wantCalls, "accept", "close")
					if !reflect.DeepEqual(calls, wantCalls) {
						t.Fatalf("retry/close calls %v, want %v", calls, wantCalls)
					}
				}
			})
		}
	}
}

func TestAcceptedFileIOAndOwnership(t *testing.T) {
	for _, runtime := range runtimeOptions {
		t.Run(runtime.name, func(t *testing.T) {
			fds, err := unix.Socketpair(unix.AF_UNIX, unix.SOCK_STREAM|unix.SOCK_CLOEXEC, 0)
			if err != nil {
				t.Fatal(err)
			}
			peer := os.NewFile(uintptr(fds[1]), "peer")
			t.Cleanup(func() { _ = peer.Close() })
			var calls []string
			ops := recordingOps(t, "", nil, &calls)
			ops.accept = func(fd int) (int, unix.Sockaddr, error) {
				if fd != 41 {
					t.Fatalf("accept(%d)", fd)
				}
				calls = append(calls, "accept")
				return fds[0], &unix.SockaddrVM{CID: 9, Port: 77}, nil
			}
			ln, err := listen(1024, runtime.opts, ops)
			if err != nil {
				t.Fatal(err)
			}
			c, err := ln.Accept()
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() { _ = c.Close() })
			file := c.(*conn).File
			if file.Name() != runtime.opts.FilePrefix+":1024" || int(file.Fd()) != fds[0] {
				t.Fatalf("accepted file %q fd=%d", file.Name(), file.Fd())
			}
			for _, a := range []struct{ network, address string }{
				{ln.Addr().Network(), ln.Addr().String()},
				{c.LocalAddr().Network(), c.LocalAddr().String()},
				{c.RemoteAddr().Network(), c.RemoteAddr().String()},
			} {
				if a.network != "vsock" || (a.address != "vsock:0" && a.address != "vsock:1024") {
					t.Fatalf("address %+v", a)
				}
			}
			if ln.Addr().String() != "vsock:1024" || c.LocalAddr().String() != "vsock:0" || c.RemoteAddr().String() != "vsock:0" {
				t.Fatal("listener or zero-port connection address changed")
			}
			// Closing the listener cannot take ownership of an accepted descriptor.
			if err := ln.Close(); err != unix.EBADF {
				t.Fatalf("close: %v", err)
			}
			if _, err := peer.Write([]byte("in")); err != nil {
				t.Fatal(err)
			}
			buf := make([]byte, 2)
			if _, err := io.ReadFull(c, buf); err != nil || string(buf) != "in" {
				t.Fatalf("read %q: %v", buf, err)
			}
			if _, err := c.Write([]byte("ok")); err != nil {
				t.Fatal(err)
			}
			if _, err := io.ReadFull(peer, buf); err != nil || string(buf) != "ok" {
				t.Fatalf("peer read %q: %v", buf, err)
			}
			// A blocking descriptor wrapped by os.NewFile has no deadline support.
			for _, set := range []func(time.Time) error{c.SetDeadline, c.SetReadDeadline, c.SetWriteDeadline} {
				if err := set(time.Now()); !errors.Is(err, os.ErrNoDeadline) {
					t.Fatalf("deadline delegation: %v", err)
				}
			}
			if err := c.Close(); err != nil {
				t.Fatal(err)
			}
			if _, err := unix.FcntlInt(uintptr(fds[0]), unix.F_GETFD, 0); !errors.Is(err, unix.EBADF) {
				t.Fatalf("accepted descriptor still open: %v", err)
			}
			if _, err := c.Read(buf); !errors.Is(err, os.ErrClosed) {
				t.Fatalf("read after close: %v", err)
			}
			if _, err := c.Write(buf); !errors.Is(err, os.ErrClosed) {
				t.Fatalf("write after close: %v", err)
			}
			if _, err := peer.Read(buf); err != io.EOF {
				t.Fatalf("peer after close: %v", err)
			}
			if !reflect.DeepEqual(calls, []string{"socket", "bind", "listen", "accept", "close"}) {
				t.Fatalf("descriptor ownership calls: %v", calls)
			}
		})
	}
}

func TestPollableFileDeadlines(t *testing.T) {
	fds, err := unix.Socketpair(unix.AF_UNIX, unix.SOCK_STREAM|unix.SOCK_NONBLOCK|unix.SOCK_CLOEXEC, 0)
	if err != nil {
		t.Fatal(err)
	}
	peer := os.NewFile(uintptr(fds[1]), "peer")
	defer peer.Close()
	var calls []string
	ops := recordingOps(t, "", nil, &calls)
	ops.accept = func(int) (int, unix.Sockaddr, error) { return fds[0], nil, nil }
	ln, err := listen(1024, runtimeOptions[0].opts, ops)
	if err != nil {
		t.Fatal(err)
	}
	c, err := ln.Accept()
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	for _, mode := range []string{"read", "write", "both"} {
		t.Run(mode, func(t *testing.T) {
			past := time.Now().Add(-time.Second)
			var err error
			switch mode {
			case "read":
				err = c.SetReadDeadline(past)
			case "write":
				err = c.SetWriteDeadline(past)
			case "both":
				err = c.SetDeadline(past)
			}
			if err != nil {
				t.Fatal(err)
			}
			if mode != "write" {
				_, err = c.Read(make([]byte, 1))
			} else {
				_, err = c.Write([]byte("x"))
			}
			if !errors.Is(err, os.ErrDeadlineExceeded) {
				t.Fatalf("%s: %v", mode, err)
			}
			if err := c.SetDeadline(time.Time{}); err != nil {
				t.Fatal(err)
			}
		})
	}
	if _, err := fmt.Fprint(peer, "x"); err != nil {
		t.Fatal(err)
	}
	if _, err := io.ReadFull(c, make([]byte, 1)); err != nil {
		t.Fatalf("reset deadline: %v", err)
	}
}
