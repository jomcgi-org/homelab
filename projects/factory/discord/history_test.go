package discord

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"slices"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"
)

// fakeHistory serves channels newest first, in pages of two.
type fakeHistory struct {
	channels  map[string][]Channel
	threads   map[string][]Channel
	messages  map[string][]HistoryMessage // newest first
	forbidden map[string]bool
}

func (f *fakeHistory) GuildChannels(_ context.Context, g string) ([]Channel, error) {
	return f.channels[g], nil
}

func (f *fakeHistory) Threads(_ context.Context, g string, _ []Channel) ([]Channel, error) {
	return f.threads[g], nil
}

func (f *fakeHistory) MessagesBefore(_ context.Context, c, before string) ([]HistoryMessage, error) {
	if f.forbidden[c] {
		return nil, &HTTPError{Status: http.StatusForbidden}
	}
	all := f.messages[c]
	start := 0
	if before != "" {
		for i, m := range all {
			if m.ID == before {
				start = i + 1
			}
		}
	}
	end := min(start+2, len(all))
	return all[start:end], nil
}

type fakeDocs struct {
	mu   sync.Mutex
	docs map[string]RetainItem // guild/document ID
}

func (f *fakeDocs) DocumentExists(_ context.Context, g, id string) (bool, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	_, ok := f.docs[g+"/"+id]
	return ok, nil
}

func (f *fakeDocs) RetainDocument(_ context.Context, g string, item RetainItem) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.docs == nil {
		f.docs = map[string]RetainItem{}
	}
	f.docs[g+"/"+item.DocumentID] = item
	return nil
}

