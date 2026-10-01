package main

// Shipped-layer proof for the S6 TLC leg (issue #6415).
//
// s6_tlc_test proves the toolchain from Bazel runfiles. This test proves the
// layer the conformance image actually carries (:tla_tar_amd64): it extracts
// the tar, checks the fixed /opt/tla paths with their owner and modes, then
// points runS6 at the extracted java, jar and spec dir. A layer that drops the
// JRE's lib/ tree, loses the exec bit on bin/java, or ships an unreadable jar
// fails here rather than in the dev cluster.

import (
	"archive/tar"
	"bufio"
	"compress/gzip"
	"context"
	"io"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strings"
	"testing"
	"time"
)

// The in-image layout the chart's S6_TLC_* env points at. Changing one means
// changing tla_layer_mtree.sh and the chart in the same change.
const (
	s6TLCImageJava    = "opt/tla/jre/bin/java"
	s6TLCImageJar     = "opt/tla/tla2tools.jar"
	s6TLCImageSpecDir = "opt/tla/specs"
)

// extractS6TLCLayer unpacks the layer under root and returns each entry's
// header keyed by its normalized path (no leading "./", no trailing "/").
func extractS6TLCLayer(t *testing.T, layer, root string) map[string]*tar.Header {
	t.Helper()
	f, err := os.Open(layer)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	buffered := bufio.NewReader(f)
	var r io.Reader = buffered
	if magic, err := buffered.Peek(2); err == nil && magic[0] == 0x1f && magic[1] == 0x8b {
		gz, err := gzip.NewReader(buffered)
		if err != nil {
			t.Fatal(err)
		}
		defer gz.Close()
		r = gz
	}

	headers := map[string]*tar.Header{}
	tr := tar.NewReader(r)
	for {
		hdr, err := tr.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			t.Fatalf("read layer %s: %v", layer, err)
		}
		name := strings.TrimSuffix(strings.TrimPrefix(path.Clean(hdr.Name), "./"), "/")
		if name == "." || name == "" {
			continue
		}
		if name == ".." || strings.HasPrefix(name, "../") || path.IsAbs(name) {
			t.Fatalf("layer entry %q escapes the image root", hdr.Name)
		}
		headers[name] = hdr
		dest := filepath.Join(root, filepath.FromSlash(name))
		switch hdr.Typeflag {
		case tar.TypeDir:
			if err := os.MkdirAll(dest, 0o755); err != nil {
				t.Fatal(err)
			}
		case tar.TypeReg:
			if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
				t.Fatal(err)
			}
			out, err := os.OpenFile(dest, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, os.FileMode(hdr.Mode)&0o777)
			if err != nil {
				t.Fatal(err)
			}
			if _, err := io.Copy(out, tr); err != nil {
				out.Close()
				t.Fatal(err)
			}
			if err := out.Close(); err != nil {
				t.Fatal(err)
			}
		case tar.TypeLink:
			// bsdtar records files that share an inode at build time as
			// hardlinks (remote execution stages identical inputs, such as the
			// JRE's limited/ and unlimited/ default_US_export.policy, as one
			// CAS file). The target must be a regular file earlier in the layer.
			target := strings.TrimPrefix(path.Clean(hdr.Linkname), "./")
			if th, ok := headers[target]; !ok || th.Typeflag != tar.TypeReg {
				t.Fatalf("layer hardlink %q points at %q, which is not an earlier regular file", name, hdr.Linkname)
			}
			if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
				t.Fatal(err)
			}
			if err := os.Link(filepath.Join(root, filepath.FromSlash(target)), dest); err != nil {
				t.Fatal(err)
			}
		default:
			t.Fatalf("layer entry %q has type %q; the layer ships only dirs, regular files and hardlinks", name, hdr.Typeflag)
		}
	}
	return headers
}

