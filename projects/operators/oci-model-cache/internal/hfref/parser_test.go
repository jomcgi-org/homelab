package hfref

import "testing"

func TestParse(t *testing.T) {
	tests := []struct {
		name       string
		volumeName string
		wantRepo   string
		wantFile   string
		wantOK     bool
	}{
		{
			name:       "simple hf ref",
			volumeName: "hf.co/bartowski/Llama-3.2-1B-Instruct-GGUF",
			wantRepo:   "bartowski/Llama-3.2-1B-Instruct-GGUF",
			wantFile:   "",
			wantOK:     true,
		},
		{
			name:       "hf ref with file selector",
			volumeName: "hf.co/bartowski/NousResearch_Hermes-4-14B-GGUF:NousResearch_Hermes-4-14B-IQ4_XS",
			wantRepo:   "bartowski/NousResearch_Hermes-4-14B-GGUF",
			wantFile:   "NousResearch_Hermes-4-14B-IQ4_XS",
			wantOK:     true,
		},
		{
			name:       "not an hf ref",
			volumeName: "ghcr.io/some/image:tag",
			wantRepo:   "",
			wantFile:   "",
			wantOK:     false,
		},
		{
			name:       "hf.co with no org/model",
			volumeName: "hf.co/justmodel",
			wantRepo:   "",
			wantFile:   "",
			wantOK:     false,
		},
		{
			name:       "empty after prefix",
			volumeName: "hf.co/",
			wantRepo:   "",
			wantFile:   "",
			wantOK:     false,
		},
		{
			name:       "empty string",
			volumeName: "",
			wantRepo:   "",
			wantFile:   "",
			wantOK:     false,
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			repo, file, ok := Parse(tt.volumeName)
			if ok != tt.wantOK {
				t.Errorf("Parse(%q) ok = %v, want %v", tt.volumeName, ok, tt.wantOK)
			}
			if repo != tt.wantRepo {
				t.Errorf("Parse(%q) repo = %q, want %q", tt.volumeName, repo, tt.wantRepo)
			}
			if file != tt.wantFile {
				t.Errorf("Parse(%q) file = %q, want %q", tt.volumeName, file, tt.wantFile)
			}
		})
	}
}
