package main

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"sort"
	"sync"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/r2"
)

type bucket struct {
	*r2.Client
	name string
}

func connect(prefix string) (bucket, error) {
	val := func(k string) string { return os.Getenv(prefix + k) }
	c, err := r2.New(val("AWS_ENDPOINT_URL"), val("AWS_REGION"), r2.Credentials{AccessKeyID: val("AWS_ACCESS_KEY_ID"), SecretAccessKey: val("AWS_SECRET_ACCESS_KEY")}, nil)
	return bucket{c, val("BUCKET")}, err
}

func (b bucket) read(ctx context.Context, key string, v any) error {
	obj, err := b.Get(ctx, b.name, key, "", 64<<20)
	if err != nil {
		return err
	}
	return json.Unmarshal(obj.Body, v)
}

func (b bucket) write(ctx context.Context, key string, v any) (string, error) {
	data, err := json.Marshal(v)
	if err != nil {
		return "", err
	}
	return key, b.Put(ctx, b.name, key, data, "public, max-age=60")
}

// Encode one site at a time. Spooling also gives SigV4 the exact length
// and digest without retaining a second full forecast on the heap.
func dataset[T any](ctx context.Context, b bucket, name string, meta map[string]any, rows []T) (string, error) {
	file, err := os.CreateTemp("", "stars-*.json")
	if err != nil {
		return "", err
	}
	defer os.Remove(file.Name())
	defer file.Close()
	hash := sha256.New()
	w := bufio.NewWriter(io.MultiWriter(file, hash))
	head, err := json.Marshal(meta)
	if err != nil {
		return "", err
	}
	w.Write(bytes.TrimSuffix(head, []byte("}")))
	if len(meta) > 0 {
		w.WriteByte(',')
	}
	w.WriteString(`"sites":[`)
	enc := json.NewEncoder(w)
	for i := range rows {
		if i > 0 {
			w.WriteByte(',')
		}
		if err := enc.Encode(rows[i]); err != nil {
			return "", err
		}
	}
	w.WriteString("]}")
	if err := w.Flush(); err != nil {
		return "", err
	}
	size, err := file.Seek(0, io.SeekCurrent)
	if err != nil {
		return "", err
	}
	if _, err = file.Seek(0, io.SeekStart); err != nil {
		return "", err
	}
	digest := fmt.Sprintf("%x", hash.Sum(nil))
	key := "stars/" + name + "-" + digest + ".json"
	return key, b.PutReader(ctx, b.name, key, io.LimitReader(file, size), size, digest, "public, max-age=31536000, immutable")
}

type counts struct {
	Dark     int `json:"clear_dark_hours"`
	Twilight int `json:"clear_twilight_hours"`
}
type window struct {
	Time  string  `json:"time"`
	Cloud float64 `json:"cloud_area_fraction"`
	Dark  bool    `json:"dark"`
}
type result struct {
	site
	counts
	Fetched *string  `json:"forecast_fetched_at"`
	Best    []window `json:"best_hours"`
}
type (
	mapSite struct {
		location
		counts
	}
	historySite struct {
		location
		Clear [12]int `json:"clear"`
		Dark  [12]int `json:"dark"`
	}
	month struct {
		ID    string `json:"site_id"`
		Month int
		Dark  int `json:"dark_hours"`
		Clear int `json:"clear_dark_hours"`
	}
)

func total(a [12]int) int {
	n := 0
	for _, v := range a {
		n += v
	}
	return n
}

func history(grid []site, months []month) []historySite {
	byID := map[string]*historySite{}
	for _, s := range grid {
		byID[s.ID] = &historySite{location: s.location}
	}
	for _, m := range months {
		s := byID[m.ID]
		if s != nil && m.Month >= 1 && m.Month <= 12 {
			s.Clear[m.Month-1] += m.Clear
			s.Dark[m.Month-1] += m.Dark
		}
	}
	out := []historySite{}
	for _, s := range grid {
		h := byID[s.ID]
		if total(h.Dark) > 0 {
			out = append(out, *h)
		}
	}
	sort.SliceStable(out, func(i, j int) bool { return total(out[i].Clear) > total(out[j].Clear) })
	return out
}

func refresh(sites []site, cache map[string]forecast, ticks <-chan time.Time, client *http.Client) int {
	var wg sync.WaitGroup
	hoursBySite := make([][]hour, len(sites))
	fetched := make([]bool, len(sites))
	workers := make(chan struct{}, 15)
	for i, s := range sites {
		workers <- struct{}{}
		<-ticks
		wg.Add(1)
		go func(i int, s site) {
			defer wg.Done()
			defer func() { <-workers }()
			hours, err := fetch(client, s)
			if err != nil {
				return
			}
			hoursBySite[i] = hours
			fetched[i] = true
		}(i, s)
	}
	wg.Wait()
	now := time.Now().UTC().Truncate(time.Microsecond)
	layout := "2006-01-02T15:04:05.000000+00:00"
	if now.Nanosecond() == 0 {
		layout = "2006-01-02T15:04:05+00:00"
	}
	stamp := now.Format(layout)
	ok := 0
	for i, s := range sites {
		if fetched[i] {
			cache[s.ID] = forecast{stamp, hoursBySite[i]}
			ok++
		}
	}
	return ok
}

const shards = 64

