package volume

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestCreateSparseAndGenerationInit(t *testing.T) {
	dir := t.TempDir()
	m := NewManager(dir)

	if m.Exists("wl-a") {
		t.Fatal("volume should not exist before Create")
	}
	if err := m.Create("wl-a", 1<<20); err != nil {
		t.Fatalf("Create: %v", err)
	}
	if !m.Exists("wl-a") {
		t.Fatal("volume should exist after Create")
	}
	size, err := m.SizeBytes("wl-a")
	if err != nil {
		t.Fatalf("SizeBytes: %v", err)
	}
	if size != 1<<20 {
		t.Errorf("SizeBytes = %d want %d", size, 1<<20)
	}
	gen, err := m.Generation("wl-a")
	if err != nil {
		t.Fatalf("Generation: %v", err)
	}
	if gen != 0 {
		t.Errorf("initial generation = %d want 0", gen)
	}
	// Sparse: allocated bytes should be far less than the declared 1 MiB cap
	// (no data has been written), proving Create used Truncate not zero-fill.
	alloc, err := m.AllocatedBytes("wl-a")
	if err != nil {
		t.Fatalf("AllocatedBytes: %v", err)
	}
	if alloc >= uint64(size) {
		t.Errorf("allocated bytes = %d should be far less than declared size %d (file should be sparse)", alloc, size)
	}
}

// TestCreateIdempotentDoesNotClobber proves a second Create on an existing
// volume does not recreate (and so does not reset) it: a live workload's data
// must never be truncated by a repeat FRESH call.
func TestCreateIdempotentDoesNotClobber(t *testing.T) {
	dir := t.TempDir()
	m := NewManager(dir)
	if err := m.Create("wl-a", 1<<20); err != nil {
		t.Fatalf("Create: %v", err)
	}
	if _, err := m.BumpGeneration("wl-a"); err != nil {
		t.Fatalf("BumpGeneration: %v", err)
	}
	if err := m.Create("wl-a", 2<<20); err != nil {
		t.Fatalf("second Create: %v", err)
	}
	size, err := m.SizeBytes("wl-a")
	if err != nil {
		t.Fatalf("SizeBytes: %v", err)
	}
	if size != 1<<20 {
		t.Errorf("second Create must not resize an existing volume: size = %d want %d", size, 1<<20)
	}
	gen, err := m.Generation("wl-a")
	if err != nil {
		t.Fatalf("Generation: %v", err)
	}
	if gen != 1 {
		t.Errorf("second Create must not reset the generation ledger: gen = %d want 1", gen)
	}
}

// TestBumpGenerationOrderingAndMonotonic proves the ledger is durable and
// strictly monotonic across repeated bumps, the pairing mechanism's core
// invariant.
func TestBumpGenerationOrderingAndMonotonic(t *testing.T) {
	dir := t.TempDir()
	m := NewManager(dir)
	if err := m.Create("wl-a", 1<<20); err != nil {
		t.Fatalf("Create: %v", err)
	}
	for want := uint64(1); want <= 5; want++ {
		got, err := m.BumpGeneration("wl-a")
		if err != nil {
			t.Fatalf("BumpGeneration #%d: %v", want, err)
		}
		if got != want {
			t.Errorf("BumpGeneration #%d = %d want %d", want, got, want)
		}
	}
	// A fresh Manager instance reading the same root sees the durable value: the
	// ledger survived process restart (simulated by a new in-memory Manager).
	m2 := NewManager(dir)
	gen, err := m2.Generation("wl-a")
	if err != nil {
		t.Fatalf("Generation after restart: %v", err)
	}
	if gen != 5 {
		t.Errorf("generation after restart = %d want 5 (durable across a new Manager)", gen)
	}
}

// TestGenerationUnreadableWithoutVolume proves a workload with no volume
// (never created) reports a distinct, non-panicking error, matching the
// "ledger_unreadable" contract the RELIGHT path relies on.
func TestGenerationUnreadableWithoutVolume(t *testing.T) {
	dir := t.TempDir()
	m := NewManager(dir)
	if _, err := m.Generation("nope"); err == nil {
		t.Error("Generation for a never-created workload should error")
	}
}

// TestGenerationUnreadableMalformedLedger proves a corrupted ledger file (not
// a valid uint64) is reported as an error, not silently coerced to 0 or
// panicking.
func TestGenerationUnreadableMalformedLedger(t *testing.T) {
	dir := t.TempDir()
	m := NewManager(dir)
	if err := m.Create("wl-a", 1<<20); err != nil {
		t.Fatalf("Create: %v", err)
	}
	if err := os.WriteFile(filepath.Join(dir, "wl-a", genFile), []byte("not-a-number"), 0o600); err != nil {
		t.Fatalf("corrupt ledger: %v", err)
	}
	if _, err := m.Generation("wl-a"); err == nil {
		t.Error("Generation should error on a malformed ledger")
	}
}

