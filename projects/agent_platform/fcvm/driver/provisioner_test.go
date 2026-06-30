package driver

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"

	"github.com/jomcgi/homelab/projects/agent_platform/substrate"
)

func TestCopyProvisionerCopiesBase(t *testing.T) {
	base := filepath.Join(shortTempDir(t), "base.ext4")
	if err := os.WriteFile(base, []byte("ROOTFS-BYTES"), 0o600); err != nil {
		t.Fatalf("write base: %v", err)
	}
	dir := shortTempDir(t)

	p := &CopyProvisioner{Base: base}
	got, err := p.Provision(context.Background(), "t1", dir)
	if err != nil {
		t.Fatalf("Provision: %v", err)
	}
	if got != filepath.Join(dir, "rootfs.ext4") {
		t.Fatalf("rootfs path = %q", got)
	}
	b, err := os.ReadFile(got)
	if err != nil {
		t.Fatalf("read provisioned: %v", err)
	}
	if string(b) != "ROOTFS-BYTES" {
		t.Fatalf("provisioned content = %q, want the base bytes", b)
	}
}

func TestCopyProvisionerEmptyBaseErrors(t *testing.T) {
	if _, err := (&CopyProvisioner{}).Provision(context.Background(), "t1", t.TempDir()); err == nil {
		t.Fatal("empty Base should error")
	}
}

// fakeProvisioner records calls and creates a stub rootfs file.
type fakeProvisioner struct {
	calls    atomic.Int32
	threadID string
	tornDown string
	fail     error
}

func (f *fakeProvisioner) Provision(_ context.Context, threadID, dir string) (string, error) {
	f.calls.Add(1)
	f.threadID = threadID
	if f.fail != nil {
		return "", f.fail
	}
	path := filepath.Join(dir, "rootfs.ext4")
	_ = os.WriteFile(path, []byte("stub"), 0o600)
	return path, nil
}

func (f *fakeProvisioner) Teardown(_ context.Context, threadID string) error {
	f.tornDown = threadID
	return nil
}

func TestDriverClaimProvisionsPerThreadRootfs(t *testing.T) {
	d := testDriver(t)
	fp := &fakeProvisioner{}
	d.SetProvisioner(fp)

	h, err := d.Claim(context.Background(), substrate.ClaimSpec{ThreadID: "t-prov"})
	if err != nil {
		t.Fatalf("Claim: %v", err)
	}
	if fp.calls.Load() != 1 {
		t.Fatalf("provisioner called %d times, want 1", fp.calls.Load())
	}
	if fp.threadID != "t-prov" {
		t.Fatalf("provisioned for %q, want t-prov", fp.threadID)
	}
	if h.ThreadID != "t-prov" {
		t.Fatalf("handle thread = %q", h.ThreadID)
	}
}

func TestDriverClaimProvisionFailureAborts(t *testing.T) {
	d := testDriver(t)
	d.SetProvisioner(&fakeProvisioner{fail: errors.New("no space")})
	if _, err := d.Claim(context.Background(), substrate.ClaimSpec{ThreadID: "t1"}); err == nil {
		t.Fatal("Claim should fail when rootfs provisioning fails")
	}
	if d.LiveCount() != 0 {
		t.Fatalf("a failed provision should not leave a live VM; LiveCount=%d", d.LiveCount())
	}
}

func TestDevmapperAllocReusesFreedThinIDs(t *testing.T) {
	p := &DevmapperProvisioner{StateDir: shortTempDir(t)}
	if err := p.loadState(); err != nil {
		t.Fatalf("loadState: %v", err)
	}
	a := p.allocThreadID()
	b := p.allocThreadID()
	if a != threadDevIDStart || b != threadDevIDStart+1 {
		t.Fatalf("alloc = %d,%d; want %d,%d", a, b, threadDevIDStart, threadDevIDStart+1)
	}
	// Free a, then the next alloc must reuse it rather than grow the high-water.
	p.state.FreeThread = append(p.state.FreeThread, a)
	if reused := p.allocThreadID(); reused != a {
		t.Fatalf("alloc after free = %d, want reused %d", reused, a)
	}
}

func TestDevmapperStateRoundTrips(t *testing.T) {
	dir := shortTempDir(t)
	p := &DevmapperProvisioner{StateDir: dir}
	if err := p.loadState(); err != nil {
		t.Fatalf("loadState: %v", err)
	}
	p.state.Active["t-1"] = p.allocThreadID()
	p.state.BaseLoaded = true
	p.state.BaseSig = "sig-1"
	if err := p.saveState(); err != nil {
		t.Fatalf("saveState: %v", err)
	}
	// A fresh provisioner over the same dir recovers the allocation table.
	q := &DevmapperProvisioner{StateDir: dir}
	if err := q.loadState(); err != nil {
		t.Fatalf("reload: %v", err)
	}
	if q.state.Active["t-1"] != threadDevIDStart || !q.state.BaseLoaded || q.state.BaseSig != "sig-1" {
		t.Fatalf("recovered state = %+v", q.state)
	}
}

func TestDMNameSanitizes(t *testing.T) {
	if got := dmName("fcthr-", "t-ab/cd:12"); got != "fcthr-t-ab_cd_12" {
		t.Fatalf("dmName = %q", got)
	}
}

func TestBootArgsAppendsInit(t *testing.T) {
	d := New(Config{
		KernelBootArgs: "console=ttyS0",
		HarnessInit:    "/usr/local/bin/fc-agent-init",
		SnapshotRoot:   shortTempDir(t),
		Node:           "node-4", Arch: "amd64",
	}, &fakeLauncher{}, nil)
	if got := d.bootArgs(); got != "console=ttyS0 init=/usr/local/bin/fc-agent-init" {
		t.Fatalf("bootArgs = %q", got)
	}
}
