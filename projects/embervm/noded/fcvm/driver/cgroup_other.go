//go:build !linux

package driver

import "fmt"

type (
	cgroupManager struct{}
	vmCgroup      struct{}
)

func newCgroupManager() *cgroupManager {
	return &cgroupManager{}
}

func (m *cgroupManager) prepare() error {
	return fmt.Errorf("cgroup v2 jailer launch requires Linux")
}

func (m *cgroupManager) Create(_ string, _ int64, _ int) (*vmCgroup, error) {
	return nil, fmt.Errorf("cgroup v2 jailer launch requires Linux")
}

func (c *vmCgroup) ParentArg() string { return "" }
func (c *vmCgroup) Path() string      { return "" }
func (c *vmCgroup) contains(_ int) (bool, error) {
	return false, fmt.Errorf("cgroup membership requires Linux")
}
func (c *vmCgroup) OOMKilled() (bool, error) { return false, nil }
func (c *vmCgroup) Remove() error            { return nil }
