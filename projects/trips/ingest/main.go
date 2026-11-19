// Command ingest serves the private Trips upload route behind the authenticated gateway.
package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"hash/crc32"
	"image"
	_ "image/gif"
	_ "image/jpeg"
	_ "image/png"
	"io"
	"log"
	"math"
	"mime/multipart"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
	_ "time/tzdata"

	"github.com/lib/pq"
)

type camera struct {
	GPSLatitude, GPSLongitude, FNumber, ExposureTime *float64
	ISO, FocalLengthIn35mmFormat                     *int
	DateTimeOriginal, ModifyDate                     string
}

func metadata(ctx context.Context, path string) (camera, error) {
	// Extract the binary EXIF directory. XMP or other location metadata must
	// never substitute for GPS in EXIF.
	data, err := exec.CommandContext(ctx, "exiftool", "-b", "-EXIF", path).Output()
	if err != nil {
		return camera{}, err
	}
	return readTIFF(bytes.TrimPrefix(data, []byte("Exif\x00\x00")))
}

// Pillow verifies PNG chunk checksums before accepting metadata. Stream the
// raster through CRC32 so even a large IDAT needs only a small copy buffer.
func verifyPNG(f *os.File) error {
	if _, err := f.Seek(8, io.SeekStart); err != nil {
		return err
	}
	for {
		var chunk [8]byte
		if _, err := io.ReadFull(f, chunk[:]); err != nil {
			return err
		}
		size := int64(binary.BigEndian.Uint32(chunk[:4]))
		crc := crc32.NewIEEE()
		crc.Write(chunk[4:])
		if _, err := io.CopyN(crc, f, size); err != nil {
			return err
		}
		var checksum [4]byte
		if _, err := io.ReadFull(f, checksum[:]); err != nil {
			return err
		}
		if crc.Sum32() != binary.BigEndian.Uint32(checksum[:]) {
			return errors.New("invalid PNG checksum")
		}
		if string(chunk[4:]) == "IEND" {
			return nil
		}
	}
}

func respond(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func reject(w http.ResponseWriter, status int, detail string) {
	respond(w, status, map[string]string{"detail": detail})
}

const maxUpload = 64 << 20

type service struct {
	db        *sql.DB
	originals string
	slots     chan struct{}
}

func missing(w http.ResponseWriter, trip, image bool) {
	var detail []map[string]any
	add := func(location, field string) {
		detail = append(detail, map[string]any{"type": "missing", "loc": []string{location, field}, "msg": "Field required", "input": nil})
	}
	if trip {
		add("query", "trip")
	}
	if image {
		add("body", "image")
	}
	respond(w, 422, map[string]any{"detail": detail})
}

func (s service) ingest(w http.ResponseWriter, r *http.Request) {
	select {
	case s.slots <- struct{}{}:
		defer func() { <-s.slots }()
	case <-r.Context().Done():
		return
	}
	q := r.URL.Query()
	r.Body = http.MaxBytesReader(w, r.Body, maxUpload+(1<<20))
	parts, err := r.MultipartReader()
	if err != nil {
		missing(w, !q.Has("trip"), true)
		return
	}
	for {
		p, err := parts.NextPart()
		if err != nil {
			missing(w, !q.Has("trip"), true)
			return
		}
		if p.FormName() != "image" || p.FileName() == "" {
			continue
		}
		if !q.Has("trip") {
			missing(w, true, false)
			return
		}
		s.store(w, r, q, p)
		return
	}
}

func (s service) store(w http.ResponseWriter, r *http.Request, q url.Values, p *multipart.Part) {
	f, err := os.CreateTemp(s.originals, ".upload-*")
	if err != nil {
		reject(w, 500, "private storage unavailable")
		return
	}
	defer os.Remove(f.Name())
	defer f.Close()
	hash := sha256.New()
	n, err := io.Copy(io.MultiWriter(f, hash), io.LimitReader(p, maxUpload+1))
	if n > maxUpload {
		reject(w, 413, "image too large")
		return
	}
	if err != nil {
		reject(w, 400, "incomplete upload")
		return
	}
	if n < 256 {
		reject(w, 422, fmt.Sprintf("image too small (%d bytes), likely corrupt or an error body", n))
		return
	}
	if _, err = f.Seek(0, 0); err != nil {
		reject(w, 500, "private storage unavailable")
		return
	}
	_, format, err := image.DecodeConfig(f)
	if err != nil {
		var header [12]byte
		_, headErr := f.ReadAt(header[:], 0)
		if headErr == nil && string(header[:4]) == "RIFF" && string(header[8:]) == "WEBP" && int64(binary.LittleEndian.Uint32(header[4:8]))+8 <= n {
			format = "webp"
			err = exec.CommandContext(r.Context(), "identify", "-ping", "webp:"+f.Name()+"[0]").Run()
		}
	} else if format == "png" {
		err = verifyPNG(f)
	} else if format == "jpeg" {
		if _, err = f.Seek(0, io.SeekStart); err == nil {
			err = verifyJPEG(f)
		}
	}
	if err != nil {
		reject(w, 422, "not a valid image")
		return
	}
	var c camera
	if format == "jpeg" {
		if _, err = f.Seek(0, 0); err == nil {
			c, err = jpegCamera(f)
		}
	} else if format == "png" || format == "webp" {
		c, err = metadata(r.Context(), f.Name())
	}
	if err != nil || c.GPSLatitude == nil || c.GPSLongitude == nil {
		reject(w, 422, "image has no GPS coordinates")
		return
	}
	lat, lng := *c.GPSLatitude, *c.GPSLongitude
	if math.Abs(lat) > 90 || math.Abs(lng) > 180 || (lat == 0 && lng == 0) {
		reject(w, 422, "image has no GPS coordinates")
		return
	}
	id := hex.EncodeToString(hash.Sum(nil)[:6])
	key := "img_" + id + ".jpg"
	var zone string
	if err := s.db.QueryRowContext(r.Context(), "SELECT timezone FROM trips.trips WHERE slug=$1", q.Get("trip")).Scan(&zone); err != nil {
		reject(w, 500, "trip unavailable")
		return
	}
	loc, err := time.LoadLocation(zone)
	if err != nil {
		reject(w, 500, "trip timezone unavailable")
		return
	}
	taken := time.Now()
	stamp := c.DateTimeOriginal
	if stamp == "" {
		stamp = c.ModifyDate
	}
	// Like the hub, an unreadable camera time falls back to the upload time.
	if parsed, err := time.ParseInLocation("2006:01:02 15:04:05", stamp, loc); err == nil {
		taken = parsed
	}
	tags := []string{}
	for _, tag := range strings.Split(q.Get("tags"), ",") {
		if tag = strings.TrimSpace(tag); tag != "" {
			tags = append(tags, tag)
		}
	}
	source := q.Get("source")
	if !q.Has("source") {
		source = "gopro"
	}
	var shutter *string
	var light *float64
	if c.FNumber != nil {
		*c.FNumber = math.RoundToEven(*c.FNumber*10) / 10
	}
	if c.ExposureTime != nil && *c.ExposureTime > 0 {
		exp := *c.ExposureTime
		speed := fmt.Sprintf("%.1fs", exp)
		if exp < 1 {
			speed = fmt.Sprintf("1/%.0f", 1/exp)
		}
		shutter = &speed
		if c.ISO != nil && *c.ISO > 0 && c.FNumber != nil && *c.FNumber > 0 {
			lv := math.RoundToEven(10*math.Log2((*c.FNumber)*(*c.FNumber)/exp*100/float64(*c.ISO))) / 10
			light = &lv
		}
	}
	if err = f.Sync(); err != nil {
		reject(w, 500, "private storage unavailable")
		return
	}
	if err = os.Rename(f.Name(), filepath.Join(s.originals, key)); err != nil {
		reject(w, 500, "private storage unavailable")
		return
	}
	if err = s.write(r.Context(), q.Get("trip"), id, math.RoundToEven(lat*1e5)/1e5, math.RoundToEven(lng*1e5)/1e5, taken, key, source, pq.Array(tags), light, c.ISO, shutter, c.FNumber, c.FocalLengthIn35mmFormat); err != nil {
		reject(w, 500, "point write failed")
		return
	}
	respond(w, 201, map[string]string{"id": id, "image": key})
}

// An approval covers the published values it was given for. A replay that
// changes any of them revokes the approval in the same transaction. The row
// lock makes a concurrent approval wait for the new values.
func (s service) write(ctx context.Context, args ...any) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if _, err = tx.ExecContext(ctx, "SELECT 1 FROM trips.points WHERE trip_slug=$1 AND id=$2 FOR UPDATE", args[:2]...); err != nil {
		return err
	}
	for _, statement := range []string{revoke, upsert} {
		if _, err = tx.ExecContext(ctx, statement, args...); err != nil {
			return err
		}
	}
	return tx.Commit()
}

