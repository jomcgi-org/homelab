package archive

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"sort"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/broker/actorjwt"
	"github.com/jomcgi/homelab/projects/factory/connector/internal/pgtest"
)

func TestMain(m *testing.M) { os.Exit(pgtest.Main(m)) }

const (
	guild    = "1501965852042330302"
	other    = "1290668195249917964"
	general  = "1501965852042331000"
	thread   = "1501965852042331001"
	random   = "1501965852042331002"
	joe      = "1501965852042339999"
	reader   = "factory_discord_archive_reader"
	sqlGroup = "factory_discord_sql"
)

var day = time.Date(2026, 11, 20, 0, 0, 0, 0, time.UTC)

// id is a snowflake at day plus minutes, so IDs sort as times do.
func id(minutes int) string {
	ms := day.Add(time.Duration(minutes)*time.Minute).UnixMilli() - discordEpoch
	return strconv.FormatUint(uint64(ms)<<22, 10)
}

func row(minutes int, channel, author, content string) Row {
	return Row{
		ID: id(minutes), GuildID: guild, ChannelID: channel, ChannelName: map[string]string{general: "general", thread: "plans", random: "random"}[channel],
		AuthorID: map[string]string{"joe": joe}[author], AuthorName: author,
		SentAt: day.Add(time.Duration(minutes) * time.Minute), Content: content,
	}
}

// setup returns the owner's store and a reader on the read-only login.
func setup(t *testing.T) (*Store, *Reader, *sql.DB) {
	t.Helper()
	db, err := sql.Open("postgres", pgtest.NewDB(t))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	if err := Migrate(context.Background(), db, reader, sqlGroup); err != nil {
		t.Fatal(err)
	}
	// Again: the schema is safe to apply twice.
	if err := Migrate(context.Background(), db, reader, sqlGroup); err != nil {
		t.Fatal(err)
	}
	ro := pgtest.AsRole(t, db, reader)
	sqlDB := map[string]*sql.DB{
		guild: pgtest.AsRole(t, db, sqlGroup+"_"+guild),
		other: pgtest.AsRole(t, db, sqlGroup+"_"+other),
	}
	return &Store{DB: db}, &Reader{DB: ro, SQLDB: sqlDB, Servers: map[string]bool{guild: true, other: true}}, ro
}

func seed(t *testing.T, s *Store) {
	t.Helper()
	p := row(30, thread, "sam", "the plan for the cabin trip")
	p.ThreadParentID = general
	rows := []Row{
		row(1, general, "joe", "Morning! Who is running the migration today?"),
		row(2, general, "sam (Sammy)", "I migrated the database last night"),
		row(3, general, "joe", "Great, the dashboards look green"),
		row(4, random, "alex", "anyone watching the match"),
		p,
	}
	b := row(5, general, "bosun", "The migration finished at 02:10 UTC.")
	b.Bosun, b.AuthorBot = true, true
	rows = append(rows, b)
	if err := s.Upsert(context.Background(), rows); err != nil {
		t.Fatal(err)
	}
	o := row(6, general, "joe", "migration in another server")
	o.GuildID = other
	if err := s.Upsert(context.Background(), []Row{o}); err != nil {
		t.Fatal(err)
	}
}

func texts(r Result) []string {
	var out []string
	for _, m := range r.Messages {
		out = append(out, m.Text)
	}
	return out
}

