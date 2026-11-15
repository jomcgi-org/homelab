// Package pgtest runs a real PostgreSQL 16 for tests from the binaries Bazel
// extracts into @postgres_test, the way projects/monolith/shared/testing
// does for pytest. Each test gets its own database with the connector's
// migrations applied.
package pgtest

import (
	"context"
	"database/sql"
	"fmt"
	"net"
	"os"
	"os/exec"
	"os/user"
	"path/filepath"
	"strconv"
	"sync"
	"sync/atomic"
	"syscall"
	"testing"
	"time"

	_ "github.com/lib/pq" // registers the postgres driver

	"github.com/jomcgi/homelab/projects/factory/connector/migrations"
)

// Server is one running postgres.
type Server struct {
	port int
	cmd  *exec.Cmd
	dir  string
}

var dbSeq atomic.Int64

// Main starts postgres, runs the package's tests and stops it. Call it from
// TestMain.
func Main(m *testing.M) int {
	srv, err := start()
	if err != nil {
		fmt.Fprintln(os.Stderr, "pgtest:", err)
		return 1
	}
	current = srv
	code := m.Run()
	srv.stop()
	return code
}

var (
	current *Server
	mu      sync.Mutex
)

// DSN returns a connection string for the named database.
func (s *Server) DSN(db string) string {
	return fmt.Sprintf("host=127.0.0.1 port=%d user=test dbname=%s sslmode=disable", s.port, db)
}

