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

func run(mode string) error {
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
	ctx := context.Background()
	c, err := r2.New(os.Getenv("AWS_ENDPOINT_URL"), os.Getenv("AWS_REGION"), r2.Credentials{AccessKeyID: os.Getenv("AWS_ACCESS_KEY_ID"), SecretAccessKey: os.Getenv("AWS_SECRET_ACCESS_KEY")}, nil)
	if err != nil {
		return err
	}
	bucket := os.Getenv("BUCKET")
	var updates []walk
	if mode == "catalogue" {
		updates, err = catalogue()
		if err != nil {
			return err
		}
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
	} else if !errors.Is(err, r2.ErrNotFound) || mode != "catalogue" {
		return err
	}
	now := time.Now().UTC()
	switch mode {
	case "catalogue":
		index := map[string]int{}
		for i, w := range data.Walks {
			index[w.ID] = i
		}
		for _, w := range updates {
			if i, ok := index[w.ID]; ok {
				w.Fetched = data.Walks[i].Fetched
				w.Hours = data.Walks[i].Hours
				data.Walks[i] = w
			} else {
				data.Walks = append(data.Walks, w)
			}
		}
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
		log.Fatal("expected catalogue, refresh or prune")
	}
	if err := run(os.Args[1]); err != nil {
		log.Fatal(err)
	}
}
