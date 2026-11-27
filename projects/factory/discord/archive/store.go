// Package archive is Bosun's verbatim message archive (HOME-909): one
// PostgreSQL row per Discord message of the allowlisted servers, with a
// full-text index. The bridge writes it (Store); bosun-history reads it for
// Bosun's history tools over MCP (Reader, Handler). Hindsight keeps extracted
// memories; this archive keeps the exact messages, so Bosun can quote one,
// date it and read the messages around it.
//
// Every row is untrusted text from Discord users. A deleted message keeps its
// IDs and time and loses its text.
package archive

import (
	"context"
	"database/sql"
	"fmt"
	"strconv"
	"time"

	"github.com/lib/pq"
)

// Schema creates the archive's table and indexes. Each statement is safe to
// run again. search indexes the text with the english configuration (stems)
// and the simple one (exact words and names), and the author's name.
const Schema = `
CREATE TABLE IF NOT EXISTS discord_message (
	id               bigint PRIMARY KEY,
	guild_id         bigint NOT NULL,
	channel_id       bigint NOT NULL,
	channel_name     text NOT NULL DEFAULT '',
	thread_parent_id bigint,
	author_id        bigint,
	author_name      text NOT NULL DEFAULT '',
	author_bot       boolean NOT NULL DEFAULT false,
	bosun            boolean NOT NULL DEFAULT false,
	sent_at          timestamptz NOT NULL,
	edited_at        timestamptz,
	reply_to         bigint,
	content          text NOT NULL DEFAULT '',
	deleted          boolean NOT NULL DEFAULT false,
	archived_at      timestamptz NOT NULL DEFAULT now(),
	search           tsvector GENERATED ALWAYS AS (
		to_tsvector('english'::regconfig, content) ||
		to_tsvector('simple'::regconfig, author_name || ' ' || content)
	) STORED
);
CREATE INDEX IF NOT EXISTS discord_message_search ON discord_message USING gin (search);
CREATE INDEX IF NOT EXISTS discord_message_channel_time ON discord_message (guild_id, channel_id, sent_at);
CREATE INDEX IF NOT EXISTS discord_message_guild_time ON discord_message (guild_id, sent_at);
`

// Migrate applies Schema and lets reader (a login role; empty for none)
// read the table and nothing else.
func Migrate(ctx context.Context, db *sql.DB, reader string) error {
	if _, err := db.ExecContext(ctx, Schema); err != nil {
		return fmt.Errorf("archive schema: %w", err)
	}
	if reader == "" {
		return nil
	}
	if _, err := db.ExecContext(ctx, "GRANT SELECT ON discord_message TO "+pq.QuoteIdentifier(reader)); err != nil {
		return fmt.Errorf("archive grant: %w", err)
	}
	return nil
}

// Row is one archived message. IDs are Discord snowflakes as decimal text;
// an empty optional ID is NULL.
type Row struct {
	ID, GuildID, ChannelID string
	// ChannelName is the channel's or thread's name when the row was written.
	ChannelName string
	// ThreadParentID is a thread's parent channel; empty for a channel.
	ThreadParentID string
	AuthorID       string
	// AuthorName is "username" or "username (display name)".
	AuthorName string
	AuthorBot  bool
	// Bosun marks Bosun's own messages.
	Bosun    bool
	SentAt   time.Time
	EditedAt time.Time // zero: never edited
	ReplyTo  string
	// Content is the text with mentions named and attachment, embed and
	// sticker placeholders.
	Content string
}

// Edit is a gateway MESSAGE_UPDATE that changed a message's text.
type Edit struct {
	ID, GuildID string
	Content     string
	EditedAt    time.Time
}

// Store writes the archive.
type Store struct {
	DB *sql.DB
}

