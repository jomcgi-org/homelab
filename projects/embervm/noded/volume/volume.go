// Package volume is embervm-noded's stateful-workload volume manager (R4). A
// stateful workload owns exactly one raw, sparse volume file on node NVMe: the
// durable data that outlives every VM instance (ADR embervm/001's state split).
// This package owns the on-disk layout (<VolumeRoot>/<workload>/vol.img plus a
// generation ledger), the singleton writable-attach lock, and the generation
// bump the StartStateful/StopStateful handlers thread through every boot.
//
// The generation ledger is the ENTIRE pairing mechanism between a volume and a
// banked stateful bundle (a memory snapshot): every writable attach bumps it
// BEFORE the VM boots, and a bank stamps the then-current value into the bundle.
// A relight resumes only when the bundle's stamped generation equals the
// volume's current generation; any mismatch means an attach happened the
// snapshot never witnessed, so the memory state cannot be trusted and the
// daemon must fall back to a cold boot from the volume (slower, never
// incorrect). The volume BYTES are never read, hashed, or copied here: this
// package manages metadata (the ledger, the attach lock, block usage) only. The
// host never mounts or parses the volume's filesystem; that is guest-init's job.
package volume

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
)

// volFile / genFile / blessedFile are the files under a workload's volume
// directory: the raw sparse block device backing file, the generation ledger,
// and the blessed-generation marker (R7, ADR embervm/011). blessedFile holds
// the last generation the control plane's blessing ledger issued for this
// volume; it lags genFile whenever a self-bump happens without a matching
// CP-issued blessed_generation (the unblessed case a fresh control plane
// quarantines on adoption).
const (
	volFile     = "vol.img"
	genFile     = "gen"
	blessedFile = "genblessed"
	leaseFile   = ".blessing-lease"
	// RetirementIntentFile marks a lineage pending node-owned retirement. Exported
	// so the artifact exporter can EXCLUDE it: the marker must never ride the S3
	// artifact, or a restore would resurrect it and retire a live workspace.
	RetirementIntentFile = ".retirement-intent"
)

// BlessingLease is the durable, exclusive generation range a brick may use
// for node-local writable attaches. lease_end is exclusive.
type BlessingLease struct {
	NextGeneration uint64 `json:"next_generation"`
	LeaseEnd       uint64 `json:"lease_end"`
}

// Manager owns the on-disk volume layout under root and the in-process
// singleton writable-attach lock. Safe for concurrent use.
type Manager struct {
	root string

	mu       sync.Mutex
	attached map[string]attachment
	leaseMu  sync.Mutex
}

// attachment records WHO holds a workload's writable-attach lock. owner is
// the vmID, and is empty while a start is still in flight, because Attach is
// acquired before the VM exists. Without an owner the lock cannot be
// reconciled against reality, which is what wedged demo-postgres in #3648.
type attachment struct {
	owner string
	since time.Time
}

// NewManager builds a Manager rooted at root (VolumeRoot). It does not touch
// disk; callers create the root lazily via Create.
func NewManager(root string) *Manager {
	return &Manager{root: root, attached: make(map[string]attachment)}
}

// dir is the per-workload volume directory.
func (m *Manager) dir(workload string) string {
	return filepath.Join(m.root, workload)
}

func (m *Manager) volPath(workload string) string {
	return filepath.Join(m.dir(workload), volFile)
}

func (m *Manager) genPath(workload string) string {
	return filepath.Join(m.dir(workload), genFile)
}

func (m *Manager) blessedPath(workload string) string {
	return filepath.Join(m.dir(workload), blessedFile)
}

func (m *Manager) leasePath(workload string) string {
	return filepath.Join(m.dir(workload), leaseFile)
}

// Exists reports whether a workload's volume file is already on disk.
func (m *Manager) Exists(workload string) bool {
	_, err := os.Stat(m.volPath(workload))
	return err == nil
}

