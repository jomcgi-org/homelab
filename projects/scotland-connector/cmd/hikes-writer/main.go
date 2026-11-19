package main

import (
	"bufio"
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"sort"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/r2"
)

type snapshot struct {
	Version   int        `json:"schema_version"`
	Generated time.Time  `json:"generated_at"`
	Fetched   *time.Time `json:"fetched_at"`
	Walks     []walk     `json:"walks"`
}
type manifest struct {
	Walks string `json:"walks"`
}

var client = &http.Client{Timeout: 30 * time.Second}

func fetch(url string, consume func(io.Reader) error) error {
	req, err := http.NewRequest("GET", url, nil)
	if err != nil {
		return err
	}
	req.Header.Set("User-Agent", "jomcgi.dev Hikes (https://github.com/jomcgi-org/homelab)")
	res, err := client.Do(req)
	if err != nil {
		return err
	}
	defer res.Body.Close()
	if res.StatusCode != 200 {
		return fmt.Errorf("HTTP %d", res.StatusCode)
	}
	return consume(io.LimitReader(res.Body, 16<<20))
}

// seedPath matches the site's content-named copy of the hub walk snapshot.
// The site names it by the first 12 hex digits of its SHA-256.
var seedPath = regexp.MustCompile(`^/data/hikes/v/walks-([0-9a-f]{12})\.json$`)