// Upsert writes rows by message ID. A repeat changes nothing; a row with a
// newer or equal edit time replaces the text; a deleted message stays
// deleted, so a history page read before the delete cannot restore it.
func (s *Store) Upsert(ctx context.Context, rows []Row) error {
	if len(rows) == 0 {
		return nil
	}
	n := len(rows)
	var (
		ids, guilds, channels, cnames, parents, authors, anames = make([]string, n), make([]string, n), make([]string, n), make([]string, n), make([]string, n), make([]string, n), make([]string, n)
		sent, edited, replies, contents                         = make([]string, n), make([]string, n), make([]string, n), make([]string, n)
		bots, bosun                                             = make([]bool, n), make([]bool, n)
	)
	for i, r := range rows {
		for _, id := range []string{r.ID, r.GuildID, r.ChannelID} {
			if !snowflake(id) {
				return fmt.Errorf("archive: row %d: %q is not a snowflake", i, id)
			}
		}
		ids[i], guilds[i], channels[i], cnames[i] = r.ID, r.GuildID, r.ChannelID, r.ChannelName
		parents[i], authors[i], anames[i], replies[i] = optional(r.ThreadParentID), optional(r.AuthorID), r.AuthorName, optional(r.ReplyTo)
		sent[i], contents[i] = r.SentAt.UTC().Format(time.RFC3339Nano), r.Content
		if !r.EditedAt.IsZero() {
			edited[i] = r.EditedAt.UTC().Format(time.RFC3339Nano)
		}
		bots[i], bosun[i] = r.AuthorBot, r.Bosun
	}
	_, err := s.DB.ExecContext(ctx, `
INSERT INTO discord_message AS m (id, guild_id, channel_id, channel_name, thread_parent_id, author_id,
	author_name, author_bot, bosun, sent_at, edited_at, reply_to, content)
SELECT u.id::bigint, u.guild::bigint, u.channel::bigint, u.cname, NULLIF(u.parent, '')::bigint,
	NULLIF(u.author, '')::bigint, u.aname, u.abot, u.bosun, u.sent::timestamptz,
	NULLIF(u.edited, '')::timestamptz, NULLIF(u.reply, '')::bigint, u.content
FROM unnest($1::text[], $2::text[], $3::text[], $4::text[], $5::text[], $6::text[], $7::text[],
	$8::boolean[], $9::boolean[], $10::text[], $11::text[], $12::text[], $13::text[])
	AS u(id, guild, channel, cname, parent, author, aname, abot, bosun, sent, edited, reply, content)
ON CONFLICT (id) DO UPDATE SET
	channel_name = EXCLUDED.channel_name,
	thread_parent_id = COALESCE(EXCLUDED.thread_parent_id, m.thread_parent_id),
	author_name = EXCLUDED.author_name,
	edited_at = COALESCE(EXCLUDED.edited_at, m.edited_at),
	reply_to = COALESCE(EXCLUDED.reply_to, m.reply_to),
	content = EXCLUDED.content
WHERE NOT m.deleted
	AND COALESCE(EXCLUDED.edited_at, '-infinity') >= COALESCE(m.edited_at, '-infinity')
	AND (m.content, m.channel_name, m.author_name, m.edited_at, m.thread_parent_id, m.reply_to)
		IS DISTINCT FROM (EXCLUDED.content, EXCLUDED.channel_name, EXCLUDED.author_name,
			COALESCE(EXCLUDED.edited_at, m.edited_at), COALESCE(EXCLUDED.thread_parent_id, m.thread_parent_id),
			COALESCE(EXCLUDED.reply_to, m.reply_to))`,
		pq.StringArray(ids), pq.StringArray(guilds), pq.StringArray(channels), pq.StringArray(cnames),
		pq.StringArray(parents), pq.StringArray(authors), pq.StringArray(anames), pq.BoolArray(bots),
		pq.BoolArray(bosun), pq.StringArray(sent), pq.StringArray(edited), pq.StringArray(replies),
		pq.StringArray(contents))
	return err
}

// Edit replaces an archived message's text. An older edit, an unknown
// message or a deleted one changes nothing.
func (s *Store) Edit(ctx context.Context, e Edit) error {
	if !snowflake(e.ID) || !snowflake(e.GuildID) {
		return fmt.Errorf("archive: edit of %q in %q: not snowflakes", e.ID, e.GuildID)
	}
	at := e.EditedAt
	if at.IsZero() {
		at = time.Now()
	}
	_, err := s.DB.ExecContext(ctx, `
UPDATE discord_message SET content = $3, edited_at = $4
WHERE id = $1::bigint AND guild_id = $2::bigint AND NOT deleted
	AND (edited_at IS NULL OR edited_at <= $4)`, e.ID, e.GuildID, e.Content, at.UTC())
	return err
}

