// Package publish writes the public Ships products to the Ships Garage bucket.
//
// Each run reads one view of the database and writes a generation: changed
// vessel tracks, the heat grid and the snapshot, each under a key that holds
// its SHA-256, then the root latest.json last. The factory-public release
// mirrors the bucket to https://factory-data.jomcgi.dev/ships/.
package publish

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"os"
	"time"
)

const (
	// grace is how long a replaced object stays in the bucket. It covers the
	// mirror interval, the manifest cache, the page refresh and a mirror
	// outage of up to about 25 minutes.
	grace = 30 * time.Minute
	// margin covers an ingester transaction that started before the previous
	// view and committed after it: its updated_at is its start time.
	margin = 2 * time.Minute
	// trackBatch is the number of vessels in one track query.
	trackBatch = 200
	// runTimeout ends a stuck run before the next cron slot.
	runTimeout = 5 * time.Minute
	// The largest snapshot and manifest the reader accepts.
	snapshotLimit = 256 << 20
	manifestLimit = 64 << 10
)

// generation is a published manifest with its snapshot.
type generation struct {
	manifest manifest
	vessels  map[string]vessel
}

// refs returns every key that the generation references.
func (g *generation) refs() map[string]bool {
	refs := map[string]bool{g.manifest.Snapshot: true, g.manifest.Heat: true}
	for _, v := range g.vessels {
		if v.Track != nil {
			refs[*v.Track] = true
		}
	}
	return refs
}

// errIncomplete reports a manifest whose snapshot is missing or does not
// match its key.
var errIncomplete = errors.New("incomplete generation")

// resolve reads the generation that latest.json names. It returns nil when
// the bucket has no manifest. It checks that the snapshot bytes match the
// digest in the key and that both have the current schema version.
func resolve(ctx context.Context, s *store) (*generation, error) {
	body, err := s.get(ctx, manifestKey, manifestLimit)
	if errors.Is(err, errNotFound) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var g generation
	if err := json.Unmarshal(body, &g.manifest); err != nil {
		return nil, fmt.Errorf("%w: manifest: %v", errIncomplete, err)
	}
	if g.manifest.Version != schemaVersion || series(g.manifest.Snapshot) != "snapshot" || series(g.manifest.Heat) != "heat" {
		return nil, fmt.Errorf("%w: unsupported manifest", errIncomplete)
	}
	body, err = s.get(ctx, g.manifest.Snapshot, snapshotLimit)
	if errors.Is(err, errNotFound) {
		return nil, fmt.Errorf("%w: %v", errIncomplete, err)
	}
	if err != nil {
		return nil, err
	}
	if !verify(g.manifest.Snapshot, body) {
		return nil, fmt.Errorf("%w: %s does not match its digest", errIncomplete, g.manifest.Snapshot)
	}
	var snap snapshot
	if err := json.Unmarshal(body, &snap); err != nil || snap.Version != schemaVersion {
		return nil, fmt.Errorf("%w: unsupported snapshot %s", errIncomplete, g.manifest.Snapshot)
	}
	g.vessels = make(map[string]vessel, len(snap.Vessels))
	for _, v := range snap.Vessels {
		g.vessels[v.MMSI] = v
	}
	return &g, nil
}

// stats are the measurements of one run (HOME-410 criterion 2).
type stats struct {
	vessels, tracks, rebuilt, skipped int
	full                              bool
	puts                              int
	putBytes                          int64
	deletes                           int
}

type publisher struct {
	store *store
	now   func() time.Time
	stats stats
	// objects maps each key in the bucket to its last-modified time.
	objects map[string]time.Time
}

// write puts a payload unless the bucket already holds its key. The key holds
// the digest, so an existing key already has these bytes.
func (p *publisher) write(ctx context.Context, key string, body []byte) error {
	if _, ok := p.objects[key]; ok {
		return nil
	}
	if err := p.store.put(ctx, key, body, immutable); err != nil {
		return err
	}
	p.stats.puts++
	p.stats.putBytes += int64(len(body))
	return nil
}

