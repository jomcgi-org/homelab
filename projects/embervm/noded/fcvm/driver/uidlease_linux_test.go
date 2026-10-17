//go:build linux

package driver

import "testing"

// TestLeaseUIDBlockDisjointAcrossHolders pins J6's uid half: two daemons that
// share a node get disjoint uid blocks while both hold their lease. flock binds
// to the open file description, so two leases in one process conflict exactly
// like two bricks.
func TestLeaseUIDBlockDisjointAcrossHolders(t *testing.T) {
	dir := t.TempDir()
	a, err := leaseUIDBlock(dir, 0)
	if err != nil {
		t.Fatal(err)
	}
	b, err := leaseUIDBlock(dir, 0)
	if err != nil {
		t.Fatal(err)
	}
	if a.base != vmUIDBase || b.base != vmUIDBase+vmUIDBlockSize {
		t.Fatalf("lease bases = %d, %d; want %d, %d", a.base, b.base, vmUIDBase, vmUIDBase+vmUIDBlockSize)
	}
	if err := a.release(); err != nil {
		t.Fatal(err)
	}
	c, err := leaseUIDBlock(dir, 0)
	if err != nil {
		t.Fatal(err)
	}
	if c.base != vmUIDBase {
		t.Fatalf("a released block was not reclaimed: base %d", c.base)
	}
	held := []*uidBlockLease{b, c}
	t.Cleanup(func() {
		for _, lease := range held {
			_ = lease.release()
		}
	})
	for len(held) < vmUIDCount/vmUIDBlockSize {
		lease, err := leaseUIDBlock(dir, 0)
		if err != nil {
			t.Fatalf("lease %d: %v", len(held), err)
		}
		held = append(held, lease)
	}
	last := held[len(held)-1].base
	if last != vmUIDBase+vmUIDCount-vmUIDBlockSize {
		t.Fatalf("last block base = %d, want %d", last, vmUIDBase+vmUIDCount-vmUIDBlockSize)
	}
	if _, err := leaseUIDBlock(dir, 0); err == nil {
		t.Fatal("a lease succeeded with every block held")
	}
	if _, err := leaseUIDBlock("", 0); err == nil {
		t.Fatal("a lease succeeded without a lease directory")
	}
}

// TestLeaseUIDBlockHonoursBase: two releases on one node lease under different
// snapshot roots, so their flocks never meet. Leasing from disjoint bases keeps
// their uids apart anyway: production from block 0 and embervm-dev from block
// 20 each take their own first block, a release never takes a block below its
// base even when those are free, and a base outside the node's blocks fails
// closed.
func TestLeaseUIDBlockHonoursBase(t *testing.T) {
	prodDir, devDir := t.TempDir(), t.TempDir()
	prod, err := leaseUIDBlock(prodDir, 0)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = prod.release() })
	dev, err := leaseUIDBlock(devDir, 20)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = dev.release() })
	if prod.base != vmUIDBase || dev.base != vmUIDBase+20*vmUIDBlockSize {
		t.Fatalf("lease bases = prod %d, dev %d; want %d and %d", prod.base, dev.base, vmUIDBase, vmUIDBase+20*vmUIDBlockSize)
	}
	held := []*uidBlockLease{}
	t.Cleanup(func() {
		for _, lease := range held {
			_ = lease.release()
		}
	})
	for range JailUIDBlocks - 21 {
		lease, err := leaseUIDBlock(devDir, 20)
		if err != nil {
			t.Fatalf("dev lease %d: %v", len(held)+1, err)
		}
		if lease.base < vmUIDBase+20*vmUIDBlockSize {
			t.Fatalf("dev leased block base %d below its base block 20", lease.base)
		}
		held = append(held, lease)
	}
	if _, err := leaseUIDBlock(devDir, 20); err == nil {
		t.Fatal("dev leased a block below its base once blocks 20-39 were held")
	}
	for _, base := range []int{-1, JailUIDBlocks} {
		if _, err := leaseUIDBlock(t.TempDir(), base); err == nil {
			t.Fatalf("a lease from base %d succeeded", base)
		}
	}
}

// withProductionUIDState swaps in a fresh uid pool for one test.
func withProductionUIDState(t *testing.T) {
	t.Helper()
	productionLaunchState.Lock()
	savedNext := productionLaunchState.nextUID
	savedUsed := productionLaunchState.usedUIDs
	savedBlock := productionLaunchState.uidBlock
	productionLaunchState.nextUID = 0
	productionLaunchState.usedUIDs = make(map[int]bool)
	productionLaunchState.uidBlock = nil
	productionLaunchState.Unlock()
	t.Cleanup(func() {
		productionLaunchState.Lock()
		_ = productionLaunchState.uidBlock.release()
		productionLaunchState.nextUID = savedNext
		productionLaunchState.usedUIDs = savedUsed
		productionLaunchState.uidBlock = savedBlock
		productionLaunchState.Unlock()
	})
}

func TestAllocateUIDStaysInLeasedBlock(t *testing.T) {
	withProductionUIDState(t)
	dir := t.TempDir()
	// A sibling daemon already holds the first block.
	sibling, err := leaseUIDBlock(dir, 0)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = sibling.release() })

	launcher := &ExecLauncher{UIDLeaseDir: dir}
	base := vmUIDBase + vmUIDBlockSize
	releases := make([]func(), 0, vmUIDBlockSize)
	for i := range vmUIDBlockSize {
		uid, release, err := launcher.allocateUID()
		if err != nil {
			t.Fatalf("allocation %d: %v", i, err)
		}
		if uid < base || uid >= base+vmUIDBlockSize {
			t.Fatalf("uid %d outside the leased block [%d, %d)", uid, base, base+vmUIDBlockSize)
		}
		releases = append(releases, release)
	}
	if _, _, err := launcher.allocateUID(); err == nil {
		t.Fatal("allocateUID succeeded with the whole leased block in use")
	}
	releases[7]()
	uid, _, err := launcher.allocateUID()
	if err != nil || uid != base+7 {
		t.Fatalf("after one release allocateUID = %d, %v; want %d", uid, err, base+7)
	}
}

// TestAllocateUIDLeasesFromItsBlockBase: a launcher configured with a uid
// block base hands out uids only from its own release's range.
func TestAllocateUIDLeasesFromItsBlockBase(t *testing.T) {
	withProductionUIDState(t)
	launcher := &ExecLauncher{UIDLeaseDir: t.TempDir(), UIDBlockBase: 20}
	uid, _, err := launcher.allocateUID()
	if err != nil {
		t.Fatal(err)
	}
	if base := vmUIDBase + 20*vmUIDBlockSize; uid < base || uid >= base+vmUIDBlockSize {
		t.Fatalf("uid %d outside block 20 [%d, %d)", uid, base, base+vmUIDBlockSize)
	}
}

func TestAllocateUIDFailsClosedWithoutLease(t *testing.T) {
	withProductionUIDState(t)
	if _, _, err := (&ExecLauncher{}).allocateUID(); err == nil {
		t.Fatal("allocateUID handed out a uid without a node-wide lease")
	}
}