// Elevation is derived later, so a replay that clears it keeps the approval.
const revoke = `DELETE FROM trips.public_points a USING trips.points p
WHERE a.trip_slug=$1 AND a.id=$2 AND p.trip_slug=a.trip_slug AND p.id=a.id
AND (p.lat,p.lng,p.taken_at,p.image,p.source,p.tags,p.light_value,p.iso,p.shutter_speed,p.aperture,p.focal_length_35mm)
IS DISTINCT FROM ($3::float8,$4::float8,$5::timestamptz,$6::text,$7::text,$8::text[],$9::float8,$10::int,$11::text,$12::float8,$13::int)`

const upsert = `INSERT INTO trips.points
(trip_slug,id,lat,lng,taken_at,image,source,tags,light_value,iso,shutter_speed,aperture,focal_length_35mm)
VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
ON CONFLICT (trip_slug,id) DO UPDATE SET
lat=excluded.lat,lng=excluded.lng,taken_at=excluded.taken_at,image=excluded.image,
source=excluded.source,tags=excluded.tags,elevation=NULL,light_value=excluded.light_value,iso=excluded.iso,
shutter_speed=excluded.shutter_speed,aperture=excluded.aperture,focal_length_35mm=excluded.focal_length_35mm`

func main() {
	if _, err := exec.LookPath("exiftool"); err != nil {
		log.Fatal(err)
	}
	originals := os.Getenv("TRIPS_ORIGINALS")
	if !filepath.IsAbs(originals) {
		log.Fatal("TRIPS_ORIGINALS requires a private absolute directory")
	}
	db, err := sql.Open("postgres", os.Getenv("DATABASE_URL"))
	if err != nil {
		log.Fatal("database configuration failed")
	}
	defer db.Close()
	s := service{db, originals, make(chan struct{}, 4)}
	mux := http.NewServeMux()
	mux.HandleFunc("POST /api/trips/ingest", s.ingest)
	server := http.Server{Addr: ":8080", Handler: mux, ReadTimeout: 2 * time.Minute, ReadHeaderTimeout: 10 * time.Second}
	log.Fatal(server.ListenAndServe())
}
