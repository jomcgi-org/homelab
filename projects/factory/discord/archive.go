package discord

import (
	"context"
	"encoding/json"
	"log/slog"
	"strings"
	"sync"
	"time"

	"github.com/prometheus/client_golang/prometheus"

	"github.com/jomcgi/homelab/projects/factory/discord/archive"
)

// ArchiveStore writes the verbatim message archive (archive.Store).
type ArchiveStore interface {
	Upsert(ctx context.Context, rows []archive.Row) error
	Edit(ctx context.Context, e archive.Edit) error
	Delete(ctx context.Context, guildID, channelID string, ids []string) error
	Reconcile(ctx context.Context, guildID, channelID, oldest, newest string, present []string) (int64, error)
}

// Archive bounds.
const (
	defaultArchiveQueue = 2048
	archiveBatch        = 200
	archiveRetryWait    = 2 * time.Second
	archiveRetries      = 3
)

// Archiver keeps the verbatim archive of the allowlisted servers (HOME-909):
// every conversation message, Bosun's own included, with edits and deletes.
// Live gateway events go through a bounded queue, so Event never blocks the
// gateway's reader; the history backfill writes each page it reads (Page).
// Joe's operator channels and their threads are never archived, as they are
// never ingested.
type Archiver struct {
	Store  ArchiveStore
	Places *Places
	Config Config
	// IncludeBots keeps other bots' and webhooks' messages; Bosun's are
	// always kept.
	IncludeBots bool
	Log         *slog.Logger
	Metrics     *ArchiveMetrics
	// Queue bounds the live events waiting to be written.
	Queue int
	// Unsafe names Joe's unsafe mode turns (HOME-938, the Bridge); nil
	// names none. Unsafe turns and their replies are never archived.
	Unsafe UnsafeMarks

	mu    sync.Mutex
	botID string
	once  sync.Once
	ops   chan archiveOp
	// unsafe holds the messages that an unsafe mode reply answers, which a
	// later (older) backfill page or a live edit must not archive.
	unsafe map[string]bool
}

// UnsafeMarks names unsafe mode turns (Bridge.UnsafeTurn, unsafe.go).
type UnsafeMarks interface {
	// UnsafeTurn decides whether a live message is an unsafe turn.
	UnsafeTurn(m Message) bool
	// UnsafeMessage reports a message already decided an unsafe turn.
	UnsafeMessage(id string) bool
}

// maxArchiveUnsafe bounds Archiver.unsafe; past it the set starts again.
const maxArchiveUnsafe = 4096

// skipUnsafe reports whether m is an unsafe mode turn or reply. With live,
// a message of Joe's is decided now; a backfill page only reads decisions.
func (a *Archiver) skipUnsafe(m HistoryMessage, live bool) bool {
	if ref, ok := unsafeReplyRef(m, a.bot()); ok {
		a.mu.Lock()
		if a.unsafe == nil || len(a.unsafe) >= maxArchiveUnsafe {
			a.unsafe = map[string]bool{}
		}
		if ref != "" {
			a.unsafe[ref] = true
		}
		a.mu.Unlock()
		return true
	}
	a.mu.Lock()
	skip := a.unsafe[m.ID]
	a.mu.Unlock()
	if skip || a.Unsafe == nil {
		return skip
	}
	if live {
		return a.Unsafe.UnsafeTurn(m.Message)
	}
	return a.Unsafe.UnsafeMessage(m.ID)
}

type archiveOp struct {
	rows []archive.Row
	edit *archive.Edit
	del  *archiveDelete
}

type archiveDelete struct {
	guildID, channelID string
	ids                []string
}

// SetBot records Bosun's user, from READY.
func (a *Archiver) SetBot(u User) {
	a.mu.Lock()
	a.botID = u.ID
	a.mu.Unlock()
}

func (a *Archiver) bot() string {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.botID
}

func (a *Archiver) queue() chan archiveOp {
	a.once.Do(func() { a.ops = make(chan archiveOp, orInt(a.Queue, defaultArchiveQueue)) })
	return a.ops
}

