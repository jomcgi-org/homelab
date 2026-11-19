package publish

import (
	"context"
	"encoding/json"
	"errors"
	"log"
	"sort"
	"time"
)

// stateKey holds the retirement time of each payload that no committed
// generation names. Its key is not a payload key, so the reader and the
// collector ignore it.
const stateKey = "publisher-state.json"

// stateLimit is the largest state object the publisher reads.
const stateLimit = 16 << 20

// gcState is the stored form of the retirement times.
type gcState struct {
	Version int                  `json:"schema_version"`
	Retired map[string]time.Time `json:"retired"`
}

// readRetired returns the stored retirement times. A missing or unreadable
// state gives none, so each unnamed payload starts a new grace period.
func readRetired(ctx context.Context, s *store, objects map[string]time.Time) (map[string]time.Time, error) {
	if _, ok := objects[stateKey]; !ok {
		return nil, nil
	}
	body, err := s.get(ctx, stateKey, stateLimit)
	if errors.Is(err, errNotFound) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var st gcState
	if err := json.Unmarshal(body, &st); err != nil || st.Version != schemaVersion {
		log.Printf("unreadable %s, every unnamed payload starts a new grace period: %v", stateKey, err)
		return nil, nil
	}
	return st.Retired, nil
}

// collect returns the retirement time of each payload in the bucket that the
// new generation does not name, and the payloads to delete.
//
// A reader can hold an old manifest or snapshot for a few minutes after the
// mirror replaces it, and the mirror can lag Garage. So a payload stays for
// the grace period after the first committed generation that does not name
// it. That is its retirement time:
//   - a payload that the new generation names is not retired;
//   - a payload that the previous generation names retires now;
//   - any other payload keeps its stored retirement time, or retires now
//     without one: an orphan of a failed run, or a lost state.
//
// prev is nil when the previous generation is unknown. retired is nil when
// the state is missing.
func collect(objects map[string]time.Time, retired map[string]time.Time, prev, cur map[string]bool, now time.Time) (map[string]time.Time, []string) {
	next := map[string]time.Time{}
	var expired []string
	for key := range objects {
		if series(key) == "" || cur[key] {
			continue
		}
		at, ok := retired[key]
		if !ok || prev == nil || prev[key] {
			at = now
		}
		next[key] = at
		if !at.After(now.Add(-grace)) {
			expired = append(expired, key)
		}
	}
	sort.Strings(expired)
	return next, expired
}
