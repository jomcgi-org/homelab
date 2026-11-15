// Package bootenv encodes environment values carried in kernel boot arguments.
// It only parses and renders strings; callers own environment mutation and logs.
package bootenv

import (
	"encoding/base64"
	"sort"
	"strings"
)

// Prefix identifies an environment boot argument.
const Prefix = "ember.env."

// ValidKey accepts nonempty ASCII letters, digits and underscores, including
// digit-leading names.
func ValidKey(key string) bool {
	if key == "" {
		return false
	}
	for _, r := range key {
		if (r >= 'A' && r <= 'Z') || (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9') || r == '_' {
			continue
		}
		return false
	}
	return true
}

// Encode renders valid entries in key order using unpadded URL-safe base64.
// Empty values produce a trailing equals sign; Decode skips those tokens.
func Encode(values map[string]string) string {
	keys := make([]string, 0, len(values))
	for key := range values {
		if ValidKey(key) {
			keys = append(keys, key)
		}
	}
	sort.Strings(keys)
	tokens := make([]string, 0, len(keys))
	for _, key := range keys {
		tokens = append(tokens, Prefix+key+"="+base64.RawURLEncoding.EncodeToString([]byte(values[key])))
	}
	return strings.Join(tokens, " ")
}

// Decode returns the last successfully decoded value for each valid key.
// Empty encoded tokens and malformed values do not erase earlier values.
// Decoded bytes, including NUL, are preserved for the caller to handle.
func Decode(cmdline string) map[string]string {
	out := map[string]string{}
	for _, tok := range strings.Fields(cmdline) {
		name, encoded, ok := strings.Cut(tok, "=")
		if !ok || encoded == "" || !strings.HasPrefix(name, Prefix) {
			continue
		}
		key := strings.TrimPrefix(name, Prefix)
		if !ValidKey(key) {
			continue
		}
		decoded, err := base64.RawURLEncoding.DecodeString(encoded)
		if err != nil {
			continue
		}
		out[key] = string(decoded)
	}
	return out
}

// Value returns the last nonempty exact-key match, splitting at the first equals
// sign and using the kernel command line helpers' whitespace tokenization.
func Value(cmdline, key string) string {
	value := ""
	for _, tok := range strings.Fields(cmdline) {
		k, val, ok := strings.Cut(tok, "=")
		if ok && k == key && val != "" {
			value = val
		}
	}
	return value
}