// Event takes one gateway dispatch. It decodes and queues, and never blocks:
// when the queue is full the event is dropped and counted. A missed create
// comes back with the next backfill pass, and so does a missed delete
// (Reconcile).
func (a *Archiver) Event(t string, data json.RawMessage) {
	if a == nil {
		return
	}
	var op archiveOp
	switch t {
	case "MESSAGE_CREATE":
		var m HistoryMessage
		if json.Unmarshal(data, &m) != nil || !a.live(m.GuildID) {
			return
		}
		place := a.Places.Lookup(m.GuildID, m.ChannelID)
		parent, known := a.Places.parentOf(m.GuildID, m.ChannelID)
		if !a.admit(m.GuildID, m.ChannelID, parent, known) || a.skipUnsafe(m, true) {
			return
		}
		r, ok := a.row(m.GuildID, Channel{ID: m.ChannelID, Name: place.Channel.Name, ParentID: parent}, m)
		if !ok {
			return
		}
		op.rows = []archive.Row{r}
	case "MESSAGE_UPDATE":
		var m HistoryMessage
		if json.Unmarshal(data, &m) != nil || m.Edited == nil || m.GuildID == "" {
			return // Not an edit: an embed unfurl or a pin.
		}
		if _, ok := a.Config.Guilds[m.GuildID]; !ok || a.skipUnsafe(m, false) {
			return
		}
		// An edit changes only a row the archive holds, so no admission
		// check is needed beyond the allowlist.
		op.edit = &archive.Edit{ID: m.ID, GuildID: m.GuildID, Content: a.text(m.GuildID, m), EditedAt: *m.Edited}
	case "MESSAGE_DELETE", "MESSAGE_DELETE_BULK":
		var d struct {
			ID        string   `json:"id"`
			IDs       []string `json:"ids"`
			ChannelID string   `json:"channel_id"`
			GuildID   string   `json:"guild_id"`
		}
		if json.Unmarshal(data, &d) != nil || d.GuildID == "" {
			return
		}
		// A delete can leave a tombstone, so it is admitted as a create is:
		// no row ever names an operator or unlisted channel.
		parent, known := a.Places.parentOf(d.GuildID, d.ChannelID)
		if !a.admit(d.GuildID, d.ChannelID, parent, known) {
			return
		}
		ids := d.IDs
		if d.ID != "" {
			ids = append(ids, d.ID)
		}
		op.del = &archiveDelete{guildID: d.GuildID, channelID: d.ChannelID, ids: ids}
	default:
		return
	}
	select {
	case a.queue() <- op:
	default:
		a.Metrics.dropped()
		a.warn("archive queue full; event dropped", "event", t)
	}
}

// live reports whether a server's live messages are archived: it is
// allowlisted and ingests, so the gateway sends every message's text.
func (a *Archiver) live(guildID string) bool {
	g, ok := a.Config.Guilds[guildID]
	return ok && g.Ingest
}

// admit reports whether a channel's messages are archived. A server's channel
// list admits only listed IDs, as Bridge.Accept does. In the operator's
// server, a channel whose parent is unknown is not archived: it could be an
// operator thread.
func (a *Archiver) admit(guildID, channelID, parentID string, known bool) bool {
	g, ok := a.Config.Guilds[guildID]
	if !ok {
		return false
	}
	if len(g.Channels) > 0 && !contains(g.Channels, channelID) {
		return false
	}
	o := a.Config.Operator
	if o.enabled() && guildID == o.Guild {
		if !known || contains(o.Channels, channelID) || contains(o.Channels, parentID) {
			return false
		}
	}
	return true
}

