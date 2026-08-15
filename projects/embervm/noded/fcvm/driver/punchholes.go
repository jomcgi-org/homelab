package driver

import "log/slog"

func bestEffortDigHoles(path, kind string) {
	freed, err := digHoles(path)
	if err != nil {
		slog.Default().Warn("driver: punch memfile holes failed", "kind", kind, "path", path, "error", err)
		return
	}
	slog.Default().Info("driver: punched memfile holes", "kind", kind, "path", path, "bytes_freed", freed)
}