// TestRecordBlessedAdvancesLedgerAndMarksBlessed proves a CP-issued blessed
// generation (R7, ADR embervm/011) both advances the generation ledger to the
// exact value the control plane issued and marks the volume blessed, unlike a
// legacy BumpGeneration which never touches the blessed marker.
func TestRecordBlessedAdvancesLedgerAndMarksBlessed(t *testing.T) {
	dir := t.TempDir()
	m := NewManager(dir)
	if err := m.Create("wl-a", 1<<20); err != nil {
		t.Fatalf("Create: %v", err)
	}
	if m.GenerationBlessed("wl-a") {
		t.Error("a freshly created volume must not read as blessed (no blessing ever issued)")
	}

	gen, err := m.RecordBlessed("wl-a", 3)
	if err != nil {
		t.Fatalf("RecordBlessed: %v", err)
	}
	if gen != 3 {
		t.Errorf("RecordBlessed = %d want 3", gen)
	}
	got, err := m.Generation("wl-a")
	if err != nil {
		t.Fatalf("Generation: %v", err)
	}
	if got != 3 {
		t.Errorf("generation ledger = %d want 3 after RecordBlessed", got)
	}
	if !m.GenerationBlessed("wl-a") {
		t.Error("GenerationBlessed should be true immediately after RecordBlessed")
	}
}

// TestRecordBlessedRejectsNonAdvancingGeneration proves RecordBlessed refuses
// a blessed generation that does not strictly exceed the ledger's current
// value: a stale or repeated blessing must never let a bundle falsely re-match.
func TestRecordBlessedRejectsNonAdvancingGeneration(t *testing.T) {
	dir := t.TempDir()
	m := NewManager(dir)
	if err := m.Create("wl-a", 1<<20); err != nil {
		t.Fatalf("Create: %v", err)
	}
	if _, err := m.RecordBlessed("wl-a", 5); err != nil {
		t.Fatalf("RecordBlessed: %v", err)
	}
	if _, err := m.RecordBlessed("wl-a", 5); err == nil {
		t.Error("RecordBlessed with a generation equal to the current ledger value should error")
	}
	if _, err := m.RecordBlessed("wl-a", 4); err == nil {
		t.Error("RecordBlessed with a generation below the current ledger value should error")
	}
}

// TestBumpGenerationLeavesBlessedMarkerBehind proves the legacy self-bump path
// (BumpGeneration) advances the generation ledger WITHOUT touching the
// blessed marker, so a volume that was blessed and then self-bumps reads
// unblessed again (the exact "unblessed report" the control plane quarantines
// on adoption).
func TestBumpGenerationLeavesBlessedMarkerBehind(t *testing.T) {
	dir := t.TempDir()
	m := NewManager(dir)
	if err := m.Create("wl-a", 1<<20); err != nil {
		t.Fatalf("Create: %v", err)
	}
	if _, err := m.RecordBlessed("wl-a", 3); err != nil {
		t.Fatalf("RecordBlessed: %v", err)
	}
	if !m.GenerationBlessed("wl-a") {
		t.Fatal("expected blessed immediately after RecordBlessed")
	}
	if _, err := m.BumpGeneration("wl-a"); err != nil {
		t.Fatalf("BumpGeneration: %v", err)
	}
	if m.GenerationBlessed("wl-a") {
		t.Error("a self-bump past the last blessed generation must read as unblessed")
	}
}

// TestAttachSingletonLockRefusal proves the singleton writable-attach
// invariant: a second Attach for the same workload while the first is still
// held is refused.
func TestAttachSingletonLockRefusal(t *testing.T) {
	dir := t.TempDir()
	m := NewManager(dir)
	if err := m.Attach("wl-a"); err != nil {
		t.Fatalf("first Attach: %v", err)
	}
	if err := m.Attach("wl-a"); err == nil {
		t.Error("second Attach on the same workload should be refused")
	}
	if !m.IsAttached("wl-a") {
		t.Error("IsAttached should report true while held")
	}
	m.Detach("wl-a")
	if m.IsAttached("wl-a") {
		t.Error("IsAttached should report false after Detach")
	}
	// Re-attach after detach succeeds.
	if err := m.Attach("wl-a"); err != nil {
		t.Errorf("Attach after Detach should succeed: %v", err)
	}
	// A different workload's attach is entirely independent.
	if err := m.Attach("wl-b"); err != nil {
		t.Errorf("Attach for a distinct workload should succeed: %v", err)
	}
}

