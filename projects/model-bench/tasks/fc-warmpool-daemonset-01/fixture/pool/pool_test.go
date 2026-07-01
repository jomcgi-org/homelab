package pool

import (
	"sync"
	"testing"
)

// TestConcurrentAcquireReleaseNoDouble hammers the pool from many goroutines. A
// correct pool never hands the same slot to two callers at once; an unsynchronised
// one both races on its internal state (caught by `go test -race`) and can pop the
// same slot twice (caught by the held-set check below).
func TestConcurrentAcquireReleaseNoDouble(t *testing.T) {
	p := New(8)

	var mu sync.Mutex
	held := make(map[int]bool)
	var wg sync.WaitGroup
	for i := 0; i < 64; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < 300; j++ {
				c, err := p.Acquire()
				if err != nil {
					continue // exhaustion under contention is expected and fine
				}
				mu.Lock()
				if held[c.Slot] {
					mu.Unlock()
					t.Errorf("slot %d handed out to two callers at once", c.Slot)
					return
				}
				held[c.Slot] = true
				mu.Unlock()

				mu.Lock()
				delete(held, c.Slot)
				mu.Unlock()
				p.Release(c)
			}
		}()
	}
	wg.Wait()
}

// TestAcquireExhaustsAndRecovers checks the basic single-threaded contract still
// holds: New(n) yields exactly n slots, then ErrExhausted, and a Release frees one.
func TestAcquireExhaustsAndRecovers(t *testing.T) {
	p := New(2)
	a, err := p.Acquire()
	if err != nil {
		t.Fatalf("first acquire: %v", err)
	}
	if _, err := p.Acquire(); err != nil {
		t.Fatalf("second acquire: %v", err)
	}
	if _, err := p.Acquire(); err != ErrExhausted {
		t.Fatalf("third acquire: want ErrExhausted, got %v", err)
	}
	p.Release(a)
	if _, err := p.Acquire(); err != nil {
		t.Fatalf("acquire after release: %v", err)
	}
}
