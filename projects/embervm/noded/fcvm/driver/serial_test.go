package driver

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"github.com/jomcgi/homelab/projects/embervm/noded/substrate"
)

func TestColdBootPrecreatesSinkAndIssuesPutSerialBeforeStart(t *testing.T) {
	launcher := &fakeLauncher{}
	d := testDriverWithLauncher(t, launcher)

	h, err := d.Claim(context.Background(), substrate.ClaimSpec{ThreadID: "serial-cold"})
	if err != nil {
		t.Fatalf("Claim: %v", err)
	}
	t.Cleanup(func() { _ = d.Release(context.Background(), h) })

	// The sink file must exist inside the bundle dir even before any output.
	sink := d.serialOutputPath("serial-cold")
	if sink != filepath.Join(d.threadDir("serial-cold"), serialOutputName) {
		t.Fatalf("sink path = %q, want under thread dir", sink)
	}
	fi, err := os.Stat(sink)
	if err != nil {
		t.Fatalf("sink not pre-created: %v", err)
	}
	if fi.Size() != 0 {
		t.Fatalf("fresh sink size = %d, want 0", fi.Size())
	}

	// Exactly one PUT /serial, immediately before Start (PUT /actions), after
	// the vsock PUT that precedes it in the boot sequence.
	paths := launcher.requestPaths()
	serialIdx, vsockIdx, startIdx := -1, -1, -1
	for i, p := range paths {
		switch p {
		case "PUT /serial":
			if serialIdx != -1 {
				t.Fatalf("PUT /serial issued more than once: %v", paths)
			}
			serialIdx = i
		case "PUT /vsock":
			vsockIdx = i
		case "PUT /actions":
			startIdx = i
		}
	}
	if serialIdx == -1 || startIdx == -1 {
		t.Fatalf("missing PUT /serial or PUT /actions: %v", paths)
	}
	if !(vsockIdx < serialIdx && serialIdx < startIdx) {
		t.Fatalf("PUT /serial at %d not between vsock (%d) and start (%d): %v", serialIdx, vsockIdx, startIdx, paths)
	}

	bodies := launcher.serialPutBodies()
	if len(bodies) != 1 {
		t.Fatalf("serial bodies = %d, want 1", len(bodies))
	}
	if got := bodies[0]["serial_out_path"]; got != sink {
		t.Fatalf("serial_out_path = %v, want %q", got, sink)
	}
	rl, ok := bodies[0]["rate_limiter"].(map[string]any)
	if !ok {
		t.Fatalf("rate_limiter missing from body: %v", bodies[0])
	}
	// n v1.16.1's serial dialect takes the token bucket FLAT under
	// rate_limiter; the drive/net {bandwidth: ...} wrapper is rejected with
	// SerdeJson "missing field `size`" (observed live on the dev fleet).
	wantSize := float64(serialBurstBytes + serialBandwidthBytesPerSec)
	if rl["size"] != wantSize ||
		rl["one_time_burst"] != float64(serialBurstBytes) ||
		rl["refill_time"] != float64(serialBandwidthRefillMs) {
		t.Fatalf("rate limiter token bucket = %v, want size/burst/refill %v/%d/%d",
			rl, wantSize, serialBurstBytes, serialBandwidthRefillMs)
	}
	if _, wrapped := rl["bandwidth"]; wrapped {
		t.Fatalf("rate_limiter must be a flat token bucket, got wrapper: %v", rl)
	}
}

func TestRestoreIssuesPutSerialBeforeLoadSnapshot(t *testing.T) {
	launcher := &fakeLauncher{}
	d := testDriverWithLauncher(t, launcher)

	ctx := context.Background()
	warm, err := d.Claim(ctx, substrate.ClaimSpec{ThreadID: "warm"})
	if err != nil {
		t.Fatalf("Claim warm: %v", err)
	}
	ref, err := d.SnapshotSession(ctx, warm, "serial-ref")
	if err != nil {
		t.Fatalf("SnapshotSession: %v", err)
	}
	if err := d.Release(ctx, warm); err != nil {
		t.Fatalf("Release warm: %v", err)
	}

	h2, err := d.RestoreSession(ctx, ref.ID, false)
	if err != nil {
		t.Fatalf("RestoreSession: %v", err)
	}
	t.Cleanup(func() { _ = d.Release(ctx, h2) })

	// The restore issued its own PUT /serial into the NEW thread's bundle dir:
	// a restored process starts with no sink until told (issue #4404). A cold
	// boot never calls /snapshot/load, so the single recorded load is the
	// restore's, and PUT /serial must sit immediately before it.
	paths := launcher.requestPaths()
	var lastLoad int
	loads := 0
	for i, p := range paths {
		if p == "PUT /snapshot/load" {
			lastLoad = i
			loads++
		}
	}
	if loads != 1 {
		t.Fatalf("snapshot loads = %d, want 1 (the restore): %v", loads, paths)
	}
	if paths[lastLoad-1] != "PUT /serial" {
		t.Fatalf("PUT /serial must immediately precede PUT /snapshot/load, got %v around it", paths[max(0, lastLoad-2):lastLoad+1])
	}

	bodies := launcher.serialPutBodies()
	if len(bodies) != 2 {
		t.Fatalf("serial puts = %d, want 2 (cold boot + restore)", len(bodies))
	}
	wantSink := d.serialOutputPath(h2.ThreadID)
	if got := bodies[1]["serial_out_path"]; got != wantSink {
		t.Fatalf("restore serial_out_path = %v, want new thread sink %q", got, wantSink)
	}
	if _, err := os.Stat(wantSink); err != nil {
		t.Fatalf("restore sink not pre-created in new bundle dir: %v", err)
	}
}

