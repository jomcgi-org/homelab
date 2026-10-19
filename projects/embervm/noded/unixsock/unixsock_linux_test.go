package unixsock

import (
	"context"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// longDir returns a directory whose api.sock path is exactly length bytes,
// built under a short temp root so the listener can bind it through a short
// alias.
func longDir(t *testing.T, length int) (dir, sock string) {
	t.Helper()
	root, err := os.MkdirTemp("/tmp", "us")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(root) })
	dir = root
	// Each segment adds len+1 bytes ("/" + name). Keep the last one at least
	// two bytes long by never leaving a one-byte remainder.
	for {
		rest := length - len(filepath.Join(dir, "api.sock"))
		if rest <= 0 {
			break
		}
		seg := min(rest, 40)
		if rest-seg == 1 {
			seg--
		}
		dir = filepath.Join(dir, strings.Repeat("d", seg-1))
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	sock = filepath.Join(dir, "api.sock")
	if len(sock) != length {
		t.Fatalf("built a %d-byte socket path, want exactly %d", len(sock), length)
	}
	return dir, sock
}

// serve binds a listener in dir through a short symlink, the way Firecracker
// binds its chroot-relative socket, and answers each connection with "ok".
func serve(t *testing.T, dir string) {
	t.Helper()
	short, err := os.MkdirTemp("/tmp", "ul")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(short) })
	link := filepath.Join(short, "d")
	if err := os.Symlink(dir, link); err != nil {
		t.Fatal(err)
	}
	ln, err := net.Listen("unix", filepath.Join(link, "api.sock"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { ln.Close() })
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			_, _ = io.WriteString(c, "ok")
			c.Close()
		}
	}()
}

func TestDialReachesSocketsAtAndPastTheSunPathLimit(t *testing.T) {
	for _, length := range []int{MaxPathLen - 10, MaxPathLen - 1, MaxPathLen, MaxPathLen + 2, 200} {
		t.Run(fmt.Sprint(length), func(t *testing.T) {
			dir, sock := longDir(t, length)
			serve(t, dir)
			if length >= MaxPathLen {
				// The bug this package exists for: a plain dial cannot even try.
				if c, err := net.Dial("unix", sock); err == nil {
					c.Close()
					t.Fatalf("plain dial of a %d-byte path succeeded; the test is not exercising the limit", len(sock))
				}
			}
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			conn, err := Dial(ctx, sock)
			if err != nil {
				t.Fatalf("Dial(%d bytes): %v", len(sock), err)
			}
			defer conn.Close()
			got, _ := io.ReadAll(conn)
			if string(got) != "ok" {
				t.Fatalf("read %q, want ok", got)
			}
		})
	}
}

// A jailed VM's host alias is a symlink into the jail root; Dial follows it.
func TestDialFollowsASymlinkedAlias(t *testing.T) {
	jail, target := longDir(t, 60)
	serve(t, jail)
	aliasDir, alias := longDir(t, 130)
	_ = aliasDir
	if err := os.Symlink(target, alias); err != nil {
		t.Fatal(err)
	}
	conn, err := Dial(context.Background(), alias)
	if err != nil {
		t.Fatalf("Dial through a %d-byte symlink alias: %v", len(alias), err)
	}
	conn.Close()
}

func TestDialReportsAMissingSocket(t *testing.T) {
	_, sock := longDir(t, 150)
	if _, err := Dial(context.Background(), sock); err == nil {
		t.Fatal("Dial of a socket that does not exist succeeded")
	}
}
