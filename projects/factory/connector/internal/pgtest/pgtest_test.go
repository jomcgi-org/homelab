package pgtest

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestStopRemovesDataDirectory(t *testing.T) {
	srv, err := start()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(srv.stop)
	if _, err := os.Stat(filepath.Join(srv.dir, "data", "PG_VERSION")); err != nil {
		t.Fatal(err)
	}
	cmd := srv.cmd
	srv.stop()
	if cmd.ProcessState == nil {
		t.Fatal("postgres was not reaped before stop returned")
	}
	if _, err := os.Stat(srv.dir); !os.IsNotExist(err) {
		t.Fatalf("data directory remains after stop: %v", err)
	}
}

func TestStartFailureRemovesDataDirectory(t *testing.T) {
	for _, tc := range []struct {
		name      string
		initExit  string
		postgres  os.FileMode
		wantError string
	}{
		{name: "initdb", initExit: "1", postgres: 0o755, wantError: "initdb:"},
		{name: "exec", initExit: "0", postgres: 0o644, wantError: "permission denied"},
		{name: "readiness", initExit: "0", postgres: 0o755, wantError: "startup failed"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			srcdir := t.TempDir()
			base := filepath.Join(srcdir, "tmp")
			t.Setenv("TEST_SRCDIR", srcdir)
			t.Setenv("TEST_TMPDIR", base)
			root := filepath.Join(srcdir, "postgres_test")
			bin := filepath.Join(root, "bin")
			for _, dir := range []string{base, bin, filepath.Join(root, "share")} {
				if err := os.MkdirAll(dir, 0o755); err != nil {
					t.Fatal(err)
				}
			}
			// Leave partial initdb output, including on failure.
			initdb := "#!/bin/sh\nmkdir -p \"$2\"\necho partial >\"$2/partial\"\nexit " + tc.initExit + "\n"
			if err := os.WriteFile(filepath.Join(bin, "initdb"), []byte(initdb), 0o755); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(filepath.Join(bin, "postgres"), []byte("#!/bin/sh\necho 'startup failed' >&2\nexit 1\n"), tc.postgres); err != nil {
				t.Fatal(err)
			}
			srv, err := start()
			if srv != nil {
				srv.stop()
				t.Fatal("start unexpectedly succeeded")
			}
			if err == nil || !strings.Contains(err.Error(), tc.wantError) {
				t.Fatalf("expected %q error, got %v", tc.wantError, err)
			}
			entries, err := os.ReadDir(base)
			if err != nil {
				t.Fatal(err)
			}
			if len(entries) != 0 {
				t.Fatalf("data directory remains after failed start: %v", entries)
			}
		})
	}
}