// TestDetachIdempotent proves Detach on an unattached workload is a safe no-op.
func TestDetachIdempotent(t *testing.T) {
	dir := t.TempDir()
	m := NewManager(dir)
	m.Detach("never-attached") // must not panic
}

func TestReleaseOrphanedHealthyBoundAttach(t *testing.T) {
	m := NewManager(t.TempDir())
	if err := m.Attach("wl-a"); err != nil {
		t.Fatalf("Attach: %v", err)
	}
	m.Bind("wl-a", "vm-a")
	if reason, released := m.ReleaseOrphaned("wl-a", "vm-a", 0); released || reason != "" {
		t.Fatalf("ReleaseOrphaned healthy attach = %q, %v want empty, false", reason, released)
	}
	if !m.IsAttached("wl-a") {
		t.Error("healthy bound attach should remain held")
	}
}

func TestReleaseOrphanedReclaimsReplacedBoundAttach(t *testing.T) {
	m := NewManager(t.TempDir())
	if err := m.Attach("wl-a"); err != nil {
		t.Fatalf("Attach: %v", err)
	}
	m.Bind("wl-a", "vm-a")
	if reason, released := m.ReleaseOrphaned("wl-a", "vm-b", time.Hour); !released || reason != `owner vm "vm-a" is no longer live` {
		t.Fatalf("ReleaseOrphaned replaced attach = %q, %v", reason, released)
	}
	if m.IsAttached("wl-a") {
		t.Error("reclaimed attach should not remain held")
	}
	if err := m.Attach("wl-a"); err != nil {
		t.Fatalf("Attach after reclaim: %v", err)
	}
}

func TestReleaseOrphanedReclaimsUnboundAttachWithoutLiveVM(t *testing.T) {
	m := NewManager(t.TempDir())
	if err := m.Attach("wl-a"); err != nil {
		t.Fatalf("Attach: %v", err)
	}
	m.Bind("wl-a", "vm-a")
	if reason, released := m.ReleaseOrphaned("wl-a", "", time.Hour); !released || reason != `owner vm "vm-a" is no longer live` {
		t.Fatalf("ReleaseOrphaned missing live VM = %q, %v", reason, released)
	}
}

func TestReleaseOrphanedPendingAttachGrace(t *testing.T) {
	t.Run("slow boot is retained", func(t *testing.T) {
		m := NewManager(t.TempDir())
		if err := m.Attach("wl-a"); err != nil {
			t.Fatalf("Attach: %v", err)
		}
		if reason, released := m.ReleaseOrphaned("wl-a", "", time.Hour); released || reason != "" {
			t.Fatalf("ReleaseOrphaned within grace = %q, %v want empty, false", reason, released)
		}
	})
	t.Run("expired start is reclaimed", func(t *testing.T) {
		m := NewManager(t.TempDir())
		if err := m.Attach("wl-a"); err != nil {
			t.Fatalf("Attach: %v", err)
		}
		if reason, released := m.ReleaseOrphaned("wl-a", "", 0); !released || reason == "" {
			t.Fatalf("ReleaseOrphaned expired attach = %q, %v", reason, released)
		}
	})
}

func TestReleaseOrphanedNeverAttached(t *testing.T) {
	m := NewManager(t.TempDir())
	if reason, released := m.ReleaseOrphaned("never-attached", "", 0); released || reason != "" {
		t.Fatalf("ReleaseOrphaned missing attach = %q, %v", reason, released)
	}
}

func TestDetachReleasesAndBindUnattachedIsNoOp(t *testing.T) {
	m := NewManager(t.TempDir())
	m.Bind("never-attached", "vm-a")
	if err := m.Attach("wl-a"); err != nil {
		t.Fatalf("Attach: %v", err)
	}
	m.Detach("wl-a")
	if m.IsAttached("wl-a") {
		t.Error("Detach should fully release the attach")
	}
}

