package driver

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sync"
	"time"
)

// RootfsProvisioner gives each thread its own writable rootfs derived from a
// read-only base image, so threads never share or corrupt one disk. The default
// impl is a file copy ("full snapshots first"); DevmapperProvisioner is the
// copy-on-write impl (ADR 026) that replaces the full copy with a devmapper
// thin-snapshot of the base, behind the same interface.
type RootfsProvisioner interface {
	// Provision creates the thread's rootfs under dir and returns its host path.
	Provision(ctx context.Context, threadID, dir string) (string, error)
	// Teardown releases any out-of-dir resources the provisioner created for the
	// thread (a CoW device + its pool allocation), called when the thread's
	// bundle is reclaimed. It must be idempotent: reclaim may run more than once
	// and the device may already be gone. A file-copy provisioner has nothing to
	// release (RemoveBundle's RemoveAll deletes the file), so its Teardown is a
	// no-op.
	Teardown(ctx context.Context, threadID string) error
}

// CopyProvisioner copies a base rootfs image to a per-thread file. Simple and
// correct; the cost is a full copy + full disk per thread, which
// DevmapperProvisioner (ADR 026) replaces with a thin-snapshot.
type CopyProvisioner struct {
	// Base is the read-only base rootfs image (a flattened harness image).
	Base string
}

// Provision copies Base to dir/rootfs.ext4.
func (p *CopyProvisioner) Provision(_ context.Context, _, dir string) (string, error) {
	if p.Base == "" {
		return "", fmt.Errorf("driver: CopyProvisioner.Base is empty")
	}
	dst := filepath.Join(dir, "rootfs.ext4")
	if err := copyFile(p.Base, dst); err != nil {
		return "", fmt.Errorf("driver: provision rootfs: %w", err)
	}
	return dst, nil
}

// Teardown is a no-op: the per-thread rootfs file lives in the bundle dir, which
// RemoveBundle deletes wholesale.
func (p *CopyProvisioner) Teardown(_ context.Context, _ string) error { return nil }

func copyFile(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()

	out, err := os.OpenFile(dst, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0o600)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		return err
	}
	return out.Close()
}

// ---------------------------------------------------------------------------
// DevmapperProvisioner (ADR 026): copy-on-write rootfs via devmapper thin-pool.
// ---------------------------------------------------------------------------

// dmNameRE bounds a thread id (or base signature) to a safe device-mapper name
// component.
var dmNameRE = regexp.MustCompile(`[^A-Za-z0-9_-]`)

// Reserved thin device-id bands. The pool is SHARED with containerd's devmapper
// snapshotter, whose ids start at 0 and increment per image snapshot; it never
// reaches the millions. fc-agentd allocates from a high, disjoint band so its
// ids can never collide with containerd's (a collision would corrupt the other
// daemon's device). Thin ids are 24-bit (max 16,777,215), so these stay well
// inside the range while leaving containerd the whole low end.
const (
	baseDevIDStart   = 3_000_000 // base thin devices (one per base-image version)
	threadDevIDStart = 3_100_000 // per-thread snapshots, reused via the free list
)

// teardownTimeout bounds a single dmsetup teardown so a wedged pool op cannot
// stall the reconcile loop indefinitely.
const teardownTimeout = 30 * time.Second

// dmThinState is the persisted allocation table. It lives next to the bundles on
// the nvme disk (the same durable medium as the pool's own metadata), so the
// daemon recovers its device-id bookkeeping across restarts.
type dmThinState struct {
	// BaseSig identifies the loaded base by file size+mtime; a new base image
	// (harness bump) changes it and triggers a reload under a fresh id.
	BaseSig     string         `json:"base_sig"`
	BaseDevID   int            `json:"base_dev_id"`
	BaseSectors int64          `json:"base_sectors"`
	BaseLoaded  bool           `json:"base_loaded"`
	NextBaseID  int            `json:"next_base_id"`
	NextThread  int            `json:"next_thread_id"`
	FreeThread  []int          `json:"free_thread_ids"`
	Active      map[string]int `json:"active"` // threadID -> thin dev id
}

// DevmapperProvisioner provisions each thread a copy-on-write rootfs as a
// devmapper thin-snapshot of a base thin device (ADR 026). The base image is
// loaded into a thin device once (a one-time ~3GB write); every thread then gets
// an instant thin-snapshot of it, so per-thread rootfs creation drops from a
// ~2s full copy to milliseconds and per-thread disk drops to only the written
// delta. It drives the pool through the `dmsetup` CLI (in the runtime image),
// with udev sync disabled because the container has no udev daemon — libdevmapper
// then creates the /dev/mapper nodes directly.
type DevmapperProvisioner struct {
	// Pool is the thin-pool name (a /dev/mapper entry, e.g. "devpool").
	Pool string
	// Base is the read-only base rootfs image baked by the rootfs-builder.
	Base string
	// StateDir holds the persisted allocation table (the snapshot root).
	StateDir string

	mu    sync.Mutex
	state dmThinState
	// loaded guards one-time state load from disk.
	loaded bool
}

