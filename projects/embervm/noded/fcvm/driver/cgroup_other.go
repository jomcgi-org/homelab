//go:build !linux

package driver

import (
	"fmt"
	"sync"
)

type (
	cgroupManager struct {
		mu       sync.Mutex
		failures map[string]struct{}
	}
	vmCgroup struct{}
)

func newCgroupManager() *cgroupManager {
	return &cgroupManager{failures: make(map[string]struct{})}
}

func (m *cgroupManager) Create(_ string, _ int64) (*vmCgroup, error) {
	return nil, fmt.Errorf("cgroup v2 jailer launch requires Linux")
}

func (m *cgroupManager) shouldLogFailure(err error) bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	cause := err.Error()
	if m.failures == nil {
		m.failures = make(map[string]struct{})
	}
	if _, ok := m.failures[cause]; ok {
		return false
	}
	m.failures[cause] = struct{}{}
	return true
}

func (c *vmCgroup) ParentArg() string        { return "" }
func (c *vmCgroup) Path() string             { return "" }
func (c *vmCgroup) OOMKilled() (bool, error) { return false, nil }
func (c *vmCgroup) Remove() error            { return nil }
