package server

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/jomcgi/homelab/projects/embervm/xds/snapshot"
)

func TestHTTP_putGetRoundTrip(t *testing.T) {
	store := NewStore()
	srv := httptest.NewServer(NewHTTPHandler(store))
	defer srv.Close()

	body := `{
		"version": "0000000001",
		"clusters": [{"name": "c1", "endpoints": [{"ip": "10.0.0.1", "port": 8080}], "connect_timeout_ms": 200}],
		"routes": [{"host": "h", "path_prefix": "/", "cluster": "c1", "request_headers": {"x-fn": "c1"}}]
	}`
	req, _ := http.NewRequest(http.MethodPut, srv.URL+"/snapshot/node-a", strings.NewReader(body))
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("PUT: %v", err)
	}
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("PUT status = %d, want 200", resp.StatusCode)
	}
	resp.Body.Close()

	// GET reports what is served.
	gresp, err := http.Get(srv.URL + "/snapshot/node-a")
	if err != nil {
		t.Fatalf("GET: %v", err)
	}
	defer gresp.Body.Close()
	if gresp.StatusCode != http.StatusOK {
		t.Fatalf("GET status = %d, want 200", gresp.StatusCode)
	}
	var got servedSnapshot
	if err := json.NewDecoder(gresp.Body).Decode(&got); err != nil {
		t.Fatalf("decode GET: %v", err)
	}
	if got.Version != "0000000001" {
		t.Errorf("served version = %q", got.Version)
	}
	if len(got.Clusters) != 1 || got.Clusters[0] != "c1" {
		t.Errorf("served clusters = %v", got.Clusters)
	}
	if len(got.Endpoints) != 1 || len(got.Routes) != 1 {
		t.Errorf("served eds=%v routes=%v", got.Endpoints, got.Routes)
	}
}

func TestHTTP_getUnknownNodeIs404(t *testing.T) {
	srv := httptest.NewServer(NewHTTPHandler(NewStore()))
	defer srv.Close()
	resp, err := http.Get(srv.URL + "/snapshot/never-pushed")
	if err != nil {
		t.Fatalf("GET: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusNotFound {
		t.Errorf("status = %d, want 404", resp.StatusCode)
	}
}

func TestHTTP_malformedBodyIs400(t *testing.T) {
	srv := httptest.NewServer(NewHTTPHandler(NewStore()))
	defer srv.Close()
	cases := map[string]string{
		"not json":          `{not json`,
		"unknown field":     `{"version": "1", "bogus": true}`,
		"missing version":   `{"clusters": []}`,
		"undefined cluster": `{"version": "1", "routes": [{"host": "h", "cluster": "ghost"}]}`,
	}
	for name, body := range cases {
		t.Run(name, func(t *testing.T) {
			req, _ := http.NewRequest(http.MethodPut, srv.URL+"/snapshot/node-a", strings.NewReader(body))
			resp, err := http.DefaultClient.Do(req)
			if err != nil {
				t.Fatalf("PUT: %v", err)
			}
			defer resp.Body.Close()
			if resp.StatusCode != http.StatusBadRequest {
				t.Errorf("status = %d, want 400", resp.StatusCode)
			}
		})
	}
}

func TestHTTP_staleVersionIs409(t *testing.T) {
	store := NewStore()
	if err := store.Apply(context.Background(), "node-a", &snapshot.Desired{Version: "0000000002"}); err != nil {
		t.Fatalf("seed apply: %v", err)
	}
	srv := httptest.NewServer(NewHTTPHandler(store))
	defer srv.Close()

	req, _ := http.NewRequest(http.MethodPut, srv.URL+"/snapshot/node-a",
		strings.NewReader(`{"version": "0000000001"}`))
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("PUT: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusConflict {
		t.Errorf("status = %d, want 409", resp.StatusCode)
	}
}

func TestHTTP_missingNodeSegmentIs400(t *testing.T) {
	srv := httptest.NewServer(NewHTTPHandler(NewStore()))
	defer srv.Close()
	resp, err := http.Get(srv.URL + "/snapshot/")
	if err != nil {
		t.Fatalf("GET: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Errorf("status = %d, want 400", resp.StatusCode)
	}
}

func TestHTTP_healthz(t *testing.T) {
	srv := httptest.NewServer(NewHTTPHandler(NewStore()))
	defer srv.Close()
	resp, err := http.Get(srv.URL + "/healthz")
	if err != nil {
		t.Fatalf("GET healthz: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Errorf("status = %d, want 200", resp.StatusCode)
	}
}