// download reads one public HTTPS object of at most limit bytes.
func download(hc *http.Client, address string, limit int64) ([]byte, error) {
	req, err := http.NewRequest("GET", address, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("User-Agent", "jomcgi.dev Hikes (https://github.com/jomcgi-org/homelab)")
	res, err := hc.Do(req)
	if err != nil {
		return nil, err
	}
	defer res.Body.Close()
	if res.StatusCode != 200 {
		return nil, fmt.Errorf("%s: HTTP %d", address, res.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(res.Body, limit+1))
	if err == nil && int64(len(body)) > limit {
		err = fmt.Errorf("%s: more than %d bytes", address, limit)
	}
	return body, err
}

// seed reads the hub's schema-v2 walk snapshot from the public copy that the
// jomcgi.dev site publishes. The site copies the hub's stars/walks.json
// bytes unchanged, so the hook needs no R2 credential. Its walk IDs and
// last-good forecasts stay for routes that the first catalogue cannot fetch.
func seed(hc *http.Client, manifestURL string) (snapshot, error) {
	var data snapshot
	base, err := url.Parse(manifestURL)
	if err != nil {
		return data, err
	}
	if base.Scheme != "https" {
		return data, fmt.Errorf("seed %s: need an https URL", manifestURL)
	}
	body, err := download(hc, manifestURL, 64<<10)
	if err != nil {
		return data, err
	}
	var index struct {
		Files struct {
			Walks string `json:"walks"`
		} `json:"files"`
		Sources struct {
			Walks json.RawMessage `json:"walks"`
		} `json:"sources"`
	}
	if err = json.Unmarshal(body, &index); err != nil {
		return data, fmt.Errorf("seed %s: %w", manifestURL, err)
	}
	// A same-origin path only, so the walks file comes from the same site.
	name := seedPath.FindStringSubmatch(index.Files.Walks)
	if name == nil {
		return data, fmt.Errorf("seed %s: unexpected walks path %q", manifestURL, index.Files.Walks)
	}
	walksURL := base.ResolveReference(&url.URL{Path: index.Files.Walks}).String()
	payload, err := download(hc, walksURL, 128<<20)
	if err != nil {
		return data, err
	}
	digest := fmt.Sprintf("%x", sha256.Sum256(payload))
	if digest[:12] != name[1] {
		return data, fmt.Errorf("seed %s: digest mismatch, sha256 %s", walksURL, digest)
	}
	if err = json.Unmarshal(payload, &data); err != nil {
		return data, fmt.Errorf("seed %s: %w", walksURL, err)
	}
	if data.Version != 2 || len(data.Walks) == 0 {
		return data, fmt.Errorf("seed %s: need schema version 2 with walks, got version %d with %d walks", walksURL, data.Version, len(data.Walks))
	}
	log.Printf("seed %s: sha256 %s, %d walks, source %s", walksURL, digest, len(data.Walks), index.Sources.Walks)
	return data, nil
}

// merge applies catalogue metadata by walk ID. A known walk keeps its
// forecast, and a walk absent from the updates stays unchanged.
func merge(data *snapshot, updates []walk) {
	index := map[string]int{}
	for i, w := range data.Walks {
		index[w.ID] = i
	}
	added := 0
	for _, w := range updates {
		if i, ok := index[w.ID]; ok {
			w.Fetched = data.Walks[i].Fetched
			w.Hours = data.Walks[i].Hours
			data.Walks[i] = w
		} else {
			index[w.ID] = len(data.Walks)
			data.Walks = append(data.Walks, w)
			added++
		}
	}
	log.Printf("catalogue: %d routes, %d updated, %d added, %d walks", len(updates), len(updates)-added, added, len(data.Walks))
}

func run(mode string) error {
	ctx := context.Background()
	c, err := r2.New(os.Getenv("AWS_ENDPOINT_URL"), os.Getenv("AWS_REGION"), r2.Credentials{AccessKeyID: os.Getenv("AWS_ACCESS_KEY_ID"), SecretAccessKey: os.Getenv("AWS_SECRET_ACCESS_KEY")}, nil)
	if err != nil {
		return err
	}
	bucket := os.Getenv("BUCKET")
	if mode == "bootstrap" {
		// Argo CD runs this hook on every sync. After the first generation
		// it stops here, without the lock, so a sync never waits on a writer.
		_, err := c.Get(ctx, bucket, "latest.json", "", 4096)
		if err == nil {
			log.Print("bootstrap: a generation exists")
			return nil
		}
		if !errors.Is(err, r2.ErrNotFound) {
			return err
		}
	}
	// All writer pods mount the same local-path directory. Linux releases its
	// advisory lock when the process exits, including on pod termination.
	lock, err := os.Open("/var/lock/hikes")
	if err != nil {
		return err
	}
	defer lock.Close()
	if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_EX); err != nil {
		return err
	}
	var data snapshot
	obj, err := c.Get(ctx, bucket, "latest.json", "", 4096)
	if err == nil {
		var m manifest
		if err = json.Unmarshal(obj.Body, &m); err != nil {
			return err
		}
		payload, err := c.Get(ctx, bucket, m.Walks, "", 128<<20)
		if err != nil {
			return err
		}
		if m.Walks != fmt.Sprintf("walks-%x.json", sha256.Sum256(payload.Body)) {
			return fmt.Errorf("snapshot digest mismatch")
		}
		if err = json.Unmarshal(payload.Body, &data); err != nil {
			return err
		}
		if data.Version != 2 {
			return fmt.Errorf("unsupported version %d", data.Version)
		}
	} else if !errors.Is(err, r2.ErrNotFound) {
		return err
	} else if mode == "bootstrap" {
		// Seed and first catalogue publish as one generation, so a failed
		// catalogue leaves the bucket empty and the next sync retries both.
		if data, err = seed(client, os.Getenv("SEED_URL")); err != nil {
			return err
		}
	} else if mode != "catalogue" {
		return err
	}
	// The scrape is the longest step, so it starts only after the state and
	// seed reads succeed.
	var updates []walk
	if mode == "catalogue" || mode == "bootstrap" {
		if updates, err = catalogue(); err != nil {
			return err
		}
	}
	now := time.Now().UTC()
	switch mode {
	case "catalogue", "bootstrap":
		merge(&data, updates)
	case "refresh":
		var successes atomic.Int64
		jobs := make(chan *walk)
		var workers sync.WaitGroup
		pace := time.NewTicker(60 * time.Millisecond)
		defer pace.Stop()
		for range 8 {
			workers.Go(func() {
				for w := range jobs {
					<-pace.C
					u := fmt.Sprintf("https://api.met.no/weatherapi/locationforecast/2.0/complete?lat=%.4f&lon=%.4f", w.Lat, w.Lon)
					if err := fetch(u, func(r io.Reader) error { return forecast(r, w, now) }); err != nil {
						log.Printf("walk %s: %v", w.ID, err)
					} else {
						successes.Add(1)
					}
				}
			})
		}
		for i := range data.Walks {
			jobs <- &data.Walks[i]
		}
		close(jobs)
		workers.Wait()
		if successes.Load() == 0 {
			return fmt.Errorf("no successful forecasts")
		}
		data.Fetched = &now
	case "prune":
	default:
		return fmt.Errorf("unknown mode %q", mode)
	}
	file, err := os.CreateTemp("", "walks-*.json")
	if err != nil {
		return err
	}
	defer os.Remove(file.Name())
	defer file.Close()
	hash := sha256.New()
	if err = encode(io.MultiWriter(file, hash), data, now); err != nil {
		return err
	}
	size, err := file.Seek(0, io.SeekCurrent)
	if err != nil {
		return err
	}
	if _, err = file.Seek(0, io.SeekStart); err != nil {
		return err
	}
	digest := fmt.Sprintf("%x", hash.Sum(nil))
	key := "walks-" + digest + ".json"
	if err = c.PutReader(ctx, bucket, key, file, size, digest, "public, max-age=31536000, immutable"); err != nil {
		return err
	}
	index, _ := json.Marshal(manifest{key})
	return c.Put(ctx, bucket, "latest.json", index, "public, max-age=60")
}

func encode(out io.Writer, data snapshot, now time.Time) error {
	sort.Slice(data.Walks, func(i, j int) bool { return data.Walks[i].ID < data.Walks[j].ID })
	buf := bufio.NewWriter(out)
	fmt.Fprintf(buf, `{"schema_version":2,"generated_at":%q,"fetched_at":`, now.Format(time.RFC3339Nano))
	enc := json.NewEncoder(buf)
	if err := enc.Encode(data.Fetched); err != nil {
		return err
	}
	buf.WriteString(`,"walks":[`)
	for i, w := range data.Walks {
		cutoff := float64(now.Truncate(time.Hour).Unix())
		keep := w.Hours[:0]
		for _, h := range w.Hours {
			if h[0] != nil && *h[0] >= cutoff {
				keep = append(keep, h)
			}
		}
		if keep == nil {
			keep = make([][7]*float64, 0)
		}
		w.Hours = keep
		if i > 0 {
			buf.WriteByte(',')
		}
		if err := enc.Encode(w); err != nil {
			return err
		}
	}
	buf.WriteString("]}")
	return buf.Flush()
}

func main() {
	if len(os.Args) != 2 {
		log.Fatal("expected bootstrap, catalogue, refresh or prune")
	}
	if err := run(os.Args[1]); err != nil {
		log.Fatal(err)
	}
}