func TestUpsertIsIdempotentAndKeepsTheNewestEdit(t *testing.T) {
	ctx := context.Background()
	s, r, _ := setup(t)
	first := row(1, general, "joe", "first text")
	for range 2 {
		if err := s.Upsert(ctx, []Row{first}); err != nil {
			t.Fatal(err)
		}
	}
	edited := first
	edited.Content, edited.EditedAt = "edited text", day.Add(time.Hour)
	// A repeat in one batch: the last row wins and the batch is not refused.
	if err := s.Upsert(ctx, []Row{first, edited, row(2, general, "sam", "second")}); err != nil {
		t.Fatal(err)
	}
	// A history page read before the edit arrives late: it must not undo it.
	if err := s.Upsert(ctx, []Row{first}); err != nil {
		t.Fatal(err)
	}
	got, err := r.Day(ctx, DayArgs{Server: guild, Channel: general, Date: "2026-11-20"})
	if err != nil {
		t.Fatal(err)
	}
	if len(got.Messages) != 2 || got.Messages[0].Text != "edited text" || got.Messages[0].Edited == "" {
		t.Fatalf("got %+v", got.Messages)
	}
	if got.Messages[0].Link != "https://discord.com/channels/"+guild+"/"+general+"/"+first.ID {
		t.Fatalf("link %q", got.Messages[0].Link)
	}
}

func TestEditAndDelete(t *testing.T) {
	ctx := context.Background()
	s, r, _ := setup(t)
	seed(t, s)
	if err := s.Edit(ctx, Edit{ID: id(3), GuildID: guild, Content: "Great, all green now", EditedAt: day.Add(2 * time.Hour)}); err != nil {
		t.Fatal(err)
	}
	// An older edit loses.
	if err := s.Edit(ctx, Edit{ID: id(3), GuildID: guild, Content: "stale", EditedAt: day.Add(time.Hour)}); err != nil {
		t.Fatal(err)
	}
	// An edit naming the wrong server changes nothing.
	if err := s.Edit(ctx, Edit{ID: id(1), GuildID: other, Content: "hijack", EditedAt: day.Add(3 * time.Hour)}); err != nil {
		t.Fatal(err)
	}
	if err := s.Delete(ctx, guild, general, []string{id(2), id(9)}); err != nil {
		t.Fatal(err)
	}
	// The deleted message comes back from a page read before the delete.
	if err := s.Upsert(ctx, []Row{row(2, general, "sam", "I migrated the database last night")}); err != nil {
		t.Fatal(err)
	}
	// A message deleted before the archive saw it stays out.
	if err := s.Upsert(ctx, []Row{row(9, general, "sam", "secret")}); err != nil {
		t.Fatal(err)
	}
	// By ID: the tombstone has no channel name.
	got, err := r.Day(ctx, DayArgs{Server: guild, Channel: general, Date: "2026-11-20"})
	if err != nil {
		t.Fatal(err)
	}
	want := []string{
		"Morning! Who is running the migration today?", "[deleted]", "Great, all green now",
		"The migration finished at 02:10 UTC.", "[deleted]",
	}
	if strings.Join(texts(got), "|") != strings.Join(want, "|") {
		t.Fatalf("got %q, want %q", texts(got), want)
	}
	var content string
	if err := s.DB.QueryRow(`SELECT content FROM discord_message WHERE id = $1::bigint`, id(2)).Scan(&content); err != nil || content != "" {
		t.Fatalf("deleted content kept: %q %v", content, err)
	}
	found, err := r.Search(ctx, SearchArgs{Server: guild, Query: "database"})
	if err != nil || len(found.Messages) != 0 {
		t.Fatalf("search found a deleted message: %+v %v", found.Messages, err)
	}
}

func TestReconcileMarksMissingMessagesDeleted(t *testing.T) {
	ctx := context.Background()
	s, r, _ := setup(t)
	seed(t, s)
	// A page covering minutes 1 to 3 returned only 1 and 3.
	n, err := s.Reconcile(ctx, guild, general, id(1), id(3), []string{id(1), id(3)})
	if err != nil || n != 1 {
		t.Fatalf("reconcile marked %d, %v", n, err)
	}
	got, err := r.Around(ctx, AroundArgs{Server: guild, MessageID: id(2)})
	if err != nil {
		t.Fatal(err)
	}
	if texts(got)[1] != "[deleted]" || len(got.Messages) != 4 {
		t.Fatalf("got %q", texts(got))
	}
	if PrevID(id(3)) >= id(3) || PrevID("abc") != "abc" {
		t.Fatal("PrevID")
	}
}