// Create makes a workload's volume directory and a sparse raw file sized
// sizeBytes, and initialises its generation ledger to 0. It does NOT write
// zeros to the file (Truncate grows a sparse hole, so a multi-GiB volume costs
// no I/O and no disk space until the guest actually writes into it). Idempotent
// in the sense that an already-present vol.img is left untouched (the caller,
// StartStateful FRESH, only calls Create when Exists is false; a race between
// two Creates for the same workload cannot happen because the volume is
// singleton and the attach lock serializes StartStateful callers upstream of
// this by workload, but Create itself is defensive: it will not clobber an
// existing file even if called again).
func (m *Manager) Create(workload string, sizeBytes uint64) error {
	if workload == "" {
		return fmt.Errorf("volume: workload required")
	}
	dir := m.dir(workload)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return fmt.Errorf("volume: mkdir %q: %w", dir, err)
	}
	path := m.volPath(workload)
	if _, err := os.Stat(path); err == nil {
		// Already present: never recreate (would truncate a live workload's data).
		return nil
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		if os.IsExist(err) {
			// Lost a create race to a concurrent caller; the file is theirs now.
			return nil
		}
		return fmt.Errorf("volume: create %q: %w", path, err)
	}
	closeErr := func() error {
		defer f.Close()
		// Truncate to sizeBytes WITHOUT writing: the file becomes sparse (a hole),
		// so a large volume_size_bytes cap costs no disk space until the guest
		// writes into it. This is the "sparse cap" the proto contract documents.
		if err := f.Truncate(int64(sizeBytes)); err != nil {
			return fmt.Errorf("volume: truncate %q to %d bytes: %w", path, sizeBytes, err)
		}
		return nil
	}()
	if closeErr != nil {
		_ = os.Remove(path)
		return closeErr
	}
	if err := m.writeGeneration(workload, 0); err != nil {
		_ = os.Remove(path)
		return fmt.Errorf("volume: init generation ledger for %q: %w", workload, err)
	}
	return nil
}

// Attach acquires the in-process singleton writable-attach lock for a workload.
// It refuses (an error the caller maps to FAILED_PRECONDITION) when the
// workload's volume is already attached to a live VM: there is exactly one
// writable attach per stateful workload, enforced where the data physically
// lives, with no maxInstances knob. Detach releases it.
func (m *Manager) Attach(workload string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if _, ok := m.attached[workload]; ok {
		return fmt.Errorf("volume: workload %q already has a writable attach in progress", workload)
	}
	m.attached[workload] = attachment{since: time.Now()}
	return nil
}

// Bind records the vmID that owns workload's writable attach, once the VM
// exists. A bound attach can be reclaimed by a later start when the registry
// says that vm is gone; an unbound one can only age out. No-op if the workload
// is not attached.
func (m *Manager) Bind(workload, vmID string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	attach, ok := m.attached[workload]
	if !ok {
		return
	}
	attach.owner = vmID
	m.attached[workload] = attach
}

// ReleaseOrphaned reclaims workload's writable attach when it is provably
// stale, returning the reason and true when it released. liveVMID is the vmID
// the caller's live-instance registry currently has for this workload, empty
// when it has none.
//
// Two stale cases, both conservative:
//   - bound to an owner that is not liveVMID: the owning VM is gone or has
//     been replaced, so nothing holds the device.
//   - unbound for longer than pendingGrace: a start that never reached the VM.
//     Gated on a grace far longer than any legitimate boot so a slow start is
//     never robbed mid-flight.
//
// A healthy attach (owner == liveVMID, both non-empty) is never touched.
func (m *Manager) ReleaseOrphaned(workload, liveVMID string, pendingGrace time.Duration) (string, bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	attach, ok := m.attached[workload]
	if !ok {
		return "", false
	}
	if attach.owner == "" {
		if time.Since(attach.since) > pendingGrace {
			delete(m.attached, workload)
			return fmt.Sprintf("in-flight start exceeded %s without binding a vm", pendingGrace), true
		}
		return "", false
	}
	if attach.owner == liveVMID {
		return "", false
	}
	delete(m.attached, workload)
	return fmt.Sprintf("owner vm %q is no longer live", attach.owner), true
}

// Detach releases the writable-attach lock for a workload. Idempotent: an
// unattached workload is a no-op, so a defensive double-detach (e.g. a reap
// path racing a normal teardown) never panics.
func (m *Manager) Detach(workload string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	delete(m.attached, workload)
}

// IsAttached reports whether a workload currently holds the writable-attach
// lock, for the DeleteVolume FAILED_PRECONDITION guard and Inventory's
// attached fact.
func (m *Manager) IsAttached(workload string) bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	_, ok := m.attached[workload]
	return ok
}

