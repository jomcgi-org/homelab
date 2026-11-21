package publish

import (
	"context"
	"encoding/json"
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
	Version int `json:"schema_version"`
	// Manifest is the SHA-256 of the latest.json bytes that the run
	// committed before it wrote this state.
	Manifest string               `json:"manifest"`
	Retired  map[string]time.Time `json:"retired"`
}

// readRetired returns the stored retirement times when the state describes
// prev, the committed generation. Otherwise it returns none, so each unnamed
// payload starts a new grace period:
//   - the state is missing, or any read or decode of it fails;
//   - the state names another manifest: a run committed latest.json but did
//     not write its state, so a payload can have retired after the stored
//     time;
//   - prev is nil: the previous generation is unknown.
func readRetired(ctx context.Context, s *store, objects map[string]time.Time, prev *generation) map[string]time.Time {
	if _, ok := objects[stateKey]; !ok {
		return nil
	}
	body, err := s.get(ctx, stateKey, stateLimit)
	var st gcState
	if err == nil {
		err = json.Unmarshal(body, &st)
	}
	switch {
	case err != nil || st.Version != schemaVersion:
		log.Printf("unreadable %s, every unnamed payload starts a new grace period: %v", stateKey, err)
		return nil
	case prev == nil || st.Manifest != prev.digest:
		log.Printf("%s describes another manifest, every unnamed payload starts a new grace period", stateKey)
		return nil
	}
	return st.Retired
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
//     without a usable one: an orphan of a failed run, a lost or stale
//     state, or a stored time that is zero (a JSON null) or after now.
//
// prev is nil when the previous generation is unknown. retired is nil when
// readRetired returns no state.
func collect(objects map[string]time.Time, retired map[string]time.Time, prev, cur map[string]bool, now time.Time) (map[string]time.Time, []string) {
	next := map[string]time.Time{}
	var expired []string
	for key := range objects {
		if series(key) == "" || cur[key] {
			continue
		}
		at, ok := retired[key]
		// A zero time (a JSON null) or a time after now is not a real
		// retirement, so the payload starts a new grace period.
		if !ok || at.IsZero() || at.After(now) || prev == nil || prev[key] {
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