func TestSearchFilters(t *testing.T) {
	ctx := context.Background()
	s, r, _ := setup(t)
	seed(t, s)
	for _, c := range []struct {
		name string
		args SearchArgs
		want []string
	}{
		// English stems: migrated, migration and migrating match.
		{"stem", SearchArgs{Query: "migrating"}, []string{id(2), id(1), id(5)}},
		{"phrase", SearchArgs{Query: `"the dashboards"`}, []string{id(3)}},
		{"author name", SearchArgs{Query: "migration", Author: "@sammy"}, []string{id(2)}},
		{"author id", SearchArgs{Author: joe}, []string{id(3), id(1)}},
		{"channel name", SearchArgs{Channel: "#random"}, []string{id(4)}},
		{"channel id has its threads", SearchArgs{Channel: general, Query: "plan"}, []string{id(30)}},
		{"time", SearchArgs{Query: "migration", From: day.Add(90 * time.Second).Format(time.RFC3339), To: "2026-11-20"}, []string{id(2), id(5)}},
		{"before the day", SearchArgs{Query: "migration", To: "2026-11-19"}, nil},
		{"limit", SearchArgs{Author: joe, Limit: 1}, []string{id(3)}},
	} {
		c.args.Server = guild
		got, err := r.Search(ctx, c.args)
		if err != nil {
			t.Fatalf("%s: %v", c.name, err)
		}
		var ids []string
		for _, m := range got.Messages {
			ids = append(ids, m.ID)
			if !strings.HasPrefix(m.Link, "https://discord.com/channels/"+guild+"/") {
				t.Fatalf("%s: other server's message %+v", c.name, m)
			}
		}
		if c.args.Query != "" {
			// Equal ranks tie; compare the matches, not their order.
			sort.Strings(ids)
			sort.Strings(c.want)
		}
		if strings.Join(ids, ",") != strings.Join(c.want, ",") {
			t.Errorf("%s: got %v, want %v", c.name, ids, c.want)
		}
		if c.name == "limit" && !got.Truncated {
			t.Errorf("limit: not marked truncated")
		}
	}
	got, err := r.Search(ctx, SearchArgs{Server: guild, Query: "finished"})
	if err != nil || len(got.Messages) != 1 || !got.Messages[0].Bosun || got.Messages[0].Author != "bosun" {
		t.Fatalf("Bosun's message: %+v %v", got.Messages, err)
	}
	if !strings.Contains(got.Note, "untrusted") {
		t.Fatalf("note %q", got.Note)
	}
}

func TestAroundAndDay(t *testing.T) {
	ctx := context.Background()
	s, r, _ := setup(t)
	seed(t, s)
	one, two := 1, 1
	got, err := r.Around(ctx, AroundArgs{Server: guild, MessageID: id(2), Before: &one, After: &two})
	if err != nil {
		t.Fatal(err)
	}
	if want := []string{id(1), id(2), id(3)}; len(got.Messages) != 3 || got.Messages[0].ID != want[0] || got.Messages[2].ID != want[2] {
		t.Fatalf("around: %+v", got.Messages)
	}
	// Day pages with next_after.
	page, err := r.Day(ctx, DayArgs{Server: guild, Channel: general, Date: "2026-11-20", Limit: 2})
	if err != nil || len(page.Messages) != 2 || !page.Truncated || page.NextAfter != id(2) {
		t.Fatalf("page 1: %+v %v", page, err)
	}
	page, err = r.Day(ctx, DayArgs{Server: guild, Channel: general, Date: "2026-11-20", Limit: 2, After: page.NextAfter})
	if err != nil || len(page.Messages) != 2 || page.Messages[0].ID != id(3) || page.Truncated || page.NextAfter != "" {
		t.Fatalf("page 2: %+v %v", page, err)
	}
	empty, err := r.Day(ctx, DayArgs{Server: guild, Channel: general, Date: "2026-11-21"})
	if err != nil || len(empty.Messages) != 0 || empty.Messages == nil {
		t.Fatalf("empty day: %+v %v", empty, err)
	}
}