// BumpGeneration increments a workload's generation ledger and returns the new
// value. It MUST be called before the VM that will hold the writable attach
// boots: the generation is the pair key a later relight checks against a
// bundle's stamped value, so any attach the ledger did not witness before the
// guest could have written breaks the pair. The bump is durable and monotonic
// by construction: a boot failure AFTER a successful bump does NOT roll the
// ledger back (there is no "un-bump"), because the daemon cannot prove the
// guest never touched the device between the bump and the failure, so treating
// the generation as consumed is the only safe assumption. Errors if the ledger
// is missing (the volume must exist first, via Create) or unreadable.
func (m *Manager) BumpGeneration(workload string) (uint64, error) {
	cur, err := m.Generation(workload)
	if err != nil {
		return 0, err
	}
	next := cur + 1
	if err := m.writeGeneration(workload, next); err != nil {
		return 0, fmt.Errorf("volume: bump generation for %q: %w", workload, err)
	}
	return next, nil
}

// Generation reads a workload's current generation from the ledger. A missing
// ledger (the volume dir exists but gen does not, or the volume itself is
// absent) or a malformed value is a distinct, non-panicking error: callers on
// the RELIGHT path treat any error here as "ledger_unreadable" and fall back
// to a cold boot rather than trusting a bundle they cannot validate.
func (m *Manager) Generation(workload string) (uint64, error) {
	b, err := os.ReadFile(m.genPath(workload))
	if err != nil {
		return 0, fmt.Errorf("volume: read generation ledger for %q: %w", workload, err)
	}
	n, err := strconv.ParseUint(strings.TrimSpace(string(b)), 10, 64)
	if err != nil {
		return 0, fmt.Errorf("volume: malformed generation ledger for %q: %w", workload, err)
	}
	return n, nil
}

// RecordBlessed records a writable attach whose generation the control plane
// issued (R7, ADR embervm/011, standing decision 4): it writes gen to BOTH the
// generation ledger and the blessed marker, so GenerationBlessed reports true
// for this attach. Unlike BumpGeneration, the caller supplies the value (the
// control plane's blessing ledger is the sole issuer); the daemon never
// invents or increments it here. gen MUST be strictly greater than the
// ledger's current value or this errors: a blessed generation that did not
// advance the ledger would let a stale bundle falsely re-match, which the
// pairing mechanism must never allow. Same durability discipline as
// writeGeneration (temp + rename, no un-bump on a later boot failure).
func (m *Manager) RecordBlessed(workload string, gen uint64) (uint64, error) {
	cur, err := m.Generation(workload)
	if err != nil {
		return 0, err
	}
	if gen <= cur {
		return 0, fmt.Errorf("volume: blessed generation %d for %q must exceed current ledger generation %d", gen, workload, cur)
	}
	if err := m.writeGeneration(workload, gen); err != nil {
		return 0, fmt.Errorf("volume: record blessed generation for %q: %w", workload, err)
	}
	if err := m.writeBlessed(workload, gen); err != nil {
		return 0, fmt.Errorf("volume: record blessed marker for %q: %w", workload, err)
	}
	return gen, nil
}

// ApplyBlessingLease persists a lease delivered through SyncRegistry. A replay
// must never move a locally consumed cursor backward, so an existing lease is
// only replaced by a range that starts at or beyond its current cursor.
func (m *Manager) ApplyBlessingLease(workload string, lease BlessingLease) error {
	if workload == "" || lease.NextGeneration >= lease.LeaseEnd {
		return nil
	}
	m.leaseMu.Lock()
	defer m.leaseMu.Unlock()
	current, err := m.readBlessingLease(workload)
	if err != nil && !os.IsNotExist(err) {
		return err
	}
	if err == nil && current.NextGeneration >= lease.NextGeneration && current.LeaseEnd >= lease.LeaseEnd {
		return nil
	}
	return m.persistBlessingLease(workload, &lease)
}

