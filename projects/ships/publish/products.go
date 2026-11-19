package publish

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"math"
	"regexp"
	"sort"
	"time"
)

const (
	// schemaVersion changes when a reader must change to read a product.
	schemaVersion = 1
	// trackLimit is the hub's default track length.
	trackLimit = 1000
	// The heat grid steps match the ingester's maintenance SQL.
	heatStepLat = 0.005
	heatStepLon = 0.0075
	// hotPercentile anchors the top colour stop, as in the hub.
	hotPercentile = 0.99
	heatStops     = 6

	manifestKey = "latest.json"
	// Payload keys hold their SHA-256, so a key never changes its bytes.
	immutable = "public, max-age=31536000, immutable"
	// The writer replaces the manifest every cycle.
	shortCache = "public, max-age=60"
)

// stamp is a UTC time that encodes like Python's datetime.isoformat, as the
// hub API does: microseconds only when they are not zero, and +00:00.
type stamp struct{ time.Time }

func (s stamp) MarshalJSON() ([]byte, error) {
	t := s.UTC().Truncate(time.Microsecond)
	layout := "2006-01-02T15:04:05.000000+00:00"
	if t.Nanosecond() == 0 {
		layout = "2006-01-02T15:04:05+00:00"
	}
	return json.Marshal(t.Format(layout))
}

func (s *stamp) UnmarshalJSON(data []byte) error {
	var text string
	if err := json.Unmarshal(data, &text); err != nil {
		return err
	}
	t, err := time.Parse(time.RFC3339Nano, text)
	s.Time = t.UTC()
	return err
}

func stampOf(t *time.Time) *stamp {
	if t == nil {
		return nil
	}
	return &stamp{*t}
}

// vessel is one snapshot entry. The fields match the hub's snapshot API.
// Track is the key of the vessel's track, or null without retained history.
type vessel struct {
	MMSI                string   `json:"mmsi"`
	Lat                 float64  `json:"lat"`
	Lon                 float64  `json:"lon"`
	Speed               *float64 `json:"speed"`
	Course              *float64 `json:"course"`
	Heading             *int64   `json:"heading"`
	NavStatus           *int64   `json:"nav_status"`
	ShipName            *string  `json:"ship_name"`
	RecordedAt          stamp    `json:"recorded_at"`
	FirstSeenAtLocation *stamp   `json:"first_seen_at_location"`
	UpdatedAt           stamp    `json:"updated_at"`
	Name                *string  `json:"name"`
	ShipType            *int64   `json:"ship_type"`
	Destination         *string  `json:"destination"`
	ETA                 *stamp   `json:"eta"`
	Track               *string  `json:"track"`
}

type snapshot struct {
	Version     int      `json:"schema_version"`
	GeneratedAt stamp    `json:"generated_at"`
	Count       int      `json:"count"`
	Vessels     []vessel `json:"vessels"`
}

// point is one track position. The fields match the hub's track API.
type point struct {
	Lat        float64  `json:"lat"`
	Lon        float64  `json:"lon"`
	Speed      *float64 `json:"speed"`
	Course     *float64 `json:"course"`
	Heading    *int64   `json:"heading"`
	NavStatus  *int64   `json:"nav_status"`
	RecordedAt stamp    `json:"recorded_at"`
}

// track holds the newest positions first. It carries no generation time, so
// an unchanged history keeps its key.
type track struct {
	Version int     `json:"schema_version"`
	MMSI    string  `json:"mmsi"`
	Count   int     `json:"count"`
	Track   []point `json:"track"`
}

// cell is [lat_bin, lon_bin, vessel-days].
type cell [3]int64

// heat is the hub's heat grid. ComputedAt is the time of the newest rollup,
// so the key changes only when a rollup changes the grid.
type heat struct {
	Version    int     `json:"schema_version"`
	ComputedAt *stamp  `json:"computed_at"`
	StepLat    float64 `json:"step_lat"`
	StepLon    float64 `json:"step_lon"`
	Count      int     `json:"count"`
	Cells      []cell  `json:"cells"`
	Stops      []int64 `json:"stops"`
}

// manifest is the root index. The writer replaces it last.
type manifest struct {
	Version     int    `json:"schema_version"`
	GeneratedAt stamp  `json:"generated_at"`
	Snapshot    string `json:"snapshot"`
	Heat        string `json:"heat"`
}

func newHeat(computedAt *time.Time, cells []cell) heat {
	if cells == nil {
		cells = []cell{}
	}
	counts := make([]int64, len(cells))
	for i, c := range cells {
		counts[i] = c[2]
	}
	return heat{
		Version: schemaVersion, ComputedAt: stampOf(computedAt),
		StepLat: heatStepLat, StepLon: heatStepLon,
		Count: len(cells), Cells: cells, Stops: logStops(counts, heatStops),
	}
}

// logStops ports the hub's colour stops: up to n ascending values spaced
// logarithmically from 1 to the 99th percentile cell count.
func logStops(counts []int64, n int) []int64 {
	var values []int64
	for _, c := range counts {
		if c > 0 {
			values = append(values, c)
		}
	}
	out := []int64{}
	if len(values) == 0 {
		return out
	}
	sort.Slice(values, func(i, j int) bool { return values[i] < values[j] })
	high := values[min(len(values)-1, int(hotPercentile*float64(len(values))))]
	if high <= 1 {
		return []int64{1}
	}
	logHigh := math.Log(float64(high))
	for i := range n {
		// Python's round() rounds a tie to the even value.
		v := int64(math.RoundToEven(math.Exp(logHigh * float64(i) / float64(n-1))))
		if len(out) == 0 || v > out[len(out)-1] {
			out = append(out, v)
		}
	}
	return out
}

var digestKey = regexp.MustCompile(`^(snapshot|heat|tracks/[0-9]{1,15})-([0-9a-f]{64})\.json$`)

// validMMSI limits track keys to MMSIs that are safe in an object key.
var validMMSI = regexp.MustCompile(`^[0-9]{1,15}$`)

// address returns the key of a payload: its series name and SHA-256.
func address(name string, body []byte) string {
	return name + "-" + digest(body) + ".json"
}

// digest returns the hex SHA-256 of body.
func digest(body []byte) string {
	sum := sha256.Sum256(body)
	return hex.EncodeToString(sum[:])
}

// series returns the series of a payload key: snapshot, heat or one vessel's
// tracks. It returns "" for a key the writer does not own.
func series(key string) string {
	m := digestKey.FindStringSubmatch(key)
	if m == nil {
		return ""
	}
	return m[1]
}

// verify reports whether body has the digest that its key names.
func verify(key string, body []byte) bool {
	m := digestKey.FindStringSubmatch(key)
	return m != nil && address(m[1], body) == key
}

func trackName(mmsi string) string { return "tracks/" + mmsi }