func (p *DevmapperProvisioner) poolPath() string { return filepath.Join("/dev/mapper", p.Pool) }

func (p *DevmapperProvisioner) statePath() string {
	return filepath.Join(p.StateDir, "dm-thin-state.json")
}

func dmName(prefix, id string) string {
	return prefix + dmNameRE.ReplaceAllString(id, "_")
}

// loadState reads the persisted allocation table once. A missing file is a fresh
// start, not an error.
func (p *DevmapperProvisioner) loadState() error {
	if p.loaded {
		return nil
	}
	b, err := os.ReadFile(p.statePath())
	switch {
	case err == nil:
		if uerr := json.Unmarshal(b, &p.state); uerr != nil {
			return fmt.Errorf("driver: dm state corrupt: %w", uerr)
		}
	case os.IsNotExist(err):
		// fresh
	default:
		return fmt.Errorf("driver: read dm state: %w", err)
	}
	if p.state.Active == nil {
		p.state.Active = map[string]int{}
	}
	if p.state.NextBaseID == 0 {
		p.state.NextBaseID = baseDevIDStart
	}
	if p.state.NextThread == 0 {
		p.state.NextThread = threadDevIDStart
	}
	p.loaded = true
	return nil
}

// saveState atomically persists the allocation table.
func (p *DevmapperProvisioner) saveState() error {
	b, err := json.MarshalIndent(&p.state, "", "  ")
	if err != nil {
		return err
	}
	tmp := p.statePath() + ".tmp"
	if err := os.WriteFile(tmp, b, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, p.statePath())
}

// dmsetup runs a dmsetup subcommand with udev sync disabled (no udev in the
// container) and returns combined output on error for diagnosis.
func dmsetup(ctx context.Context, args ...string) error {
	cmd := exec.CommandContext(ctx, "dmsetup", args...)
	cmd.Env = append(os.Environ(), "DM_DISABLE_UDEV=1")
	if out, err := cmd.CombinedOutput(); err != nil {
		return fmt.Errorf("dmsetup %v: %w: %s", args, err, string(out))
	}
	return nil
}

// poolMessage sends a thin-pool message (create_thin / create_snap / delete).
func (p *DevmapperProvisioner) poolMessage(ctx context.Context, msg string) error {
	return dmsetup(ctx, "message", p.Pool, "0", msg)
}

// activateThin creates a live dm device named name backed by thin dev id over
// the pool (table: "0 <sectors> thin <pool> <id>").
func (p *DevmapperProvisioner) activateThin(ctx context.Context, name string, devID int, sectors int64) error {
	table := fmt.Sprintf("0 %d thin %s %d", sectors, p.poolPath(), devID)
	return dmsetup(ctx, "create", name, "--table", table)
}

// removeDev tears down a live dm device, tolerating "already gone". --retry
// rides out the brief busy window after the VM process exits.
func (p *DevmapperProvisioner) removeDev(ctx context.Context, name string) error {
	if err := dmsetup(ctx, "info", name); err != nil {
		return nil // not present; nothing to remove
	}
	return dmsetup(ctx, "remove", "--retry", name)
}

// baseSig identifies the base image by size+mtime so a rebuilt base reloads, and
// returns its size in 512-byte sectors.
func baseSig(path string) (string, int64, error) {
	fi, err := os.Stat(path)
	if err != nil {
		return "", 0, err
	}
	// Round sectors UP so the device always holds the whole image even if the file
	// is not a 512-multiple (ext4 images are, but ceil is strictly safe: a short
	// device would fail the base copy on the trailing bytes).
	return fmt.Sprintf("%d-%d", fi.Size(), fi.ModTime().UnixNano()), (fi.Size() + 511) / 512, nil
}