// ConsumeGenerationFromLease atomically advances the persisted lease cursor.
// An absent or exhausted lease returns an empty result so callers can preserve
// the legacy self-bump behavior.
func (m *Manager) ConsumeGenerationFromLease(workload string, count uint64) ([]uint64, error) {
	if count == 0 {
		return nil, nil
	}
	m.leaseMu.Lock()
	defer m.leaseMu.Unlock()
	lease, err := m.readBlessingLease(workload)
	if os.IsNotExist(err) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if lease.NextGeneration >= lease.LeaseEnd {
		return nil, nil
	}
	start := lease.NextGeneration
	// This clamp is load-bearing: it prevents double-issuance on the real path
	// where auto_heal_checkpoint_abort (in control/lib/embervm/stateful_store.ex)
	// durably blesses reported_gen directly, bypassing handle_call_next_blessed,
	// and can therefore bless a value INSIDE this outstanding lease range.
	// Fenced-writer adoption advances the watermark the same way. Removing this
	// clamp on the belief that the CP-side max() guarantees invariant 1 would
	// reintroduce double-issuance. This is not defensive; it is critical to the
	// pairing mechanism.
	if current, err := m.Generation(workload); err == nil && start <= current {
		start = current + 1
	}
	if start >= lease.LeaseEnd {
		return nil, nil
	}
	n := count
	if remaining := lease.LeaseEnd - start; n > remaining {
		n = remaining
	}
	lease.NextGeneration = start + n
	if err := m.persistBlessingLease(workload, lease); err != nil {
		return nil, err
	}
	values := make([]uint64, n)
	for i := range values {
		values[i] = start + uint64(i)
	}
	return values, nil
}

func (m *Manager) readBlessingLease(workload string) (*BlessingLease, error) {
	b, err := os.ReadFile(m.leasePath(workload))
	if err != nil {
		return nil, err
	}
	var lease BlessingLease
	if err := json.Unmarshal(b, &lease); err != nil {
		return nil, fmt.Errorf("volume: malformed blessing lease for %q: %w", workload, err)
	}
	return &lease, nil
}

func (m *Manager) persistBlessingLease(workload string, lease *BlessingLease) error {
	dir := m.dir(workload)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return fmt.Errorf("volume: mkdir %q: %w", dir, err)
	}
	b, err := json.Marshal(lease)
	if err != nil {
		return fmt.Errorf("volume: encode blessing lease for %q: %w", workload, err)
	}
	path := m.leasePath(workload)
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, b, 0o600); err != nil {
		return fmt.Errorf("volume: write blessing lease temp file %q: %w", tmp, err)
	}
	if err := os.Rename(tmp, path); err != nil {
		return fmt.Errorf("volume: publish blessing lease %q: %w", path, err)
	}
	return nil
}

// GenerationBlessed reports whether a workload's CURRENT generation (per
// genFile) matches the last generation the control plane's blessing ledger
// recorded (per blessedFile). False whenever the two diverge: a legacy
// self-bump (BumpGeneration, never touching the blessed marker) advances
// genFile past blessedFile, so a volume that has EVER self-bumped since its
// last blessing reads unblessed until the control plane blesses again. An
// absent blessed marker (a volume that has never been blessed, including
// every volume created before R7) also reads false: fail closed, never
// fabricate a blessing the control plane never issued.
func (m *Manager) GenerationBlessed(workload string) bool {
	cur, err := m.Generation(workload)
	if err != nil {
		return false
	}
	blessed, err := m.readBlessed(workload)
	if err != nil {
		return false
	}
	return blessed == cur
}

func (m *Manager) readBlessed(workload string) (uint64, error) {
	b, err := os.ReadFile(m.blessedPath(workload))
	if err != nil {
		return 0, fmt.Errorf("volume: read blessed marker for %q: %w", workload, err)
	}
	n, err := strconv.ParseUint(strings.TrimSpace(string(b)), 10, 64)
	if err != nil {
		return 0, fmt.Errorf("volume: malformed blessed marker for %q: %w", workload, err)
	}
	return n, nil
}

func (m *Manager) writeBlessed(workload string, gen uint64) error {
	dir := m.dir(workload)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return fmt.Errorf("volume: mkdir %q: %w", dir, err)
	}
	path := m.blessedPath(workload)
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, []byte(strconv.FormatUint(gen, 10)), 0o600); err != nil {
		return fmt.Errorf("volume: write blessed marker temp file %q: %w", tmp, err)
	}
	if err := os.Rename(tmp, path); err != nil {
		return fmt.Errorf("volume: publish blessed marker %q: %w", path, err)
	}
	return nil
}