// row makes the archive row of one message, or reports that it is not
// archived: a system message, another bot's or a webhook's (unless
// IncludeBots), or one with nothing to show.
func (a *Archiver) row(guildID string, c Channel, m HistoryMessage) (archive.Row, bool) {
	if !conversationTypes[m.Type] || m.ID == "" {
		return archive.Row{}, false
	}
	bot := a.bot()
	bosun := bot != "" && m.Author.ID == bot
	if (m.Author.Bot || m.WebhookID != "") && !bosun && !a.IncludeBots {
		return archive.Row{}, false
	}
	text := a.text(guildID, m)
	if text == "" {
		return archive.Row{}, false
	}
	author := m.Author
	if m.Member != nil {
		author.Member = m.Member
	}
	r := archive.Row{
		ID: m.ID, GuildID: guildID, ChannelID: c.ID, ChannelName: c.Name, ThreadParentID: c.ParentID,
		AuthorID: author.ID, AuthorName: strings.TrimPrefix(mentionName(author), "@"),
		AuthorBot: author.Bot || m.WebhookID != "", Bosun: bosun,
		SentAt: SentAt(m.Message), Content: text,
	}
	if m.Edited != nil {
		r.EditedAt = *m.Edited
	}
	if m.Type == 19 && m.Referenced != nil {
		r.ReplyTo = m.Referenced.ID
	}
	return r, true
}

// text is a message's archived text: its text with mentions named, then a
// placeholder for each attachment, embed and sticker, and for a forward.
func (a *Archiver) text(guildID string, m HistoryMessage) string {
	a.Places.Learn(guildID, m.Message.Users()...)
	var parts []string
	if s := a.Places.Resolve(guildID, m.Text(), m.Mentions); s != "" {
		parts = append(parts, s)
	}
	if m.Forwarded() {
		parts = append(parts, "[forwarded message]")
	}
	for _, at := range m.Attachments {
		parts = append(parts, "[attachment: "+at.Filename+"]")
	}
	for _, e := range m.Embeds {
		if e.Title != "" {
			parts = append(parts, "[embed: "+e.Title+"]")
		} else {
			parts = append(parts, "[embed]")
		}
	}
	for _, s := range m.Stickers {
		parts = append(parts, "[sticker: "+s.Name+"]")
	}
	return strings.Join(parts, " ")
}

// Page archives one page of a channel's history, newest first as Discord
// returns it. before is the cursor the page was read with: empty for the
// newest page. Archived messages from the page's oldest message up to the
// cursor that Discord did not return are marked deleted, so a delete the
// gateway missed is caught on the next pass. Errors only log: the archive
// never stops the memory backfill.
func (a *Archiver) Page(ctx context.Context, guildID string, c Channel, before string, page []HistoryMessage) {
	if a == nil || len(page) == 0 || !a.admit(guildID, c.ID, c.ParentID, true) {
		return
	}
	var rows []archive.Row
	present := make([]string, 0, len(page))
	for _, m := range page {
		present = append(present, m.ID)
		if a.skipUnsafe(m, false) {
			continue
		}
		if r, ok := a.row(guildID, c, m); ok {
			rows = append(rows, r)
		}
	}
	if err := a.Store.Upsert(ctx, rows); err != nil {
		a.Metrics.failed("backfill", "upsert")
		a.warn("archive backfill write failed", "guild", guildID, "channel", c.ID, "error", err)
		return
	}
	a.Metrics.wrote("backfill", "upsert", len(rows))
	// The newest page covers up to its newest message only: a message sent
	// after the read is not missing.
	newest := page[0].ID
	if before != "" {
		newest = archive.PrevID(before)
	}
	n, err := a.Store.Reconcile(ctx, guildID, c.ID, page[len(page)-1].ID, newest, present)
	if err != nil {
		a.Metrics.failed("backfill", "reconcile")
		a.warn("archive reconcile failed", "guild", guildID, "channel", c.ID, "error", err)
		return
	}
	a.Metrics.wrote("backfill", "reconcile", int(n))
}

