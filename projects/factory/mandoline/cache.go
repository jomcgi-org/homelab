package mandoline

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"maps"
	"os"
	"path/filepath"
	"strconv"
	"syscall"
)

type cache struct{ directory string }

func openCache(workspace string) (cache, error) {
	parent, err := filepath.EvalSymlinks(os.TempDir())
	if err != nil {
		return cache{}, err
	}
	dir := filepath.Join(parent, "factory-mandoline-"+strconv.Itoa(os.Getuid()))
	if beneath(workspace, dir) {
		return cache{}, errors.New("cache must be outside the task workspace")
	}
	if err := os.Mkdir(dir, 0o700); err != nil && !errors.Is(err, os.ErrExist) {
		return cache{}, err
	}
	info, err := os.Lstat(dir)
	if err != nil {
		return cache{}, err
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || !info.IsDir() || info.Mode().Perm()&0o077 != 0 || int(stat.Uid) != os.Getuid() {
		return cache{}, errors.New("cache must be private")
	}
	return cache{dir}, nil
}

func cacheKey(tree, packs string, req Request) string {
	// The tree covers tracked analysis configuration and ignore files.
	// Project pack installs and ambient tool configuration are excluded.
	input, _ := json.Marshal([]any{1, tree, packs, req})
	digest := sha256.Sum256(input)
	return hex.EncodeToString(digest[:])
}

func (c cache) read(key string, expected Envelope) (Envelope, bool) {
	f, err := os.OpenFile(filepath.Join(c.directory, key), os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
	if err != nil {
		return Envelope{}, false
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil || !info.Mode().IsRegular() || info.Size() > outputLimit+4096 {
		return Envelope{}, false
	}
	b, err := io.ReadAll(io.LimitReader(f, outputLimit+4096))
	if err != nil {
		return Envelope{}, false
	}
	var got Envelope
	valid := json.Unmarshal(b, &got) == nil &&
		got.Status == "complete" && got.Gaps != nil && len(got.Gaps) == 0 &&
		got.Commit == expected.Commit && got.ToolVersion == expected.ToolVersion &&
		maps.Equal(got.PackDigests, expected.PackDigests) && len(got.Result) > 0 &&
		string(got.Result) != "null"
	return got, valid
}

func (c cache) store(key string, result Envelope) {
	f, err := os.CreateTemp(c.directory, "result-")
	if err != nil {
		return
	}
	defer os.Remove(f.Name())
	writeErr := json.NewEncoder(f).Encode(result)
	if f.Close() == nil && writeErr == nil {
		_ = os.Rename(f.Name(), filepath.Join(c.directory, key))
	}
}
