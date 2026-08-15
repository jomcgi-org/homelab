//go:build !linux

package driver

// digHoles is unavailable off Linux. The caller treats hole punching as
// best-effort, so retaining fully allocated files is safe.
func digHoles(string) (int64, error) { return 0, nil }