func (p *publisher) run(ctx context.Context, open func(context.Context) (reader, error)) error {
	var err error
	if p.objects, err = p.store.list(ctx); err != nil {
		return err
	}
	prev, err := resolve(ctx, p.store)
	if errors.Is(err, errIncomplete) {
		// Rebuild every track. The new generation replaces the broken one.
		log.Printf("previous generation unusable, rebuilding: %v", err)
		prev, err = nil, nil
	}
	if err != nil {
		return err
	}
	r, err := open(ctx)
	if err != nil {
		return err
	}
	defer r.close()
	at := r.at()
	vessels, err := r.vessels(ctx)
	if err != nil {
		return err
	}
	// A full rebuild each hour catches history that changes without a newer
	// latest position: a late report, or retention dropping a day.
	p.stats.full = prev == nil || at.Before(prev.manifest.GeneratedAt.Time) ||
		!at.Truncate(time.Hour).Equal(prev.manifest.GeneratedAt.Truncate(time.Hour))
	index := make(map[string]int, len(vessels))
	var rebuild []string
	for i := range vessels {
		v := &vessels[i]
		index[v.MMSI] = i
		if !p.stats.full {
			old, ok := prev.vessels[v.MMSI]
			_, stored := p.objects[deref(old.Track)]
			if ok && v.UpdatedAt.Before(prev.manifest.GeneratedAt.Add(-margin)) && (old.Track == nil || stored) {
				v.Track = old.Track
				continue
			}
		}
		if !validMMSI.MatchString(v.MMSI) {
			p.stats.skipped++
			continue
		}
		rebuild = append(rebuild, v.MMSI)
	}
	p.stats.vessels, p.stats.rebuilt = len(vessels), len(rebuild)
	for start := 0; start < len(rebuild); start += trackBatch {
		batch := rebuild[start:min(start+trackBatch, len(rebuild))]
		err := r.tracks(ctx, batch, func(mmsi string, points []point) error {
			body, err := json.Marshal(track{Version: schemaVersion, MMSI: mmsi, Count: len(points), Track: points})
			if err != nil {
				return err
			}
			key := address(trackName(mmsi), body)
			if err := p.write(ctx, key, body); err != nil {
				return err
			}
			vessels[index[mmsi]].Track = &key
			return nil
		})
		if err != nil {
			return err
		}
	}
	h, err := r.heat(ctx)
	if err != nil {
		return err
	}
	r.close()
	heatBody, err := json.Marshal(h)
	if err != nil {
		return err
	}
	if vessels == nil {
		vessels = []vessel{}
	}
	snapBody, err := json.Marshal(snapshot{Version: schemaVersion, GeneratedAt: stamp{at}, Count: len(vessels), Vessels: vessels})
	if err != nil {
		return err
	}
	cur := &generation{
		manifest: manifest{Version: schemaVersion, GeneratedAt: stamp{at}, Snapshot: address("snapshot", snapBody), Heat: address("heat", heatBody)},
		vessels:  map[string]vessel{},
	}
	for _, v := range vessels {
		cur.vessels[v.MMSI] = v
		if v.Track != nil {
			p.stats.tracks++
		}
	}
	// Tracks first, then heat and the snapshot, then the manifest: every
	// key that a manifest names exists before the manifest does.
	if err := p.write(ctx, cur.manifest.Heat, heatBody); err != nil {
		return err
	}
	if err := p.write(ctx, cur.manifest.Snapshot, snapBody); err != nil {
		return err
	}
	top, err := json.Marshal(cur.manifest)
	if err != nil {
		return err
	}
	if err := p.store.put(ctx, manifestKey, top, shortCache); err != nil {
		return err
	}
	p.stats.puts++
	p.stats.putBytes += int64(len(top))

	refs := cur.refs()
	if prev != nil {
		for key := range prev.refs() {
			refs[key] = true
		}
	}
	var failed error
	for _, key := range expired(p.objects, refs, p.now().Add(-grace)) {
		if err := p.store.remove(ctx, key); err != nil {
			failed = errors.Join(failed, err)
			continue
		}
		p.stats.deletes++
	}
	return failed
}

func deref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}

// Main publishes one generation. The CronJob runs it every two minutes.
func Main() {
	if err := publishOnce(); err != nil {
		log.Fatal(err)
	}
}

func publishOnce() error {
	env := os.Getenv
	s, err := newStore(env("AWS_ENDPOINT_URL"), env("AWS_REGION"), env("BUCKET"), env("AWS_ACCESS_KEY_ID"), env("AWS_SECRET_ACCESS_KEY"))
	if err != nil {
		return err
	}
	if env("DATABASE_URL") == "" {
		return errors.New("DATABASE_URL required")
	}
	db, err := sql.Open("postgres", env("DATABASE_URL"))
	if err != nil {
		return errors.New("invalid database configuration")
	}
	defer db.Close()
	ctx, cancel := context.WithTimeout(context.Background(), runTimeout)
	defer cancel()
	p := &publisher{store: s, now: time.Now}
	err = p.run(ctx, func(ctx context.Context) (reader, error) { return openReader(ctx, db) })
	st := p.stats
	// One line per run gives the bytes and object writes of each cycle.
	log.Printf("publish: vessels=%d tracks=%d rebuilt=%d skipped=%d full=%t puts=%d put_bytes=%d deletes=%d",
		st.vessels, st.tracks, st.rebuilt, st.skipped, st.full, st.puts, st.putBytes, st.deletes)
	return err
}
