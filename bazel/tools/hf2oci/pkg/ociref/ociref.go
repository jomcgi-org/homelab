// Package ociref provides pure naming functions for deriving OCI references
// from HuggingFace model repositories. It also provides a lightweight ref
// resolver that calls ModelInfo for smart naming without requiring Tree/Classify.
package ociref

import (
	"context"
	"crypto/sha256"
	"fmt"
	"math/big"
	"path"
	"strings"

	"github.com/jomcgi/homelab/bazel/tools/hf2oci/pkg/hf"
)

// DeriveTag returns the OCI tag to use. If tag is non-empty it is returned as-is;
// otherwise it is derived from revision as "rev-{revision[:12]}".
func DeriveTag(tag, revision string) string {
	if tag != "" {
		return tag
	}
	rev := revision
	if len(rev) > 12 {
		rev = rev[:12]
	}
	return "rev-" + rev
}

// DeriveRepoName converts a HuggingFace repo name to an OCI repo path,
// preserving the org/model structure for cleaner registry organization.
// e.g. "NousResearch/Hermes-4.3-Llama-3-36B-AWQ" -> "nousresearch/hermes-4.3-llama-3-36b-awq"
func DeriveRepoName(repo string) string {
	return strings.ToLower(repo)
}

// DeriveVariantTag flattens a HuggingFace repo name into a valid OCI tag.
// Used for derivative models to encode the variant identity in the tag.
// e.g. "Emilio407/nllb-200-distilled-1.3B-4bit" -> "emilio407-nllb-200-distilled-1.3b-4bit"
func DeriveVariantTag(repo string) string {
	return strings.ToLower(strings.ReplaceAll(repo, "/", "-"))
}

// DeriveCompactVariantTag creates a compact OCI tag from structured model info.
// It strips the base model name prefix from the file to avoid redundancy with the
// repo path, producing tags like "bartowski-gguf-q4-k-m" instead of
// "llama-3.2-1b-instruct-q4-k-m".
func DeriveCompactVariantTag(author, format, file, baseModelName string) string {
	// Case-insensitive prefix strip: remove baseModelName from file.
	remainder := file
	lower := strings.ToLower(file)
	prefix := strings.ToLower(baseModelName)
	if strings.HasPrefix(lower, prefix) {
		remainder = file[len(prefix):]
		// Trim leading separators after stripping prefix.
		remainder = strings.TrimLeft(remainder, "-_")
	}

	// Strip known model file extensions. Only strip recognized extensions
	// to avoid eating version numbers that contain dots (e.g. "4.3" in
	// "NousResearch_Hermes-4.3-36B-IQ4_XS").
	for _, ext := range []string{".gguf", ".safetensors", ".bin", ".pt"} {
		if strings.HasSuffix(strings.ToLower(remainder), ext) {
			remainder = remainder[:len(remainder)-len(ext)]
			break
		}
	}

	// Build tag: author-format-remainder, normalize.
	tag := strings.ToLower(author + "-" + format + "-" + remainder)
	tag = strings.ReplaceAll(tag, "_", "-")

	// OCI tag limit is 128 chars.
	if len(tag) > 128 {
		return base36Hash(author + ":" + file)
	}
	return tag
}

// DeriveFileTag creates a compact OCI tag for a file using ModelInfo metadata.
// Falls back gracefully when author or base model info is unavailable.
func DeriveFileTag(info *hf.ModelInfo, format, file string) string {
	// Extract author: prefer info.Author, fallback to splitting info.ID.
	author := ""
	if info != nil {
		author = info.Author
		if author == "" && strings.Contains(info.ID, "/") {
			parts := strings.SplitN(info.ID, "/", 2)
			author = parts[0]
		}
	}

	if author == "" {
		return DeriveVariantTag(file)
	}

	// Extract base model name (part after /).
	baseModelName := ""
	if info.BaseModels != nil && len(info.BaseModels.Models) > 0 {
		baseModelName = path.Base(info.BaseModels.Models[0].ID)
	}

	return DeriveCompactVariantTag(author, format, file, baseModelName)
}

// base36Hash returns a deterministic, DNS-safe base36 encoding of the SHA-256
// hash of s. The result is ~50 chars, always valid as an OCI tag or DNS label.
// Also implemented in projects/operators/oci-model-cache/internal/naming/naming.go;
// both packages belong to the repository's root Go module.
func base36Hash(s string) string {
	h := sha256.Sum256([]byte(s))
	n := new(big.Int).SetBytes(h[:])
	return strings.ToLower(n.Text(36))
}

// ResolveRef computes the full OCI reference for a HuggingFace model by calling
// ModelInfo to determine base model relationships. On failure, falls back to
// simple naming (repo path + rev tag). When file is non-empty (GGUF selector),
// the tag is derived from the file selector instead of the revision.
func ResolveRef(ctx context.Context, client *hf.Client, repo, registry, file string) string {
	var repoPath, ociTag string
	info, err := client.ModelInfo(ctx, repo)
	if err == nil && info.BaseModels != nil && len(info.BaseModels.Models) > 0 {
		// Derivative model: group under base model's repo path for layer dedup.
		repoPath = DeriveRepoName(info.BaseModels.Models[0].ID)
		if file != "" {
			// File selectors are currently GGUF-only. The full resolver in
			// copy/resolve.go uses string(format) from Classify(); this lightweight
			// resolver hardcodes "gguf" since it skips Tree/Classify.
			ociTag = DeriveFileTag(info, "gguf", file)
		} else {
			ociTag = DeriveVariantTag(repo)
		}
	} else {
		// Base model or ModelInfo unavailable: use repo directly.
		repoPath = DeriveRepoName(repo)
		if file != "" {
			ociTag = DeriveFileTag(info, "gguf", file) // see comment above re: hardcoded "gguf"
		} else {
			ociTag = DeriveTag("", "main")
		}
	}

	return fmt.Sprintf("%s/%s:%s", registry, repoPath, ociTag)
}