// NewDB creates an empty, migrated database for one test and returns its DSN.
func NewDB(t testing.TB) string {
	t.Helper()
	mu.Lock()
	srv := current
	mu.Unlock()
	if srv == nil {
		t.Fatal("pgtest: call pgtest.Main from TestMain")
	}
	name := fmt.Sprintf("t%d_%d", os.Getpid(), dbSeq.Add(1))
	admin, err := sql.Open("postgres", srv.DSN("postgres"))
	if err != nil {
		t.Fatal(err)
	}
	defer admin.Close()
	if _, err := admin.Exec("CREATE DATABASE " + name); err != nil {
		t.Fatal(err)
	}
	dsn := srv.DSN(name)
	db, err := sql.Open("postgres", dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := migrations.Apply(context.Background(), db); err != nil {
		t.Fatal(err)
	}
	return dsn
}

func findRoot() (bin, share, root string, err error) {
	srcdir := os.Getenv("TEST_SRCDIR")
	candidates := []string{
		filepath.Join(srcdir, "+postgres+postgres_test"),
		filepath.Join(srcdir, "postgres_test"),
		filepath.Join(srcdir, "_main", "external", "+postgres+postgres_test"),
		filepath.Join(srcdir, "_main", "external", "postgres_test"),
	}
	for _, c := range candidates {
		deb := filepath.Join(c, "usr", "lib", "postgresql", "16")
		if _, err := os.Stat(filepath.Join(deb, "bin", "postgres")); err == nil {
			return filepath.Join(deb, "bin"), filepath.Join(c, "usr", "share", "postgresql", "16"), c, nil
		}
		if _, err := os.Stat(filepath.Join(c, "bin", "postgres")); err == nil {
			return filepath.Join(c, "bin"), filepath.Join(c, "share"), c, nil
		}
	}
	return "", "", "", fmt.Errorf("postgres_test binaries not found under TEST_SRCDIR=%q", srcdir)
}

// initdb needs these templates, which the OCI extraction can miss.
var samples = map[string]string{
	"postgresql.conf.sample": "listen_addresses = '127.0.0.1'\nmax_connections = 100\nshared_buffers = 128MB\n" +
		"dynamic_shared_memory_type = posix\nlog_timezone = 'UTC'\ntimezone = 'UTC'\n",
	"pg_hba.conf.sample":   "local all all trust\nhost all all 127.0.0.1/32 trust\nhost all all ::1/128 trust\n",
	"pg_ident.conf.sample": "# MAPNAME SYSTEM-USERNAME PG-USERNAME\n",
}

func start() (*Server, error) {
	bin, share, root, err := findRoot()
	if err != nil {
		return nil, err
	}
	for name, content := range samples {
		p := filepath.Join(share, name)
		if _, err := os.Stat(p); os.IsNotExist(err) {
			if err := os.WriteFile(p, []byte(content), 0o644); err != nil {
				return nil, err
			}
		}
	}
	base := os.Getenv("TEST_TMPDIR")
	dir, err := os.MkdirTemp(base, "pgdata")
	if err != nil {
		return nil, err
	}
	// PostgreSQL refuses to run as root, which is how remote CI runs tests.
	var cred *syscall.Credential
	if os.Getuid() == 0 {
		nobody, err := user.Lookup("nobody")
		if err != nil {
			return nil, err
		}
		uid, _ := strconv.Atoi(nobody.Uid)
		gid, _ := strconv.Atoi(nobody.Gid)
		cred = &syscall.Credential{Uid: uint32(uid), Gid: uint32(gid)}
		if err := os.Chown(dir, uid, gid); err != nil {
			return nil, err
		}
		for p := filepath.Dir(dir); p != "/" && p != "."; p = filepath.Dir(p) {
			if fi, err := os.Stat(p); err == nil {
				_ = os.Chmod(p, fi.Mode().Perm()|0o711)
			}
		}
		if out, err := exec.Command("chmod", "-R", "a+rX", root).CombinedOutput(); err != nil {
			return nil, fmt.Errorf("chmod postgres runfiles: %v: %s", err, out)
		}
	}
	run := func(name string, args ...string) *exec.Cmd {
		cmd := exec.Command(filepath.Join(bin, name), args...)
		if cred != nil {
			cmd.SysProcAttr = &syscall.SysProcAttr{Credential: cred}
		}
		return cmd
	}
	data := filepath.Join(dir, "data")
	if cred != nil {
		if err := os.Mkdir(data, 0o700); err != nil {
			return nil, err
		}
		if err := os.Chown(data, int(cred.Uid), int(cred.Gid)); err != nil {
			return nil, err
		}
	}
	if out, err := run("initdb", "-D", data, "--no-locale", "-U", "test", "-L", share).CombinedOutput(); err != nil {
		return nil, fmt.Errorf("initdb: %v: %s", err, out)
	}
	port, err := freePort()
	if err != nil {
		return nil, err
	}
	logFile, err := os.Create(filepath.Join(dir, "postgres.log"))
	if err != nil {
		return nil, err
	}
	cmd := run("postgres", "-D", data, "-p", strconv.Itoa(port), "-k", dir, "-c", "fsync=off")
	cmd.Stdout = logFile
	cmd.Stderr = logFile
	if err := cmd.Start(); err != nil {
		return nil, err
	}
	srv := &Server{port: port, cmd: cmd, dir: dir}
	deadline := time.Now().Add(20 * time.Second)
	for {
		db, _ := sql.Open("postgres", srv.DSN("postgres"))
		err = db.Ping()
		db.Close()
		if err == nil {
			return srv, nil
		}
		if time.Now().After(deadline) {
			srv.stop()
			logs, _ := os.ReadFile(logFile.Name())
			return nil, fmt.Errorf("postgres not ready: %v\n%s", err, logs)
		}
		time.Sleep(100 * time.Millisecond)
	}
}

func (s *Server) stop() {
	if s.cmd.Process != nil {
		_ = s.cmd.Process.Signal(syscall.SIGTERM)
		done := make(chan struct{})
		go func() { _ = s.cmd.Wait(); close(done) }()
		select {
		case <-done:
		case <-time.After(10 * time.Second):
			_ = s.cmd.Process.Kill()
		}
	}
}

func freePort() (int, error) {
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return 0, err
	}
	defer l.Close()
	return l.Addr().(*net.TCPAddr).Port, nil
}
