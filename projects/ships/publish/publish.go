// Package publish writes the public Ships products to the Ships Garage bucket.
//
// Each run reads one view of the database and writes a generation: changed
// vessel tracks, the heat grid and the snapshot, each under a key that holds
// its SHA-256, then the root latest.json last. The factory-public release
// mirrors the bucket to https://factory-data.jomcgi.dev/ships/.
package publish

import (
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"os"
	"time"
)

const (
	// grace is how long a payload stays in the bucket after the first
	// committed generation that does not name it. It covers the mirror
	// interval, the manifest cache, the page refresh and a mirror outage of
	// up to about 25 minutes.
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
	// digest is the SHA-256 of the latest.json bytes.
	digest  string
	vessels map[string]vessel
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
	g := generation{digest: digest(body)}
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
	// retired counts the payloads in their grace period after this run.
	retired int
}

type publisher struct {
	store *store
	now   func() time.Time
	stats stats
	// objects maps each key in the bucket to its last-modified time.
	objects map[string]time.Time
	// previousKeys maps a content address to its key in the previous manifest.
	previousKeys map[string]string
	incarnation  string
}

// write reuses a key only while the previous generation names it. A
// retired key may have a DELETE in flight from a publisher that lost its
// database session. Republishing the same bytes gives them a fresh key.
func (p *publisher) write(ctx context.Context, name string, body []byte) (string, error) {
	contentKey := address(name, body)
	if key, ok := p.previousKeys[contentKey]; ok {
		if _, exists := p.objects[key]; exists {
			return key, nil
		}
	}
	key := contentKey[:len(contentKey)-len(".json")] + "-" + p.incarnation + ".json"
	if err := p.store.put(ctx, key, body, immutable); err != nil {
		return "", err
	}
	p.stats.puts++
	p.stats.putBytes += int64(len(body))
	return key, nil
}

// errLocked reports that another run holds the writer lock.
var errLocked = errors.New("another run holds the writer lock")

// source is the Ships database.
type source interface {
	// lock takes the writer lock for the rest of the run. It returns false
	// when another run holds the lock.
	lock(ctx context.Context) (bool, error)
	// held returns an error when the run no longer holds the lock.
	held(ctx context.Context) error
	// open starts the read-only view of the run.
	open(ctx context.Context) (reader, error)
}

func (p *publisher) run(ctx context.Context, src source) error {
	// One run at a time lists, writes and deletes. Otherwise a run can delete
	// a payload that another run found in its listing and named again.
	ok, err := src.lock(ctx)
	if err != nil {
		return err
	}
	if !ok {
		return errLocked
	}
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
	p.incarnation = rand.Text()
	p.previousKeys = make(map[string]string)
	if prev != nil {
		for key := range prev.refs() {
			if parts := digestKey.FindStringSubmatch(key); parts != nil {
				p.previousKeys[parts[1]+"-"+parts[2]+".json"] = key
			}
		}
	}
	retired := readRetired(ctx, p.store, p.objects, prev)
	r, err := src.open(ctx)
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
			key, err := p.write(ctx, trackName(mmsi), body)
			if err != nil {
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
		manifest: manifest{Version: schemaVersion, GeneratedAt: stamp{at}},
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
	if cur.manifest.Heat, err = p.write(ctx, "heat", heatBody); err != nil {
		return err
	}
	if cur.manifest.Snapshot, err = p.write(ctx, "snapshot", snapBody); err != nil {
		return err
	}
	top, err := json.Marshal(cur.manifest)
	if err != nil {
		return err
	}
	if err := src.held(ctx); err != nil {
		return fmt.Errorf("writer lock lost before the manifest: %w", err)
	}
	if err := p.store.put(ctx, manifestKey, top, shortCache); err != nil {
		return err
	}
	p.stats.puts++
	p.stats.putBytes += int64(len(top))

	// The manifest commits the generation. Record when each payload left the
	// committed generations, then delete what retired a grace period ago.
	var prevRefs map[string]bool
	if prev != nil {
		prevRefs = prev.refs()
	}
	next, expired := collect(p.objects, retired, prevRefs, cur.refs(), p.now())
	if len(expired) > 0 {
		if err := src.held(ctx); err != nil {
			// The state stays bound to an older manifest, so the next run
			// restarts every grace period.
			return fmt.Errorf("writer lock lost before the deletes: %w", err)
		}
	}
	var failed error
	for _, key := range expired {
		if err := p.store.remove(ctx, key); err != nil {
			// The key keeps its retirement time, so the next run retries.
			failed = errors.Join(failed, err)
			continue
		}
		delete(next, key)
		p.stats.deletes++
	}
	state, err := json.Marshal(gcState{Version: schemaVersion, Manifest: digest(top), Retired: next})
	if err != nil {
		return errors.Join(failed, err)
	}
	if err := p.store.put(ctx, stateKey, state, "no-store"); err != nil {
		return errors.Join(failed, err)
	}
	p.stats.puts++
	p.stats.putBytes += int64(len(state))
	p.stats.retired = len(next)
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
	// Closing the pool ends the session, which releases the writer lock.
	defer db.Close()
	ctx, cancel := context.WithTimeout(context.Background(), runTimeout)
	defer cancel()
	// One session holds the lock and reads the view, so a run uses one of
	// the role's two connections and a second run can still connect.
	conn, err := db.Conn(ctx)
	if err != nil {
		return err
	}
	defer conn.Close()
	p := &publisher{store: s, now: time.Now}
	err = p.run(ctx, pgSource{conn})
	if errors.Is(err, errLocked) {
		log.Printf("publish: %v, nothing to do", err)
		return nil
	}
	st := p.stats
	// One line per run gives the bytes and object writes of each cycle.
	log.Printf("publish: vessels=%d tracks=%d rebuilt=%d skipped=%d full=%t puts=%d put_bytes=%d deletes=%d retired=%d",
		st.vessels, st.tracks, st.rebuilt, st.skipped, st.full, st.puts, st.putBytes, st.deletes, st.retired)
	return err
}
