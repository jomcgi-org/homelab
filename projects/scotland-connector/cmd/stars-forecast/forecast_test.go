package main

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"runtime"
	"strings"
	"testing"
	"time"
)

func winterInput() (site, []entry) {
	s := site{location: location{Lat: 56.25, Lon: -4.5}}
	entries := make([]entry, 72)
	start := time.Date(2026, 2, 1, 0, 0, 0, 0, time.UTC)
	for i := range entries {
		entries[i].Time = start.Add(time.Duration(i) * time.Hour).Format(time.RFC3339)
		cloud := float64(i % 20)
		entries[i].Data.Instant.Details.Cloud = &cloud
	}
	return s, entries
}

func TestPythonWinterHours(t *testing.T) {
	s, entries := winterInput()
	h := sha256.New()
	for _, hour := range score(s, entries) {
		fmt.Fprintf(h, "%s %.1f %.0f\n", hour.Time.Format(time.RFC3339), hour.Sun, hour.Cloud)
	}
	if got := fmt.Sprintf("%x", h.Sum(nil)); got != "a6fe94ec17e06a6f27b9695fb6deb560970cdd382146643143241f48281bfca8" {
		t.Fatalf("hours differ from Python: %s", got)
	}
}

func BenchmarkWinterBatch(b *testing.B) {
	s, entries := winterInput()
	for n := 0; n < b.N; n++ {
		batch := make([][]hour, 500)
		for i := range batch {
			batch[i] = score(s, entries)
		}
		runtime.KeepAlive(batch)
	}
}

type roundTrip func(*http.Request) (*http.Response, error)

func (f roundTrip) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestRefreshGeneration(t *testing.T) {
	ctx := context.Background()
	oldTime := "2026-01-01T00:00:00.000000+00:00"
	// Winter midnight is below nautical darkness at both sites.
	at := time.Date(time.Now().Year()+1, 1, 1, 0, 0, 0, 0, time.UTC)
	objects := map[string][]byte{
		"/source/grid.json":        []byte(`[{"id":"a","lat":56,"lon":-4},{"id":"b","lat":57,"lon":-4}]`),
		"/source/climatology.json": []byte(`[{"site_id":"a","month":2,"dark_hours":7,"clear_dark_hours":3}]`),
	}
	j := sha256.Sum256([]byte("b"))[0] % shards
	objects["/state/"+shardKey(int(j))], _ = json.Marshal(map[string]forecast{"b": {oldTime, []hour{{at, 2, -20}}}})
	var writes []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		key := r.URL.Path
		if r.Method == "PUT" {
			body, err := io.ReadAll(r.Body)
			if err != nil {
				t.Error(err)
			}
			if fmt.Sprintf("%x", sha256.Sum256(body)) != r.Header.Get("X-Amz-Content-Sha256") {
				t.Error("wrong upload digest")
			}
			objects[key] = body
			writes = append(writes, key)
			return
		}
		data, ok := objects[key]
		if !ok {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("ETag", `"fixture"`)
		w.Write(data)
	}))
	defer srv.Close()
	for _, prefix := range []string{"SOURCE_", "STATE_", "PUBLIC_"} {
		t.Setenv(prefix+"AWS_ENDPOINT_URL", srv.URL)
		t.Setenv(prefix+"BUCKET", strings.ToLower(strings.TrimSuffix(prefix, "_")))
		t.Setenv(prefix+"AWS_ACCESS_KEY_ID", "fixture")
		t.Setenv(prefix+"AWS_SECRET_ACCESS_KEY", "fixture")
	}
	// The inputs sync hook imports both before the refresh CronJob syncs.
	if err := run(ctx, "inputs"); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"grid", "climatology"} {
		key := "/" + name + ".json"
		if string(objects["/source"+key]) != string(objects["/state"+key]) {
			t.Fatalf("import changed %s", name)
		}
	}
	delete(objects, "/source/climatology.json")
	if err := run(ctx, "climatology"); err == nil {
		t.Fatal("imported a missing climatology")
	}
	allFailed := false
	previous := http.DefaultTransport
	defer func() { http.DefaultTransport = previous }()
	http.DefaultTransport = roundTrip(func(r *http.Request) (*http.Response, error) {
		if r.URL.Host != "api.met.no" {
			return previous.RoundTrip(r)
		}
		status := 200
		if allFailed || r.URL.Query().Get("lat") == "57" {
			status = 503
		}
		body := fmt.Sprintf(`{"properties":{"timeseries":[{"time":%q,"data":{"instant":{"details":{"cloud_area_fraction":0}}}}]}}`, at.Format(time.RFC3339))
		return &http.Response{StatusCode: status, Body: io.NopCloser(strings.NewReader(body)), Header: http.Header{}}, nil
	})
	if err := run(ctx, "refresh"); err != nil {
		t.Fatal(err)
	}
	if writes[len(writes)-1] != "/public/latest.json" {
		t.Fatal(writes)
	}
	var manifest struct{ Files map[string]string }
	if err := json.Unmarshal(objects["/public/latest.json"], &manifest); err != nil {
		t.Fatal(err)
	}
	read := func(k string, out any) {
		t.Helper()
		key := manifest.Files[k]
		data := objects["/public/"+key]
		if !strings.HasSuffix(key, fmt.Sprintf("-%x.json", sha256.Sum256(data))) {
			t.Fatal(key)
		}
		if err := json.Unmarshal(data, out); err != nil {
			t.Fatal(err)
		}
	}
	var public struct {
		Count    int
		Darkness string
		Sites    []result
	}
	read("sites", &public)
	if public.Count != 2 || public.Darkness != "astronomical" || len(public.Sites) != 2 {
		t.Fatal(public)
	}
	for _, s := range public.Sites {
		if s.Dark != 1 || s.Twilight != 1 || len(s.Best) != 1 || s.Fetched == nil {
			t.Fatal(s)
		}
		if (s.ID == "b") != (*s.Fetched == oldTime) {
			t.Fatal(s)
		}
	}
	var compact struct{ Sites []mapSite }
	read("map", &compact)
	if len(compact.Sites) != 2 || compact.Sites[1].Dark != 1 {
		t.Fatal(compact)
	}
	var history struct{ Sites []historySite }
	read("historyMap", &history)
	if len(history.Sites) != 1 || history.Sites[0].Dark[1] != 7 || history.Sites[0].Clear[1] != 3 {
		t.Fatal(history)
	}
	// A total provider outage must keep the last published generation.
	allFailed = true
	writes = nil
	if err := run(ctx, "refresh"); err == nil {
		t.Fatal("expected all fetches to fail")
	}
	for _, key := range writes {
		if strings.HasPrefix(key, "/public/") {
			t.Fatalf("overwrote public object during outage: %s", key)
		}
	}
}