func (f *fakeDocs) ids() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	var out []string
	for k := range f.docs {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func msg(id, user string, at time.Time, text string) HistoryMessage {
	return HistoryMessage{Message: Message{ID: id, Author: User{ID: user, Username: user}, Content: text, Timestamp: at}}
}

func day(d, h int) time.Time { return time.Date(2026, 11, d, h, 0, 0, 0, time.UTC) }

func newImporter(src *fakeHistory, docs *fakeDocs, guilds map[string]Guild) *Importer {
	return &Importer{
		Source: src, Store: docs, Config: Config{Guilds: guilds}, BotID: "bosun",
		Workers: 2, Log: slog.New(slog.NewTextHandler(io.Discard, nil)),
		Now: func() time.Time { return day(17, 21) },
	}
}

func history() *fakeHistory {
	bot := msg("m6", "alertbot", day(16, 9), "disk full")
	bot.Author.Bot = true
	hook := msg("m7", "hook", day(16, 9), "deployed")
	hook.WebhookID = "w"
	join := msg("m8", "cy", day(16, 9), "")
	join.Type = 7
	// Bosun's replies are Components V2: empty content, a text display.
	bosun := msg("m4", "bosun", day(16, 11), "")
	bosun.Author.Bot = true
	bosun.Components = []Component{{Type: 17, Components: []Component{{Type: componentText, Content: "the code word is heron"}}}}
	pic := msg("m3", "bo", day(15, 22), "")
	pic.Attachments = []Attachment{{Filename: "boat.png"}}
	return &fakeHistory{
		channels: map[string][]Channel{
			"g1": {{ID: "c1", Name: "general", Type: 0}, {ID: "v1", Name: "voice", Type: 2}},
			"g2": {{ID: "c9", Name: "other", Type: 0}},
		},
		threads: map[string][]Channel{"g1": {{ID: "t1", Name: "plans", Type: 11, ParentID: "c1"}}},
		messages: map[string][]HistoryMessage{
			// Newest first, as Discord returns them.
			"c1": {
				msg("m9", "ann", day(17, 8), "today's message is the bridge's"),
				bosun, bot, hook, join,
				msg("m5", "ann", day(16, 10), "what is the code word?"),
				pic,
				msg("m2", "ann", day(15, 21), "first"),
			},
			"t1": {msg("t2", "cy", day(14, 12), "thread reply")},
			"c9": {msg("x1", "dee", day(14, 12), "another server")},
		},
	}
}

func TestImportStoresOneChronologicalDocumentPerChannelDay(t *testing.T) {
	docs := &fakeDocs{}
	im := newImporter(history(), docs, map[string]Guild{"g1": {History: true}, "g2": {}})
	stats, err := im.Run(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"g1/discord:c1:2026-11-15:history", "g1/discord:c1:2026-11-16:history", "g1/discord:t1:2026-11-14:history"}
	if got := docs.ids(); fmt.Sprint(got) != fmt.Sprint(want) {
		t.Fatalf("documents = %v, want %v (no day of today, none from a server without history)", got, want)
	}
	d16 := docs.docs["g1/discord:c1:2026-11-16:history"]
	if d16.Content != "ann (2026-11-16T10:00:00Z): what is the code word?\nbosun (2026-11-16T11:00:00Z): the code word is heron" {
		t.Errorf("content = %q: oldest first; other bots, webhooks and joins left out; Bosun kept", d16.Content)
	}
	if d16.UpdateMode != "replace" || d16.Context != "Discord #general" || d16.Timestamp != "2026-11-16T10:00:00Z" ||
		fmt.Sprint(d16.Tags) != "[channel:c1]" || fmt.Sprint(d16.ObservationScopes) != "[[channel:c1]]" {
		t.Errorf("document = %+v", d16)
	}
	if got := docs.docs["g1/discord:c1:2026-11-15:history"].Content; !strings.HasSuffix(got, "bo (2026-11-15T22:00:00Z):  [attachment: boat.png]") {
		t.Errorf("attachment line = %q", got)
	}
	if stats.Retained != 3 || stats.Channels != 2 {
		t.Errorf("stats = %+v", stats)
	}
	// A second run stores nothing new.
	stats, err = newImporter(history(), docs, map[string]Guild{"g1": {History: true}}).Run(context.Background())
	if err != nil || stats.Retained != 0 || stats.Skipped != 3 || len(docs.ids()) != 3 {
		t.Fatalf("second run = %+v, %v, %d documents", stats, err, len(docs.ids()))
	}
}

func TestImportFiltersChannelsCapsAndSkipsUnreadableChannels(t *testing.T) {
	// A channel list admits only listed IDs, as the live bridge does: a
	// thread of a listed channel needs its own entry.
	docs := &fakeDocs{}
	src := history()
	src.channels["g1"] = append(src.channels["g1"], Channel{ID: "c2", Name: "secret", Type: 0})
	src.messages["c2"] = []HistoryMessage{msg("s1", "ann", day(10, 1), "hidden")}
	if _, err := newImporter(src, docs, map[string]Guild{"g1": {History: true, Channels: []string{"c1"}}}).Run(context.Background()); err != nil {
		t.Fatal(err)
	}
	for _, id := range docs.ids() {
		if strings.Contains(id, "c2") || strings.Contains(id, "t1") {
			t.Fatalf("unlisted channel or thread imported: %v", docs.ids())
		}
	}
	docs = &fakeDocs{}
	if _, err := newImporter(history(), docs, map[string]Guild{"g1": {History: true, Channels: []string{"c1", "t1"}}}).Run(context.Background()); err != nil {
		t.Fatal(err)
	}
	if !slices.Contains(docs.ids(), "g1/discord:t1:2026-11-14:history") {
		t.Fatalf("a listed thread was not imported: %v", docs.ids())
	}
	// The cap stops the run; the next run continues.
	docs = &fakeDocs{}
	im := newImporter(history(), docs, map[string]Guild{"g1": {History: true}})
	im.MaxDocuments, im.Workers = 1, 1
	stats, err := im.Run(context.Background())
	if !errors.Is(err, ErrCapped) || !stats.Capped || len(docs.ids()) != 1 {
		t.Fatalf("capped run = %+v, %v, %v", stats, err, docs.ids())
	}
	im = newImporter(history(), docs, map[string]Guild{"g1": {History: true}})
	if _, err := im.Run(context.Background()); err != nil || len(docs.ids()) != 3 {
		t.Fatalf("continued run = %v, %v", err, docs.ids())
	}
	// A channel the bot cannot read is skipped.
	src = history()
	src.forbidden = map[string]bool{"c1": true}
	docs = &fakeDocs{}
	if _, err := newImporter(src, docs, map[string]Guild{"g1": {History: true}}).Run(context.Background()); err != nil || fmt.Sprint(docs.ids()) != "[g1/discord:t1:2026-11-14:history]" {
		t.Fatalf("forbidden channel run = %v, %v", err, docs.ids())
	}
	// IncludeBots keeps other bots and webhooks.
	docs = &fakeDocs{}
	im = newImporter(history(), docs, map[string]Guild{"g1": {History: true}})
	im.IncludeBots = true
	if _, err := im.Run(context.Background()); err != nil || !strings.Contains(docs.docs["g1/discord:c1:2026-11-16:history"].Content, "alertbot") {
		t.Fatalf("include bots = %v, %+v", err, docs.docs["g1/discord:c1:2026-11-16:history"])
	}
}

// The Hindsight client checks a document with a GET and retains it
// synchronously.
func TestHindsightDocumentCalls(t *testing.T) {
	var calls []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		calls = append(calls, r.Method+" "+r.URL.EscapedPath()+" "+string(body))
		if r.Method == http.MethodGet && strings.HasSuffix(r.URL.Path, ":2026-11-15") {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		_, _ = w.Write([]byte(`{}`))
	}))
	defer srv.Close()
	h := &Hindsight{BaseURL: srv.URL, Key: key}
	ctx := context.Background()
	if ok, err := h.DocumentExists(ctx, "g1", "discord:c1:2026-11-15"); ok || err != nil {
		t.Fatalf("missing document = %v, %v", ok, err)
	}
	if ok, err := h.DocumentExists(ctx, "g1", "discord:c1:2026-11-16"); !ok || err != nil {
		t.Fatalf("stored document = %v, %v", ok, err)
	}
	if err := h.RetainDocument(ctx, "g1", NewRetain("c1", day(15, 1), "x")); err != nil {
		t.Fatal(err)
	}
	if calls[1] != "GET /v1/default/banks/discord-g1/documents/discord:c1:2026-11-15 " {
		t.Errorf("exists call = %q: a GET carries no body", calls[1])
	}
	if !strings.Contains(calls[len(calls)-1], `"async":false`) {
		t.Errorf("retain call = %q", calls[len(calls)-1])
	}
}

