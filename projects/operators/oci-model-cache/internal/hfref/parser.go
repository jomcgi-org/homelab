package hfref

import "strings"

const prefix = "hf.co/"

// Parse extracts the HuggingFace repo and optional file selector from an image volume name.
// Volume names follow the pattern "hf.co/{org}/{model}" or "hf.co/{org}/{model}:{file}".
// The file selector is used for GGUF repos with multiple quantization variants.
//
// Examples:
//
//	Parse("hf.co/bartowski/Llama-3.2-1B-Instruct-GGUF")
//	-> ("bartowski/Llama-3.2-1B-Instruct-GGUF", "", true)
//
//	Parse("hf.co/bartowski/NousResearch_Hermes-4-14B-GGUF:NousResearch_Hermes-4-14B-IQ4_XS")
//	-> ("bartowski/NousResearch_Hermes-4-14B-GGUF", "NousResearch_Hermes-4-14B-IQ4_XS", true)
//
//	Parse("ghcr.io/some/image:tag")
//	-> ("", "", false)
func Parse(volumeName string) (repo string, file string, ok bool) {
	if !strings.HasPrefix(volumeName, prefix) {
		return "", "", false
	}

	rest := volumeName[len(prefix):]
	if rest == "" {
		return "", "", false
	}

	// Split on : for file selector
	if idx := strings.Index(rest, ":"); idx >= 0 {
		repo = rest[:idx]
		file = rest[idx+1:]
	} else {
		repo = rest
		file = ""
	}

	// Validate: repo must contain org/model (at least one /)
	if !strings.Contains(repo, "/") {
		return "", "", false
	}

	return repo, file, true
}
