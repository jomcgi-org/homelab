package main

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/lib/pq"

	"github.com/jomcgi/homelab/projects/factory/connector/internal/pgtest"
)

func TestMain(m *testing.M) { os.Exit(pgtest.Main(m)) }

func setup(t *testing.T) (service, *sql.DB) {
	t.Helper()
	db, err := sql.Open("postgres", pgtest.NewDB(t))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	for _, path := range []string{"../../monolith/chart/migrations/20260615120000_trips_schema.sql", "../approvals.sql"} {
		schema, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		if _, err = db.Exec(string(schema)); err != nil {
			t.Fatalf("%s: %v", path, err)
		}
	}
	if _, err = db.Exec(`INSERT INTO trips.trips (slug,title,timezone) VALUES ('yukon','Yukon','America/Vancouver')`); err != nil {
		t.Fatal(err)
	}
	return service{db, t.TempDir(), make(chan struct{}, 4)}, db
}

func upload(t *testing.T, s service, query string, image []byte) (int, map[string]any) {
	t.Helper()
	var body bytes.Buffer
	form := multipart.NewWriter(&body)
	if image != nil {
		part, err := form.CreateFormFile("image", "photo.jpg")
		if err != nil {
			t.Fatal(err)
		}
		part.Write(image)
	}
	form.Close()
	req := httptest.NewRequest(http.MethodPost, "/api/trips/ingest?"+query, &body)
	req.Header.Set("Content-Type", form.FormDataContentType())
	rec := httptest.NewRecorder()
	s.ingest(rec, req)
	var out map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("response %d is not JSON: %q", rec.Code, rec.Body.String())
	}
	return rec.Code, out
}

func fixture(t *testing.T, name string) []byte {
	t.Helper()
	data, err := os.ReadFile("../../monolith/trips/testdata/" + name)
	if err != nil {
		t.Fatal(err)
	}
	return data
}

// Invalid uploads keep the hub's status codes and write nothing.
func TestInvalidUploads(t *testing.T) {
	s, db := setup(t)
	photo := fixture(t, "geotagged.jpg")
	scan := bytes.Index(photo, []byte{255, 0xda})
	if scan < 0 {
		t.Fatal("fixture has no scan")
	}
	cases := []struct {
		name, query string
		image       []byte
		status      int
	}{
		{"missing trip", "", photo, 422},
		{"missing image", "trip=yukon", nil, 422},
		{"too small", "trip=yukon", photo[:200], 422},
		{"not an image", "trip=yukon", bytes.Repeat([]byte("not an image "), 40), 422},
		{"no end marker", "trip=yukon", photo[:len(photo)-2], 422},
		{"truncated raster", "trip=yukon", photo[:(scan+len(photo))/2], 422},
		{"truncated raster with end marker", "trip=yukon", append(append([]byte{}, photo[:scan+16]...), 255, 0xd9), 422},
		{"no GPS", "trip=yukon", fixture(t, "no_gps.jpg"), 422},
		{"unknown trip", "trip=nowhere", photo, 500},
	}
	for _, c := range cases {
		if status, out := upload(t, s, c.query, c.image); status != c.status || out["detail"] == nil {
			t.Errorf("%s: got %d %v, want %d with detail", c.name, status, out, c.status)
		}
	}
	var points int
	if err := db.QueryRow("SELECT count(*) FROM trips.points").Scan(&points); err != nil || points != 0 {
		t.Fatalf("invalid uploads wrote %d points (%v)", points, err)
	}
	if files, _ := filepath.Glob(filepath.Join(s.originals, "*")); len(files) != 0 {
		t.Fatalf("invalid uploads left files: %v", files)
	}
}

// A duplicate upload keeps one row and the approval. A replay that changes a
// published field revokes the approval until it is approved again.
func TestReplay(t *testing.T) {
	s, db := setup(t)
	photo := fixture(t, "geotagged.jpg")
	status, first := upload(t, s, "trip=yukon&tags=lake", photo)
	if status != 201 {
		t.Fatalf("first upload: %d %v", status, first)
	}
	if _, err := os.Stat(filepath.Join(s.originals, first["image"].(string))); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`INSERT INTO trips.public_trips VALUES ('yukon')`); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`INSERT INTO trips.public_points VALUES ('yukon',$1)`, first["id"]); err != nil {
		t.Fatal(err)
	}
	approved := func() (n int) {
		if err := db.QueryRow("SELECT count(*) FROM trips.public_points").Scan(&n); err != nil {
			t.Fatal(err)
		}
		return n
	}
	status, again := upload(t, s, "trip=yukon&tags=lake", photo)
	if status != 201 || again["id"] != first["id"] || again["image"] != first["image"] {
		t.Fatalf("duplicate upload: %d %v, first %v", status, again, first)
	}
	var points int
	var taken time.Time
	if err := db.QueryRow("SELECT count(*), max(taken_at) FROM trips.points").Scan(&points, &taken); err != nil || points != 1 {
		t.Fatalf("duplicate upload left %d points (%v)", points, err)
	}
	if want := time.Date(2024, 6, 15, 21, 30, 0, 0, time.UTC); !taken.Equal(want) {
		t.Fatalf("camera time %v, want %v", taken, want)
	}
	if approved() != 1 {
		t.Fatal("identical replay revoked the approval")
	}
	// A migrated point carries a derived elevation. An identical replay keeps
	// both the elevation and the approval.
	if _, err := db.Exec(`UPDATE trips.points SET elevation=345`); err != nil {
		t.Fatal(err)
	}
	if status, _ = upload(t, s, "trip=yukon&tags=lake", photo); status != 201 {
		t.Fatalf("replay with elevation: %d", status)
	}
	var elevation sql.NullFloat64
	if err := db.QueryRow("SELECT elevation FROM trips.points").Scan(&elevation); err != nil || elevation.Float64 != 345 {
		t.Fatalf("identical replay changed elevation to %v (%v)", elevation, err)
	}
	if approved() != 1 {
		t.Fatal("identical replay of a point with elevation revoked the approval")
	}
	if status, _ = upload(t, s, "trip=yukon&tags=lake,private", photo); status != 201 {
		t.Fatalf("changed replay: %d", status)
	}
	if approved() != 0 {
		t.Fatal("replay with changed tags kept the approval")
	}
}

