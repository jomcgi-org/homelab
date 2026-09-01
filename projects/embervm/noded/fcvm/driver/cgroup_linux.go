//go:build linux

package driver

import (
	"bufio"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
)

const (
	cgroupDaemonName = "embervm-noded-daemon"
	cgroupVMParent   = "embervm-vms"
)

// cgroupManager prepares delegation below noded's own container cgroup. A
// container cgroup initially contains noded, so the cgroup v2 no-internal-
// process rule prevents the jailer from enabling memory directly there. We move
// noded into a daemon-only child once, enable memory on the now-empty parent,
// and create VM leaves as siblings of that daemon child. Every leaf therefore
// remains below the kubelet-owned pod/container hierarchy.
type cgroupManager struct {
	once      sync.Once
	initErr   error
	mount     string
	parentDir string
	parentArg string
}

type vmCgroup struct {
	dir          string
	parentArg    string
	oomKillStart uint64
}

func newCgroupManager() *cgroupManager { return &cgroupManager{} }

func (m *cgroupManager) Create(vmID string, limitBytes int64) (*vmCgroup, error) {
	m.once.Do(func() { m.initErr = m.initialize() })
	if m.initErr != nil {
		return nil, m.initErr
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
	current := filepath.Join(mount, strings.TrimPrefix(filepath.Clean(rel), string(filepath.Separator)))
	daemon := filepath.Join(current, cgroupDaemonName)
	if err := os.Mkdir(daemon, 0o755); err != nil && !os.IsExist(err) {
		return fmt.Errorf("mkdir daemon cgroup: %w", err)
	}
	if err := moveCgroupProcesses(filepath.Join(current, "cgroup.procs"), filepath.Join(daemon, "cgroup.procs")); err != nil {
		return fmt.Errorf("move noded processes to daemon cgroup: %w", err)
	}
	if err := writeCgroupFile(filepath.Join(current, "cgroup.subtree_control"), "+memory"); err != nil {
		return fmt.Errorf("delegate memory controller: %w", err)
	}
	m.parentDir = filepath.Join(current, cgroupVMParent)
	if err := os.Mkdir(m.parentDir, 0o755); err != nil && !os.IsExist(err) {
		return fmt.Errorf("mkdir VM cgroup parent: %w", err)
	}
	if err := writeCgroupFile(filepath.Join(m.parentDir, "cgroup.subtree_control"), "+memory"); err != nil {
		return fmt.Errorf("delegate memory controller to VM leaves: %w", err)
	}
	relCurrent, err := filepath.Rel(mount, current)
	if err != nil {
		return fmt.Errorf("derive cgroup parent: %w", err)
	}
	if relCurrent == "." {
		relCurrent = ""
	}
	m.mount = mount
	m.parentArg = filepath.ToSlash(filepath.Join(relCurrent, cgroupVMParent))
	return nil
}

func cgroup2Mount() (string, error) {
	f, err := os.Open("/proc/mounts")
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
	f, err := os.Open("/proc/self/cgroup")
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

func (c *vmCgroup) OOMKilled() (bool, error) {
	current, err := readOOMKillCount(filepath.Join(c.dir, "memory.events"))
	return current > c.oomKillStart, err
}

func (c *vmCgroup) Remove() error {
	if err := os.Remove(c.dir); err != nil && !os.IsNotExist(err) {
		return err
	}
	return nil
}