// TestDeleteRefusesWhileAttached proves the ONLY destructive data verb refuses
// to run against a live writable attach, and succeeds (idempotently) once
// detached or on an already-absent volume.
func TestDeleteRefusesWhileAttached(t *testing.T) {
	dir := t.TempDir()
	m := NewManager(dir)
	if err := m.Create("wl-a", 1<<20); err != nil {
		t.Fatalf("Create: %v", err)
	}
	if err := m.Attach("wl-a"); err != nil {
		t.Fatalf("Attach: %v", err)
	}
	if err := m.Delete("wl-a"); err == nil {
		t.Error("Delete while attached should be refused")
	}
	m.Detach("wl-a")
	if err := m.Delete("wl-a"); err != nil {
		t.Errorf("Delete after Detach should succeed: %v", err)
	}
	if m.Exists("wl-a") {
		t.Error("volume should be gone after Delete")
	}
	// Idempotent on an already-absent volume.
	if err := m.Delete("wl-a"); err != nil {
		t.Errorf("Delete of an already-absent volume should be idempotent OK: %v", err)
	}
}

// TestScanInventoryRediscoversVolumes proves the boot-rescan source: a fresh
// Manager pointed at the same root sees every workload's volume facts purely
// from disk, with no in-memory state carried over (simulating a daemon
// restart).
func TestScanInventoryRediscoversVolumes(t *testing.T) {
	dir := t.TempDir()
	m := NewManager(dir)
	if err := m.Create("wl-a", 1<<20); err != nil {
		t.Fatalf("Create wl-a: %v", err)
	}
	if _, err := m.BumpGeneration("wl-a"); err != nil {
		t.Fatalf("bump wl-a: %v", err)
	}
	if err := m.Create("wl-b", 2<<20); err != nil {
		t.Fatalf("Create wl-b: %v", err)
	}
	if err := m.Attach("wl-b"); err != nil {
		t.Fatalf("Attach wl-b: %v", err)
	}

	m2 := NewManager(dir) // simulates a restarted daemon: no in-memory state
	inv, err := m2.Scan()
	if err != nil {
		t.Fatalf("Scan: %v", err)
	}
	if len(inv) != 2 {
		t.Fatalf("Scan found %d volumes want 2", len(inv))
	}
	byWorkload := map[string]Inventory{}
	for _, v := range inv {
		byWorkload[v.Workload] = v
	}
	if a := byWorkload["wl-a"]; a.Generation != 1 || a.SizeBytes != 1<<20 {
		t.Errorf("wl-a inventory = %+v", a)
	}
	// wl-b's attach state does NOT survive a restart (a fresh Manager has an
	// empty in-process lock map): a restarted daemon's live-VM adoption is the
	// control plane's job, not volume.Manager's, exactly as ReconcileStatefulFromDisk
	// documents (a live stateful VM does not survive a daemon restart).
	if b := byWorkload["wl-b"]; b.Attached {
		t.Error("a fresh Manager (simulated restart) should not report an attach that died with the prior process")
	}
}

// TestScanSkipsHalfCreatedWorkloadDir proves a directory with no vol.img
// (half-written, or a race) is skipped rather than reported as a phantom
// volume.
func TestScanSkipsHalfCreatedWorkloadDir(t *testing.T) {
	dir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(dir, "half-baked"), 0o700); err != nil {
		t.Fatal(err)
	}
	m := NewManager(dir)
	inv, err := m.Scan()
	if err != nil {
		t.Fatalf("Scan: %v", err)
	}
	if len(inv) != 0 {
		t.Errorf("Scan should skip a workload dir with no vol.img, got %+v", inv)
	}
}

// TestScanEmptyRoot proves Scan on a not-yet-created VolumeRoot returns an
// empty inventory, not an error (a fresh node before any StartStateful).
func TestScanEmptyRoot(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "does-not-exist-yet")
	m := NewManager(dir)
	inv, err := m.Scan()
	if err != nil {
		t.Fatalf("Scan on absent root should not error: %v", err)
	}
	if len(inv) != 0 {
		t.Errorf("Scan on absent root should be empty, got %+v", inv)
	}
}

func TestBlessingLeasePersistsAndConsumesMonotonically(t *testing.T) {
	dir := t.TempDir()
	m := NewManager(dir)
	if err := m.Create("wl", 4096); err != nil {
		t.Fatal(err)
	}
	if err := m.ApplyBlessingLease("wl", BlessingLease{NextGeneration: 10, LeaseEnd: 13}); err != nil {
		t.Fatal(err)
	}
	got, err := m.ConsumeGenerationFromLease("wl", 1)
	if err != nil || len(got) != 1 || got[0] != 10 {
		t.Fatalf("first consume = %v, %v", got, err)
	}
	restarted := NewManager(dir)
	got, err = restarted.ConsumeGenerationFromLease("wl", 2)
	if err != nil || len(got) != 2 || got[0] != 11 || got[1] != 12 {
		t.Fatalf("restart consume = %v, %v", got, err)
	}
	got, err = restarted.ConsumeGenerationFromLease("wl", 1)
	if err != nil || len(got) != 0 {
		t.Fatalf("exhausted consume = %v, %v", got, err)
	}
}

