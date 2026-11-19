package publish

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"encoding/xml"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"reflect"
	"slices"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"
)

// fakeS3 is one in-memory bucket behind the S3 calls the store makes.
type fakeS3 struct {
	mu      sync.Mutex
	objects map[string]stored
	now     func() time.Time
	// failPutAfter fails every PUT after that many succeed; -1 never fails.
	failPutAfter int
	puts         []string
}

type stored struct {
	body     []byte
	cache    string
	kind     string
	modified time.Time
}

func (f *fakeS3) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if !strings.HasPrefix(r.Header.Get("Authorization"), "AWS4-HMAC-SHA256 Credential=test-key/") {
		http.Error(w, "unsigned", http.StatusForbidden)
		return
	}
	key, ok := strings.CutPrefix(r.URL.Path, "/ships/")
	switch {
	case r.Method == http.MethodGet && r.URL.Path == "/ships" && r.URL.Query().Get("list-type") == "2":
		f.list(w, r.URL.Query().Get("continuation-token"))
	case !ok:
		http.Error(w, "wrong bucket", http.StatusBadRequest)
	case r.Method == http.MethodGet:
		obj, found := f.objects[key]
		if !found {
			http.Error(w, "NoSuchKey", http.StatusNotFound)
			return
		}
		w.Write(obj.body)
	case r.Method == http.MethodPut:
		body, _ := io.ReadAll(r.Body)
		sum := sha256.Sum256(body)
		if r.Header.Get("X-Amz-Content-Sha256") != hex.EncodeToString(sum[:]) {
			http.Error(w, "XAmzContentSHA256Mismatch", http.StatusBadRequest)
			return
		}
		if f.failPutAfter >= 0 && len(f.puts) >= f.failPutAfter {
			http.Error(w, "interrupted", http.StatusServiceUnavailable)
			return
		}
		f.puts = append(f.puts, key)
		f.objects[key] = stored{body, r.Header.Get("Cache-Control"), r.Header.Get("Content-Type"), f.now()}
	case r.Method == http.MethodDelete:
		delete(f.objects, key)
		w.WriteHeader(http.StatusNoContent)
	default:
		http.Error(w, "unsupported", http.StatusMethodNotAllowed)
	}
}

// list returns two keys a page, so the store must follow continuation tokens.
func (f *fakeS3) list(w http.ResponseWriter, after string) {
	keys := make([]string, 0, len(f.objects))
	for key := range f.objects {
		if key > after {
			keys = append(keys, key)
		}
	}
	sort.Strings(keys)
	type content struct {
		Key          string
		LastModified string
	}
	page := struct {
		XMLName               xml.Name `xml:"http://s3.amazonaws.com/doc/2006-03-01/ ListBucketResult"`
		Contents              []content
		IsTruncated           bool
		NextContinuationToken string `xml:",omitempty"`
	}{}
	for _, key := range keys[:min(2, len(keys))] {
		page.Contents = append(page.Contents, content{key, f.objects[key].modified.UTC().Format("2006-01-02T15:04:05.000Z")})
	}
	if len(keys) > 2 {
		page.IsTruncated, page.NextContinuationToken = true, keys[1]
	}
	xml.NewEncoder(w).Encode(page)
}

func (f *fakeS3) object(t *testing.T, key string) stored {
	t.Helper()
	f.mu.Lock()
	defer f.mu.Unlock()
	obj, ok := f.objects[key]
	if !ok {
		t.Fatalf("bucket has no %s", key)
	}
	return obj
}

func (f *fakeS3) keys() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	keys := make([]string, 0, len(f.objects))
	for key := range f.objects {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return keys
}

// fakeDB is one database view. tracks records the MMSIs that a run asks for.
type fakeDB struct {
	now      time.Time
	rows     []vessel
	points   map[string][]point
	cells    []cell
	computed *time.Time
	asked    []string
}

func (f *fakeDB) open(context.Context) (reader, error) { f.asked = nil; return f, nil }
func (f *fakeDB) at() time.Time                        { return f.now }
func (f *fakeDB) close() error                         { return nil }

func (f *fakeDB) vessels(context.Context) ([]vessel, error) { return slices.Clone(f.rows), nil }

func (f *fakeDB) tracks(_ context.Context, mmsis []string, each func(string, []point) error) error {
	f.asked = append(f.asked, mmsis...)
	for _, mmsi := range mmsis {
		if points := f.points[mmsi]; len(points) > 0 {
			if err := each(mmsi, points); err != nil {
				return err
			}
		}
	}
	return nil
}

