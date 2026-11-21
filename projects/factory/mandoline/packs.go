package mandoline

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
)

var errPackInventoryTruncated = errors.New("truncated pack inventory")

// packInventory hashes the pinned CLI's verified catalogs. The executable
// digest is reported separately. This release exposes pack identities and
// descriptors, but does not expose each bundled pack's source bytes.
func packInventory(ctx context.Context, tool, root string, env []string) (map[string]string, error) {
	read := func(target any, args ...string) error {
		output := capture(ctx, tool, root, env, append(args, "--json", "--no-cache"))
		if output.truncated {
			return errPackInventoryTruncated
		}
		if output.err != nil {
			return output.err
		}
		return json.Unmarshal(output.stdout, target)
	}
	var packs struct {
		Command    string            `json:"command"`
		Version    int               `json:"schema_version"`
		Registries []json.RawMessage `json:"registries"`
		Bundled    []struct {
			Name     string `json:"name"`
			Disabled *bool  `json:"disabled"`
		} `json:"bundled"`
	}
	if err := read(&packs, "pack", "list"); err != nil {
		return nil, err
	}
	if packs.Command != "pack list" || packs.Version != 1 || packs.Registries == nil || len(packs.Registries) > 0 {
		return nil, errors.New("unexpected pack catalog")
	}
	active := map[string]bool{}
	for _, p := range packs.Bundled {
		if p.Name == "" || p.Disabled == nil {
			return nil, errors.New("invalid bundled pack descriptor")
		}
		if !*p.Disabled {
			active[p.Name] = true
		}
	}

	var taint struct {
		Libraries []struct {
			Name        string `json:"name"`
			Ecosystem   string `json:"ecosystem"`
			Description string `json:"description"`
			Bundled     bool   `json:"bundled"`
			Propagators int    `json:"propagators"`
			Sources     int    `json:"sources"`
			Sinks       int    `json:"sinks"`
			Sanitizers  int    `json:"sanitizers"`
		} `json:"libraries"`
	}
	if err := read(&taint, "pack", "taint", "list"); err != nil {
		return nil, err
	}
	libraries := map[string]string{}
	for _, lib := range taint.Libraries {
		if lib.Name == "" || !lib.Bundled {
			return nil, errors.New("unexpected taint library descriptor")
		}
		name := lib.Ecosystem + "/" + lib.Name
		if _, found := libraries[name]; found {
			return nil, errors.New("duplicate taint library descriptor")
		}
		libraries[name] = inventoryDigest(lib)
	}
	if len(active) == 0 || len(libraries) == 0 {
		return nil, errors.New("missing bundled pack inventory")
	}
	return map[string]string{
		"bundled_pack_catalog":  inventoryDigest(active),
		"bundled_taint_catalog": inventoryDigest(libraries),
	}, nil
}

func inventoryDigest(value any) string {
	data, _ := json.Marshal(value)
	digest := sha256.Sum256(data)
	return "sha256:" + hex.EncodeToString(digest[:])
}