// An unreadable camera time falls back to the upload time, as on the hub.
func TestMalformedCameraTime(t *testing.T) {
	s, db := setup(t)
	photo := fixture(t, "geotagged.jpg")
	stamp := []byte("2024:06:15 14:30:00")
	i := bytes.Index(photo, stamp)
	if i < 0 {
		t.Fatal("fixture has no camera time")
	}
	broken := append([]byte{}, photo...)
	for j := i; j >= 0; j = bytes.Index(broken, stamp) {
		copy(broken[j:], "not a camera time!!")
	}
	before := time.Now().Add(-time.Second)
	if status, out := upload(t, s, "trip=yukon", broken); status != 201 {
		t.Fatalf("malformed camera time: %d %v", status, out)
	}
	var taken time.Time
	if err := db.QueryRow("SELECT taken_at FROM trips.points").Scan(&taken); err != nil || taken.Before(before) {
		t.Fatalf("taken_at %v (%v), want upload time", taken, err)
	}
	// A replay of the same bytes keeps the stored time and the approval.
	if _, err := db.Exec(`INSERT INTO trips.public_trips VALUES ('yukon')`); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`INSERT INTO trips.public_points SELECT trip_slug,id FROM trips.points`); err != nil {
		t.Fatal(err)
	}
	if status, out := upload(t, s, "trip=yukon", broken); status != 201 {
		t.Fatalf("replay without camera time: %d %v", status, out)
	}
	var again time.Time
	var approved int
	if err := db.QueryRow("SELECT taken_at, (SELECT count(*) FROM trips.public_points) FROM trips.points").Scan(&again, &approved); err != nil {
		t.Fatal(err)
	}
	if !again.Equal(taken) || approved != 1 {
		t.Fatalf("replay without camera time: taken_at %v (was %v), %d approvals", again, taken, approved)
	}
}

// Two first uploads of one point run at the same time. The first commits its
// row with an approval while the second waits. The second then changes a
// published field, so it must revoke that approval.
func TestConcurrentFirstUpload(t *testing.T) {
	s, db := setup(t)
	if _, err := db.Exec(`INSERT INTO trips.public_trips VALUES ('yukon')`); err != nil {
		t.Fatal(err)
	}
	args := func(tag string) []any {
		return []any{"yukon", "abc123", 50.6745, -120.0, nil, "img_abc123.jpg", "gopro", pq.Array([]string{tag}), nil, nil, nil, nil, nil}
	}
	first, err := db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer first.Rollback()
	for _, statement := range []string{lockPoint, upsert, `INSERT INTO trips.public_points VALUES ($1,$2)`} {
		a := args("lake")
		if statement != upsert {
			a = a[:2]
		}
		if _, err := first.Exec(statement, a...); err != nil {
			t.Fatal(err)
		}
	}
	done := make(chan error, 1)
	go func() { done <- s.write(context.Background(), args("private")...) }()
	deadline := time.Now().Add(10 * time.Second)
	for waiting := 0; waiting == 0; {
		if time.Now().After(deadline) {
			t.Fatal("second upload never waited for the first")
		}
		if err := db.QueryRow(`SELECT count(*) FROM pg_stat_activity WHERE wait_event_type='Lock' AND datname=current_database()`).Scan(&waiting); err != nil {
			t.Fatal(err)
		}
		time.Sleep(10 * time.Millisecond)
	}
	if err := first.Commit(); err != nil {
		t.Fatal(err)
	}
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	var approved int
	var tags pq.StringArray
	if err := db.QueryRow(`SELECT tags, (SELECT count(*) FROM trips.public_points) FROM trips.points`).Scan(&tags, &approved); err != nil {
		t.Fatal(err)
	}
	if len(tags) != 1 || tags[0] != "private" || approved != 0 {
		t.Fatalf("second upload left tags %v with %d approvals", tags, approved)
	}
}
