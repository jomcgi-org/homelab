package publish

import (
	"sort"
	"time"
)

// expired returns the payload keys that no reader can still need.
//
// A reader can hold an old manifest or snapshot for a few minutes: the mirror
// runs every minute, the manifest is cached for 60 seconds and the page keeps
// a snapshot between refreshes. So an object leaves only after a newer object
// of its series was written before cutoff, the start of the grace period.
// Each series keeps:
//   - every object that the new or the previous generation references;
//   - every object written after cutoff;
//   - if the new generation references the series, its newest object
//     written before cutoff, which was current at cutoff.
//
// A series without a reference is a track whose history expired, or an
// orphan of a failed run. Its objects leave once they are older than cutoff.
func expired(objects map[string]time.Time, refs map[string]bool, cutoff time.Time) []string {
	live := map[string]bool{}
	for key := range refs {
		if s := series(key); s != "" {
			live[s] = true
		}
	}
	// The newest object written before cutoff in each series.
	newest := map[string]string{}
	for key, modified := range objects {
		s := series(key)
		if s == "" || modified.After(cutoff) {
			continue
		}
		if n, ok := newest[s]; !ok || modified.After(objects[n]) || (modified.Equal(objects[n]) && key > n) {
			newest[s] = key
		}
	}
	var out []string
	for key, modified := range objects {
		s := series(key)
		if s == "" || refs[key] || modified.After(cutoff) || (live[s] && newest[s] == key) {
			continue
		}
		out = append(out, key)
	}
	sort.Strings(out)
	return out
}