// Run writes queued live events until ctx ends, then for at most a few
// seconds more. Consecutive creates go in one batch.
func (a *Archiver) Run(ctx context.Context) {
	q := a.queue()
	for {
		select {
		case <-ctx.Done():
			final, cancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
			defer cancel()
			for {
				select {
				case op := <-q:
					a.apply(final, op)
				default:
					return
				}
			}
		case op := <-q:
			// Gather the creates waiting behind this one; an edit or delete
			// ends the batch, so the order holds.
			var pending *archiveOp
		gather:
			for op.rows != nil && len(op.rows) < archiveBatch {
				select {
				case next := <-q:
					if next.rows != nil {
						op.rows = append(op.rows, next.rows...)
						continue
					}
					pending = &next
					break gather
				default:
					break gather
				}
			}
			a.apply(ctx, op)
			if pending != nil {
				a.apply(ctx, *pending)
			}
		}
	}
}

// apply writes one op, and tries again after a failure. A write that still
// fails is dropped and counted.
func (a *Archiver) apply(ctx context.Context, op archiveOp) {
	kind, n := "upsert", len(op.rows)
	write := func() error { return a.Store.Upsert(ctx, op.rows) }
	switch {
	case op.edit != nil:
		kind, n = "edit", 1
		write = func() error { return a.Store.Edit(ctx, *op.edit) }
	case op.del != nil:
		kind, n = "delete", len(op.del.ids)
		write = func() error { return a.Store.Delete(ctx, op.del.guildID, op.del.channelID, op.del.ids) }
	}
	wait := archiveRetryWait
	for attempt := 0; ; attempt++ {
		err := write()
		if err == nil {
			a.Metrics.wrote("live", kind, n)
			return
		}
		if attempt == archiveRetries || sleep(ctx, wait) != nil {
			a.Metrics.failed("live", kind)
			a.warn("archive write failed; dropped", "op", kind, "error", err)
			return
		}
		wait *= 2
	}
}

func (a *Archiver) warn(msg string, args ...any) {
	if a.Log != nil {
		a.Log.Warn(msg, args...)
	}
}

// parentOf returns a cached channel's thread parent, and whether the channel
// is cached.
func (p *Places) parentOf(guildID, channelID string) (string, bool) {
	if p == nil {
		return "", false
	}
	p.mu.RLock()
	defer p.mu.RUnlock()
	pg, ok := p.guilds[guildID]
	if !ok {
		return "", false
	}
	c, ok := pg.channels[channelID]
	if !ok {
		return "", false
	}
	if threadTypes[c.Type] {
		return c.ParentID, true
	}
	return "", true
}

// ArchiveMetrics is the archive's Prometheus metrics. Labels take fixed
// values only.
type ArchiveMetrics struct {
	Rows     *prometheus.CounterVec // source, op
	Failures *prometheus.CounterVec // source, op
	Dropped  prometheus.Counter
}

// NewArchiveMetrics registers the metrics with reg; a nil reg leaves them
// unregistered.
func NewArchiveMetrics(reg prometheus.Registerer) *ArchiveMetrics {
	m := &ArchiveMetrics{
		Rows: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "factory_discord_archive_rows_total",
			Help: "Archive rows written, by source (live, backfill) and op (upsert, edit, delete, reconcile: marked deleted by a backfill page).",
		}, []string{"source", "op"}),
		Failures: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "factory_discord_archive_failures_total",
			Help: "Archive writes that failed, by source and op. A failed live write is dropped after its retries.",
		}, []string{"source", "op"}),
		Dropped: prometheus.NewCounter(prometheus.CounterOpts{
			Name: "factory_discord_archive_dropped_total",
			Help: "Live gateway events dropped because the archive queue was full.",
		}),
	}
	if reg != nil {
		reg.MustRegister(m.Rows, m.Failures, m.Dropped)
	}
	return m
}

func (m *ArchiveMetrics) wrote(source, op string, n int) {
	if m != nil && n > 0 {
		m.Rows.WithLabelValues(source, op).Add(float64(n))
	}
}

func (m *ArchiveMetrics) failed(source, op string) {
	if m != nil {
		m.Failures.WithLabelValues(source, op).Inc()
	}
}

func (m *ArchiveMetrics) dropped() {
	if m != nil {
		m.Dropped.Inc()
	}
}
