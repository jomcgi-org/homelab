// Command export publishes an approved snapshot to the scoped Garage claim.
package main

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"

	_ "github.com/lib/pq"
)

type object = map[string]any

var imageKey = regexp.MustCompile(`^img_[A-Za-z0-9_-]{1,64}\.jpg$`)

const query = `SELECT t.slug,
 jsonb_build_object('slug',slug,'title',title,'short_title',short_title,'subtitle',subtitle,'default_image',default_image),
 jsonb_build_object('slug',slug,'title',title,'short_title',short_title,'subtitle',subtitle,'tz',timezone,'default_image',default_image,'default_zoom',default_zoom,'days',days,'highlights',highlights,'stats',stats),
 coalesce((SELECT jsonb_agg(jsonb_build_object('id',p.id,'lat',p.lat,'lng',p.lng,'taken_at',p.taken_at,
 'image',p.image,'source',p.source,'tags',p.tags,'elevation',p.elevation,
 'light_value',p.light_value,'iso',p.iso,'shutter_speed',p.shutter_speed,
 'aperture',p.aperture,'focal_length_35mm',p.focal_length_35mm) ORDER BY p.taken_at,p.id)
 FROM trips.points p INNER JOIN trips.public_points a USING(trip_slug,id) WHERE p.trip_slug=t.slug),'[]')
 FROM trips.trips t INNER JOIN trips.public_trips a USING(slug) ORDER BY t.slug DESC`

func write(root, name string, data any) (string, error) {
	body, err := json.Marshal(data)
	if err != nil {
		return "", err
	}
	key := fmt.Sprintf("trips/v/%s-%x.json", name, sha256.Sum256(body))
	path := filepath.Join(root, key)
	if err = os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return "", err
	}
	return key, os.WriteFile(path, body, 0o600)
}

func garage(ctx context.Context, args ...string) *exec.Cmd {
	args = append(args, "--s3-no-check-bucket", "--s3-provider", "Other", "--s3-env-auth", "--s3-endpoint", os.Getenv("AWS_ENDPOINT_URL"))
	return exec.CommandContext(ctx, "rclone", args...)
}

type preset struct{ name, geometry, quality string }

var presets = []preset{
	{"thumb", "300x300>", "78"},
	{"gallery", "600x600>", "80"},
	{"preview", "1200x1200>", "82"},
	{"display", "1920x1080>", "82"},
	{"full", "", "90"},
}

// Check the generated RIFF container before allowing any bytes onto the mirror.
func webpSize(path string) (int64, error) {
	file, err := os.Open(path)
	if err != nil {
		return 0, err
	}
	defer file.Close()
	st, err := file.Stat()
	if err != nil {
		return 0, err
	}
	var header [12]byte
	if _, err := io.ReadFull(file, header[:]); err != nil {
		return 0, err
	}
	if string(header[:4]) != "RIFF" || string(header[8:]) != "WEBP" || int64(binary.LittleEndian.Uint32(header[4:]))+8 != st.Size() {
		return 0, fmt.Errorf("invalid WebP container")
	}
	frame := false
	for remaining := st.Size() - 12; remaining > 0; {
		var chunk [8]byte
		if _, err := io.ReadFull(file, chunk[:]); err != nil {
			return 0, err
		}
		size := int64(binary.LittleEndian.Uint32(chunk[4:]))
		switch string(chunk[:4]) {
		case "EXIF", "XMP ":
			return 0, fmt.Errorf("photo retained metadata")
		case "VP8 ", "VP8L":
			frame = size > 0
		}
		padded := size + size%2
		remaining -= 8 + padded
		if remaining < 0 {
			return 0, fmt.Errorf("truncated WebP chunk")
		}
		if _, err = file.Seek(padded, io.SeekCurrent); err != nil {
			return 0, err
		}
	}
	if !frame {
		return 0, fmt.Errorf("missing WebP frame")
	}
	return st.Size(), nil
}

func render(ctx context.Context, root, key string, p preset) (int64, error) {
	dir := filepath.Join(root, "trips/photos", key)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return 0, err
	}
	path := filepath.Join(dir, p.name+".webp")
	args := []string{filepath.Join(os.Getenv("TRIPS_ORIGINALS"), key) + "[0]", "-auto-orient", "-strip"}
	if p.geometry != "" {
		args = append(args, "-resize", p.geometry)
	}
	args = append(args, "-quality", p.quality, path)
	if err := exec.CommandContext(ctx, "convert", args...).Run(); err != nil {
		return 0, fmt.Errorf("render %s: %w", p.name, err)
	}
	return webpSize(path)
}