func TestS6TLCImageLayer(t *testing.T) {
	layer := s6TLCRunfile("S6_TLC_TEST_LAYER")
	if layer == "" {
		t.Skip("S6 TLC image layer not staged; run with Bazel: //projects/embervm/conformance:s6_tlc_image_test")
	}
	root := t.TempDir()
	headers := extractS6TLCLayer(t, layer, root)

	// Fixed paths and their intended modes.
	for name, want := range map[string]int64{
		s6TLCImageJava:                            0o755,
		"opt/tla/jre/lib/jspawnhelper":            0o755,
		"opt/tla/jre/lib/server/libjvm.so":        0o644,
		"opt/tla/jre/lib/libjli.so":               0o644,
		"opt/tla/jre/lib/modules":                 0o644,
		s6TLCImageJar:                             0o644,
		s6TLCImageSpecDir + "/adoption_trace.tla": 0o644,
	} {
		hdr, ok := headers[name]
		if !ok {
			t.Fatalf("layer is missing /%s", name)
		}
		if hdr.Typeflag != tar.TypeReg {
			t.Fatalf("/%s has type %q, want a regular file", name, hdr.Typeflag)
		}
		if got := hdr.Mode & 0o7777; got != want {
			t.Fatalf("/%s mode = %o, want %o", name, got, want)
		}
	}

	// Owner 0:0 everywhere, everything readable by the runner's uid 65532
	// (other), and exactly the JRE's own executables carry an exec bit.
	var executables []string
	for name, hdr := range headers {
		if !strings.HasPrefix(name, "opt/tla/") && name != "opt" && name != "opt/tla" {
			t.Fatalf("layer entry /%s is outside /opt/tla", name)
		}
		if hdr.Uid != 0 || hdr.Gid != 0 {
			t.Fatalf("/%s owner = %d:%d, want 0:0", name, hdr.Uid, hdr.Gid)
		}
		mode := hdr.Mode & 0o7777
		if hdr.Typeflag == tar.TypeDir {
			if mode != 0o755 {
				t.Fatalf("dir /%s mode = %o, want 755", name, mode)
			}
			continue
		}
		if mode != 0o644 && mode != 0o755 {
			t.Fatalf("/%s mode = %o, want 644 or 755", name, mode)
		}
		if mode == 0o755 {
			executables = append(executables, name)
		}
	}
	sort.Strings(executables)
	wantExecutables := []string{
		"opt/tla/jre/bin/java",
		"opt/tla/jre/bin/jfr",
		"opt/tla/jre/bin/jrunscript",
		"opt/tla/jre/bin/jwebserver",
		"opt/tla/jre/bin/keytool",
		"opt/tla/jre/bin/rmiregistry",
		"opt/tla/jre/lib/jexec",
		"opt/tla/jre/lib/jspawnhelper",
	}
	if strings.Join(executables, "\n") != strings.Join(wantExecutables, "\n") {
		t.Fatalf("layer executables = %v, want %v", executables, wantExecutables)
	}

	cfg := config{
		minTraceEvents: 4,
		tlcJava:        filepath.Join(root, filepath.FromSlash(s6TLCImageJava)),
		tlcJar:         filepath.Join(root, filepath.FromSlash(s6TLCImageJar)),
		tlcSpecDir:     filepath.Join(root, filepath.FromSlash(s6TLCImageSpecDir)),
	}

	t.Run("pass window", func(t *testing.T) {
		records := s6TLCPassWindow()
		client, close := s6TLCClient(t, s6TLCWindowBody(t, records))
		defer close()

		result := runS6(context.Background(), cfg, client, time.Now())
		if result.Verdict != verdictPass {
			t.Fatalf("pass window verdict = %q, want pass; detail=%s", result.Verdict, result.Detail)
		}
		if !strings.Contains(result.Detail, "coverage=10") {
			t.Fatalf("pass window detail %q does not carry coverage=10", result.Detail)
		}
		_, _, output := runS6TLCWithOutput(context.Background(), cfg, records)
		if !strings.Contains(output, "0 states left on queue") {
			t.Fatalf("pass window TLC output lacks the queue-drained line; output:\n%s", output)
		}
		t.Logf("TLC output from the shipped layer:\n%s", output)
	})

	t.Run("destroy before confirm", func(t *testing.T) {
		client, close := s6TLCClient(t, s6TLCWindowBody(t, s6TLCDestroyBeforeConfirmWindow()))
		defer close()

		result := runS6(context.Background(), cfg, client, time.Now())
		if result.Verdict != verdictFail {
			t.Fatalf("destroy-before-confirm verdict = %q, want fail; detail=%s", result.Verdict, result.Detail)
		}
		if !strings.Contains(result.Detail, "NoDestroyBeforeConfirm") {
			t.Fatalf("destroy-before-confirm detail %q does not name NoDestroyBeforeConfirm", result.Detail)
		}
	})
}
