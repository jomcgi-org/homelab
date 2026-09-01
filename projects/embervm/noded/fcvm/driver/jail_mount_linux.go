//go:build linux

package driver

import (
	"os"
	"syscall"
)

func bindMountFile(source, target string, readOnly bool) error {
	f, err := os.OpenFile(target, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	if err := syscall.Mount(source, target, "", syscall.MS_BIND, ""); err != nil {
		return err
	}
	if readOnly {
		if err := syscall.Mount("", target, "", syscall.MS_BIND|syscall.MS_REMOUNT|syscall.MS_RDONLY, ""); err != nil {
			_ = syscall.Unmount(target, syscall.MNT_DETACH)
			return err
		}
	}
	return nil
}

func unmountFile(target string) error {
	return syscall.Unmount(target, syscall.MNT_DETACH)
}