func export(ctx context.Context, db *sql.DB, root string) error {
	rows, err := db.QueryContext(ctx, query)
	if err != nil {
		return err
	}
	defer rows.Close()
	index := []json.RawMessage{}
	files := map[string]string{}
	photos := map[string]bool{}
	for rows.Next() {
		var slug string
		var entry, trip, points json.RawMessage
		if err := rows.Scan(&slug, &entry, &trip, &points); err != nil {
			return err
		}
		var meta struct {
			DefaultImage string `json:"default_image"`
			Highlights   []struct{ Image string }
		}
		var list []struct{ Image string }
		if err = json.Unmarshal(trip, &meta); err != nil {
			return err
		}
		if err = json.Unmarshal(points, &list); err != nil {
			return err
		}
		for _, p := range list {
			photos[p.Image] = true
		}
		photos[meta.DefaultImage] = true
		for _, h := range meta.Highlights {
			photos[h.Image] = true
		}
		name := strings.NewReplacer(".", "%2E", "~", "%7E", "+", "%20").Replace(url.QueryEscape(slug))
		if len(name) > 200 {
			name = fmt.Sprintf("~%x", sha256.Sum256([]byte(slug)))
		}
		files[slug], err = write(root, "trip/"+name, object{"trip": trip, "points": points})
		if err != nil {
			return err
		}
		index = append(index, entry)
	}
	if err = rows.Err(); err != nil {
		return err
	}
	indexKey, err := write(root, "index", object{"count": len(index), "trips": index})
	if err != nil {
		return err
	}
	delete(photos, "")
	images := []string{}
	dest := ":s3:" + os.Getenv("BUCKET")
	listing, err := garage(ctx, "lsjson", dest, "--recursive", "--files-only", "--include", "/trips/photos/**").Output()
	if err != nil {
		return fmt.Errorf("list existing derivatives: %w", err)
	}
	var objects []struct {
		Path string
		Size int64
	}
	if err = json.Unmarshal(listing, &objects); err != nil {
		return err
	}
	existing := make(map[string]int64, len(objects))
	for _, o := range objects {
		existing[o.Path] = o.Size
	}
	var total, largest int64
	for key := range photos {
		if !imageKey.MatchString(key) {
			return fmt.Errorf("invalid approved image key")
		}
		images = append(images, key)
		for _, p := range presets {
			size := existing["trips/photos/"+key+"/"+p.name+".webp"]
			if size == 0 {
				size, err = render(ctx, root, key, p)
				if err != nil {
					return err
				}
			}
			total += size
			largest = max(largest, size)
		}
	}
	sort.Strings(images)
	mode, base := "snapshot", "/data/trips/photos"
	if len(images)*5 > 10000 || total > 1<<30 || largest > 25<<20 {
		mode, base = "public", strings.TrimRight(os.Getenv("TRIPS_PUBLIC_BASE"), "/")+"/trips/photos"
	}
	manifest := object{
		"files":  object{"trips": files, "index": indexKey},
		"images": images, "asOf": time.Now().UTC(), "refreshSeconds": 86400,
		"photoCount": len(images), "photoAssetCount": len(images) * len(presets),
		"photoBytes": total, "photoMaxBytes": largest, "photoMode": mode, "photoBase": base,
	}
	body, err := json.Marshal(manifest)
	if err != nil {
		return err
	}
	if err = garage(ctx, "copy", filepath.Join(root, "trips"), dest+"/trips", "--immutable", "--checksum", "--header-upload", "Cache-Control: public, max-age=31536000, immutable").Run(); err != nil {
		return err
	}
	path := filepath.Join(root, "latest.json")
	if err = os.WriteFile(path, body, 0o600); err != nil {
		return err
	}
	return garage(ctx, "copyto", path, dest+"/latest.json", "--ignore-times", "--header-upload", "Cache-Control: public, max-age=60").Run()
}

func run() error {
	for _, key := range []string{"BUCKET", "TRIPS_PUBLIC_BASE", "AWS_ENDPOINT_URL", "DATABASE_URL", "TRIPS_ORIGINALS"} {
		if os.Getenv(key) == "" {
			return fmt.Errorf("%s required", key)
		}
	}
	db, err := sql.Open("postgres", os.Getenv("DATABASE_URL"))
	if err != nil {
		return err
	}
	defer db.Close()
	root, err := os.MkdirTemp("", "trips-export-*")
	if err != nil {
		return err
	}
	defer os.RemoveAll(root)
	return export(context.Background(), db, root)
}

func main() {
	if err := run(); err != nil {
		log.Fatal("Trips export failed: ", err)
	}
}
