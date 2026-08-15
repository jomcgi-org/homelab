//go:build !linux

package sparse

// DigHoles is unavailable off Linux. The caller treats hole punching as
// best-effort, so retaining fully allocated files is safe.
func DigHoles(string) (int64, error) { return 0, nil }