func shardKey(i int) string { return fmt.Sprintf("forecast/%02d.json", i) }
func prune(ctx context.Context, state bucket, cutoff time.Time) error {
	for j := 0; j < shards; j++ {
		key := shardKey(j)
		obj, err := state.Get(ctx, state.name, key, "", 64<<20)
		if errors.Is(err, r2.ErrNotFound) {
			continue
		}
		if err != nil {
			return err
		}
		var cache map[string]forecast
		if err = json.Unmarshal(obj.Body, &cache); err != nil {
			return err
		}
		changed := false
		for id, f := range cache {
			i := sort.Search(len(f.Hours), func(i int) bool { return !f.Hours[i].Time.Before(cutoff) })
			if i > 0 {
				changed = true
				f.Hours = f.Hours[i:]
				cache[id] = f
			}
		}
		if changed {
			data, err := json.Marshal(cache)
			if err != nil {
				return err
			}
			if err = state.Replace(ctx, state.name, key, data, obj.ETag); err != nil {
				return err
			}
		}
	}
	return nil
}

func run(ctx context.Context, mode string) error {
	state, err := connect("STATE_")
	if err != nil {
		return err
	}
	if mode == "grid" || mode == "climatology" {
		source, err := connect("SOURCE_")
		if err != nil {
			return err
		}
		key := mode + ".json"
		var rows []json.RawMessage
		if err := source.read(ctx, key, &rows); err != nil {
			return err
		}
		if len(rows) == 0 {
			return fmt.Errorf("empty %s input", mode)
		}
		_, err = state.write(ctx, key, rows)
		return err
	}
	if mode == "prune" {
		return prune(ctx, state, time.Now().UTC().Truncate(time.Hour))
	}
	if mode != "refresh" {
		return fmt.Errorf("unknown mode %q", mode)
	}
	public, err := connect("PUBLIC_")
	if err != nil {
		return err
	}
	var grid []site
	var months []month
	if err = state.read(ctx, "grid.json", &grid); err != nil {
		return err
	}
	if err = state.read(ctx, "climatology.json", &months); err != nil {
		return err
	}
	if len(grid) == 0 || len(months) == 0 {
		return fmt.Errorf("empty Stars inputs")
	}
	groups := [shards][]site{}
	positions := map[string]int{}
	for i, s := range grid {
		if _, ok := positions[s.ID]; s.ID == "" || ok {
			return fmt.Errorf("missing or duplicate grid id")
		}
		positions[s.ID] = i
		if s.Zone == "" {
			s.Zone = "unknown"
		}
		j := sha256.Sum256([]byte(s.ID))[0] % shards
		groups[j] = append(groups[j], s)
	}
	now := time.Now().UTC()
	cutoff := now.Truncate(time.Hour)
	client := &http.Client{Timeout: 30 * time.Second}
	ticker := time.NewTicker(time.Second / 15)
	defer ticker.Stop()
	sites := make([]result, len(grid))
	darkness := "none"
	var latest *string
	successes := 0
	for j, group := range groups {
		if len(group) == 0 {
			continue
		}
		key := shardKey(j)
		cache := map[string]forecast{}
		if err = state.read(ctx, key, &cache); err != nil && !errors.Is(err, r2.ErrNotFound) {
			return err
		}
		for id := range cache {
			if _, ok := positions[id]; !ok {
				delete(cache, id)
			}
		}
		n := refresh(group, cache, ticker.C, client)
		successes += n
		if n > 0 {
			if _, err = state.write(ctx, key, cache); err != nil {
				return err
			}
		}
		for _, s := range group {
			f := cache[s.ID]
			r := result{site: s, Best: []window{}}
			if f.Fetched != "" {
				r.Fetched = &f.Fetched
				if latest == nil || f.Fetched > *latest {
					latest = r.Fetched
				}
			}
			for _, h := range f.Hours {
				if h.Time.Before(cutoff) || h.Sun >= -10 {
					continue
				}
				dark := h.Sun < -12
				if dark {
					darkness = "astronomical"
				} else if darkness == "none" {
					darkness = "twilight"
				}
				if h.Cloud >= 10 {
					continue
				}
				r.Twilight++
				if dark {
					r.Dark++
				}
				if len(r.Best) < 25 {
					r.Best = append(r.Best, window{h.Time.UTC().Format("2006-01-02T15:04:05+00:00"), h.Cloud, dark})
				}
			}
			sites[positions[s.ID]] = r
		}
	}
	log.Printf("fetched forecasts for %d/%d sites", successes, len(grid))
	if latest == nil || successes == 0 {
		return fmt.Errorf("no successful forecasts")
	}
	sort.SliceStable(sites, func(i, j int) bool {
		a, b := sites[i], sites[j]
		if a.Dark != b.Dark {
			return a.Dark > b.Dark
		}
		return a.Twilight > b.Twilight
	})
	maps := make([]mapSite, len(sites))
	for i, s := range sites {
		maps[i] = mapSite{s.location, s.counts}
	}
	files := map[string]string{}
	payload := map[string]any{"total_sites": len(sites), "count": len(sites), "darkness": darkness, "fetched_at": latest}
	files["sites"], err = dataset(ctx, public, "sites", payload, sites)
	if err != nil {
		return err
	}
	delete(payload, "total_sites")

	files["map"], err = dataset(ctx, public, "map", payload, maps)
	if err != nil {
		return err
	}
	files["historyMap"], err = dataset(ctx, public, "history-map", map[string]any{}, history(grid, months))
	if err != nil {
		return err
	}
	_, err = public.write(ctx, "latest.json", map[string]any{"files": files})
	return err
}

func main() {
	if len(os.Args) != 2 {
		log.Fatal("expected grid, climatology, refresh, or prune")
	}
	if err := run(context.Background(), os.Args[1]); err != nil {
		log.Fatal(err)
	}
}