// ensureBase loads the base image into a base thin device exactly once per base
// version (the caller holds the lock). The base is activated only long enough to
// copy the image in, then deactivated; its data persists in the pool under
// BaseDevID and every thread snapshots it without re-activating it.
func (p *DevmapperProvisioner) ensureBase(ctx context.Context) error {
	sig, sectors, err := baseSig(p.Base)
	if err != nil {
		return fmt.Errorf("driver: stat base rootfs: %w", err)
	}
	if p.state.BaseLoaded && p.state.BaseSig == sig {
		return nil
	}

	devID := p.state.NextBaseID
	if err := p.poolMessage(ctx, fmt.Sprintf("create_thin %d", devID)); err != nil {
		return fmt.Errorf("driver: create base thin dev: %w", err)
	}
	name := dmName("fcbase-", sig)
	if err := p.activateThin(ctx, name, devID, sectors); err != nil {
		return fmt.Errorf("driver: activate base thin dev: %w", err)
	}
	// Copy the base image into the activated thin device, then deactivate it. The
	// blocks now live in the pool under devID; snapshots share them copy-on-write.
	copyErr := copyToDevice(filepath.Join("/dev/mapper", name), p.Base)
	_ = p.removeDev(ctx, name)
	if copyErr != nil {
		return fmt.Errorf("driver: load base image into thin dev: %w", copyErr)
	}

	p.state.BaseSig = sig
	p.state.BaseDevID = devID
	p.state.BaseSectors = sectors
	p.state.BaseLoaded = true
	p.state.NextBaseID = devID + 1
	return p.saveState()
}

// copyToDevice writes a file's contents to a block device and flushes to disk.
func copyToDevice(dev, src string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.OpenFile(dev, os.O_WRONLY, 0)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		return err
	}
	if err := out.Sync(); err != nil {
		out.Close()
		return err
	}
	return out.Close()
}

// allocThreadID returns a thin dev id for a new thread, reusing a freed id when
// one is available. The caller holds the lock.
func (p *DevmapperProvisioner) allocThreadID() int {
	if n := len(p.state.FreeThread); n > 0 {
		id := p.state.FreeThread[n-1]
		p.state.FreeThread = p.state.FreeThread[:n-1]
		return id
	}
	id := p.state.NextThread
	p.state.NextThread++
	return id
}

// Provision creates a thin-snapshot of the base for the thread and activates it,
// returning the /dev/mapper path firecracker uses as the rootfs drive.
func (p *DevmapperProvisioner) Provision(ctx context.Context, threadID, _ string) (string, error) {
	if p.Base == "" {
		return "", fmt.Errorf("driver: DevmapperProvisioner.Base is empty")
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	if err := p.loadState(); err != nil {
		return "", err
	}
	if err := p.ensureBase(ctx); err != nil {
		return "", err
	}

	name := dmName("fcthr-", threadID)
	// Reuse the existing allocation if the thread is re-provisioned (idempotent).
	if existing, ok := p.state.Active[threadID]; ok {
		if err := dmsetup(ctx, "info", name); err == nil {
			return filepath.Join("/dev/mapper", name), nil
		}
		// id recorded but device gone (restart): rebuild the device from the id.
		if err := p.activateThin(ctx, name, existing, p.state.BaseSectors); err != nil {
			return "", fmt.Errorf("driver: re-activate thread thin dev: %w", err)
		}
		return filepath.Join("/dev/mapper", name), nil
	}

	devID := p.allocThreadID()
	if err := p.poolMessage(ctx, fmt.Sprintf("create_snap %d %d", devID, p.state.BaseDevID)); err != nil {
		p.state.FreeThread = append(p.state.FreeThread, devID)
		return "", fmt.Errorf("driver: create thread snapshot: %w", err)
	}
	if err := p.activateThin(ctx, name, devID, p.state.BaseSectors); err != nil {
		_ = p.poolMessage(ctx, fmt.Sprintf("delete %d", devID))
		p.state.FreeThread = append(p.state.FreeThread, devID)
		return "", fmt.Errorf("driver: activate thread thin dev: %w", err)
	}
	p.state.Active[threadID] = devID
	if err := p.saveState(); err != nil {
		return "", err
	}
	return filepath.Join("/dev/mapper", name), nil
}

// Teardown deactivates the thread's CoW device and frees its thin dev id + pool
// space. Idempotent: a thread with no recorded device is a no-op.
func (p *DevmapperProvisioner) Teardown(ctx context.Context, threadID string) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	if err := p.loadState(); err != nil {
		return err
	}
	devID, ok := p.state.Active[threadID]
	if !ok {
		return nil
	}
	if err := p.removeDev(ctx, dmName("fcthr-", threadID)); err != nil {
		return fmt.Errorf("driver: remove thread thin dev: %w", err)
	}
	if err := p.poolMessage(ctx, fmt.Sprintf("delete %d", devID)); err != nil {
		return fmt.Errorf("driver: delete thread thin dev id: %w", err)
	}
	delete(p.state.Active, threadID)
	p.state.FreeThread = append(p.state.FreeThread, devID)
	return p.saveState()
}
