//go:build linux

package driver

import (
	"bufio"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
)

const cgroupVMParent = "embervm-vms"

// procMountsPath and procSelfCgroupPath are the /proc files initialize reads to
// find the cgroup v2 mount and noded's own cgroup. Package vars so tests can
// point them at fixtures, mirroring the budget reader's seams in server.
var (
	procMountsPath     = "/proc/mounts"
	procSelfCgroupPath = "/proc/self/cgroup"
)

// removeLeafCgroup removes one stale VM leaf during the startup sweep. A real
// cgroupfs directory holds only virtual control files, so rmdir is enough; the
// sweep tests swap this for a plain-filesystem remover.
var removeLeafCgroup = func(dir string) error { return (&vmCgroup{dir: dir}).Remove() }

// cgroupManager prepares delegation below noded's own container cgroup. A
// container cgroup initially contains noded, so the cgroup v2 no-internal-
// process rule prevents the jailer from enabling memory directly there. We move
// noded into a daemon-only child once, enable memory on the now-empty parent,
// and create VM leaves as siblings of that daemon child. Every leaf therefore
// remains below the kubelet-owned pod/container hierarchy.
//
// The shared embervm-vms parent is capped at the container's memory.max minus
// the daemon reserve, with memory.oom.group left 0. An aggregate guest overrun
// then OOMs inside embervm-vms, where the kernel group-kills only the chosen VM
// leaf (each leaf has memory.oom.group=1), never the kubelet container and with
// it noded and every other guest.
type cgroupManager struct {
	mu           sync.Mutex
	mount        string
	brickDir     string
	parentDir    string
	parentArg    string
	initializeFn func() error
}

type vmCgroup struct {
	dir          string
	parentArg    string
	oomKillStart uint64
}

func newCgroupManager() *cgroupManager {
	return &cgroupManager{}
}

// ensureInitializedLocked delegates once. A failure is never memoized, so a
// repaired brick recovers on its next attempt without a daemon redeploy.
func (m *cgroupManager) ensureInitializedLocked() error {
	if m.parentDir != "" {
		return nil
	}
	initialize := m.initialize
	if m.initializeFn != nil {
		initialize = m.initializeFn
	}
	return initialize()
}

// prepare delegates eagerly at daemon start, before anything forks, so a brick
// that cannot contain guests fails startup rather than its first launch, and
// the process move cannot race a short-lived child. It then removes VM leaves
// that hold no process, left behind in this container cgroup by an earlier
// daemon; at startup this daemon owns no VM, and a leaf with a live VMM is
// never empty.
func (m *cgroupManager) prepare() error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if err := m.ensureInitializedLocked(); err != nil {
		return err
	}
	removed, err := sweepEmptyLeafCgroups(m.parentDir)
	for _, dir := range removed {
		slog.Info("driver: removed orphaned VM cgroup", "cgroup", dir)
	}
	if err != nil {
		slog.Warn("driver: sweep orphaned VM cgroups", "parent", m.parentDir, "err", err)
	}
	return nil
}

// Create makes one VM leaf below the shared parent. The parent's cap is
// re-derived from the brick's current memory.max on every launch, so an
// in-place pod resize is honoured by the next guest without a daemon restart.
func (m *cgroupManager) Create(vmID string, limitBytes int64, daemonReserveMib int) (*vmCgroup, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if err := m.ensureInitializedLocked(); err != nil {
		return nil, err
	}
	if m.brickDir != "" {
		if err := capVMParent(m.brickDir, m.parentDir, daemonReserveMib); err != nil {
			return nil, err
		}
	}
	dir := filepath.Join(m.parentDir, vmID)
	if err := os.Mkdir(dir, 0o755); err != nil && !os.IsExist(err) {
		return nil, fmt.Errorf("mkdir %q: %w", dir, err)
	}
	cleanup := func(err error) (*vmCgroup, error) {
		_ = os.Remove(dir)
		return nil, err
	}
	if err := writeCgroupFile(filepath.Join(dir, "memory.max"), strconv.FormatInt(limitBytes, 10)); err != nil {
		return cleanup(fmt.Errorf("write memory.max: %w", err))
	}
	if err := writeCgroupFile(filepath.Join(dir, "memory.oom.group"), "1"); err != nil {
		return cleanup(fmt.Errorf("write memory.oom.group: %w", err))
	}
	start, err := readOOMKillCount(filepath.Join(dir, "memory.events"))
	if err != nil {
		return cleanup(fmt.Errorf("read memory.events baseline: %w", err))
	}
	return &vmCgroup{
		dir:          dir,
		parentArg:    filepath.ToSlash(filepath.Join(m.parentArg, vmID)),
		oomKillStart: start,
	}, nil
}

