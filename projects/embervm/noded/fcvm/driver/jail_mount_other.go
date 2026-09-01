//go:build !linux

package driver

import "fmt"

func bindMountFile(_, _ string, _ bool) error { return fmt.Errorf("bind mounts require Linux") }
func unmountFile(_ string) error              { return nil }
