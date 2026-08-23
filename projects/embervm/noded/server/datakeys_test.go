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

func TestCPDataKeyProviderRewrapEnvelope(t *testing.T) {
	oldEnvelope := []byte("old-envelope")
	newEnvelope := []byte("new-envelope")
	var gotPath, gotAuth string
	var gotBody struct {
		Kind     string `json:"kind"`
		Workload string `json:"workload"`
		Ref      string `json:"ref"`
		Envelope []byte `json:"envelope"`
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		gotAuth = r.Header.Get("Authorization")
		if err := json.NewDecoder(r.Body).Decode(&gotBody); err != nil {
			t.Errorf("decode request: %v", err)
		}
		_ = json.NewEncoder(w).Encode(struct {
			Changed  bool   `json:"changed"`
			Envelope []byte `json:"envelope"`
		}{Changed: true, Envelope: newEnvelope})
	}))
	defer srv.Close()

	tokenPath := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(tokenPath, []byte("projected-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	p := newCPDataKeyProvider(srv.URL, tokenPath, srv.Client())
	gotEnvelope, changed, err := p.RewrapEnvelope(context.Background(), "serving", "api", "ref-2", oldEnvelope)
	if err != nil || !changed {
		t.Fatalf("RewrapEnvelope = (%q, %v, %v), want changed", gotEnvelope, changed, err)
	}
	if !bytes.Equal(gotEnvelope, newEnvelope) {
		t.Fatalf("envelope = %q, want %q", gotEnvelope, newEnvelope)
	}
	if gotPath != "/v1/artifacts/rewrap" || gotAuth != "Bearer projected-token" {
		t.Fatalf("request path/auth = %q, %q", gotPath, gotAuth)
	}
	if gotBody.Kind != "serving" || gotBody.Workload != "api" || gotBody.Ref != "ref-2" || !bytes.Equal(gotBody.Envelope, oldEnvelope) {
		t.Fatalf("request body = %#v", gotBody)
	}
}