func (m *cgroupManager) initialize() error {
	mount, err := cgroup2Mount()
	if err != nil {
		return err
	}
	rel, err := selfCgroupPath()
	if err != nil {
		return err
	}
	// After a partial delegation (the move succeeded but a later step failed)
	// noded already sits in the daemon child. Resolving the brick cgroup keeps a
	// retry on the same container instead of nesting a second daemon/VM pair.
	rel = BrickCgroupPath(rel)
	current := filepath.Join(mount, strings.TrimPrefix(filepath.Clean(rel), string(filepath.Separator)))
	daemon := filepath.Join(current, DaemonCgroupName)
	if err := os.Mkdir(daemon, 0o755); err != nil && !os.IsExist(err) {
		return fmt.Errorf("mkdir daemon cgroup: %w", err)
	}
	if err := moveCgroupProcesses(filepath.Join(current, "cgroup.procs"), filepath.Join(daemon, "cgroup.procs")); err != nil {
		return fmt.Errorf("move noded processes to daemon cgroup: %w", err)
	}
	if err := writeCgroupFile(filepath.Join(current, "cgroup.subtree_control"), "+memory"); err != nil {
		return fmt.Errorf("delegate memory controller: %w", err)
	}
	parentDir := filepath.Join(current, cgroupVMParent)
	if err := os.Mkdir(parentDir, 0o755); err != nil && !os.IsExist(err) {
		return fmt.Errorf("mkdir VM cgroup parent: %w", err)
	}
	if err := writeCgroupFile(filepath.Join(parentDir, "cgroup.subtree_control"), "+memory"); err != nil {
		return fmt.Errorf("delegate memory controller to VM leaves: %w", err)
	}
	// An OOM inside the shared parent must kill one VM leaf, never every guest.
	if err := writeCgroupFile(filepath.Join(parentDir, "memory.oom.group"), "0"); err != nil {
		return fmt.Errorf("write VM cgroup parent memory.oom.group: %w", err)
	}
	relCurrent, err := filepath.Rel(mount, current)
	if err != nil {
		return fmt.Errorf("derive cgroup parent: %w", err)
	}
	if relCurrent == "." {
		relCurrent = ""
	}
	m.mount = mount
	m.brickDir = current
	m.parentDir = parentDir
	m.parentArg = filepath.ToSlash(filepath.Join(relCurrent, cgroupVMParent))
	return nil
}

// capVMParent writes the shared VM parent's memory.max from the brick's own.
func capVMParent(brickDir, parentDir string, daemonReserveMib int) error {
	raw, err := os.ReadFile(filepath.Join(brickDir, "memory.max"))
	if err != nil {
		return fmt.Errorf("read brick memory.max: %w", err)
	}
	limit, err := vmParentMemoryMax(string(raw), daemonReserveMib)
	if err != nil {
		return err
	}
	if err := writeCgroupFile(filepath.Join(parentDir, "memory.max"), limit); err != nil {
		return fmt.Errorf("cap VM cgroup parent memory.max: %w", err)
	}
	return nil
}

// vmParentMemoryMax derives the embervm-vms memory.max from the brick's: the
// brick ceiling minus the daemon reserve, so guests in aggregate can never push
// the container itself over its limit. An unlimited brick stays unlimited. A
// reserve that leaves no room for any guest is an error, never a silent "max".
func vmParentMemoryMax(brickMax string, daemonReserveMib int) (string, error) {
	value := strings.TrimSpace(brickMax)
	if value == "max" {
		return "max", nil
	}
	limit, err := strconv.ParseInt(value, 10, 64)
	if err != nil || limit <= 0 {
		return "", fmt.Errorf("parse brick memory.max %q", value)
	}
	reserve := int64(max(daemonReserveMib, 0)) << 20
	if limit <= reserve {
		return "", fmt.Errorf("brick memory.max %d leaves no room for VMs after the %d MiB daemon reserve", limit, daemonReserveMib)
	}
	return strconv.FormatInt(limit-reserve, 10), nil
}

// sweepEmptyLeafCgroups removes VM leaves under parentDir whose whole subtree
// holds no process. A live VM always has its Firecracker process in its leaf,
// and rmdir on a populated cgroup fails with EBUSY anyway, so the sweep can
// never strand a running VMM outside its limit.
func sweepEmptyLeafCgroups(parentDir string) ([]string, error) {
	entries, err := os.ReadDir(parentDir)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}
	var (
		removed  []string
		sweepErr error
	)
	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		dir := filepath.Join(parentDir, entry.Name())
		empty, err := cgroupTreeEmpty(dir)
		if err != nil {
			sweepErr = errors.Join(sweepErr, fmt.Errorf("inspect %q: %w", dir, err))
			continue
		}
		if !empty {
			continue
		}
		if err := removeLeafCgroup(dir); err != nil {
			sweepErr = errors.Join(sweepErr, fmt.Errorf("remove %q: %w", dir, err))
			continue
		}
		removed = append(removed, dir)
	}
	return removed, sweepErr
}

