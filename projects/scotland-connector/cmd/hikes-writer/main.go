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
	"os"
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

// connect reads PREFIX+AWS_* credentials. The writer bucket has no prefix;
// the bootstrap's hub snapshot source uses SOURCE_.
func connect(prefix string) (*r2.Client, error) {
	val := func(k string) string { return os.Getenv(prefix + k) }
	return r2.New(val("AWS_ENDPOINT_URL"), val("AWS_REGION"), r2.Credentials{AccessKeyID: val("AWS_ACCESS_KEY_ID"), SecretAccessKey: val("AWS_SECRET_ACCESS_KEY")}, nil)
}

// seed reads the hub's schema-v2 walk snapshot. Its walk IDs and last-good
// forecasts stay for routes that the first catalogue cannot fetch.
func seed(ctx context.Context, c *r2.Client, bucket, key string) (snapshot, error) {
	var data snapshot
	obj, err := c.Get(ctx, bucket, key, "", 128<<20)
	if err != nil {
		return data, err
	}
	if err = json.Unmarshal(obj.Body, &data); err != nil {
		return data, err
	}
	if data.Version != 2 || len(data.Walks) == 0 {
		return data, fmt.Errorf("seed %s/%s: need schema version 2 with walks, got version %d with %d walks", bucket, key, data.Version, len(data.Walks))
	}
	log.Printf("seed %s/%s: sha256 %x, %d walks", bucket, key, sha256.Sum256(obj.Body), len(data.Walks))
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
	c, err := connect("")
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
		source, err := connect("SOURCE_")
		if err != nil {
			return err
		}
		if data, err = seed(ctx, source, os.Getenv("SOURCE_BUCKET"), os.Getenv("SOURCE_KEY")); err != nil {
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