func TestScope(t *testing.T) {
	ctx := context.Background()
	s, r, ro := setup(t)
	seed(t, s)
	var pe *ParamError
	if _, err := r.Search(ctx, SearchArgs{Server: "1512814732392927463", Query: "migration"}); !errors.As(err, &pe) || pe.Param != "server" {
		t.Fatalf("unlisted server: %v", err)
	}
	if _, err := r.Search(ctx, SearchArgs{Server: guild}); !errors.As(err, &pe) || pe.Param != "query" {
		t.Fatalf("no filter: %v", err)
	}
	// A message of another server is not found from this one.
	if _, err := r.Around(ctx, AroundArgs{Server: other, MessageID: id(1)}); !errors.Is(err, ErrNotFound) {
		t.Fatalf("around across servers: %v", err)
	}
	got, err := r.Search(ctx, SearchArgs{Server: other, Query: "migration"})
	if err != nil || len(got.Messages) != 1 || got.Messages[0].ID != id(6) {
		t.Fatalf("other server: %+v %v", got.Messages, err)
	}
	for _, bad := range []AroundArgs{{Server: guild, MessageID: "x"}, {Server: guild, MessageID: id(1), Before: ptr(MaxAround + 1)}} {
		if _, err := r.Around(ctx, bad); !errors.As(err, &pe) {
			t.Fatalf("%+v: %v", bad, err)
		}
	}
	if _, err := r.Day(ctx, DayArgs{Server: guild, Channel: general, Date: "20-11-2026"}); !errors.As(err, &pe) || pe.Param != "date" {
		t.Fatalf("bad date: %v", err)
	}
	// The read login cannot write.
	if _, err := ro.Exec(`DELETE FROM discord_message`); err == nil {
		t.Fatal("the reader deleted rows")
	}
}

func TestCaps(t *testing.T) {
	ctx := context.Background()
	s, r, _ := setup(t)
	long := strings.Repeat("é", MaxText) // two bytes each
	var rows []Row
	for i := range 40 {
		rows = append(rows, row(i, general, "joe", "cap "+long))
	}
	if err := s.Upsert(ctx, rows); err != nil {
		t.Fatal(err)
	}
	got, err := r.Search(ctx, SearchArgs{Server: guild, Query: "cap", Limit: MaxSearch})
	if err != nil {
		t.Fatal(err)
	}
	total := 0
	for _, m := range got.Messages {
		if !m.TextCut || len(m.Text) > MaxText+len("…") || !strings.HasSuffix(m.Text, "…") {
			t.Fatalf("text not cut: %d bytes", len(m.Text))
		}
		total += len(m.Text)
	}
	if !got.Truncated || total > MaxChars {
		t.Fatalf("answer of %d chars, truncated %v", total, got.Truncated)
	}
}

func TestPurge(t *testing.T) {
	ctx := context.Background()
	s, r, _ := setup(t)
	seed(t, s)
	n, err := s.Purge(ctx, other)
	if err != nil || n != 1 {
		t.Fatalf("purged %d, %v", n, err)
	}
	got, err := r.Search(ctx, SearchArgs{Server: other, Query: "migration"})
	if err != nil || len(got.Messages) != 0 {
		t.Fatalf("after purge: %+v %v", got.Messages, err)
	}
}

func ptr(n int) *int { return &n }

type fakeVerifier struct{}

func (fakeVerifier) Verify(_ context.Context, raw string) (actorjwt.Identity, error) {
	if raw != "good" {
		return actorjwt.Identity{}, &actorjwt.Error{Reason: actorjwt.ReasonSignature}
	}
	return actorjwt.Identity{Subject: "actor/discord-" + guild + "/session-x"}, nil
}

