package archive

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/internal/pgtest"
)

func sqlRun(t *testing.T, r *Reader, server, q string) (SQLResult, error) {
	t.Helper()
	return r.SQL(context.Background(), SQLArgs{Server: server, Query: q})
}

func count(t *testing.T, r *Reader, server, q string) int64 {
	t.Helper()
	res, err := sqlRun(t, r, server, q)
	if err != nil {
		t.Fatalf("%s: %v", q, err)
	}
	if len(res.Rows) != 1 || len(res.Rows[0]) != 1 {
		t.Fatalf("%s: rows %v", q, res.Rows)
	}
	n, ok := res.Rows[0][0].(int64)
	if !ok {
		t.Fatalf("%s: %T %v", q, res.Rows[0][0], res.Rows[0][0])
	}
	return n
}

func TestSQLAggregates(t *testing.T) {
	s, r, _ := setup(t)
	seed(t, s)
	res, err := sqlRun(t, r, guild, `SELECT author_name, count(*) AS messages FROM discord_message
		WHERE NOT deleted AND NOT bosun GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 20`)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Join(res.Columns, ",") != "author_name,messages" || len(res.Rows) != 4 || res.Truncated {
		t.Fatalf("got %+v", res)
	}
	if res.Rows[0][0] != "joe" || res.Rows[0][1] != int64(2) {
		t.Fatalf("top poster %v", res.Rows[0])
	}
	// The documented word-count query: stop words go, words stay unstemmed.
	words, err := sqlRun(t, r, guild, `SELECT word, nentry FROM ts_stat($$SELECT to_tsvector('simple', content)
		FROM discord_message WHERE NOT deleted AND NOT bosun$$) WHERE length(word) > 2
		AND length(to_tsvector('english', word)) > 0 ORDER BY nentry DESC, word LIMIT 150`)
	if err != nil {
		t.Fatal(err)
	}
	got := map[string]bool{}
	for _, w := range words.Rows {
		got[w[0].(string)] = true
	}
	if !got["migration"] || !got["dashboards"] || got["the"] || got["who"] {
		t.Fatalf("words %v", words.Rows)
	}
	// Times come back as RFC 3339.
	first, err := sqlRun(t, r, guild, `SELECT min(sent_at) FROM discord_message`)
	if err != nil || first.Rows[0][0] != day.Add(time.Minute).Format(time.RFC3339) {
		t.Fatalf("first %v %v", first.Rows, err)
	}
}

