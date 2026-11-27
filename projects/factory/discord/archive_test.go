package discord

import (
	"context"
	"encoding/json"
	"errors"
	"sync"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus/testutil"

	"github.com/jomcgi/homelab/projects/factory/discord/archive"
)

type fakeArchive struct {
	mu         sync.Mutex
	rows       []archive.Row
	edits      []archive.Edit
	deletes    [][]string
	reconciles [][2]string
	fail       int
}

func (f *fakeArchive) Upsert(_ context.Context, rows []archive.Row) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.fail > 0 {
		f.fail--
		return errors.New("down")
	}
	f.rows = append(f.rows, rows...)
	return nil
}

func (f *fakeArchive) Edit(_ context.Context, e archive.Edit) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.edits = append(f.edits, e)
	return nil
}

func (f *fakeArchive) Delete(_ context.Context, _, _ string, ids []string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.deletes = append(f.deletes, ids)
	return nil
}

func (f *fakeArchive) Reconcile(_ context.Context, _, _, oldest, newest string, _ []string) (int64, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.reconciles = append(f.reconciles, [2]string{oldest, newest})
	return 1, nil
}

const (
	arcGuild    = "1501965852042330302"
	arcOpGuild  = "1290668195249917964"
	arcChannel  = "1501965852042331000"
	arcOperator = "1575140752776757368"
	arcOpThread = "1575140752776757999"
	arcOpOther  = "1290668195249917000"
	arcBot      = "1501965852042339000"
)

func newTestArchiver(store *fakeArchive) *Archiver {
	places := &Places{}
	guild := func(id string, channels ...Channel) json.RawMessage {
		b, _ := json.Marshal(map[string]any{"id": id, "name": "s", "channels": channels})
		return b
	}
	places.Apply("GUILD_CREATE", guild(arcGuild, Channel{ID: arcChannel, Name: "general", Type: 0}))
	places.Apply("GUILD_CREATE", guild(arcOpGuild,
		Channel{ID: arcOperator, Name: "factory", Type: 0},
		Channel{ID: arcOpThread, Name: "session", Type: 11, ParentID: arcOperator},
		Channel{ID: arcOpOther, Name: "chat", Type: 0}))
	a := &Archiver{
		Store:  store,
		Places: places,
		Config: Config{
			Guilds:   map[string]Guild{arcGuild: {Ingest: true}, arcOpGuild: {Ingest: true}},
			Operator: Operator{Guild: arcOpGuild, Channels: []string{arcOperator}},
		},
		Metrics: NewArchiveMetrics(nil),
		Queue:   4,
	}
	a.SetBot(User{ID: arcBot})
	return a
}

func archiveEvent(t *testing.T, v any) json.RawMessage {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func drain(a *Archiver) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	a.Run(ctx) // A cancelled Run writes what is queued, then returns.
}

