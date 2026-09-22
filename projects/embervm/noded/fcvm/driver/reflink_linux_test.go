package driver

import (
	"context"
	"errors"
	"io"
	"os"
	"path/filepath"
	"testing"

	"golang.org/x/sys/unix"
)

func installReflinkStub(t *testing.T, stub func(dst, src *os.File) error) {
	t.Helper()
	previous := reflinkFile
	reflinkFile = stub
	t.Cleanup(func() { reflinkFile = previous })
}

func TestReflinkFileRealCloneIsIndependent(t *testing.T) {
	root := t.TempDir()
	srcPath := filepath.Join(root, "source")
	dstPath := filepath.Join(root, "destination")
	if err := os.WriteFile(srcPath, []byte("source-before-clone"), 0o600); err != nil {
		t.Fatal(err)
	}
	src, err := os.Open(srcPath)
	if err != nil {
		t.Fatal(err)
	}
	dst, err := os.OpenFile(dstPath, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		_ = src.Close()
		t.Fatal(err)
	}
	cloneErr := reflinkFile(dst, src)
	closeDstErr := dst.Close()
	closeSrcErr := src.Close()
	if errors.Is(cloneErr, unix.EOPNOTSUPP) || errors.Is(cloneErr, unix.EXDEV) {
		t.Skipf("test filesystem does not support FICLONE: %v", cloneErr)
	}
	if cloneErr != nil {
		t.Fatalf("real FICLONE: %v", cloneErr)
	}
	if closeDstErr != nil {
		t.Fatal(closeDstErr)
	}
	if closeSrcErr != nil {
		t.Fatal(closeSrcErr)
	}

	if err := os.WriteFile(dstPath, []byte("destination-after-clone"), 0o600); err != nil {
		t.Fatal(err)
	}
	gotSource, err := os.ReadFile(srcPath)
	if err != nil {
		t.Fatal(err)
	}
	if string(gotSource) != "source-before-clone" {
		t.Fatalf("source changed with destination: %q", gotSource)
	}

	if err := os.WriteFile(srcPath, []byte("source-after-clone"), 0o600); err != nil {
		t.Fatal(err)
	}
	gotDestination, err := os.ReadFile(dstPath)
	if err != nil {
		t.Fatal(err)
	}
	if string(gotDestination) != "destination-after-clone" {
		t.Fatalf("destination changed with source: %q", gotDestination)
	}
}

func TestCloneOrCopyFileCloneSuccessIsIndependent(t *testing.T) {
	root := t.TempDir()
	src := filepath.Join(root, "source")
	dst := filepath.Join(root, "destination")
	if err := os.WriteFile(src, []byte("source-bytes"), 0o600); err != nil {
		t.Fatal(err)
	}
	installReflinkStub(t, func(dstFile, srcFile *os.File) error {
		_, err := io.Copy(dstFile, srcFile)
		return err
	})

	if err := cloneOrCopyFile(src, dst); err != nil {
		t.Fatalf("cloneOrCopyFile: %v", err)
	}
	if err := os.WriteFile(dst, []byte("changed"), 0o600); err != nil {
		t.Fatal(err)
	}
	got, err := os.ReadFile(src)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != "source-bytes" {
		t.Fatalf("source changed with destination: %q", got)
	}
}

func TestCloneOrCopyFileUnsupportedFallsBackFromCleanOffsets(t *testing.T) {
	for _, unsupported := range []error{unix.EOPNOTSUPP, unix.EXDEV} {
		t.Run(unsupported.Error(), func(t *testing.T) {
			root := t.TempDir()
			src := filepath.Join(root, "source")
			dst := filepath.Join(root, "destination")
			if err := os.WriteFile(src, []byte("complete-source"), 0o600); err != nil {
				t.Fatal(err)
			}
			installReflinkStub(t, func(dstFile, srcFile *os.File) error {
				if _, err := dstFile.WriteString("partial-clone"); err != nil {
					return err
				}
				if _, err := srcFile.Seek(5, io.SeekStart); err != nil {
					return err
				}
				return unsupported
			})

			if err := cloneOrCopyFile(src, dst); err != nil {
				t.Fatalf("fallback: %v", err)
			}
			got, err := os.ReadFile(dst)
			if err != nil {
				t.Fatal(err)
			}
			if string(got) != "complete-source" {
				t.Fatalf("fallback output = %q", got)
			}
		})
	}
}

func TestCloneOrCopyFileHardErrorCleansDestination(t *testing.T) {
	root := t.TempDir()
	src := filepath.Join(root, "source")
	dst := filepath.Join(root, "destination")
	if err := os.WriteFile(src, []byte("source"), 0o600); err != nil {
		t.Fatal(err)
	}
	installReflinkStub(t, func(dstFile, _ *os.File) error {
		_, _ = dstFile.WriteString("partial")
		return unix.EPERM
	})

	err := cloneOrCopyFile(src, dst)
	if !errors.Is(err, unix.EPERM) {
		t.Fatalf("error = %v, want EPERM", err)
	}
	if _, err := os.Stat(dst); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("failed destination survived: %v", err)
	}
	got, err := os.ReadFile(src)
	if err != nil || string(got) != "source" {
		t.Fatalf("source after hard error = %q, %v", got, err)
	}
}

func TestCloneOrCopyFileRejectsExistingDestination(t *testing.T) {
	root := t.TempDir()
	src := filepath.Join(root, "source")
	dst := filepath.Join(root, "destination")
	if err := os.WriteFile(src, []byte("source"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(dst, []byte("keep"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := cloneOrCopyFile(src, dst); !errors.Is(err, os.ErrExist) {
		t.Fatalf("existing destination error = %v", err)
	}
	got, _ := os.ReadFile(dst)
	if string(got) != "keep" {
		t.Fatalf("existing destination was changed: %q", got)
	}
	if err := cloneOrCopyFile(src, src); !errors.Is(err, os.ErrExist) {
		t.Fatalf("same-file error = %v", err)
	}
}

func TestMergeMemoryDiffUsesCloneBeforeEditor(t *testing.T) {
	root := t.TempDir()
	base := filepath.Join(root, "base")
	diff := filepath.Join(root, "diff")
	output := filepath.Join(root, "merged")
	editor := filepath.Join(root, "snapshot-editor")
	if err := os.WriteFile(base, []byte("base"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(diff, []byte("+diff"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(editor, []byte(`#!/bin/sh
memory=$4
diff=$6
cat "$diff" >> "$memory"
`), 0o700); err != nil {
		t.Fatal(err)
	}
	cloneCalls := 0
	installReflinkStub(t, func(dstFile, srcFile *os.File) error {
		cloneCalls++
		_, err := io.Copy(dstFile, srcFile)
		return err
	})

	if err := mergeMemoryDiff(context.Background(), editor, base, diff, output); err != nil {
		t.Fatalf("mergeMemoryDiff: %v", err)
	}
	if cloneCalls != 1 {
		t.Fatalf("FICLONE calls = %d, want 1", cloneCalls)
	}
	got, _ := os.ReadFile(output)
	if string(got) != "base+diff" {
		t.Fatalf("merged output = %q", got)
	}
	baseBytes, _ := os.ReadFile(base)
	if string(baseBytes) != "base" {
		t.Fatalf("committed base changed: %q", baseBytes)
	}
}
