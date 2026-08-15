package sparse

import "log/slog"

// BestEffort punches holes in zero-filled regions without making a failure
// fatal to the operation that produced the file.
func BestEffort(path, kind string) {
	freed, err := DigHoles(path)
	if err != nil {
		slog.Default().Warn("sparse: punch holes failed", "kind", kind, "path", path, "error", err)
		return
	}
	slog.Default().Info("sparse: punched holes", "kind", kind, "path", path, "bytes_freed", freed)
}
