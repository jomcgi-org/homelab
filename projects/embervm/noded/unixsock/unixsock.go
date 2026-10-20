// Package unixsock dials Unix domain sockets whose path may be longer than
// Linux's sockaddr_un holds.
//
// A jailed VM's host-side socket aliases live under its jail tree, and on a
// deep snapshot root they can exceed the 108-byte sun_path. connect(2) then
// fails with EINVAL before it resolves anything, which a readiness loop that
// retries every error turns into a timeout (the 2026-10-19 embervm-dev jailer
// preflight). Dial reaches such a path through a short /proc/self/fd name for
// its directory, the same technique egress.listenUnix uses to bind one.
package unixsock

import (
	"context"
	"fmt"
	"net"
	"os"
	"path/filepath"
)

// MaxPathLen is the size of sockaddr_un.sun_path on Linux. A path must be
// shorter than this to fit with its terminating NUL.
const MaxPathLen = 108

// Dial connects to the Unix socket at path. A path that fits is dialled as is.
// A longer one is dialled as /proc/self/fd/<dir>/<base>: the kernel resolves
// the directory descriptor, and a symlink at base, during connect, and a
// connected socket never needs the path again, so the directory is closed as
// soon as the dial returns.
func Dial(ctx context.Context, path string) (net.Conn, error) {
	return DialWith(ctx, &net.Dialer{}, path)
}

// DialWith is Dial through d, so a caller keeps its own per-attempt timeout.
func DialWith(ctx context.Context, d *net.Dialer, path string) (net.Conn, error) {
	if len(path) < MaxPathLen {
		return d.DialContext(ctx, "unix", path)
	}
	dir, err := os.Open(filepath.Dir(path))
	if err != nil {
		return nil, fmt.Errorf("unixsock: open directory of %q: %w", path, err)
	}
	defer dir.Close()
	short := fmt.Sprintf("/proc/self/fd/%d/%s", dir.Fd(), filepath.Base(path))
	if len(short) >= MaxPathLen {
		return nil, fmt.Errorf("unixsock: socket name %q is too long to dial", filepath.Base(path))
	}
	conn, err := d.DialContext(ctx, "unix", short)
	if err != nil {
		return nil, fmt.Errorf("unixsock: dial %q: %w", path, err)
	}
	return conn, nil
}
