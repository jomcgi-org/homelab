// Package analytics reads a single bounded Cloudflare Analytics window. It has
// no public handler and no access to origin request counts or R2 credentials.
package analytics

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"math"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"

	"github.com/prometheus/client_golang/prometheus"
)

const (
	Endpoint     = "https://api.cloudflare.com/client/v4/graphql"
	PollInterval = 5 * time.Minute
	Window       = 5 * time.Minute
	Lag          = 2 * time.Minute
	MaxAge       = 8 * time.Minute
	MaxResponse  = 64 << 10
	GroupLimit   = 16
)

type Config struct {
	Enabled                         bool
	Zone, Hostname, PlanPath, Token string
}

func (c Config) Validate() error {
	if !c.Enabled {
		return nil
	}
	if len(c.Zone) != 32 || strings.Trim(c.Zone, "0123456789abcdef") != "" {
		return errors.New("analytics requires a zone ID")
	}
	u, err := url.Parse("https://" + c.Hostname)
	if err != nil || u.Hostname() != c.Hostname || u.Port() != "" || u.Path != "" || c.Hostname == "" {
		return errors.New("analytics requires a bare hostname")
	}
	if !strings.HasPrefix(c.PlanPath, "/") || !strings.HasSuffix(c.PlanPath, "/v1/plan") || strings.ContainsAny(c.PlanPath, "?%#\\") || strings.Contains(c.PlanPath, "..") || strings.Contains(c.PlanPath, "//") {
		return errors.New("analytics requires an exact connector plan path")
	}
	if strings.TrimSpace(c.Token) == "" {
		return errors.New("analytics requires an independently configured read token")
	}
	return nil
}

// Observation is an adaptive-sampling estimate for GET eyeball plan requests.
// Eligible excludes dynamic, bypass and none/unknown cache states. Hits includes
// hit, stale and updating (edge-served bodies); revalidated remains in the
// denominator only. Counts are already extrapolated by Cloudflare.
type Observation struct {
	Hits, Eligible, Ratio  float64
	Start, End, ObservedAt time.Time
}

type Reader struct {
	cfg      Config
	client   *http.Client
	endpoint string
	pollMu   sync.Mutex
	mu       sync.RWMutex
	last     *Observation
	descs    []*prometheus.Desc
}

func New(c Config, transport http.RoundTripper) (*Reader, error) {
	if err := c.Validate(); err != nil {
		return nil, err
	}
	r := &Reader{cfg: c, endpoint: Endpoint, client: &http.Client{Transport: transport, Timeout: 10 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}}
	for _, name := range []string{"hits", "eligible", "ratio", "window_start_seconds", "window_end_seconds", "observed_at_seconds", "sampled_estimate"} {
		r.descs = append(r.descs, prometheus.NewDesc("scotland_connector_cache_"+name, "Cloudflare GET plan window estimate; aggregate replicas with max, never sum. Sampling rate is unknown.", []string{"hostname", "path"}, nil))
	}
	return r, nil
}

// Current checks freshness at consumption time, including between failed polls.
func (r *Reader) Current(now time.Time) (Observation, bool) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	if r.last == nil || now.Before(r.last.ObservedAt) || now.Sub(r.last.ObservedAt) > MaxAge || now.Sub(r.last.End) > MaxAge+Lag {
		return Observation{}, false
	}
	return *r.last, true
}

func (r *Reader) Describe(ch chan<- *prometheus.Desc) {
	for _, d := range r.descs {
		ch <- d
	}
}
func (r *Reader) Collect(ch chan<- prometheus.Metric) {
	o, ok := r.Current(time.Now())
	if !ok {
		return
	}
	values := []float64{o.Hits, o.Eligible, o.Ratio, float64(o.Start.Unix()), float64(o.End.Unix()), float64(o.ObservedAt.Unix()), 1}
	for i, v := range values {
		ch <- prometheus.MustNewConstMetric(r.descs[i], prometheus.GaugeValue, v, r.cfg.Hostname, r.cfg.PlanPath)
	}
}

