package server

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/jomcgi/homelab/projects/embervm/noded/fcvm/driver"
)

// writeCgroupFixture writes cgroup v2 control files into dir.
func writeCgroupFixture(t *testing.T, dir string, files map[string]string) {
	t.Helper()
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	for name, content := range files {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(content), 0o644); err != nil {
			t.Fatalf("write fixture %s: %v", name, err)
		}
	}
}

// TestCgroupDirSurvivesJailerDelegation pins J3: once the jailer delegates,
// /proc/self/cgroup names noded's daemon child, whose memory.max is "max". The
// reader must keep reading the brick cgroup, or the budget reads 0 and a classed
// brick turns into an unbounded wildcard.
func TestCgroupDirSurvivesJailerDelegation(t *testing.T) {
	root := t.TempDir()
	orig := budgetFsRoot
	budgetFsRoot = root
	t.Cleanup(func() { budgetFsRoot = orig })

	scope := filepath.Join(root, "kubepods.slice", "cri-containerd-abc.scope")
	writeCgroupFixture(t, scope, map[string]string{
		"memory.max":     "4294967296\n", // 4 GiB
		"memory.current": "1073741824\n", // 1 GiB, hierarchical over daemon and VMs
		"memory.stat":    "file 0\nshmem 0\n",
		"cpu.max":        "200000 100000\n",
		"cpu.stat":       "usage_usec 1000\n",
	})
	writeCgroupFixture(t, filepath.Join(scope, driver.DaemonCgroupName), map[string]string{
		"memory.max":     "max\n",
		"memory.current": "104857600\n",
		"cpu.stat":       "usage_usec 10\n",
	})
	withSelfCgroup(t, "0::/kubepods.slice/cri-containerd-abc.scope/"+driver.DaemonCgroupName+"\n")

	if got := cgroupDir(); got != scope {
		t.Fatalf("cgroupDir() = %q, want the brick scope %q", got, scope)
	}
	b := newBudget(256)
	if got, want := b.MemBudgetMib(), uint64(4096-256); got != want {
		t.Errorf("MemBudgetMib() after delegation = %d, want %d", got, want)
	}
	if got, want := b.MemHeadroomMib(), uint64(4096-1024); got != want {
		t.Errorf("MemHeadroomMib() after delegation = %d, want %d", got, want)
	}
	if got, want := b.CpuBudgetMillicores(), uint64(2000); got != want {
		t.Errorf("CpuBudgetMillicores() after delegation = %d, want %d", got, want)
	}
}

// TestCgroupDirSurvivesJailerDelegationPrivateNamespace is the private-cgroupns
// shape: the brick is the namespace root and noded moves to /embervm-noded-daemon.
func TestCgroupDirSurvivesJailerDelegationPrivateNamespace(t *testing.T) {
	root := t.TempDir()
	orig := budgetFsRoot
	budgetFsRoot = root
	t.Cleanup(func() { budgetFsRoot = orig })
	writeCgroupFixture(t, root, map[string]string{
		"memory.max":     "4294967296\n",
		"memory.current": "1073741824\n",
		"memory.stat":    "file 0\n",
	})
	writeCgroupFixture(t, filepath.Join(root, driver.DaemonCgroupName), map[string]string{
		"memory.max": "max\n",
	})
	withSelfCgroup(t, "0::/"+driver.DaemonCgroupName+"\n")

	if got := cgroupDir(); got != root {
		t.Fatalf("cgroupDir() = %q, want budgetFsRoot %q", got, root)
	}
	if got, want := newBudget(256).MemBudgetMib(), uint64(4096-256); got != want {
		t.Errorf("MemBudgetMib() private-ns after delegation = %d, want %d", got, want)
	}
}

// TestCgroupDirAfterPartialDelegation covers a move that succeeded before the
// controller write failed: the daemon child has no memory.max at all, and the
// reader must still resolve the brick scope, never the host-root fallback.
func TestCgroupDirAfterPartialDelegation(t *testing.T) {
	root := t.TempDir()
	orig := budgetFsRoot
	budgetFsRoot = root
	t.Cleanup(func() { budgetFsRoot = orig })
	scope := filepath.Join(root, "kubepods.slice", "cri-containerd-abc.scope")
	writeCgroupFixture(t, scope, map[string]string{"memory.max": "2147483648\n"})
	if err := os.MkdirAll(filepath.Join(scope, driver.DaemonCgroupName), 0o755); err != nil {
		t.Fatal(err)
	}
	withSelfCgroup(t, "0::/kubepods.slice/cri-containerd-abc.scope/"+driver.DaemonCgroupName+"\n")

	if got := cgroupDir(); got != scope {
		t.Fatalf("cgroupDir() = %q, want the brick scope %q", got, scope)
	}
}