func serve(t *testing.T) *httptest.Server {
	t.Helper()
	s, r, _ := setup(t)
	seed(t, s)
	srv := httptest.NewServer(&Handler{Reader: r, Verifier: fakeVerifier{}, Log: slog.New(slog.NewTextHandler(io.Discard, nil))})
	t.Cleanup(srv.Close)
	return srv
}

func rpc(t *testing.T, srv *httptest.Server, token, body string, header ...string) (int, map[string]any) {
	t.Helper()
	req, _ := http.NewRequest(http.MethodPost, srv.URL+"/mcp", strings.NewReader(body))
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	for i := 0; i+1 < len(header); i += 2 {
		req.Header.Set(header[i], header[i+1])
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&out)
	return resp.StatusCode, out
}

func TestMCPAuth(t *testing.T) {
	srv := serve(t)
	list := `{"jsonrpc":"2.0","id":1,"method":"tools/list"}`
	if code, _ := rpc(t, srv, "", list); code != http.StatusUnauthorized {
		t.Fatalf("no token: %d", code)
	}
	if code, _ := rpc(t, srv, "bad", list); code != http.StatusUnauthorized {
		t.Fatalf("bad token: %d", code)
	}
	if code, _ := rpc(t, srv, "good", list, "Origin", "https://evil.example"); code != http.StatusForbidden {
		t.Fatalf("browser origin: %d", code)
	}
	code, out := rpc(t, srv, "good", list)
	if code != http.StatusOK {
		t.Fatalf("tools/list: %d", code)
	}
	tools := out["result"].(map[string]any)["tools"].([]any)
	if len(tools) != 4 {
		t.Fatalf("tools: %v", tools)
	}
	for _, tool := range tools {
		var schema map[string]any
		raw, _ := json.Marshal(tool.(map[string]any)["inputSchema"])
		if err := json.Unmarshal(raw, &schema); err != nil || schema["type"] != "object" {
			t.Fatalf("schema of %v: %v", tool, err)
		}
	}
}

func TestMCPCalls(t *testing.T) {
	srv := serve(t)
	call := func(args string) map[string]any {
		_, out := rpc(t, srv, "good", `{"jsonrpc":"2.0","id":2,"method":"tools/call","params":`+args+`}`)
		return out["result"].(map[string]any)
	}
	res := call(`{"name":"history_search","arguments":{"server":"` + guild + `","query":"dashboards"}}`)
	if res["isError"] != false {
		t.Fatalf("search: %v", res)
	}
	msgs := res["structuredContent"].(map[string]any)["messages"].([]any)
	if len(msgs) != 1 || msgs[0].(map[string]any)["id"] != id(3) {
		t.Fatalf("search messages: %v", msgs)
	}
	for _, c := range []struct{ args, want string }{
		{`{"name":"history_search","arguments":{"server":"` + guild + `","query":"x","typo":1}}`, "Invalid arguments"},
		{`{"name":"history_search","arguments":{"server":"999999999999999999","query":"x"}}`, "Invalid server"},
		{`{"name":"history_around","arguments":{"server":"` + other + `","message_id":"` + id(1) + `"}}`, "No message"},
		{`{"name":"history_tail","arguments":{}}`, "Unknown tool"},
	} {
		res := call(c.args)
		text := res["content"].([]any)[0].(map[string]any)["text"].(string)
		if res["isError"] != true || !strings.HasPrefix(text, c.want) {
			t.Errorf("%s: %v", c.args, res)
		}
	}
	got := call(`{"name":"history_day","arguments":{"server":"` + guild + `","channel":"` + general + `","date":"2026-11-20"}}`)
	if n := len(got["structuredContent"].(map[string]any)["messages"].([]any)); n != 4 {
		t.Fatalf("day: %d messages", n)
	}
}
