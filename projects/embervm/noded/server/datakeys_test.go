package server

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
)

type dataKeyDoerFunc func(*http.Request) (*http.Response, error)

func (f dataKeyDoerFunc) Do(req *http.Request) (*http.Response, error) { return f(req) }

type eofTrackingBody struct {
	io.Reader
	sawEOF bool
}

func (b *eofTrackingBody) Read(p []byte) (int, error) {
	n, err := b.Reader.Read(p)
	if err == io.EOF {
		b.sawEOF = true
	}
	return n, err
}

func (*eofTrackingBody) Close() error { return nil }

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

func TestCPDataKeyProviderWrapRejectedError(t *testing.T) {
	tests := []struct {
		name      string
		status    int
		reason    string
		permanent bool
	}{
		{name: "400 bad request", status: http.StatusBadRequest, reason: "bad_request", permanent: false},
		{name: "403 forbidden", status: http.StatusForbidden, reason: "forbidden", permanent: false},
		{name: "404 unknown artifact", status: http.StatusNotFound, reason: "unknown_artifact", permanent: true},
		{name: "404 padded unknown artifact", status: http.StatusNotFound, reason: " unknown_artifact ", permanent: false},
		{name: "404 unknown kind", status: http.StatusNotFound, reason: "unknown_kind", permanent: false},
		{name: "404 other reason", status: http.StatusNotFound, reason: "not_found", permanent: false},
		{name: "503 key service unavailable", status: http.StatusServiceUnavailable, reason: "key_service_unavailable", permanent: false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				w.WriteHeader(tt.status)
				_ = json.NewEncoder(w).Encode(map[string]string{"error": tt.reason})
			}))
			defer srv.Close()

			p := newCPDataKeyProvider(srv.URL, "", srv.Client())
			_, _, err := p.DataKey(context.Background(), "session-workspace", "sbx", "orphan")
			if !errors.Is(err, ErrWrapRejected) {
				t.Fatalf("DataKey error = %v, want ErrWrapRejected", err)
			}
			var rejected *WrapRejectedError
			if !errors.As(err, &rejected) {
				t.Fatalf("DataKey error type = %T, want *WrapRejectedError", err)
			}
			if rejected.StatusCode != tt.status || rejected.Reason != tt.reason || rejected.Permanent() != tt.permanent {
				t.Fatalf("wrap rejection = %#v permanent=%v, want status=%d reason=%q permanent=%v",
					rejected, rejected.Permanent(), tt.status, tt.reason, tt.permanent)
			}
			if got := rejected.Error(); got != "control plane rejected artifact wrap request: status "+strconv.Itoa(tt.status) {
				t.Fatalf("Error() = %q", got)
			}
		})
	}
}

func TestCPDataKeyProviderDrainsRejectedResponse(t *testing.T) {
	body := &eofTrackingBody{Reader: strings.NewReader(`{"error":"forbidden"}` + strings.Repeat(" ", 2048))}
	doer := dataKeyDoerFunc(func(*http.Request) (*http.Response, error) {
		return &http.Response{StatusCode: http.StatusForbidden, Body: body, Header: make(http.Header)}, nil
	})
	p := newCPDataKeyProvider("http://control.invalid", "", doer)

	_, _, err := p.DataKey(context.Background(), "session-workspace", "sbx", "lineage")
	var rejected *WrapRejectedError
	if !errors.As(err, &rejected) || rejected.Reason != "forbidden" {
		t.Fatalf("DataKey error = %#v, want forbidden WrapRejectedError", err)
	}
	if !body.sawEOF {
		t.Fatal("rejected response body was not drained to EOF")
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
