package capabilities

import (
	"context"
	"fmt"
	"sync"
	"testing"
)

// MapObjectStore is an in-memory ObjectStore for use in tests. It is
// concurrency-safe.
type MapObjectStore struct {
	mu   sync.RWMutex
	data map[string][]byte
}

// NewMapObjectStore returns an empty MapObjectStore.
func NewMapObjectStore() *MapObjectStore {
	return &MapObjectStore{data: make(map[string][]byte)}
}

// Pull returns the bytes stored at key, or an error if the key does not exist.
func (m *MapObjectStore) Pull(_ context.Context, key string) ([]byte, error) {
	m.mu.RLock()
	defer m.mu.RUnlock()
	v, ok := m.data[key]
	if !ok {
		return nil, fmt.Errorf("objectstore: key %q not found", key)
	}
	// Return a copy so callers cannot mutate stored state.
	out := make([]byte, len(v))
	copy(out, v)
	return out, nil
}

// Push stores data at key, overwriting any previous value.
func (m *MapObjectStore) Push(_ context.Context, key string, data []byte) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	cp := make([]byte, len(data))
	copy(cp, data)
	m.data[key] = cp
	return nil
}

// Compile-time check: MapObjectStore satisfies ObjectStore.
var _ ObjectStore = (*MapObjectStore)(nil)

func TestMapObjectStoreRoundTrip(t *testing.T) {
	ctx := context.Background()
	store := NewMapObjectStore()

	want := []byte("hello, wave-1b")
	if err := store.Push(ctx, "my/key", want); err != nil {
		t.Fatalf("Push: %v", err)
	}
	got, err := store.Pull(ctx, "my/key")
	if err != nil {
		t.Fatalf("Pull: %v", err)
	}
	if string(got) != string(want) {
		t.Fatalf("Pull = %q, want %q", got, want)
	}
}

func TestMapObjectStorePullMissing(t *testing.T) {
	ctx := context.Background()
	store := NewMapObjectStore()

	_, err := store.Pull(ctx, "does/not/exist")
	if err == nil {
		t.Fatal("Pull of missing key should return an error")
	}
}
