package handler

import (
	"archive/tar"
	"bufio"
	"bytes"
	"compress/gzip"
	"context"
	"encoding/json"
	"errors"
	"io"
	"io/fs"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"

	"github.com/jomcgi/homelab/projects/firecracker/substrate/shim"
)

func TestElixirUnicodeStdoutThroughHandler(t *testing.T) {
	// Use the APK payloads that build the sandbox image. This makes the test
	// exercise the shipped BEAM and Elixir, not a toolchain installed on the
	// runner. The Firecracker/vsock boundary remains a deployment-level check.
	root := t.TempDir()
	for _, apk := range elixirRuntimeAPKs(t) {
		extractAPK(t, apk, root)
	}

	originalDropPrivileges := dropPrivileges
	dropPrivileges = false
	t.Cleanup(func() { dropPrivileges = originalDropPrivileges })

	imagePath := strings.Join([]string{
		filepath.Join(root, "usr/local/bin"),
		filepath.Join(root, "usr/bin"),
		"/usr/bin",
		"/bin",
	}, ":")
	spec := languageSpecs["elixir"]
	spec.Run = []string{filepath.Join(root, "usr/local/bin/elixir"), "main.exs"}
	spec.Env = append(append([]string{}, spec.Env...),
		"PATH="+imagePath,
		"ERL_ROOTDIR="+filepath.Join(root, "usr/lib/erlang"),
		"LD_LIBRARY_PATH="+filepath.Join(root, "usr/lib"),
	)

	const source = `IO.puts("non-ascii round trip: #{String.upcase("café αβγ")}")`
	body, err := json.Marshal(ExecRequest{Code: source})
	if err != nil {
		t.Fatalf("marshal request: %v", err)
	}
	resp, err := Handle(context.Background(), &shim.Request{
		Path: "/invoke/sandbox",
		Body: bytes.NewReader(body),
	}, spec)
	if err != nil {
		t.Fatalf("Handle: %v", err)
	}
	if resp.Status != http.StatusOK {
		t.Fatalf("status = %d, want %d", resp.Status, http.StatusOK)
	}

	var result ExecResult
	if err := json.Unmarshal(resp.Body, &result); err != nil {
		t.Fatalf("decode result: %v", err)
	}
	if result.ExitCode != 0 {
		t.Fatalf("exit code = %d, want 0; error=%q stderr=%q", result.ExitCode, result.Error, result.Stderr)
	}
	if result.Stdout != "non-ascii round trip: CAFÉ ΑΒΓ\n" {
		t.Errorf("stdout = %q, want exact UTF-8 round trip", result.Stdout)
	}
	if result.Stderr != "" {
		t.Errorf("stderr = %q, want empty", result.Stderr)
	}
	if result.Error != "" {
		t.Errorf("error = %q, want empty", result.Error)
	}
}

func elixirRuntimeAPKs(t *testing.T) []string {
	t.Helper()
	root := os.Getenv("TEST_SRCDIR")
	if root == "" {
		t.Fatal("TEST_SRCDIR is empty; lock-pinned APK runfiles are unavailable")
	}

	var apks []string
	seenDirs := map[string]bool{}
	var walk func(string)
	walk = func(dir string) {
		realDir, err := filepath.EvalSymlinks(dir)
		if err != nil {
			t.Fatalf("resolve runfiles directory %s: %v", dir, err)
		}
		if seenDirs[realDir] {
			return
		}
		seenDirs[realDir] = true
		entries, err := os.ReadDir(realDir)
		if err != nil {
			t.Fatalf("read runfiles directory %s: %v", realDir, err)
		}
		for _, entry := range entries {
			path := filepath.Join(realDir, entry.Name())
			info, err := os.Stat(path)
			if err != nil {
				t.Fatalf("stat runfile %s: %v", path, err)
			}
			if info.IsDir() {
				walk(path)
				continue
			}
			if strings.HasSuffix(entry.Name(), ".apk") {
				apks = append(apks, path)
			}
		}
	}
	walk(root)
	sort.Strings(apks)
	if len(apks) == 0 {
		t.Fatal("no APKs found in lock-pinned Elixir runtime runfiles")
	}
	return apks
}

