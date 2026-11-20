package main

import (
	"context"
	"database/sql"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"

	"github.com/jomcgi/homelab/projects/factory/connector/internal/pgtest"
)

// The test binary stands in for rclone and convert. The exporter finds them
// on PATH as symlinks to this binary, so the test needs no host tools.
func TestMain(m *testing.M) {
	switch filepath.Base(os.Args[0]) {
	case "rclone":
		os.Exit(fakeRclone(os.Args[1:]))
	case "convert":
		os.Exit(fakeConvert(os.Args[1:]))
	}
	os.Exit(pgtest.Main(m))
}

// bucketPath maps ":s3:<bucket>/<key>" to the local bucket directory.
func bucketPath(remote string) string {
	key := strings.TrimPrefix(remote, ":s3:"+os.Getenv("BUCKET"))
	return filepath.Join(os.Getenv("FAKE_BUCKET_DIR"), key)
}

func fakeRclone(args []string) int {
	var err error
	switch args[0] {
	case "lsjson":
		_, err = os.Stdout.WriteString("[]")
	case "copy":
		src, dest := args[1], bucketPath(args[2])
		err = filepath.WalkDir(src, func(path string, d fs.DirEntry, err error) error {
			if err != nil || d.IsDir() {
				return err
			}
			rel, _ := filepath.Rel(src, path)
			return copyFile(path, filepath.Join(dest, rel))
		})
	case "copyto":
		err = copyFile(args[1], bucketPath(args[2]))
	default:
		err = fmt.Errorf("unexpected rclone command %q", args[0])
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	return 0
}

func copyFile(src, dest string) error {
	data, err := os.ReadFile(src)
	if err != nil {
		return err
	}
	if err = os.MkdirAll(filepath.Dir(dest), 0o700); err != nil {
		return err
	}
	return os.WriteFile(dest, data, 0o600)
}

// fakeConvert writes the smallest valid WebP container. FAKE_WEBP_EXIF adds an
// EXIF chunk, which the exporter must refuse to publish.
func fakeConvert(args []string) int {
	if _, err := os.Stat(strings.TrimSuffix(args[0], "[0]")); err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	chunks := []byte("VP8 \x02\x00\x00\x00ab")
	if os.Getenv("FAKE_WEBP_EXIF") != "" {
		chunks = append(chunks, []byte("EXIF\x02\x00\x00\x00gp")...)
	}
	body := append([]byte("RIFF\x00\x00\x00\x00WEBP"), chunks...)
	binary.LittleEndian.PutUint32(body[4:], uint32(len(body)-8))
	if err := os.WriteFile(args[len(args)-1], body, 0o600); err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	return 0
}

func setup(t *testing.T) (*sql.DB, string) {
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
	tools := t.TempDir()
	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"rclone", "convert"} {
		if err := os.Symlink(self, filepath.Join(tools, name)); err != nil {
			t.Fatal(err)
		}
	}
	originals, bucket := t.TempDir(), t.TempDir()
	t.Setenv("PATH", tools+string(os.PathListSeparator)+os.Getenv("PATH"))
	t.Setenv("BUCKET", "trips-public")
	t.Setenv("FAKE_BUCKET_DIR", bucket)
	t.Setenv("TRIPS_ORIGINALS", originals)
	t.Setenv("TRIPS_PUBLIC_BASE", "https://trips-data.example")
	t.Setenv("AWS_ENDPOINT_URL", "http://garage.invalid")
	for _, key := range []string{"img_cover.jpg", "img_public.jpg", "img_private.jpg", "img_hidden.jpg"} {
		if err := os.WriteFile(filepath.Join(originals, key), []byte("private original"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	_, err = db.Exec(`
INSERT INTO trips.trips (slug,title,default_image) VALUES
 ('yukon','Yukon','img_cover.jpg'), ('draft','Draft','img_hidden.jpg');
INSERT INTO trips.points (trip_slug,id,lat,lng,taken_at,image,tags,elevation,iso) VALUES
 ('yukon','public',60.7,-135.0,'2024-06-15T21:30:00Z','img_public.jpg','{lake}',650,100),
 ('yukon','private',60.8,-135.1,'2024-06-15T22:30:00Z','img_private.jpg','{home}',700,200),
 ('draft','hidden',61.0,-136.0,'2024-06-16T21:30:00Z','img_hidden.jpg','{}',NULL,NULL);
INSERT INTO trips.public_trips VALUES ('yukon');
INSERT INTO trips.public_points VALUES ('yukon','public');`)
	if err != nil {
		t.Fatal(err)
	}
	return db, bucket
}

func readJSON(t *testing.T, path string, v any) {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if err = json.Unmarshal(data, v); err != nil {
		t.Fatalf("%s: %v", path, err)
	}
}

// The export publishes only approved trips and points, their WebP variants and
// the published fields. Unapproved points and trips leave no trace.
func TestExportPublishesApprovedOnly(t *testing.T) {
	db, bucket := setup(t)
	if err := export(context.Background(), db, t.TempDir()); err != nil {
		t.Fatal(err)
	}
	var objects []string
	err := filepath.WalkDir(bucket, func(path string, d fs.DirEntry, err error) error {
		if err == nil && !d.IsDir() {
			rel, _ := filepath.Rel(bucket, path)
			objects = append(objects, rel)
		}
		return err
	})
	if err != nil {
		t.Fatal(err)
	}
	sort.Strings(objects)
	var webp []string
	for _, o := range objects {
		switch {
		case strings.HasPrefix(o, "trips/photos/"):
			if !strings.HasSuffix(o, ".webp") {
				t.Errorf("non-WebP photo object %s", o)
			}
			webp = append(webp, o)
		case o == "latest.json", strings.HasPrefix(o, "trips/v/") && strings.HasSuffix(o, ".json"):
		default:
			t.Errorf("unexpected object %s", o)
		}
		for _, private := range []string{"img_private", "img_hidden", "draft", "private original"} {
			if strings.Contains(o, private) {
				t.Errorf("object %s names unapproved data", o)
			}
		}
	}
	if len(webp) != 2*len(presets) {
		t.Fatalf("published WebP variants %v, want 5 each for the cover and the approved point", webp)
	}

	var manifest struct {
		Files struct {
			Trips map[string]string
			Index string
		}
		Images    []string
		PhotoMode string
	}
	readJSON(t, filepath.Join(bucket, "latest.json"), &manifest)
	if strings.Join(manifest.Images, ",") != "img_cover.jpg,img_public.jpg" || manifest.PhotoMode != "snapshot" {
		t.Fatalf("manifest images %v mode %s", manifest.Images, manifest.PhotoMode)
	}
	if len(manifest.Files.Trips) != 1 || manifest.Files.Trips["yukon"] == "" {
		t.Fatalf("manifest trips %v, want only yukon", manifest.Files.Trips)
	}
	var index struct {
		Count int
		Trips []map[string]any
	}
	readJSON(t, filepath.Join(bucket, manifest.Files.Index), &index)
	if index.Count != 1 || index.Trips[0]["slug"] != "yukon" {
		t.Fatalf("index %+v", index)
	}
	var trip struct {
		Trip   map[string]any
		Points []map[string]any
	}
	readJSON(t, filepath.Join(bucket, manifest.Files.Trips["yukon"]), &trip)
	if len(trip.Points) != 1 || trip.Points[0]["id"] != "public" || trip.Points[0]["elevation"] != 650.0 {
		t.Fatalf("published points %v, want only the approved point", trip.Points)
	}
	var fields []string
	for k := range trip.Points[0] {
		fields = append(fields, k)
	}
	sort.Strings(fields)
	want := "aperture,elevation,focal_length_35mm,id,image,iso,lat,light_value,lng,shutter_speed,source,tags,taken_at"
	if strings.Join(fields, ",") != want {
		t.Fatalf("published point fields %v, want %s", fields, want)
	}
}

// A derivative that keeps EXIF stops the export before anything is published.
func TestExportRefusesRetainedMetadata(t *testing.T) {
	db, bucket := setup(t)
	t.Setenv("FAKE_WEBP_EXIF", "1")
	if err := export(context.Background(), db, t.TempDir()); err == nil || !strings.Contains(err.Error(), "retained metadata") {
		t.Fatalf("export with EXIF derivative: %v", err)
	}
	if entries, _ := os.ReadDir(bucket); len(entries) != 0 {
		t.Fatalf("export published %d objects after refusing a derivative", len(entries))
	}
}