// Poll serializes callers and discards the previous success before every read.
// Failure bodies and tokens never enter returned errors or logs.
func (r *Reader) Poll(ctx context.Context, now time.Time) error {
	if !r.cfg.Enabled {
		return nil
	}
	if !r.pollMu.TryLock() {
		return errors.New("analytics poll already running")
	}
	defer r.pollMu.Unlock()
	r.mu.Lock()
	r.last = nil
	r.mu.Unlock()
	end := now.UTC().Truncate(PollInterval).Add(-Lag)
	start := end.Add(-Window)
	filter := map[string]any{"datetime_geq": start.Format(time.RFC3339), "datetime_lt": end.Format(time.RFC3339), "clientRequestHTTPHost": r.cfg.Hostname, "clientRequestPath": r.cfg.PlanPath, "clientRequestHTTPMethodName": "GET", "requestSource": "eyeball"}
	// No time/path grouping: the only dimension is the finite cache status set.
	// Reaching limit is treated as truncation, with no pagination or retry.
	query := `query ConnectorCache($zoneTag: string, $filter: ZoneHttpRequestsAdaptiveGroupsFilter_InputObject) { viewer { zones(filter: {zoneTag: $zoneTag}) { httpRequestsAdaptiveGroups(limit: 16, filter: $filter) { count dimensions { cacheStatus } } } } }`
	body, _ := json.Marshal(map[string]any{"query": query, "variables": map[string]any{"zoneTag": r.cfg.Zone, "filter": filter}})
	rctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	req, _ := http.NewRequestWithContext(rctx, http.MethodPost, r.endpoint, bytes.NewReader(body))
	req.Header.Set("Authorization", "Bearer "+r.cfg.Token)
	req.Header.Set("Content-Type", "application/json")
	res, err := r.client.Do(req)
	if err != nil {
		return errors.New("analytics request unavailable")
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusOK {
		return errors.New("analytics request denied or unavailable")
	}
	b, err := io.ReadAll(io.LimitReader(res.Body, MaxResponse+1))
	if err != nil || len(b) > MaxResponse {
		return errors.New("analytics response incomplete or oversized")
	}
	o, err := decode(b)
	if err != nil {
		return err
	}
	o.Start, o.End, o.ObservedAt = start, end, now
	r.mu.Lock()
	r.last = &o
	r.mu.Unlock()
	return nil
}

func decode(b []byte) (Observation, error) {
	var payload struct {
		Errors json.RawMessage `json:"errors"`
		Data   *struct {
			Viewer *struct {
				Zones []struct {
					Groups []struct {
						Count      *float64 `json:"count"`
						Dimensions *struct {
							Status string `json:"cacheStatus"`
						} `json:"dimensions"`
					} `json:"httpRequestsAdaptiveGroups"`
				} `json:"zones"`
			} `json:"viewer"`
		} `json:"data"`
	}
	unknown := errors.New("analytics observation unknown")
	if json.Unmarshal(b, &payload) != nil || (len(payload.Errors) > 0 && string(payload.Errors) != "null" && string(payload.Errors) != "[]") || payload.Data == nil || payload.Data.Viewer == nil || len(payload.Data.Viewer.Zones) != 1 {
		return Observation{}, unknown
	}
	groups := payload.Data.Viewer.Zones[0].Groups
	if len(groups) == 0 || len(groups) >= GroupLimit {
		return Observation{}, unknown
	}
	o := Observation{}
	seen := map[string]bool{}
	for _, g := range groups {
		if g.Count == nil || g.Dimensions == nil || math.IsNaN(*g.Count) || math.IsInf(*g.Count, 0) || *g.Count < 0 {
			return Observation{}, unknown
		}
		s := g.Dimensions.Status
		if seen[s] {
			return Observation{}, unknown
		}
		seen[s] = true
		switch s {
		case "hit", "stale", "updating":
			o.Hits += *g.Count
			o.Eligible += *g.Count
		case "miss", "expired", "revalidated":
			o.Eligible += *g.Count
		case "dynamic", "bypass", "none", "unknown", "none/unknown":
		default:
			return Observation{}, unknown
		}
	}
	if o.Eligible <= 0 || math.IsInf(o.Eligible, 0) {
		return Observation{}, unknown
	}
	o.Ratio = o.Hits / o.Eligible
	return o, nil
}

// Run has no overlapping polls. The chart requires a singleton Recreate
// deployment while analytics is enabled. Window counts are gauges, not usage
// increments, and must never be added across time or replicas.
func (r *Reader) Run(ctx context.Context) {
	if !r.cfg.Enabled {
		return
	}
	for {
		_ = r.Poll(ctx, time.Now())
		timer := time.NewTimer(PollInterval)
		select {
		case <-ctx.Done():
			timer.Stop()
			return
		case <-timer.C:
		}
	}
}