func extractAPK(t *testing.T, apk, root string) {
	t.Helper()
	f, err := os.Open(apk)
	if err != nil {
		t.Fatalf("open APK %s: %v", apk, err)
	}
	defer f.Close()

	// APKs concatenate gzip-compressed signature, control, and data tarballs.
	// Multistream(false) lets us advance member by member until the data archive
	// has been extracted too.
	buffered := bufio.NewReader(f)
	for member := 0; ; member++ {
		if _, err := buffered.Peek(1); errors.Is(err, io.EOF) {
			return
		} else if err != nil {
			t.Fatalf("inspect APK %s member %d: %v", apk, member, err)
		}
		zipped, err := gzip.NewReader(buffered)
		if err != nil {
			t.Fatalf("open APK %s member %d: %v", apk, member, err)
		}
		zipped.Multistream(false)
		extractTarMember(t, tar.NewReader(zipped), root, apk)
		if _, err := io.Copy(io.Discard, zipped); err != nil {
			t.Fatalf("finish APK %s member %d: %v", apk, member, err)
		}
		if err := zipped.Close(); err != nil {
			t.Fatalf("close APK %s member %d: %v", apk, member, err)
		}
	}
}

func extractTarMember(t *testing.T, archive *tar.Reader, root, apk string) {
	t.Helper()
	for {
		header, err := archive.Next()
		if errors.Is(err, io.EOF) {
			return
		}
		if err != nil {
			t.Fatalf("read APK %s: %v", apk, err)
		}
		name := filepath.Clean(filepath.FromSlash(strings.TrimPrefix(header.Name, "./")))
		if name == "." || !filepath.IsLocal(name) {
			continue
		}
		target := filepath.Join(root, name)
		if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
			t.Fatalf("create parent for %s from %s: %v", name, apk, err)
		}

		switch header.Typeflag {
		case tar.TypeDir:
			if err := os.MkdirAll(target, fs.FileMode(header.Mode)); err != nil {
				t.Fatalf("create directory %s from %s: %v", name, apk, err)
			}
		case tar.TypeReg, tar.TypeRegA:
			if err := os.RemoveAll(target); err != nil && !errors.Is(err, os.ErrNotExist) {
				t.Fatalf("replace file %s from %s: %v", name, apk, err)
			}
			if err := writeTarFile(target, fs.FileMode(header.Mode), archive); err != nil {
				t.Fatalf("extract file %s from %s: %v", name, apk, err)
			}
		case tar.TypeSymlink:
			if err := os.RemoveAll(target); err != nil && !errors.Is(err, os.ErrNotExist) {
				t.Fatalf("replace symlink %s from %s: %v", name, apk, err)
			}
			if err := os.Symlink(header.Linkname, target); err != nil {
				t.Fatalf("create symlink %s from %s: %v", name, apk, err)
			}
		case tar.TypeLink:
			linkName := filepath.Clean(filepath.FromSlash(strings.TrimPrefix(header.Linkname, "./")))
			if !filepath.IsLocal(linkName) {
				t.Fatalf("hard link %s in %s escapes extraction root", header.Linkname, apk)
			}
			if err := os.RemoveAll(target); err != nil && !errors.Is(err, os.ErrNotExist) {
				t.Fatalf("replace hard link %s from %s: %v", name, apk, err)
			}
			if err := os.Link(filepath.Join(root, linkName), target); err != nil {
				t.Fatalf("create hard link %s from %s: %v", name, apk, err)
			}
		}
	}
}

func writeTarFile(path string, mode fs.FileMode, source io.Reader) error {
	file, err := os.OpenFile(path, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, mode)
	if err != nil {
		return err
	}
	_, copyErr := io.Copy(file, source)
	closeErr := file.Close()
	if copyErr != nil {
		return copyErr
	}
	if closeErr != nil {
		return closeErr
	}
	return os.Chmod(path, mode)
}
