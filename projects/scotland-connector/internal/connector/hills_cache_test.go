package connector

import (
	"fmt"
	"sync"
	"testing"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/walks"
)

type cachedWalkSource struct{ ds *walks.Dataset }

func (s cachedWalkSource) Current() *walks.Dataset { return s.ds }

type cachedHillSource struct{ ds *walks.HillDataset }

func (s cachedHillSource) Current() *walks.HillDataset { return s.ds }

func TestHillJoinCache(t *testing.T) {
	w := &walks.Dataset{Version: "w1", Walks: []walks.Walk{{Name: "Ben Nevis", SummitNames: []string{"Ben Nevis"}, Lat: 56.8, Lon: -5, LengthKm: 17}}}
	h := &walks.HillDataset{Version: "h1", Hills: []walks.Hill{{Number: 1, Name: "Ben Nevis", Classes: []string{"Munro"}, Lat: 56.8, Lon: -5, HeightM: 1345}}}
	svc := &Service{Walks: cachedWalkSource{w}, Hills: cachedHillSource{h}}
	joined := svc.walks()
	if joined == w || len(joined.Walks[0].Hills) != 1 || w.HillSource != nil || len(w.Walks[0].Hills) != 0 {
		t.Fatal("join must preserve source snapshot")
	}
	// Concurrent readers of one version pair share the same immutable result.
	var wg sync.WaitGroup
	for range 20 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if svc.walks() != joined {
				t.Error("same versions rebuilt the join")
			}
		}()
	}
	wg.Wait()
	nextH := *h
	nextH.Version = "h2"
	nextH.Hills = append([]walks.Hill(nil), h.Hills...)
	nextH.Hills[0].HeightM = 1344
	svc.Hills = cachedHillSource{&nextH}
	hillChanged := svc.walks()
	if hillChanged == joined || hillChanged.Version != "w1|hills:h2" || hillChanged.Walks[0].Hills[0].HeightM != 1344 || joined.Walks[0].Hills[0].HeightM != 1345 {
		t.Fatal("hill replacement did not invalidate the join immutably")
	}
	nextW := *w
	nextW.Version = "w2"
	nextW.Walks = append([]walks.Walk(nil), w.Walks...)
	nextW.Walks[0].Name = "Changed walk"
	svc.Walks = cachedWalkSource{&nextW}
	walkChanged := svc.walks()
	if walkChanged == hillChanged || walkChanged.Version != "w2|hills:h2" || walkChanged.Walks[0].Name != "Changed walk" {
		t.Fatal("walk replacement did not invalidate the join")
	}
	svc.Hills = cachedHillSource{}
	if svc.walks() != &nextW {
		t.Fatal("unloaded hill source reused a stale join")
	}
	svc.Hills = nil
	if svc.walks() != &nextW {
		t.Fatal("disabled hill source reused a stale join")
	}
	svc.Walks = cachedWalkSource{}
	if svc.walks() != nil {
		t.Fatal("missing walks reused a stale join")
	}
}

func BenchmarkHillJoinCache(b *testing.B) {
	w := &walks.Dataset{Version: "w1", Walks: make([]walks.Walk, 1600)}
	h := &walks.HillDataset{Version: "h1", Hills: make([]walks.Hill, 1000)}
	for i := range h.Hills {
		h.Hills[i] = walks.Hill{Number: i + 1, Name: fmt.Sprintf("Hill %d", i), Classes: []string{"Munro"}, Lat: 56.8, Lon: -5, HeightM: 1000}
	}
	for i := range w.Walks {
		w.Walks[i] = walks.Walk{Name: fmt.Sprintf("Hill %d via the track", i%1000), Lat: 56.8, Lon: -5, AscentM: 1000, LengthKm: 10}
	}
	svc := &Service{Walks: cachedWalkSource{w}, Hills: cachedHillSource{h}}
	svc.walks()
	b.Run("rebuild", func(b *testing.B) {
		b.ReportAllocs()
		for b.Loop() {
			w.WithHills(h)
		}
	})
	b.Run("cached", func(b *testing.B) {
		b.ReportAllocs()
		for b.Loop() {
			svc.walks()
		}
	})
}