// writeGeneration durably persists a generation value: write to a temp file in
// the same directory, then os.Rename into place. rename(2) is atomic within a
// filesystem, so a crash mid-write never leaves a torn ledger (the reader sees
// either the old value or the new one, never a partial write); this is the
// same publish discipline the fcvm driver's SnapshotBase uses for its bundle
// files.
func (m *Manager) writeGeneration(workload string, gen uint64) error {
	dir := m.dir(workload)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return fmt.Errorf("volume: mkdir %q: %w", dir, err)
	}
	path := m.genPath(workload)
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, []byte(strconv.FormatUint(gen, 10)), 0o600); err != nil {
		return fmt.Errorf("volume: write generation temp file %q: %w", tmp, err)
	}
	if err := os.Rename(tmp, path); err != nil {
		return fmt.Errorf("volume: publish generation ledger %q: %w", path, err)
	}
	return nil
}

// AllocatedBytes reports a workload's volume file's actual block usage (not its
// sparse declared size): the watermark source for disk-pressure accounting.
// Prefers the real block count (stat.st_blocks * 512, the portable POSIX
// sector-count field every unix Stat_t exposes) and falls back to the file's
// logical size if the platform-specific Sys() assertion fails (a non-unix
// build), so the daemon still reports a defensible number rather than erroring.
func (m *Manager) AllocatedBytes(workload string) (uint64, error) {
	fi, err := os.Stat(m.volPath(workload))
	if err != nil {
		return 0, fmt.Errorf("volume: stat volume for %q: %w", workload, err)
	}
	if blocks, ok := statBlocks(fi); ok {
		// st_blocks is always counted in 512-byte units regardless of the
		// filesystem's actual block size (a POSIX invariant), so this needs no
		// st_blksize lookup.
		return uint64(blocks) * 512, nil
	}
	return uint64(fi.Size()), nil
}

// SizeBytes reports a workload's volume file's declared (sparse) size.
func (m *Manager) SizeBytes(workload string) (uint64, error) {
	fi, err := os.Stat(m.volPath(workload))
	if err != nil {
		return 0, fmt.Errorf("volume: stat volume for %q: %w", workload, err)
	}
	return uint64(fi.Size()), nil
}

// VolumePath returns the host path of a workload's raw volume file, for the
// driver to attach as a writable drive. Callers must have already confirmed
// Exists(workload).
func (m *Manager) VolumePath(workload string) string {
	return m.volPath(workload)
}

// SessionVolumePath returns the node-local workspace image for one session
// lineage. Session volumes intentionally live in a separate keyspace from the
// singleton stateful volume so two sessions of one workload cannot share data.
func (m *Manager) SessionVolumePath(workload, lineageID string) string {
	return filepath.Join(m.root, "session", workload, lineageID, "workspace.img")
}

// SessionLineageDir returns the durable directory for one session lineage.
func (m *Manager) SessionLineageDir(workload, lineageID string) string {
	return filepath.Dir(m.SessionVolumePath(workload, lineageID))
}

// WriteRetirementIntent atomically records a node-owned session retirement.
// The marker is deliberately tiny and lives beside the workspace so a crash
// between export and deletion is resumable without CP state.
func (m *Manager) WriteRetirementIntent(workload, lineageID string) error {
	if workload == "" || lineageID == "" {
		return fmt.Errorf("volume: workload and lineage required")
	}
	dir := m.SessionLineageDir(workload, lineageID)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	return os.WriteFile(filepath.Join(dir, RetirementIntentFile), []byte("retire\n"), 0o600)
}

// HasRetirementIntent reports whether a lineage is pending node-owned retirement.
func (m *Manager) HasRetirementIntent(workload, lineageID string) bool {
	_, err := os.Stat(filepath.Join(m.SessionLineageDir(workload, lineageID), RetirementIntentFile))
	return err == nil
}

// ClearRetirementIntent removes a completed retirement marker.
func (m *Manager) ClearRetirementIntent(workload, lineageID string) error {
	err := os.Remove(filepath.Join(m.SessionLineageDir(workload, lineageID), RetirementIntentFile))
	if os.IsNotExist(err) {
		return nil
	}
	return err
}