func TestBootFailureAttachesSerialTailToError(t *testing.T) {
	console := []byte("Linux version 6.6.0\nKernel panic - not syncing: init died\n")
	launcher := &fakeLauncher{failPath: "/actions", serialOutput: console}
	d := testDriverWithLauncher(t, launcher)

	_, err := d.Claim(context.Background(), substrate.ClaimSpec{ThreadID: "panic-boot"})
	if err == nil {
		t.Fatal("Claim should fail when Start fails")
	}
	if !strings.Contains(err.Error(), "Kernel panic - not syncing") {
		t.Fatalf("error missing guest serial tail: %v", err)
	}
	if !strings.Contains(err.Error(), fmt.Sprintf("%d bytes kept", len(console))) {
		t.Fatalf("error missing kept-bytes annotation: %v", err)
	}
}

// TestBootFailureWithSilentConsoleReturnsCleanCause covers an empty VM: a
// guest that produced zero output adds no tail noise to the failure cause.
func TestBootFailureWithSilentConsoleReturnsCleanCause(t *testing.T) {
	launcher := &fakeLauncher{failPath: "/actions"}
	d := testDriverWithLauncher(t, launcher)

	_, err := d.Claim(context.Background(), substrate.ClaimSpec{ThreadID: "silent-boot"})
	if err == nil {
		t.Fatal("Claim should fail when Start fails")
	}
	if strings.Contains(err.Error(), "serial tail") {
		t.Fatalf("empty VM must return the clean cause, got: %v", err)
	}
	if !strings.Contains(err.Error(), "status 500") {
		t.Fatalf("underlying cause lost: %v", err)
	}
}

func TestRestoreFailureAttachesSerialTailToError(t *testing.T) {
	launcher := &fakeLauncher{serialOutput: []byte("restored guest says hi\n")}
	d := testDriverWithLauncher(t, launcher)

	ctx := context.Background()
	warm, err := d.Claim(ctx, substrate.ClaimSpec{ThreadID: "warm2"})
	if err != nil {
		t.Fatalf("Claim warm: %v", err)
	}
	ref, err := d.SnapshotSession(ctx, warm, "serial-fail-ref")
	if err != nil {
		t.Fatalf("SnapshotSession: %v", err)
	}
	if err := d.Release(ctx, warm); err != nil {
		t.Fatalf("Release warm: %v", err)
	}
	launcher.failPath = "/snapshot/load"

	_, err = d.RestoreSession(ctx, ref.ID, false)
	if err == nil {
		t.Fatal("RestoreSession should fail when snapshot/load fails")
	}
	if !strings.Contains(err.Error(), "restored guest says hi") {
		t.Fatalf("restore error missing serial tail: %v", err)
	}
}

func TestPrepareSerialOutputTruncatesStaleIncarnation(t *testing.T) {
	d := testDriver(t)
	if err := os.MkdirAll(d.threadDir("reuse-thread"), 0o750); err != nil {
		t.Fatalf("mkdir thread dir: %v", err)
	}
	stale := d.serialOutputPath("reuse-thread")
	if err := os.WriteFile(stale, []byte("old incarnation console spam"), 0o640); err != nil {
		t.Fatalf("write stale sink: %v", err)
	}
	path, err := d.prepareSerialOutput("reuse-thread")
	if err != nil {
		t.Fatalf("prepareSerialOutput: %v", err)
	}
	if path != stale {
		t.Fatalf("path = %q, want %q", path, stale)
	}
	data, err := os.ReadFile(stale)
	if err != nil {
		t.Fatalf("read sink: %v", err)
	}
	if len(data) != 0 {
		t.Fatalf("stale sink not truncated: %q", data)
	}
}