func TestArchiverLiveEvents(t *testing.T) {
	store := &fakeArchive{}
	a := newTestArchiver(store)
	msg := func(guild, channel, id string, author map[string]any, content string) json.RawMessage {
		return archiveEvent(t, map[string]any{
			"id": id, "guild_id": guild, "channel_id": channel, "type": 0, "author": author,
			"content": content, "timestamp": "2026-11-27T10:00:00Z",
			"attachments": []map[string]string{{"filename": "plan.pdf"}},
		})
	}
	joe := map[string]any{"id": "1501965852042339999", "username": "joe", "global_name": "Joe"}
	a.Event("MESSAGE_CREATE", msg(arcGuild, arcChannel, "1501965852042340001", joe, "hi <#"+arcChannel+">"))
	// Bosun's own reply is kept and marked; another bot's is not.
	a.Event("MESSAGE_CREATE", msg(arcGuild, arcChannel, "1501965852042340002", map[string]any{"id": arcBot, "username": "bosun", "bot": true}, "answer"))
	a.Event("MESSAGE_CREATE", msg(arcGuild, arcChannel, "1501965852042340003", map[string]any{"id": "7", "username": "other", "bot": true}, "spam"))
	drain(a)
	// Operator channels, their threads and unknown channels in the
	// operator's server are never archived; its other channels are.
	a.Event("MESSAGE_CREATE", msg(arcOpGuild, arcOperator, "1290668195249918001", joe, "private"))
	a.Event("MESSAGE_CREATE", msg(arcOpGuild, arcOpThread, "1290668195249918002", joe, "private"))
	a.Event("MESSAGE_CREATE", msg(arcOpGuild, "1290668195249917555", "1290668195249918003", joe, "unknown thread"))
	a.Event("MESSAGE_CREATE", msg(arcOpGuild, arcOpOther, "1290668195249918004", joe, "public"))
	// A server off the allowlist.
	a.Event("MESSAGE_CREATE", msg("999999999999999999", arcChannel, "1501965852042340005", joe, "elsewhere"))
	drain(a)
	if len(store.rows) != 3 {
		t.Fatalf("rows: %+v", store.rows)
	}
	first := store.rows[0]
	if first.Content != "hi #general [attachment: plan.pdf]" || first.AuthorName != "joe (Joe)" || first.ChannelName != "general" || first.Bosun {
		t.Fatalf("first row: %+v", first)
	}
	if !store.rows[1].Bosun || store.rows[1].Content != "answer [attachment: plan.pdf]" {
		t.Fatalf("Bosun's row: %+v", store.rows[1])
	}
	if store.rows[2].Content != "public [attachment: plan.pdf]" {
		t.Fatalf("operator server row: %+v", store.rows[2])
	}

	edited := "2026-11-27T11:00:00Z"
	a.Event("MESSAGE_UPDATE", archiveEvent(t, map[string]any{
		"id": "1501965852042340001", "guild_id": arcGuild, "channel_id": arcChannel,
		"type": 0, "author": joe, "content": "hi again", "edited_timestamp": edited,
	}))
	// An embed unfurl is not an edit.
	a.Event("MESSAGE_UPDATE", archiveEvent(t, map[string]any{"id": "1501965852042340001", "guild_id": arcGuild, "channel_id": arcChannel, "embeds": []any{map[string]any{}}}))
	a.Event("MESSAGE_DELETE", archiveEvent(t, map[string]any{"id": "1501965852042340001", "guild_id": arcGuild, "channel_id": arcChannel}))
	a.Event("MESSAGE_DELETE_BULK", archiveEvent(t, map[string]any{"ids": []string{"1", "2"}, "guild_id": arcGuild, "channel_id": arcChannel}))
	// A delete in an operator channel leaves no tombstone.
	a.Event("MESSAGE_DELETE", archiveEvent(t, map[string]any{"id": "1290668195249918001", "guild_id": arcOpGuild, "channel_id": arcOperator}))
	drain(a)
	if len(store.edits) != 1 || store.edits[0].Content != "hi again" || store.edits[0].EditedAt.Format(time.RFC3339) != edited {
		t.Fatalf("edits: %+v", store.edits)
	}
	if len(store.deletes) != 2 || store.deletes[0][0] != "1501965852042340001" || len(store.deletes[1]) != 2 {
		t.Fatalf("deletes: %+v", store.deletes)
	}
}

func TestArchiverDropsWhenTheQueueIsFull(t *testing.T) {
	store := &fakeArchive{}
	a := newTestArchiver(store)
	for i := range 6 {
		a.Event("MESSAGE_DELETE", archiveEvent(t, map[string]any{"id": snowflakeAt(time.Now(), i), "guild_id": arcGuild, "channel_id": arcChannel}))
	}
	if got := testutil.ToFloat64(a.Metrics.Dropped); got != 2 {
		t.Fatalf("dropped %v, want 2", got)
	}
	drain(a)
	if len(store.deletes) != 4 {
		t.Fatalf("deletes %d", len(store.deletes))
	}
}

func TestArchiverPage(t *testing.T) {
	store := &fakeArchive{}
	a := newTestArchiver(store)
	c := Channel{ID: arcChannel, Name: "general"}
	page := []HistoryMessage{
		{Message: Message{ID: "1501965852042340010", Author: User{ID: "5", Username: "sam"}, Content: "newest"}},
		{Message: Message{ID: "1501965852042340009", Author: User{ID: "5", Username: "sam"}, Type: 7}}, // a join
		{
			Message:    Message{ID: "1501965852042340008", Author: User{ID: arcBot, Username: "bosun", Bot: true}},
			Components: []Component{{Type: componentText, Content: "a reply"}},
		},
	}
	a.Page(context.Background(), arcGuild, c, "", page)
	a.Page(context.Background(), arcGuild, c, "1501965852042340100", page)
	if len(store.rows) != 4 || store.rows[1].Content != "a reply" || !store.rows[1].Bosun {
		t.Fatalf("rows: %+v", store.rows)
	}
	// The newest page reconciles up to its newest message; a later page up
	// to just before its cursor.
	want := [][2]string{{"1501965852042340008", "1501965852042340010"}, {"1501965852042340008", "1501965852042340099"}}
	if len(store.reconciles) != 2 || store.reconciles[0] != want[0] || store.reconciles[1] != want[1] {
		t.Fatalf("reconciles: %v", store.reconciles)
	}
	// An operator channel's page is not archived.
	a.Page(context.Background(), arcOpGuild, Channel{ID: arcOpThread, ParentID: arcOperator, Type: 11}, "", page)
	if len(store.rows) != 4 {
		t.Fatalf("operator page archived: %+v", store.rows)
	}
	// A failed write skips the reconcile, so nothing is marked deleted on a
	// page that was not stored.
	store.fail = 1
	a.Page(context.Background(), arcGuild, c, "", page)
	if len(store.reconciles) != 2 {
		t.Fatalf("reconciled after a failed write: %v", store.reconciles)
	}
}