func (f *fakeDB) heat(context.Context) (heat, error) { return newHeat(f.computed, f.cells), nil }

func ptr[T any](v T) *T { return &v }

var t0 = time.Date(2026, 11, 19, 12, 0, 0, 0, time.UTC)

func newFixture(t *testing.T) (*fakeS3, *fakeDB, *store, *time.Time) {
	t.Helper()
	clock := t0
	s3 := &fakeS3{objects: map[string]stored{}, now: func() time.Time { return clock }, failPutAfter: -1}
	server := httptest.NewServer(s3)
	t.Cleanup(server.Close)
	st, err := newStore(server.URL, "garage", "ships", "test-key", "test-secret")
	if err != nil {
		t.Fatal(err)
	}
	db := &fakeDB{
		now: t0,
		rows: []vessel{
			{
				MMSI: "111", Lat: 48.5, Lon: -123.25, Speed: ptr(12.5), Course: ptr(271.5), Heading: ptr(int64(270)),
				NavStatus: ptr(int64(0)), ShipName: ptr("ALPHA"), RecordedAt: stamp{t0.Add(-time.Minute)},
				FirstSeenAtLocation: &stamp{t0.Add(-time.Minute)}, UpdatedAt: stamp{t0.Add(-time.Minute + 123*time.Microsecond)},
				Name: ptr("ALPHA"), ShipType: ptr(int64(70)), Destination: ptr("SEATTLE"), ETA: &stamp{t0.Add(48 * time.Hour)},
			},
			{MMSI: "222", Lat: 47.6, Lon: -122.4, RecordedAt: stamp{t0.Add(-3 * time.Hour)}, UpdatedAt: stamp{t0.Add(-3 * time.Hour)}},
			// No retained positions: the hub returns an empty track.
			{MMSI: "333", Lat: 49, Lon: -124, RecordedAt: stamp{t0.Add(-30 * 24 * time.Hour)}, UpdatedAt: stamp{t0.Add(-30 * 24 * time.Hour)}},
		},
		points: map[string][]point{
			"111": {
				{Lat: 48.5, Lon: -123.25, Speed: ptr(12.5), Course: ptr(271.5), Heading: ptr(int64(270)), NavStatus: ptr(int64(0)), RecordedAt: stamp{t0.Add(-time.Minute)}},
				{Lat: 48.4, Lon: -123.1, RecordedAt: stamp{t0.Add(-11 * time.Minute)}},
			},
			"222": {{Lat: 47.6, Lon: -122.4, RecordedAt: stamp{t0.Add(-3 * time.Hour)}}},
		},
		cells:    []cell{{9600, -16400, 3}, {9601, -16400, 120}},
		computed: ptr(t0.Add(-20 * time.Minute)),
	}
	return s3, db, st, &clock
}

func publishAt(t *testing.T, st *store, db *fakeDB, clock *time.Time, at time.Time) (*publisher, error) {
	t.Helper()
	*clock, db.now = at, at
	p := &publisher{store: st, now: func() time.Time { return *clock }}
	return p, p.run(context.Background(), db.open)
}