// cgroupTreeEmpty reports whether no cgroup in the tree rooted at dir has a
// member process. An unreadable cgroup.procs is not evidence of emptiness.
func cgroupTreeEmpty(dir string) (bool, error) {
	empty := true
	err := filepath.WalkDir(dir, func(path string, entry os.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if !entry.IsDir() {
			return nil
		}
		b, err := os.ReadFile(filepath.Join(path, "cgroup.procs"))
		if err != nil {
			return err
		}
		if strings.TrimSpace(string(b)) != "" {
			empty = false
			return filepath.SkipAll
		}
		return nil
	})
	return empty, err
}

func cgroup2Mount() (string, error) {
	f, err := os.Open(procMountsPath)
	if err != nil {
		return "", err
	}
	defer f.Close()
	s := bufio.NewScanner(f)
	for s.Scan() {
		fields := strings.Fields(s.Text())
		if len(fields) >= 3 && fields[2] == "cgroup2" {
			return fields[1], nil
		}
	}
	if err := s.Err(); err != nil {
		return "", err
	}
	return "", fmt.Errorf("cgroup v2 mount not found")
}

func selfCgroupPath() (string, error) {
	f, err := os.Open(procSelfCgroupPath)
	if err != nil {
		return "", err
	}
	defer f.Close()
	s := bufio.NewScanner(f)
	for s.Scan() {
		parts := strings.SplitN(s.Text(), ":", 3)
		if len(parts) == 3 && parts[0] == "0" && parts[1] == "" {
			return parts[2], nil
		}
	}
	if err := s.Err(); err != nil {
		return "", err
	}
	return "", fmt.Errorf("unified cgroup path not found")
}

func writeCgroupFile(path, value string) error {
	return os.WriteFile(path, []byte(value), 0o644)
}

func moveCgroupProcesses(source, target string) error {
	b, err := os.ReadFile(source)
	if err != nil {
		return err
	}
	for _, pid := range strings.Fields(string(b)) {
		if err := writeCgroupFile(target, pid); err != nil {
			return fmt.Errorf("move pid %s: %w", pid, err)
		}
	}
	return nil
}

func readOOMKillCount(path string) (uint64, error) {
	f, err := os.Open(path)
	if err != nil {
		return 0, err
	}
	defer f.Close()
	s := bufio.NewScanner(f)
	for s.Scan() {
		fields := strings.Fields(s.Text())
		if len(fields) != 2 || fields[0] != "oom_kill" {
			continue
		}
		return strconv.ParseUint(fields[1], 10, 64)
	}
	if err := s.Err(); err != nil {
		return 0, err
	}
	return 0, fmt.Errorf("memory.events has no oom_kill counter")
}

func (c *vmCgroup) ParentArg() string { return c.parentArg }
func (c *vmCgroup) Path() string      { return c.dir }

// contains reports whether pid is a member of this leaf, read from its
// cgroup.procs. The kernel lists members in the reader's PID namespace, so
// noded compares the Firecracker pid it sees even when the VMM is init of its
// own namespace.
func (c *vmCgroup) contains(pid int) (bool, error) {
	b, err := os.ReadFile(filepath.Join(c.dir, "cgroup.procs"))
	if err != nil {
		return false, err
	}
	want := strconv.Itoa(pid)
	for _, member := range strings.Fields(string(b)) {
		if member == want {
			return true, nil
		}
	}
	return false, nil
}

func (c *vmCgroup) OOMKilled() (bool, error) {
	current, err := readOOMKillCount(filepath.Join(c.dir, "memory.events"))
	return current > c.oomKillStart, err
}

func (c *vmCgroup) Remove() error {
	var dirs []string
	err := filepath.WalkDir(c.dir, func(path string, entry os.DirEntry, walkErr error) error {
		if walkErr != nil {
			if os.IsNotExist(walkErr) {
				return nil
			}
			return walkErr
		}
		if entry.IsDir() {
			dirs = append(dirs, path)
		}
		return nil
	})
	if err != nil {
		return err
	}
	sort.Slice(dirs, func(i, j int) bool { return len(dirs[i]) > len(dirs[j]) })
	for _, dir := range dirs {
		if err := os.Remove(dir); err != nil && !os.IsNotExist(err) {
			return err
		}
	}
	return nil
}