// Each server's login reads only its rows, whatever the query tries.
func TestSQLRowSecurity(t *testing.T) {
	s, r, _ := setup(t)
	seed(t, s)
	if _, err := s.DB.Exec("ANALYZE discord_message"); err != nil {
		t.Fatal(err)
	}
	if n := count(t, r, guild, "SELECT count(*) FROM discord_message"); n != 6 {
		t.Fatalf("own rows: %d", n)
	}
	if n := count(t, r, other, "SELECT count(*) FROM discord_message"); n != 1 {
		t.Fatalf("other server's own rows: %d", n)
	}
	for _, q := range []string{
		"SELECT count(*) FROM discord_message WHERE guild_id = " + other,
		"SELECT count(*) FROM (SELECT * FROM discord_message) m WHERE m.guild_id = " + other,
		"WITH m AS (SELECT * FROM discord_message) SELECT count(*) FROM m WHERE guild_id = " + other,
		// A session setting does not change the policy, which keys on current_user.
		"SELECT count(*) FROM discord_message, set_config('app.guild', '" + other + "', true) WHERE guild_id = " + other,
		// SPI runs as the caller too.
		"SELECT count(*) FROM ts_stat($$SELECT to_tsvector('simple', content) FROM discord_message$$) WHERE word = 'another'",
		"SELECT cardinality(xpath('/table/row', query_to_xml('SELECT id FROM discord_message WHERE guild_id = " + other + "', false, false, '')))",
		// Planner statistics of a row-secured table are hidden.
		"SELECT count(*) FROM pg_stats WHERE tablename = 'discord_message'",
	} {
		if n := count(t, r, guild, q); n != 0 {
			t.Errorf("%s: %d rows of the other server", q, n)
		}
	}
	// The probes do read the caller's own rows.
	if n := count(t, r, guild, "SELECT cardinality(xpath('/table/row', query_to_xml('SELECT id FROM discord_message', false, false, '')))"); n != 6 {
		t.Errorf("query_to_xml own rows: %d", n)
	}
	// set_config('role') is SET ROLE: the login is a member of no other server's role.
	for _, role := range []string{sqlGroup + "_" + other, reader, "factory_connector"} {
		_, err := sqlRun(t, r, guild, "SELECT set_config('role', '"+role+"', true)")
		var qe *QueryError
		if !errors.As(err, &qe) {
			t.Errorf("set_config role %s: %v", role, err)
		}
	}

	// Outside the tool too: the login itself, on a plain connection.
	login := pgtest.AsRole(t, s.DB, sqlGroup+"_"+guild)
	for _, role := range []string{sqlGroup + "_" + other, reader, "factory_connector"} {
		if _, err := login.Exec("SET ROLE " + role); err == nil {
			t.Errorf("SET ROLE %s allowed", role)
		}
	}
	if _, err := login.Exec("SET SESSION AUTHORIZATION factory_connector"); err == nil {
		t.Error("SET SESSION AUTHORIZATION allowed")
	}
	// The group role is a member's only other role, and sees nothing.
	conn, err := login.Conn(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	if _, err := conn.ExecContext(context.Background(), "SET ROLE "+sqlGroup); err != nil {
		t.Fatal(err)
	}
	var n int
	if err := conn.QueryRowContext(context.Background(), "SELECT count(*) FROM discord_message").Scan(&n); err != nil || n != 0 {
		t.Fatalf("as the group: %d rows, %v", n, err)
	}
	for _, q := range []string{
		"DELETE FROM discord_message",
		"UPDATE discord_message SET content = ''",
		"INSERT INTO discord_message (id, guild_id, channel_id, sent_at) VALUES (1, 1, 1, now())",
		"CREATE TABLE public.x (a int)",
		"SELECT pg_read_file('/etc/passwd')",
		"ALTER TABLE discord_message DISABLE ROW LEVEL SECURITY",
	} {
		if _, err := login.Exec(q); err == nil {
			t.Errorf("%s allowed", q)
		}
	}
}

func TestSQLRejects(t *testing.T) {
	s, r, _ := setup(t)
	seed(t, s)
	for _, c := range []struct {
		q     string
		param bool
	}{
		{"SELECT 1; SELECT 2", false},
		{"SELECT 1; DELETE FROM discord_message", false},
		{"SET ROLE factory_connector", true},
		{"EXPLAIN SELECT 1", true},
		{"DO $$ BEGIN END $$", true},
		{"COPY discord_message TO STDOUT", true},
		{"-- only a comment", true},
		{"", true},
		{"WITH d AS (DELETE FROM discord_message RETURNING 1) SELECT count(*) FROM d", false},
		{"SELECT * INTO copy FROM discord_message", false},
		{"SELECT nope FROM discord_message", false},
	} {
		_, err := sqlRun(t, r, guild, c.q)
		var (
			pe *ParamError
			qe *QueryError
		)
		if c.param && !errors.As(err, &pe) || !c.param && !errors.As(err, &qe) {
			t.Errorf("%q: %T %v", c.q, err, err)
		}
	}
	// Comments and parentheses before the keyword are fine.
	for _, q := range []string{"/* c */ (SELECT count(*) FROM discord_message)", "-- c\nSELECT count(*) FROM discord_message;"} {
		if n := count(t, r, guild, q); n != 6 {
			t.Errorf("%q: %d", q, n)
		}
	}
	// A server off the allowlist, or without a login.
	if _, err := sqlRun(t, r, "999999999999999999", "SELECT 1"); err == nil {
		t.Error("unlisted server allowed")
	}
	delete(r.SQLDB, other)
	var pe *ParamError
	if _, err := sqlRun(t, r, other, "SELECT 1"); !errors.As(err, &pe) {
		t.Errorf("server without a login: %v", err)
	}
	// The rows still exist: nothing above wrote.
	if n := count(t, r, guild, "SELECT count(*) FROM discord_message"); n != 6 {
		t.Fatalf("rows after the refused writes: %d", n)
	}
}

func TestSQLTimeout(t *testing.T) {
	_, r, _ := setup(t)
	old := sqlTimeout
	sqlTimeout = 500 * time.Millisecond
	t.Cleanup(func() { sqlTimeout = old })
	for _, q := range []string{
		"SELECT pg_sleep(5)",
		// Turning the timeout off inside the statement does not stop it.
		"SELECT set_config('statement_timeout', '0', false), pg_sleep(5)",
	} {
		start := time.Now()
		_, err := sqlRun(t, r, guild, q)
		var qe *QueryError
		if !errors.As(err, &qe) || !strings.Contains(qe.Message, "longer than") {
			t.Errorf("%s: %v", q, err)
		}
		if d := time.Since(start); d > 3*time.Second {
			t.Errorf("%s: took %s", q, d)
		}
	}
	// A session-level set_config is rolled back with the transaction.
	res, err := sqlRun(t, r, guild, "SELECT current_setting('statement_timeout')")
	if err != nil || res.Rows[0][0] != "500ms" {
		t.Fatalf("timeout after the call: %v %v", res.Rows, err)
	}
}

func TestSQLCaps(t *testing.T) {
	_, r, _ := setup(t)
	res, err := sqlRun(t, r, guild, "SELECT g FROM generate_series(1, 5000) g")
	if err != nil {
		t.Fatal(err)
	}
	if len(res.Rows) != MaxSQLRows || !res.Truncated {
		t.Fatalf("rows %d truncated %v", len(res.Rows), res.Truncated)
	}
	res, err = sqlRun(t, r, guild, "SELECT repeat('x', 5000) FROM generate_series(1, 100)")
	if err != nil {
		t.Fatal(err)
	}
	if !res.Truncated || len(res.Rows) == 0 || len(res.Rows) >= 100 {
		t.Fatalf("bytes cap: %d rows truncated %v", len(res.Rows), res.Truncated)
	}
	if v := res.Rows[0][0].(string); len(v) > MaxText+len("…") {
		t.Fatalf("cell not cut: %d bytes", len(v))
	}
	res, err = sqlRun(t, r, guild, "SELECT 1 WHERE false")
	if err != nil || len(res.Rows) != 0 || res.Truncated || len(res.Columns) != 1 {
		t.Fatalf("empty: %+v %v", res, err)
	}
}

func TestMCPSQL(t *testing.T) {
	srv := serve(t)
	_, out := rpc(t, srv, "good", `{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"history_sql","arguments":{"server":"`+
		guild+`","query":"SELECT count(*) AS n FROM discord_message WHERE NOT bosun"}}}`)
	res := out["result"].(map[string]any)
	if res["isError"] != false {
		t.Fatalf("sql: %v", res)
	}
	sc := res["structuredContent"].(map[string]any)
	if rows := sc["rows"].([]any); len(rows) != 1 || rows[0].([]any)[0] != float64(5) {
		t.Fatalf("rows %v", sc)
	}
	_, out = rpc(t, srv, "good", `{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"history_sql","arguments":{"server":"`+
		guild+`","query":"SELECT nope"}}}`)
	res = out["result"].(map[string]any)
	if text := res["content"].([]any)[0].(map[string]any)["text"].(string); res["isError"] != true || !strings.HasPrefix(text, "PostgreSQL: ") {
		t.Fatalf("bad query: %v", res)
	}
}