// The "GET Object" and "GET Bucket (List Objects)" examples from the AWS
// SigV4 documentation for S3, signed with the documented example key pair.
func TestAuthorizationMatchesAWSExamples(t *testing.T) {
	for _, tc := range []struct {
		url, rangeHeader, signature string
	}{
		{"https://examplebucket.s3.amazonaws.com/test.txt", "bytes=0-9", "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"},
		{"https://examplebucket.s3.amazonaws.com/?max-keys=2&prefix=J", "", "34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7"},
	} {
		req, err := http.NewRequest(http.MethodGet, tc.url, nil)
		if err != nil {
			t.Fatal(err)
		}
		req.Header.Set("X-Amz-Content-Sha256", "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
		req.Header.Set("X-Amz-Date", "20130524T000000Z")
		signed := []string{"host", "x-amz-content-sha256", "x-amz-date"}
		if tc.rangeHeader != "" {
			req.Header.Set("Range", tc.rangeHeader)
			signed = append(signed, "range")
		}
		got := authorization(req, signed, "20130524T000000Z", "us-east-1", "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
		if want := "Signature=" + tc.signature; !strings.HasSuffix(got, want) {
			t.Errorf("%s: got %s, want %s", tc.url, got, want)
		}
	}
}

func TestStampMatchesPythonIsoformat(t *testing.T) {
	for at, want := range map[time.Time]string{
		t0:                             `"2026-11-19T12:00:00+00:00"`,
		t0.Add(123 * time.Microsecond): `"2026-11-19T12:00:00.000123+00:00"`,
		t0.In(time.FixedZone("PST", -8*3600)).Add(time.Microsecond + 999): `"2026-11-19T12:00:00.000001+00:00"`,
	} {
		got, err := json.Marshal(stamp{at})
		if err != nil || string(got) != want {
			t.Errorf("stamp(%v) = %s, %v; want %s", at, got, err, want)
		}
		var back stamp
		if err := json.Unmarshal(got, &back); err != nil || !back.Equal(at.Truncate(time.Microsecond)) {
			t.Errorf("parse %s = %v, %v", got, back, err)
		}
	}
}

// The expected stops come from the hub's _log_stops in ships/router.py.
func TestLogStopsMatchHub(t *testing.T) {
	hundred := make([]int64, 100)
	for i := range hundred {
		hundred[i] = int64(i + 1)
	}
	for _, tc := range []struct{ counts, want []int64 }{
		{hundred, []int64{1, 3, 6, 16, 40, 100}},
		{[]int64{0, 5}, []int64{1, 2, 3, 4, 5}},
		{[]int64{1, 1, 1}, []int64{1}},
		{nil, []int64{}},
		{[]int64{3, 7, 2, 900, 40, 1, 1, 1, 12, 5000}, []int64{1, 5, 30, 166, 910, 5000}},
		{[]int64{2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 1000}, []int64{1, 4, 16, 63, 251, 1000}},
	} {
		if got := logStops(tc.counts, heatStops); !reflect.DeepEqual(got, tc.want) {
			t.Errorf("logStops(%v) = %v, want %v", tc.counts, got, tc.want)
		}
	}
}

// decode reads a stored object as generic JSON.
func decode(t *testing.T, s3 *fakeS3, key string) map[string]any {
	t.Helper()
	var v map[string]any
	if err := json.Unmarshal(s3.object(t, key).body, &v); err != nil {
		t.Fatal(err)
	}
	return v
}

func fields(v map[string]any) []string {
	keys := make([]string, 0, len(v))
	for key := range v {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return keys
}

func TestProductsSchemaTimestampsAndCacheHeaders(t *testing.T) {
	s3, db, st, clock := newFixture(t)
	if _, err := publishAt(t, st, db, clock, t0.Add(456*time.Microsecond)); err != nil {
		t.Fatal(err)
	}
	top := decode(t, s3, "latest.json")
	if got := fields(top); !reflect.DeepEqual(got, []string{"generated_at", "heat", "schema_version", "snapshot"}) {
		t.Fatalf("manifest fields %v", got)
	}
	if top["generated_at"] != "2026-11-19T12:00:00.000456+00:00" || top["schema_version"] != 1.0 {
		t.Fatalf("manifest %v", top)
	}
	if obj := s3.object(t, "latest.json"); obj.cache != "public, max-age=60" || obj.kind != "application/json" {
		t.Fatalf("manifest headers %q %q", obj.cache, obj.kind)
	}

	snapKey := top["snapshot"].(string)
	snap := decode(t, s3, snapKey)
	if snap["generated_at"] != top["generated_at"] || snap["count"] != 3.0 || snap["schema_version"] != 1.0 {
		t.Fatalf("snapshot header %v", fields(snap))
	}
	vessels := snap["vessels"].([]any)
	first := vessels[0].(map[string]any)
	// The hub's snapshot fields, plus the vessel's track key.
	want := []string{
		"course", "destination", "eta", "first_seen_at_location", "heading", "lat", "lon", "mmsi",
		"name", "nav_status", "recorded_at", "ship_name", "ship_type", "speed", "track", "updated_at",
	}
	if got := fields(first); !reflect.DeepEqual(got, want) {
		t.Fatalf("vessel fields %v", got)
	}
	if first["recorded_at"] != "2026-11-19T11:59:00+00:00" || first["updated_at"] != "2026-11-19T11:59:00.000123+00:00" ||
		first["eta"] != "2026-11-21T12:00:00+00:00" {
		t.Fatalf("vessel timestamps %v", first)
	}
	if third := vessels[2].(map[string]any); third["mmsi"] != "333" || third["track"] != nil || third["eta"] != nil || third["speed"] != nil {
		t.Fatalf("vessel without history %v", third)
	}

	trackKey := first["track"].(string)
	if !strings.HasPrefix(trackKey, "tracks/111-") || !verify(trackKey, s3.object(t, trackKey).body) {
		t.Fatalf("track key %s", trackKey)
	}
	tr := decode(t, s3, trackKey)
	if got := fields(tr); !reflect.DeepEqual(got, []string{"count", "mmsi", "schema_version", "track"}) {
		t.Fatalf("track fields %v", got)
	}
	points := tr["track"].([]any)
	newest := points[0].(map[string]any)
	if tr["count"] != 2.0 || newest["recorded_at"] != "2026-11-19T11:59:00+00:00" ||
		points[1].(map[string]any)["recorded_at"] != "2026-11-19T11:49:00+00:00" {
		t.Fatalf("track %v", tr)
	}
	if got := fields(newest); !reflect.DeepEqual(got, []string{"course", "heading", "lat", "lon", "nav_status", "recorded_at", "speed"}) {
		t.Fatalf("point fields %v", got)
	}

	h := decode(t, s3, top["heat"].(string))
	if got := fields(h); !reflect.DeepEqual(got, []string{"cells", "computed_at", "count", "schema_version", "step_lat", "step_lon", "stops"}) {
		t.Fatalf("heat fields %v", got)
	}
	if h["computed_at"] != "2026-11-19T11:40:00+00:00" || h["step_lat"] != 0.005 || h["step_lon"] != 0.0075 ||
		!reflect.DeepEqual(h["cells"], []any{[]any{9600.0, -16400.0, 3.0}, []any{9601.0, -16400.0, 120.0}}) ||
		!reflect.DeepEqual(h["stops"], []any{1.0, 3.0, 7.0, 18.0, 46.0, 120.0}) {
		t.Fatalf("heat %v", h)
	}

	// Every payload is immutable under a key that holds its digest.
	for _, key := range s3.keys() {
		if key == "latest.json" {
			continue
		}
		obj := s3.object(t, key)
		if !verify(key, obj.body) || obj.cache != "public, max-age=31536000, immutable" || obj.kind != "application/json" {
			t.Errorf("%s: digest %t, Cache-Control %q, Content-Type %q", key, verify(key, obj.body), obj.cache, obj.kind)
		}
	}
	// Tracks, heat and snapshot before the manifest.
	if puts := s3.puts; puts[len(puts)-1] != "latest.json" || puts[len(puts)-2] != snapKey {
		t.Fatalf("write order %v", puts)
	}
}

func TestSnapshotReaderResolvesGeneration(t *testing.T) {
	s3, db, st, clock := newFixture(t)
	if g, err := resolve(context.Background(), st); g != nil || err != nil {
		t.Fatalf("empty bucket resolved to %v, %v", g, err)
	}
	if _, err := publishAt(t, st, db, clock, t0); err != nil {
		t.Fatal(err)
	}
	g, err := resolve(context.Background(), st)
	if err != nil {
		t.Fatal(err)
	}
	if !g.manifest.GeneratedAt.Equal(t0) || len(g.vessels) != 3 || g.vessels["111"].Track == nil || g.vessels["333"].Track != nil {
		t.Fatalf("resolved %+v", g)
	}
	// A reader can fetch every key the generation names, with matching bytes.
	for key := range g.refs() {
		if !verify(key, s3.object(t, key).body) {
			t.Errorf("%s does not match its digest", key)
		}
	}

	// A snapshot that does not match its key is not a generation.
	s3.mu.Lock()
	obj := s3.objects[g.manifest.Snapshot]
	obj.body = append(obj.body, ' ')
	s3.objects[g.manifest.Snapshot] = obj
	s3.mu.Unlock()
	if _, err := resolve(context.Background(), st); !errors.Is(err, errIncomplete) {
		t.Fatalf("tampered snapshot: %v", err)
	}
	// The next run rebuilds every track and publishes a complete generation.
	p, err := publishAt(t, st, db, clock, t0.Add(2*time.Minute))
	if err != nil || !p.stats.full {
		t.Fatalf("run after a broken generation: full=%t, %v", p.stats.full, err)
	}
	if _, err := resolve(context.Background(), st); err != nil {
		t.Fatal(err)
	}
}

func TestChangedVesselsOnlyRebuildTheirTracks(t *testing.T) {
	s3, db, st, clock := newFixture(t)
	p, err := publishAt(t, st, db, clock, t0)
	if err != nil || !p.stats.full || !reflect.DeepEqual(db.asked, []string{"111", "222", "333"}) {
		t.Fatalf("first run: full=%t asked=%v, %v", p.stats.full, db.asked, err)
	}
	first, _ := resolve(context.Background(), st)

	// Vessel 111 reports again. Only its track is read and written.
	db.rows[0].UpdatedAt = stamp{t0.Add(time.Minute)}
	db.points["111"] = append([]point{{Lat: 48.6, Lon: -123.3, RecordedAt: stamp{t0.Add(time.Minute)}}}, db.points["111"]...)
	before := len(s3.puts)
	p, err = publishAt(t, st, db, clock, t0.Add(2*time.Minute))
	if err != nil || p.stats.full || !reflect.DeepEqual(db.asked, []string{"111"}) {
		t.Fatalf("second run: full=%t asked=%v, %v", p.stats.full, db.asked, err)
	}
	second, _ := resolve(context.Background(), st)
	if *second.vessels["111"].Track == *first.vessels["111"].Track || *second.vessels["222"].Track != *first.vessels["222"].Track {
		t.Fatal("track keys did not follow the changed vessel")
	}
	// The new track, the snapshot and the manifest. Heat is unchanged.
	if got := s3.puts[before:]; len(got) != 3 || !strings.HasPrefix(got[0], "tracks/111-") {
		t.Fatalf("second run wrote %v", got)
	}

	// The first run of a new hour reads every track. History of vessel 222
	// expired, so its track key goes; unchanged tracks are not written again.
	delete(db.points, "222")
	before = len(s3.puts)
	p, err = publishAt(t, st, db, clock, t0.Add(time.Hour))
	if err != nil || !p.stats.full || len(db.asked) != 3 {
		t.Fatalf("hourly run: full=%t asked=%v, %v", p.stats.full, db.asked, err)
	}
	third, _ := resolve(context.Background(), st)
	if third.vessels["222"].Track != nil || *third.vessels["111"].Track != *second.vessels["111"].Track {
		t.Fatalf("hourly run tracks %v %v", third.vessels["222"].Track, third.vessels["111"].Track)
	}
	if got := s3.puts[before:]; len(got) != 2 {
		t.Fatalf("hourly run wrote %v", got)
	}
}

// An interrupted run leaves the previous manifest, and every object it names,
// in place. The next run completes the new generation.
func TestInterruptedPublishKeepsCompleteGeneration(t *testing.T) {
	s3, db, st, clock := newFixture(t)
	if _, err := publishAt(t, st, db, clock, t0); err != nil {
		t.Fatal(err)
	}
	first, _ := resolve(context.Background(), st)
	db.rows[0].UpdatedAt = stamp{t0.Add(time.Minute)}
	db.points["111"] = db.points["111"][:1]
	db.cells = append(db.cells, cell{9602, -16400, 7})
	for writes := range 3 {
		// The store fails after 0, 1 and 2 more writes. The third run writes
		// everything except the manifest.
		s3.failPutAfter = len(s3.puts) + writes
		if _, err := publishAt(t, st, db, clock, t0.Add(2*time.Minute)); err == nil {
			t.Fatalf("run with %d writes succeeded", writes)
		}
		g, err := resolve(context.Background(), st)
		if err != nil || g.manifest.Snapshot != first.manifest.Snapshot || g.manifest.Heat != first.manifest.Heat {
			t.Fatalf("after %d writes the manifest is %+v, %v", writes, g.manifest, err)
		}
		for key := range g.refs() {
			if !verify(key, s3.object(t, key).body) {
				t.Fatalf("%s does not match its digest", key)
			}
		}
	}
	s3.failPutAfter = -1
	if _, err := publishAt(t, st, db, clock, t0.Add(4*time.Minute)); err != nil {
		t.Fatal(err)
	}
	g, err := resolve(context.Background(), st)
	if err != nil || !g.manifest.GeneratedAt.Equal(t0.Add(4*time.Minute)) {
		t.Fatalf("recovery: %+v, %v", g, err)
	}
	for key := range g.refs() {
		if !verify(key, s3.object(t, key).body) {
			t.Fatalf("%s does not match its digest", key)
		}
	}
}

func TestExpiredKeepsWhatReadersCanStillHold(t *testing.T) {
	key := func(name string, n int) string { return name + "-" + strings.Repeat(string(rune('a'+n)), 64) + ".json" }
	cutoff := t0.Add(-grace)
	before := func(d time.Duration) time.Time { return cutoff.Add(-d) }
	objects := map[string]time.Time{
		"latest.json":        before(time.Hour),
		"notes.txt":          before(time.Hour), // not the writer's key
		key("snapshot", 0):   before(20 * time.Minute),
		key("snapshot", 1):   before(10 * time.Minute), // current at cutoff
		key("snapshot", 2):   cutoff.Add(time.Minute),  // previous
		key("heat", 0):       before(time.Hour),        // current
		key("tracks/111", 0): before(time.Hour),
		key("tracks/111", 1): before(time.Minute), // current at cutoff
		key("tracks/111", 2): cutoff.Add(time.Minute),
		key("tracks/222", 0): before(48 * time.Hour), // unchanged, current
		key("tracks/333", 0): before(time.Hour),      // history expired
		key("tracks/333", 1): before(time.Minute),
		key("tracks/444", 0): cutoff.Add(time.Minute), // recent orphan
	}
	refs := map[string]bool{
		key("snapshot", 2): true, key("snapshot", 3): true, key("heat", 0): true,
		key("tracks/111", 3): true, key("tracks/222", 0): true,
	}
	want := []string{key("snapshot", 0), key("tracks/111", 0), key("tracks/333", 0), key("tracks/333", 1)}
	if got := expired(objects, refs, cutoff); !reflect.DeepEqual(got, want) {
		t.Fatalf("expired %v\nwant %v", got, want)
	}
}

// Over time the bucket keeps each replaced generation for the grace period
// after its replacement, then drops it.
func TestReplacedGenerationsLeaveAfterGrace(t *testing.T) {
	s3, db, st, clock := newFixture(t)
	move := func(at time.Time, lat float64) {
		db.rows[0].UpdatedAt = stamp{at}
		db.points["111"] = append([]point{{Lat: lat, Lon: -123, RecordedAt: stamp{at}}}, db.points["111"]...)
	}
	if _, err := publishAt(t, st, db, clock, t0.Add(time.Minute)); err != nil {
		t.Fatal(err)
	}
	g1, _ := resolve(context.Background(), st)
	move(t0.Add(2*time.Minute), 48.7)
	if _, err := publishAt(t, st, db, clock, t0.Add(3*time.Minute)); err != nil {
		t.Fatal(err)
	}
	g2, _ := resolve(context.Background(), st)
	move(t0.Add(39*time.Minute), 48.8)
	if p, err := publishAt(t, st, db, clock, t0.Add(40*time.Minute)); err != nil || p.stats.deletes != 2 {
		t.Fatalf("deletes %d, %v", p.stats.deletes, err)
	}
	keys := s3.keys()
	// The first generation's snapshot and moved track left.
	if slices.Contains(keys, g1.manifest.Snapshot) || slices.Contains(keys, *g1.vessels["111"].Track) {
		t.Fatalf("first generation still in %v", keys)
	}
	// The second generation was current until now, so it stays for the grace period.
	for key := range g2.refs() {
		if !slices.Contains(keys, key) {
			t.Fatalf("second generation lost %s", key)
		}
	}
	if _, err := publishAt(t, st, db, clock, t0.Add(69*time.Minute)); err != nil {
		t.Fatal(err)
	}
	if !slices.Contains(s3.keys(), g2.manifest.Snapshot) {
		t.Fatal("second snapshot left before its grace period ended")
	}
	if _, err := publishAt(t, st, db, clock, t0.Add(71*time.Minute)); err != nil {
		t.Fatal(err)
	}
	if slices.Contains(s3.keys(), g2.manifest.Snapshot) {
		t.Fatal("second snapshot stayed after its grace period")
	}
	// The vessel without history never had a track; 222 keeps its old key.
	g, _ := resolve(context.Background(), st)
	if *g.vessels["222"].Track != *g1.vessels["222"].Track || !slices.Contains(s3.keys(), *g.vessels["222"].Track) {
		t.Fatal("an unchanged track left the bucket")
	}
}
