//go:build !linux

package driver

import "errors"

type uidBlockLease struct {
	base int
}

func leaseUIDBlock(string, int) (*uidBlockLease, error) {
	return nil, errors.New("driver: jailer uid lease requires Linux")
}

func (l *uidBlockLease) release() error { return nil }