// TestSerialTailBoundEnforcementKeepsOnlyLastBytes proves the diagnostic cap:
// write more than the cap into the sink and retrieval keeps exactly the last
// cap bytes (the oldest output is dropped, per design).
func TestSerialTailBoundEnforcementKeepsOnlyLastBytes(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, serialOutputName)
	total := int(serialTailBytes)*2 + 111 // well past the cap
	content := make([]byte, total)
	for i := range content {
		content[i] = byte('A' + i%26)
	}
	if err := os.WriteFile(path, content, 0o640); err != nil {
		t.Fatalf("write: %v", err)
	}
	got, ok := serialTail(path, serialTailBytes)
	if !ok {
		t.Fatal("serialTail reported unreadable sink")
	}
	if int64(len(got)) != serialTailBytes {
		t.Fatalf("tail length = %d, want exactly the cap %d", len(got), serialTailBytes)
	}
	if string(got) != string(content[total-int(serialTailBytes):]) {
		t.Fatal("tail is not the LAST cap bytes of the sink")
	}
}

func TestSerialTailUnderCapReturnsWholeSink(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, serialOutputName)
	content := []byte("short boot\n")
	if err := os.WriteFile(path, content, 0o640); err != nil {
		t.Fatalf("write: %v", err)
	}
	got, ok := serialTail(path, serialTailBytes)
	if !ok || string(got) != string(content) {
		t.Fatalf("tail = %q ok=%v, want whole content", got, ok)
	}
}

// TestSerialTailEmptyVMAndMissingSink: a missing sink reports not-ok (the
// caller returns the cause unchanged) and an existing but empty sink (a guest
// that said nothing) retrieves cleanly as empty, with no error.
func TestSerialTailEmptyVMAndMissingSink(t *testing.T) {
	dir := t.TempDir()
	if _, ok := serialTail(filepath.Join(dir, "absent.log"), serialTailBytes); ok {
		t.Fatal("missing sink must report not-ok")
	}
	empty := filepath.Join(dir, "empty.log")
	if err := os.WriteFile(empty, nil, 0o640); err != nil {
		t.Fatalf("write empty: %v", err)
	}
	got, ok := serialTail(empty, serialTailBytes)
	if !ok || len(got) != 0 {
		t.Fatalf("empty sink tail = %q ok=%v, want empty ok=true", got, ok)
	}
}

// TestSerialTailSurvivesConcurrentAppenders proves retrieval is safe against
// concurrent writers (Firecracker's UART threads in production): every writer
// appends whole records through single O_APPEND writes, and the retrieved tail
// must be an exact suffix of the appended stream with length exactly
// min(cap, total), never torn or reordered.
func TestSerialTailSurvivesConcurrentAppenders(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, serialOutputName)
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o640)
	if err != nil {
		t.Fatalf("open append: %v", err)
	}

	const writers, records = 8, 64
	record := strings.Repeat("x", 99) + "\n"
	var wg sync.WaitGroup
	errCh := make(chan error, writers)
	for range writers {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for range records {
				if _, err := f.WriteString(record); err != nil {
					errCh <- err
					return
				}
			}
		}()
	}
	wg.Wait()
	close(errCh)
	for err := range errCh {
		t.Fatalf("concurrent append failed: %v", err)
	}
	_ = f.Close()

	total := int64(writers * records * len(record))
	got, ok := serialTail(path, serialTailBytes)
	if !ok {
		t.Fatal("serialTail unreadable after concurrent appends")
	}
	wantLen := min(total, serialTailBytes)
	if int64(len(got)) != wantLen {
		t.Fatalf("tail length = %d, want min(cap,total) = %d", len(got), wantLen)
	}
	full, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read full: %v", err)
	}
	if string(full[total-wantLen:]) != string(got) {
		t.Fatal("retrieved tail is not an exact suffix of the written stream")
	}
	if n := strings.Count(string(full), record); n != writers*records {
		t.Fatalf("full stream has %d intact records, want %d (writes torn?)", n, writers*records)
	}
}

// testDriverWithLauncher mirrors testDriver but hands back the launcher so
// tests can inspect recorded Firecracker API traffic.
func testDriverWithLauncher(t *testing.T, launcher Launcher) *Driver {
	t.Helper()
	return New(Config{
		KernelImagePath: "/opt/kata/vmlinux",
		RootfsPath:      "/dev/mapper/thread",
		SnapshotRoot:    shortTempDir(t),
		Node:            "node-4",
		Arch:            "amd64",
	}, launcher, nil)
}