func TestApplyBlessingLeaseAppliesForwardRenewalOfActiveLease(t *testing.T) {
	m := NewManager(t.TempDir())
	if err := m.ApplyBlessingLease("wl", BlessingLease{NextGeneration: 4, LeaseEnd: 1001}); err != nil {
		t.Fatal(err)
	}
	if err := m.ApplyBlessingLease("wl", BlessingLease{NextGeneration: 1002, LeaseEnd: 2002}); err != nil {
		t.Fatal(err)
	}

	got, err := m.ConsumeGenerationFromLease("wl", 1)
	if err != nil || len(got) != 1 || got[0] != 1002 {
		t.Fatalf("renewed lease consume = %v, %v", got, err)
	}
}

func TestBlessingLeaseForwardRenewalAfterControlPlaneBlessing(t *testing.T) {
	m := NewManager(t.TempDir())
	if err := m.Create("wl", 4096); err != nil {
		t.Fatal(err)
	}
	if err := m.ApplyBlessingLease("wl", BlessingLease{NextGeneration: 1, LeaseEnd: 1001}); err != nil {
		t.Fatal(err)
	}

	for want := uint64(1); want <= 3; want++ {
		generations, err := m.ConsumeGenerationFromLease("wl", 1)
		if err != nil || len(generations) != 1 || generations[0] != want {
			t.Fatalf("activator consume #%d = %v, %v", want, generations, err)
		}
		if _, err := m.RecordBlessed("wl", want); err != nil {
			t.Fatalf("RecordBlessed #%d: %v", want, err)
		}
	}

	if _, err := m.RecordBlessed("wl", 1001); err != nil {
		t.Fatalf("RecordBlessed CP generation: %v", err)
	}
	if err := m.ApplyBlessingLease("wl", BlessingLease{NextGeneration: 1002, LeaseEnd: 2002}); err != nil {
		t.Fatal(err)
	}

	generations, err := m.ConsumeGenerationFromLease("wl", 1)
	if err != nil || len(generations) != 1 || generations[0] != 1002 {
		t.Fatalf("renewed activator consume = %v, %v", generations, err)
	}
	gen, err := m.RecordBlessed("wl", generations[0])
	if err != nil || gen != 1002 {
		t.Fatalf("renewed RecordBlessed = %d, %v", gen, err)
	}
}

func TestBlessingLeaseMissingFailsOpen(t *testing.T) {
	m := NewManager(t.TempDir())
	got, err := m.ConsumeGenerationFromLease("missing", 1)
	if err != nil || len(got) != 0 {
		t.Fatalf("missing lease = %v, %v", got, err)
	}
}

func TestConsumeClampsPastInRangeLedgerFromAutoHeal(t *testing.T) {
	m := NewManager(t.TempDir())
	if err := m.Create("wl", 4096); err != nil {
		t.Fatalf("Create: %v", err)
	}
	if err := m.ApplyBlessingLease("wl", BlessingLease{NextGeneration: 10, LeaseEnd: 15}); err != nil {
		t.Fatalf("ApplyBlessingLease: %v", err)
	}

	// This models auto_heal_checkpoint_abort directly blessing reported_gen
	// inside the outstanding lease. The load-bearing clamp must skip that
	// already-ledgered generation so the lease cannot double-issue it.
	if _, err := m.RecordBlessed("wl", 12); err != nil {
		t.Fatalf("auto-heal RecordBlessed(12): %v", err)
	}
	ledger, err := m.Generation("wl")
	if err != nil {
		t.Fatalf("Generation after auto-heal blessing: %v", err)
	}
	if ledger != 12 {
		t.Fatalf("auto-heal ledger = %d, want 12 before consuming lease", ledger)
	}

	got, err := m.ConsumeGenerationFromLease("wl", 1)
	if err != nil {
		t.Fatalf("ConsumeGenerationFromLease: %v", err)
	}
	if len(got) != 1 {
		t.Fatalf("clamped consume returned %v, want exactly one generation", got)
	}
	if got[0] <= ledger {
		t.Fatalf("clamped consume returned %v with ledger at %d, want a generation strictly greater than the ledger", got, ledger)
	}
	if got[0] != 13 {
		t.Fatalf("clamped consume returned %v, want [13] after in-range ledger advance to 12", got)
	}
}