// Delete marks messages deleted and drops their text and reply link
// (Discord's developer policy: deleted content is not kept). A message the
// archive does not hold yet gets a tombstone, so a later history read cannot
// add it.
func (s *Store) Delete(ctx context.Context, guildID, channelID string, ids []string) error {
	if !snowflake(guildID) || !snowflake(channelID) {
		return fmt.Errorf("archive: delete in %q/%q: not snowflakes", guildID, channelID)
	}
	var keep, sent []string
	for _, id := range ids {
		if !snowflake(id) {
			continue
		}
		keep = append(keep, id)
		sent = append(sent, SnowflakeTime(id).UTC().Format(time.RFC3339Nano))
	}
	if len(keep) == 0 {
		return nil
	}
	_, err := s.DB.ExecContext(ctx, `
INSERT INTO discord_message AS m (id, guild_id, channel_id, sent_at, deleted)
SELECT u.id::bigint, $2::bigint, $3::bigint, u.sent::timestamptz, true
FROM unnest($1::text[], $4::text[]) AS u(id, sent)
ON CONFLICT (id) DO UPDATE SET deleted = true, content = '', reply_to = NULL
WHERE m.guild_id = $2::bigint`, pq.StringArray(keep), guildID, channelID, pq.StringArray(sent))
	return err
}

// Reconcile marks deleted the archived messages of a channel with IDs from
// oldest to newest, both included, that are not in present: a history page
// covered that range and Discord no longer returns them. It returns how many
// it marked.
func (s *Store) Reconcile(ctx context.Context, guildID, channelID, oldest, newest string, present []string) (int64, error) {
	for _, id := range []string{guildID, channelID, oldest, newest} {
		if !snowflake(id) {
			return 0, fmt.Errorf("archive: reconcile: %q is not a snowflake", id)
		}
	}
	res, err := s.DB.ExecContext(ctx, `
UPDATE discord_message SET deleted = true, content = '', reply_to = NULL
WHERE guild_id = $1::bigint AND channel_id = $2::bigint AND NOT deleted
	AND id BETWEEN $3::bigint AND $4::bigint AND id <> ALL ($5::bigint[])`,
		guildID, channelID, oldest, newest, pq.StringArray(present))
	if err != nil {
		return 0, err
	}
	return res.RowsAffected()
}

// Purge deletes every row of a server: the archive keeps nothing of it.
func (s *Store) Purge(ctx context.Context, guildID string) (int64, error) {
	if !snowflake(guildID) {
		return 0, fmt.Errorf("archive: purge of %q: not a snowflake", guildID)
	}
	res, err := s.DB.ExecContext(ctx, `DELETE FROM discord_message WHERE guild_id = $1::bigint`, guildID)
	if err != nil {
		return 0, err
	}
	return res.RowsAffected()
}

// discordEpoch is the time of snowflake 0, in milliseconds.
const discordEpoch = 1420070400000

// SnowflakeTime is the time in a snowflake; the zero time when it is not one.
func SnowflakeTime(id string) time.Time {
	n, err := strconv.ParseUint(id, 10, 64)
	if err != nil {
		return time.Time{}
	}
	return time.UnixMilli(int64(n>>22) + discordEpoch)
}

// PrevID is the snowflake just before id: a page read before id covers the
// IDs up to PrevID(id).
func PrevID(id string) string {
	n, err := strconv.ParseUint(id, 10, 64)
	if err != nil || n == 0 {
		return id
	}
	return strconv.FormatUint(n-1, 10)
}

func snowflake(s string) bool {
	if len(s) < 15 || len(s) > 20 {
		return false
	}
	n, err := strconv.ParseUint(s, 10, 64)
	return err == nil && n <= 1<<63-1
}

func optional(id string) string {
	if snowflake(id) {
		return id
	}
	return ""
}