// A live bridge document for a day does not stop that day's import: the
// two documents have different IDs.
func TestImportIsApartFromLiveDocuments(t *testing.T) {
	docs := &fakeDocs{}
	_ = docs.RetainDocument(context.Background(), "g1", NewRetain("c1", day(16, 10), "ann: tagged exchange"))
	if _, err := newImporter(history(), docs, map[string]Guild{"g1": {History: true}}).Run(context.Background()); err != nil {
		t.Fatal(err)
	}
	if !slices.Contains(docs.ids(), "g1/discord:c1:2026-11-16:history") || !slices.Contains(docs.ids(), "g1/discord:c1:2026-11-16") {
		t.Fatalf("documents = %v: the live document and the day's import must both exist", docs.ids())
	}
}

// Threads come from every parent that holds them, text, announcement, forum
// and media, and active private threads the bot can see are kept.
func TestThreadsListEveryParentKind(t *testing.T) {
	var paths []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		paths = append(paths, r.URL.Path)
		switch {
		case r.URL.Path == "/guilds/g1/threads/active":
			_, _ = w.Write([]byte(`{"threads":[{"id":"p1","type":12,"parent_id":"c1"}]}`))
		case strings.HasPrefix(r.URL.Path, "/channels/f1/"):
			_, _ = w.Write([]byte(`{"threads":[{"id":"post1","type":11,"parent_id":"f1","thread_metadata":{"archive_timestamp":"2026-11-01T00:00:00Z"}}],"has_more":false}`))
		default:
			_, _ = w.Write([]byte(`{"threads":[],"has_more":false}`))
		}
	}))
	defer srv.Close()
	r := &REST{Base: srv.URL, Token: func() (string, error) { return "t", nil }}
	got, err := r.Threads(context.Background(), "g1", []Channel{
		{ID: "c1", Type: 0}, {ID: "f1", Type: 15}, {ID: "m1", Type: 16}, {ID: "v1", Type: 2},
	})
	if err != nil {
		t.Fatal(err)
	}
	ids := []string{}
	for _, c := range got {
		ids = append(ids, c.ID)
	}
	if fmt.Sprint(ids) != "[p1 post1]" {
		t.Errorf("threads = %v", ids)
	}
	want := "/guilds/g1/threads/active /channels/c1/threads/archived/public /channels/f1/threads/archived/public /channels/m1/threads/archived/public"
	if strings.Join(paths, " ") != want {
		t.Errorf("paths = %v", paths)
	}
	if !historyChannelTypes[12] {
		t.Error("private threads are not imported")
	}
}
