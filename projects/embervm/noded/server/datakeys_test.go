package server

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
)

func TestCPDataKeyProvider(t *testing.T) {
	key := bytes.Repeat([]byte{0x71}, 32)
	envelope := []byte("opaque-envelope")
	var gotPath, gotAuth string
	var gotBody map[string]string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		gotAuth = r.Header.Get("Authorization")
		if err := json.NewDecoder(r.Body).Decode(&gotBody); err != nil {
			t.Errorf("decode request: %v", err)
		}
		_ = json.NewEncoder(w).Encode(map[string]string{
			"data_key": base64.StdEncoding.EncodeToString(key),
			"envelope": base64.StdEncoding.EncodeToString(envelope),
		})
	}))
	defer srv.Close()

	tokenPath := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(tokenPath, []byte("projected-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	p := newCPDataKeyProvider(srv.URL, tokenPath, srv.Client())
	gotKey, gotEnvelope, err := p.DataKey(context.Background(), "session", "sandbox", "ref-1")
	if err != nil {
		t.Fatalf("DataKey: %v", err)
	}
	if !bytes.Equal(gotKey, key) || !bytes.Equal(gotEnvelope, envelope) {
		t.Fatalf("DataKey = (%x, %q), want (%x, %q)", gotKey, gotEnvelope, key, envelope)
	}
	if gotPath != "/v1/artifacts/wrap" {
		t.Fatalf("path = %q", gotPath)
	}
	if gotAuth != "Bearer projected-token" {
		t.Fatalf("Authorization = %q", gotAuth)
	}
	if gotBody["kind"] != "session" || gotBody["workload"] != "sandbox" || gotBody["ref"] != "ref-1" {
		t.Fatalf("request body = %#v", gotBody)
	}
}
