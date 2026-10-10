package store

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestValidateRestoreName(t *testing.T) {
	for _, ok := range []string{"snapfile", "memfile", "leader/snapfile", "a.b-c_d/mem.file"} {
		if err := validateRestoreName(ok); err != nil {
			t.Errorf("validateRestoreName(%q) = %v, want accepted", ok, err)
		}
	}
	for _, bad := range []string{
		"", "/", "/etc/passwd", "../escape", "..", ".", "a/../b", "a/./b", "a//b", "a/b/c",
		"a\\b", "a/", "trailing/", "nul\x00byte",
	} {
		if err := validateRestoreName(bad); err == nil {
			t.Errorf("validateRestoreName(%q) accepted, want refused", bad)
		}
	}
}

// TestRestoreRefusesTraversalName proves a tampered meta.json cannot place a
// restored file outside the artifact directory: the restore fails before any
// object is fetched and nothing lands beside the restore dir.
func TestRestoreRefusesTraversalName(t *testing.T) {
	s, fake := newTestStore(t)
	ctx := context.Background()
	srcDir, names := writeLocalArtifact(t, map[string]string{"snapfile": "good-bytes"})
	prefix := "session/sandbox-session/sess-traversal"
	if _, _, err := s.Export(ctx, prefix, srcDir, names, 0, 1, "", ""); err != nil {
		t.Fatalf("Export: %v", err)
	}
	fake.mu.Lock()
	var meta Meta
	if err := json.Unmarshal(fake.objects["/embervm/"+prefix+"/"+metaObject], &meta); err != nil {
		fake.mu.Unlock()
		t.Fatalf("decode meta: %v", err)
	}
	fm := meta.Files["snapfile"]
	meta.Files = map[string]FileMeta{"../escaped": fm}
	tampered, _ := json.Marshal(meta)
	fake.objects["/embervm/"+prefix+"/"+metaObject] = tampered
	fake.objects["/embervm/"+prefix+"/../escaped"] = []byte("good-bytes")
	fake.mu.Unlock()

	parent := t.TempDir()
	dstDir := filepath.Join(parent, "bundle")
	if _, _, err := s.Restore(ctx, prefix, dstDir, nil); err == nil {
		t.Fatal("Restore with a traversal file name should fail")
	}
	if _, err := os.Stat(filepath.Join(parent, "escaped")); !os.IsNotExist(err) {
		t.Fatalf("traversal name wrote outside the artifact dir (stat err = %v)", err)
	}
	if _, err := os.Stat(filepath.Join(parent, "escaped.restore.tmp")); !os.IsNotExist(err) {
		t.Fatalf("traversal name left a temp file outside the artifact dir (stat err = %v)", err)
	}
}

// TestRestoreCreatesMemberSubdirectory proves a group-set member file, named
// <member>/<file>, restores into its member directory.
func TestRestoreCreatesMemberSubdirectory(t *testing.T) {
	s, _ := newTestStore(t)
	ctx := context.Background()
	srcDir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(srcDir, "leader"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(srcDir, "leader", "snapfile"), []byte("leader-snap"), 0o600); err != nil {
		t.Fatal(err)
	}
	prefix := "group/k3s/set-1"
	if _, _, err := s.Export(ctx, prefix, srcDir, []string{"leader/snapfile"}, 0, 1, "", ""); err != nil {
		t.Fatalf("Export: %v", err)
	}
	dstDir := t.TempDir()
	if _, _, err := s.Restore(ctx, prefix, dstDir, nil); err != nil {
		t.Fatalf("Restore: %v", err)
	}
	got, err := os.ReadFile(filepath.Join(dstDir, "leader", "snapfile"))
	if err != nil || string(got) != "leader-snap" {
		t.Fatalf("restored member file = %q, %v; want leader-snap", got, err)
	}
}
