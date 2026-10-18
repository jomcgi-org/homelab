package naming

import (
	"crypto/sha256"
	"fmt"
	"math/big"
	"strings"
)

// ModelCacheName derives a deterministic, DNS-safe Kubernetes resource name
// from a HuggingFace repo and optional file selector. The name is unique per
// (repo, file) pair and safe for use as a Kubernetes object name.
//
// When file is empty, the name is just the repo slug:
//
//	ModelCacheName("bartowski/Llama-3.2-1B-Instruct-GGUF", "")
//	-> "bartowski-llama-3.2-1b-instruct-gguf"
//
// When file is set (GGUF selector), the file slug is appended:
//
//	ModelCacheName("bartowski/NousResearch_Hermes-4-14B-GGUF", "NousResearch_Hermes-4-14B-IQ4_XS")
//	-> "bartowski-nousresearch-hermes-4-14b-gguf-nousresearch-hermes-4-14b-iq4-xs"  (truncated + hash if >63)
func ModelCacheName(repo, file string) string {
	// Normalize: lowercase, replace / with -
	name := strings.ToLower(strings.ReplaceAll(repo, "/", "-"))

	var full string
	if file != "" {
		filePart := strings.ToLower(strings.ReplaceAll(file, "_", "-"))
		full = fmt.Sprintf("%s-%s", name, filePart)
	} else {
		full = name
	}

	// Kubernetes names must be <= 63 chars and DNS-safe
	if len(full) <= 63 {
		return sanitizeDNS(full)
	}

	// Truncate and add a hash suffix for uniqueness
	hash := sha256.Sum256([]byte(fmt.Sprintf("%s:%s", repo, file)))
	suffix := fmt.Sprintf("%x", hash[:4]) // 8 hex chars
	maxPrefix := 63 - len(suffix) - 1     // -1 for the separator
	return sanitizeDNS(full[:maxPrefix]) + "-" + suffix
}

// JobName derives a DNS-safe Kubernetes Job name from a resolved OCI reference.
// It extracts the tag (after the last ':') and sanitizes it. If the result
// exceeds 63 chars or is empty, it falls back to a deterministic base36 hash
// of the full ref (no truncation, always DNS-safe).
//
// Precondition: resolvedRef must be non-empty. The state machine validates
// ResolvedRef != "" before reaching the Resolving state that calls buildCopyJob.
func JobName(resolvedRef string) string {
	tag := ""
	if idx := strings.LastIndex(resolvedRef, ":"); idx >= 0 {
		tag = resolvedRef[idx+1:]
	}

	name := sanitizeDNS(strings.ToLower(tag))
	if name == "" || len(name) > 63 {
		return base36Hash(resolvedRef)
	}
	return name
}

// base36Hash returns a deterministic, DNS-safe base36 encoding of the SHA-256
// hash. The result is ~50 chars, always valid as a DNS label.
// Also implemented in bazel/tools/hf2oci/pkg/ociref/ociref.go; both packages
// belong to the repository's root Go module.
func base36Hash(s string) string {
	h := sha256.Sum256([]byte(s))
	n := new(big.Int).SetBytes(h[:])
	return strings.ToLower(n.Text(36))
}

// sanitizeDNS ensures a string is a valid DNS subdomain label:
// lowercase alphanumeric and hyphens, no leading/trailing hyphens.
func sanitizeDNS(s string) string {
	var b strings.Builder
	for _, c := range s {
		switch {
		case c >= 'a' && c <= 'z', c >= '0' && c <= '9', c == '.':
			b.WriteRune(c)
		case c == '-' || c == '_' || c == '/':
			b.WriteByte('-')
			// skip anything else
		}
	}
	return strings.Trim(b.String(), "-.")
}