// CreateSession provisions a sparse per-lineage workspace image idempotently.
// Existing images are never resized. A changed workspace size applies only to
// new lineages, and atomic publication ensures Prime never sees a partial image.
func (m *Manager) CreateSession(workload, lineageID string, sizeBytes uint64) error {
	if workload == "" || lineageID == "" {
		return fmt.Errorf("volume: workload and lineage required")
	}
	path := m.SessionVolumePath(workload, lineageID)
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return fmt.Errorf("volume: mkdir session dir: %w", err)
	}
	if _, err := os.Stat(path); err == nil {
		return nil
	}
	f, err := os.CreateTemp(filepath.Dir(path), "workspace-img-*.tmp")
	if err != nil {
		if os.IsExist(err) {
			return nil
		}
		return fmt.Errorf("volume: create session image: %w", err)
	}
	tmp := f.Name()
	if err := f.Chmod(0o600); err != nil {
		_ = f.Close()
		_ = os.Remove(tmp)
		return fmt.Errorf("volume: chmod session image: %w", err)
	}
	if err := f.Truncate(int64(sizeBytes)); err != nil {
		_ = f.Close()
		_ = os.Remove(tmp)
		return fmt.Errorf("volume: size session image: %w", err)
	}
	if err := f.Close(); err != nil {
		return fmt.Errorf("volume: close session image: %w", err)
	}
	// Link is atomic and refuses to replace a concurrently published image.
	if err := os.Link(tmp, path); err != nil {
		if os.IsExist(err) {
			_ = os.Remove(tmp)
			return nil
		}
		_ = os.Remove(tmp)
		return fmt.Errorf("volume: publish session image: %w", err)
	}
	_ = os.Remove(tmp)
	return nil
}

// DeleteSession removes one lineage workspace. Existing images are never resized.
func (m *Manager) DeleteSession(workload, lineageID string) error {
	if workload == "" || lineageID == "" {
		return fmt.Errorf("volume: workload and lineage required")
	}
	if m.IsAttached(workload) {
		return fmt.Errorf("volume: workload %q volume is attached; detach before deleting", workload)
	}
	return os.RemoveAll(filepath.Join(m.root, "session", workload, lineageID))
}

// Delete removes a workload's volume directory (vol.img plus the generation
// ledger). It refuses (an error the caller maps to FAILED_PRECONDITION) while
// the volume is attached: deletion is the ONLY destructive data verb and must
// never race a live writable guest. Idempotent on an already-absent volume
// (RemoveAll on a missing path succeeds), matching the desired-end-state
// contract DeleteVolumeRequest documents.
func (m *Manager) Delete(workload string) error {
	if m.IsAttached(workload) {
		return fmt.Errorf("volume: workload %q volume is attached; detach before deleting", workload)
	}
	if err := os.RemoveAll(m.dir(workload)); err != nil {
		return fmt.Errorf("volume: remove volume dir for %q: %w", workload, err)
	}
	return nil
}

// Inventory is one workload's volume facts, the boot-rescan / NodeStatus
// projection shape (mirrors nodev1.Volume without importing the proto here, so
// this package stays independent of the gRPC contract; the server maps it).
type Inventory struct {
	Workload          string
	Generation        uint64
	SizeBytes         uint64
	AllocatedBytes    uint64
	Attached          bool
	GenerationBlessed bool
}

// Scan walks VolumeRoot for every workload's volume directory and returns its
// current facts, the boot-rescan source: a restarted daemon has no live
// in-memory state, but every volume and its generation ledger survive on NVMe,
// so this rebuilds the full durable-volume inventory from disk truth alone. A
// workload directory with no vol.img (half-created, or a deleted volume whose
// directory removal raced a crash) is skipped. A missing or unreadable
// generation ledger is reported as generation 0 with a logged-by-caller
// omission is NOT done here (Scan returns only fully-valid entries); a
// workload whose ledger cannot be read is skipped entirely rather than
// reporting a fabricated generation, since a stateful control plane must never
// receive a wrong generation as if it were real.
func (m *Manager) Scan() ([]Inventory, error) {
	entries, err := os.ReadDir(m.root)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, fmt.Errorf("volume: scan %q: %w", m.root, err)
	}
	out := make([]Inventory, 0, len(entries))
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		workload := e.Name()
		if !m.Exists(workload) {
			continue
		}
		gen, err := m.Generation(workload)
		if err != nil {
			continue
		}
		size, err := m.SizeBytes(workload)
		if err != nil {
			continue
		}
		alloc, err := m.AllocatedBytes(workload)
		if err != nil {
			continue
		}
		out = append(out, Inventory{
			Workload:          workload,
			Generation:        gen,
			SizeBytes:         size,
			AllocatedBytes:    alloc,
			Attached:          m.IsAttached(workload),
			GenerationBlessed: m.GenerationBlessed(workload),
		})
	}
	return out, nil
}
