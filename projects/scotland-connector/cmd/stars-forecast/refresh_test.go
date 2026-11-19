package main

import (
	"io"
	"net/http"
	"slices"
	"strconv"
	"strings"
	"sync"
	"testing"
	"testing/synctest"
	"time"
)

// providerLimit is the met.no budget in ARCHITECTURE.md: at most 15 request
// starts in any one-second window and at most 15 requests in flight.
const providerLimit = 15

// provider is a fake met.no. It records the start time of each request and
// counts the requests in flight. It holds each request open for hold, or
// until the test sends on release.
type provider struct {
	hold    time.Duration
	release chan struct{}

	mu       sync.Mutex
	starts   []time.Time
	inFlight int
	peak     int
}

func (p *provider) RoundTrip(*http.Request) (*http.Response, error) {
	p.mu.Lock()
	p.starts = append(p.starts, time.Now())
	p.inFlight++
	p.peak = max(p.peak, p.inFlight)
	p.mu.Unlock()
	if p.release != nil {
		<-p.release
	} else {
		time.Sleep(p.hold)
	}
	p.mu.Lock()
	p.inFlight--
	p.mu.Unlock()
	body := io.NopCloser(strings.NewReader(`{"properties":{"timeseries":[]}}`))
	return &http.Response{StatusCode: 200, Body: body, Header: http.Header{}}, nil
}

func (p *provider) inFlightNow() int {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.inFlight
}

func gridSites(n int) []site {
	sites := make([]site, n)
	for i := range sites {
		sites[i].ID = strconv.Itoa(i)
	}
	return sites
}

// TestRefreshRequestRate runs two shards through one pacer, as run does, on
// the synctest virtual clock. Requests finish quickly, so the rate limit and
// not the in-flight cap holds refresh back. The pause between shards is a
// stall, after which a ticker would release a stale tick early.
func TestRefreshRequestRate(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		p := &provider{hold: 100 * time.Millisecond}
		client := &http.Client{Transport: p}
		wait := pace()
		for range 2 {
			if n := refresh(gridSites(40), map[string]forecast{}, wait, client); n != 40 {
				t.Fatalf("refresh fetched %d of 40 sites", n)
			}
			time.Sleep(300 * time.Millisecond)
		}
		starts := p.starts
		slices.SortFunc(starts, time.Time.Compare)
		if len(starts) != 80 {
			t.Fatalf("provider saw %d requests, want 80", len(starts))
		}
		// A half-open one-second window holds at most providerLimit starts
		// only if each providerLimit+1 consecutive starts span one second.
		for i := providerLimit; i < len(starts); i++ {
			if d := starts[i].Sub(starts[i-providerLimit]); d < time.Second {
				t.Fatalf("requests %d to %d started within %v", i-providerLimit, i, d)
			}
		}
		// Refresh uses the full budget: the first 15 starts fit in one second.
		if d := starts[providerLimit-1].Sub(starts[0]); d >= time.Second {
			t.Fatalf("the first %d requests took %v to start", providerLimit, d)
		}
	})
}

// TestRefreshInFlightCap holds every request open and releases them one at a
// time. synctest.Wait returns only when refresh and the provider are blocked,
// so each check sees every request that refresh can start.
func TestRefreshInFlightCap(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		const sites = 40
		p := &provider{release: make(chan struct{})}
		done := make(chan int)
		go func() {
			// No pacing, so only the in-flight cap holds refresh back.
			done <- refresh(gridSites(sites), map[string]forecast{}, func() {}, &http.Client{Transport: p})
		}()
		for finished := range sites {
			synctest.Wait()
			if got, want := p.inFlightNow(), min(providerLimit, sites-finished); got != want {
				close(p.release) // Let refresh finish, so that the bubble can exit.
				<-done
				t.Fatalf("%d requests in flight after %d finished, want %d", got, finished, want)
			}
			p.release <- struct{}{}
		}
		if n := <-done; n != sites {
			t.Fatalf("refresh fetched %d of %d sites", n, sites)
		}
		if p.peak != providerLimit {
			t.Fatalf("peak of %d requests in flight, want %d", p.peak, providerLimit)
		}
	})
}
