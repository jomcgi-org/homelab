package discord

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"net/url"
	"runtime/debug"
	"sort"
	"strings"
	"sync"
	"time"
)

// Channel is the subset of a Discord channel or thread the import reads.
type Channel struct {
	ID       string `json:"id"`
	Name     string `json:"name"`
	Type     int    `json:"type"`
	ParentID string `json:"parent_id"`
}

// Channels whose history the import reads: text, announcement, and
// announcement, public and private threads (forum and media posts are public
// threads). The bot sees a private thread only when it is a member.
var historyChannelTypes = map[int]bool{0: true, 5: true, 10: true, 11: true, 12: true}

// Parents whose archived public threads the import lists: text,
// announcement, forum and media channels.
var threadParentTypes = map[int]bool{0: true, 5: true, 15: true, 16: true}

// Message types that are conversation: default and reply. Joins, pins and
// other system messages are left out.
var conversationTypes = map[int]bool{0: true, 19: true}

// Attachment is a message attachment; the import keeps only its file name.
type Attachment struct {
	Filename string `json:"filename"`
}

// Component is a message component; containers nest others.
type Component struct {
	Type       int         `json:"type"`
	Content    string      `json:"content"`
	Components []Component `json:"components"`
}

// HistoryMessage is a message read from a channel's history.
type HistoryMessage struct {
	Message
	Attachments []Attachment `json:"attachments"`
	Components  []Component  `json:"components"`
}

// Text is a message's text: its content, or for a Components V2 message,
// such as Bosun's replies (NewReply), its text displays.
func (m HistoryMessage) Text() string {
	if s := strings.TrimSpace(m.Content); s != "" {
		return s
	}
	var parts []string
	var walk func([]Component)
	walk = func(cs []Component) {
		for _, c := range cs {
			if c.Type == componentText && strings.TrimSpace(c.Content) != "" {
				parts = append(parts, strings.TrimSpace(c.Content))
			}
			walk(c.Components)
		}
	}
	walk(m.Components)
	return strings.Join(parts, "\n")
}

// HistoryDocumentID is an imported channel-day's document. It is apart from
// the live bridge's (DocumentID), so a day the bridge appended to before the
// import still gets its full history.
func HistoryDocumentID(channelID string, day time.Time) string {
	return DocumentID(channelID, day) + ":history"
}

// GuildChannels lists a server's channels.
func (r *REST) GuildChannels(ctx context.Context, guildID string) ([]Channel, error) {
	var out []Channel
	err := r.do(ctx, http.MethodGet, "/guilds/"+guildID+"/channels", nil, &out)
	return out, err
}

// Threads lists a server's active threads and each history channel's
// archived public threads.
func (r *REST) Threads(ctx context.Context, guildID string, channels []Channel) ([]Channel, error) {
	var active struct {
		Threads []Channel `json:"threads"`
	}
	if err := r.do(ctx, http.MethodGet, "/guilds/"+guildID+"/threads/active", nil, &active); err != nil {
		return nil, err
	}
	out := active.Threads
	for _, c := range channels {
		if !threadParentTypes[c.Type] {
			continue
		}
		before := ""
		for {
			var page struct {
				Threads []struct {
					Channel
					Metadata struct {
						ArchiveTimestamp string `json:"archive_timestamp"`
					} `json:"thread_metadata"`
				} `json:"threads"`
				HasMore bool `json:"has_more"`
			}
			q := url.Values{"limit": {"100"}}
			if before != "" {
				q.Set("before", before)
			}
			err := r.do(ctx, http.MethodGet, "/channels/"+c.ID+"/threads/archived/public?"+q.Encode(), nil, &page)
			var he *HTTPError
			if errors.As(err, &he) && he.Status == http.StatusForbidden {
				break // A channel the bot cannot read.
			}
			if err != nil {
				return nil, err
			}
			for _, t := range page.Threads {
				out = append(out, t.Channel)
				before = t.Metadata.ArchiveTimestamp
			}
			if !page.HasMore || len(page.Threads) == 0 {
				break
			}
		}
	}
	return out, nil
}

// MessagesBefore reads up to 100 messages older than before, newest first;
// an empty before starts at the newest.
func (r *REST) MessagesBefore(ctx context.Context, channelID, before string) ([]HistoryMessage, error) {
	q := url.Values{"limit": {"100"}}
	if before != "" {
		q.Set("before", before)
	}
	var out []HistoryMessage
	err := r.do(ctx, http.MethodGet, "/channels/"+channelID+"/messages?"+q.Encode(), nil, &out)
	return out, err
}

// HistorySource reads Discord history.
type HistorySource interface {
	GuildChannels(ctx context.Context, guildID string) ([]Channel, error)
	Threads(ctx context.Context, guildID string, channels []Channel) ([]Channel, error)
	MessagesBefore(ctx context.Context, channelID, before string) ([]HistoryMessage, error)
}

// Documents stores the import's channel-day documents.
type Documents interface {
	DocumentExists(ctx context.Context, guildID, documentID string) (bool, error)
	RetainDocument(ctx context.Context, guildID string, item RetainItem) error
}

// Importer replays allowlisted servers' Discord history into their banks
// (HOME-164): one document per channel and UTC day (HistoryDocumentID), with
// the live bridge's tag and observation scope, so history and live memory
// recall together. Days already imported are skipped, so a re-run adds no
// documents, and today is left to the live bridge.
type Importer struct {
	Source HistorySource
	Store  Documents
	Config Config
	// BotID is Bosun's user: its replies are kept; other bots' and webhooks'
	// messages are left out unless IncludeBots.
	BotID       string
	IncludeBots bool
	// MaxDocuments bounds the documents one run retains, and so its spend.
	MaxDocuments int
	// Workers is how many channels import at once.
	Workers int
	// RetainRetries is how many more times a failed retain is tried when
	// the failure is transient: a timeout, a network error, HTTP 408, 429
	// or 5xx. Backoff is the first wait; each later wait doubles it.
	RetainRetries int
	Backoff       time.Duration
	Log           *slog.Logger
	Now           func() time.Time
	// Sleep waits between retries; tests replace it.
	Sleep func(ctx context.Context, d time.Duration) error
	// Pace, when set, runs before each Discord history read and each
	// Hindsight call, so a rate limit keeps the import from starving live
	// turns that share them.
	Pace func(ctx context.Context) error
	// SkipIngested skips a channel-day of a server with `ingest` when the day
	// is on or after its IngestSince and the live document for that day
	// exists: the ingestor stored its messages. An earlier day's live
	// document holds only tagged turns, so that day is imported.
	SkipIngested bool
	// Observe, when set, sees each channel-day as it is retained or skipped.
	Observe func(guildID string, retained bool)

	mu       sync.Mutex
	retained int
}

// ImportStats counts what one run did.
type ImportStats struct {
	Channels, Retained, Skipped int
	Capped                      bool
}

// ErrCapped is a run that reached MaxDocuments; the next run continues.
var ErrCapped = errors.New("discord: import reached its document cap")

// HistoryGuilds lists the allowlisted servers marked for history, in order.
func (c Config) HistoryGuilds() []string {
	var out []string
	for id, g := range c.Guilds {
		if g.History {
			out = append(out, id)
		}
	}
	sort.Strings(out)
	return out
}

// Run imports every allowlisted server marked for history.
func (im *Importer) Run(ctx context.Context) (ImportStats, error) {
	return im.run(ctx, im.Config.HistoryGuilds())
}

// RunGuild imports one allowlisted server marked for history.
func (im *Importer) RunGuild(ctx context.Context, guildID string) (ImportStats, error) {
	if g, ok := im.Config.Guilds[guildID]; !ok || !g.History {
		return ImportStats{}, fmt.Errorf("guild %s is not marked for history", guildID)
	}
	return im.run(ctx, []string{guildID})
}

// run imports guilds. MaxDocuments bounds each run.
func (im *Importer) run(ctx context.Context, guilds []string) (ImportStats, error) {
	var stats ImportStats
	im.mu.Lock()
	im.retained = 0
	im.mu.Unlock()
	today := im.now().UTC().Truncate(24 * time.Hour)
	type job struct {
		guildID string
		channel Channel
	}
	var jobs []job
	for _, gid := range guilds {
		g := im.Config.Guilds[gid]
		if err := im.pace(ctx); err != nil {
			return stats, err
		}
		channels, err := im.Source.GuildChannels(ctx, gid)
		if err != nil {
			return stats, fmt.Errorf("guild %s channels: %w", gid, err)
		}
		if err := im.pace(ctx); err != nil {
			return stats, err
		}
		threads, err := im.Source.Threads(ctx, gid, channels)
		if err != nil {
			return stats, fmt.Errorf("guild %s threads: %w", gid, err)
		}
		for _, c := range append(channels, threads...) {
			if !historyChannelTypes[c.Type] {
				continue
			}
			// A channel list admits only listed IDs, threads included, as the
			// live bridge does (Bridge.Accept).
			if len(g.Channels) > 0 && !contains(g.Channels, c.ID) {
				continue
			}
			jobs = append(jobs, job{gid, c})
		}
	}
	stats.Channels = len(jobs)
	work := make(chan job)
	var wg sync.WaitGroup
	var mu sync.Mutex
	var firstErr error
	runCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	for range max(im.Workers, 1) {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := range work {
				retained, skipped, err := im.safeChannel(runCtx, j.guildID, j.channel, today)
				mu.Lock()
				stats.Retained += retained
				stats.Skipped += skipped
				if err != nil && firstErr == nil {
					firstErr = err
					cancel()
				}
				mu.Unlock()
			}
		}()
	}
	for _, j := range jobs {
		select {
		case work <- j:
		case <-runCtx.Done():
		}
	}
	close(work)
	wg.Wait()
	if errors.Is(firstErr, ErrCapped) {
		stats.Capped = true
	}
	return stats, firstErr
}

// safeChannel runs channel and turns a panic into an error, so a bad
// message or store fails the run instead of the process.
func (im *Importer) safeChannel(ctx context.Context, guildID string, c Channel, today time.Time) (retained, skipped int, err error) {
	defer func() {
		if r := recover(); r != nil {
			if im.Log != nil {
				im.Log.Error("history import panicked", "guild", guildID, "channel", c.ID,
					"panic", fmt.Sprint(r), "stack", string(debug.Stack()))
			}
			err = fmt.Errorf("channel %s: panic: %v", c.ID, r)
		}
	}()
	return im.channel(ctx, guildID, c, today)
}

// channel walks one channel's history newest first and stores each
// finished UTC day before today.
func (im *Importer) channel(ctx context.Context, guildID string, c Channel, today time.Time) (retained, skipped int, err error) {
	var day time.Time
	var lines []HistoryMessage
	flush := func() error {
		if len(lines) == 0 || !day.Before(today) {
			lines = nil
			return nil
		}
		ok, err := im.store(ctx, guildID, c, day, lines)
		lines = nil
		if err != nil {
			return err
		}
		if ok {
			retained++
		} else {
			skipped++
		}
		if im.Observe != nil {
			im.Observe(guildID, ok)
		}
		return nil
	}
	before := ""
	for {
		if err := im.pace(ctx); err != nil {
			return retained, skipped, err
		}
		page, err := im.Source.MessagesBefore(ctx, c.ID, before)
		var he *HTTPError
		if errors.As(err, &he) && he.Status == http.StatusForbidden {
			return retained, skipped, nil // A channel the bot cannot read.
		}
		if err != nil {
			return retained, skipped, fmt.Errorf("channel %s: %w", c.ID, err)
		}
		if len(page) == 0 {
			break
		}
		for _, m := range page {
			before = m.ID
			if !im.keep(m) {
				continue
			}
			d := SentAt(m.Message).UTC().Truncate(24 * time.Hour)
			if !d.Equal(day) {
				if err := flush(); err != nil {
					return retained, skipped, err
				}
				day = d
			}
			lines = append(lines, m)
		}
	}
	return retained, skipped, flush()
}

// keep reports whether a message is conversation worth remembering.
func (im *Importer) keep(m HistoryMessage) bool {
	if !conversationTypes[m.Type] {
		return false
	}
	if (m.Author.Bot || m.WebhookID != "") && m.Author.ID != im.BotID && !im.IncludeBots {
		return false
	}
	return m.Text() != "" || len(m.Attachments) > 0
}

// store retains one channel-day unless it is already stored. lines are
// newest first, as Discord returns them.
func (im *Importer) store(ctx context.Context, guildID string, c Channel, day time.Time, lines []HistoryMessage) (bool, error) {
	id := HistoryDocumentID(c.ID, day)
	exists, err := im.exists(ctx, guildID, id)
	if err != nil || exists {
		return false, err
	}
	if im.SkipIngested && im.Config.Guilds[guildID].ingestedLive(day) {
		exists, err := im.exists(ctx, guildID, DocumentID(c.ID, day))
		if err != nil || exists {
			return false, err
		}
	}
	im.mu.Lock()
	if im.MaxDocuments > 0 && im.retained >= im.MaxDocuments {
		im.mu.Unlock()
		return false, ErrCapped
	}
	im.retained++
	im.mu.Unlock()
	var b strings.Builder
	for i := len(lines) - 1; i >= 0; i-- {
		m := lines[i]
		fmt.Fprintf(&b, "%s (%s): %s", m.Author.Username, SentAt(m.Message).UTC().Format(time.RFC3339), m.Text())
		for _, a := range m.Attachments {
			fmt.Fprintf(&b, " [attachment: %s]", a.Filename)
		}
		b.WriteString("\n")
	}
	item := NewRetain(c.ID, SentAt(lines[len(lines)-1].Message), strings.TrimRight(b.String(), "\n"))
	item.DocumentID = id
	item.UpdateMode = "replace"
	item.Context = "Discord #" + c.Name
	if err := im.retain(ctx, guildID, item); err != nil {
		return false, err
	}
	return true, nil
}

// retain stores one document and tries again after a transient failure. A
// synchronous retain that timed out on the client can still finish on the
// server, so each retry first checks whether the document is there. The
// retain replaces the document by ID, so a second write adds no duplicate.
func (im *Importer) retain(ctx context.Context, guildID string, item RetainItem) error {
	if err := im.pace(ctx); err != nil {
		return err
	}
	err := im.Store.RetainDocument(ctx, guildID, item)
	wait := im.Backoff
	for attempt := 1; attempt <= im.RetainRetries && err != nil && transient(ctx, err); attempt++ {
		if im.Log != nil {
			im.Log.Warn("retain failed, trying again", "document", item.DocumentID,
				"attempt", attempt, "wait", wait.String(), "error", err)
		}
		if serr := im.sleep(ctx, wait); serr != nil {
			return err
		}
		wait *= 2
		exists, xerr := im.exists(ctx, guildID, item.DocumentID)
		if xerr == nil && exists {
			return nil
		}
		if perr := im.pace(ctx); perr != nil {
			return err
		}
		err = im.Store.RetainDocument(ctx, guildID, item)
	}
	return err
}

func (im *Importer) exists(ctx context.Context, guildID, documentID string) (bool, error) {
	if err := im.pace(ctx); err != nil {
		return false, err
	}
	return im.Store.DocumentExists(ctx, guildID, documentID)
}

func (im *Importer) pace(ctx context.Context) error {
	if im.Pace == nil {
		return ctx.Err()
	}
	return im.Pace(ctx)
}

// transient reports whether a retain failure may pass on its own. A
// cancelled run is not transient.
func transient(ctx context.Context, err error) bool {
	if ctx.Err() != nil || errors.Is(err, context.Canceled) {
		return false
	}
	var se *StatusError
	if errors.As(err, &se) {
		return se.Status == http.StatusRequestTimeout || se.Status == http.StatusTooManyRequests || se.Status >= 500
	}
	// Client timeouts and network errors.
	return true
}

func (im *Importer) sleep(ctx context.Context, d time.Duration) error {
	if im.Sleep != nil {
		return im.Sleep(ctx, d)
	}
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-t.C:
		return nil
	}
}

func (im *Importer) now() time.Time {
	if im.Now != nil {
		return im.Now()
	}
	return time.Now()
}

// DocumentExists reports whether a server's bank holds a document.
func (h *Hindsight) DocumentExists(ctx context.Context, guildID, documentID string) (bool, error) {
	if err := h.ensureBank(ctx, guildID); err != nil {
		return false, err
	}
	err := h.call(ctx, http.MethodGet, "/v1/default/banks/"+BankID(guildID)+"/documents/"+url.PathEscape(documentID), nil, nil)
	var se *StatusError
	switch {
	case err == nil:
		return true, nil
	case errors.As(err, &se) && se.Status == http.StatusNotFound:
		return false, nil
	}
	return false, err
}

// RetainDocument stores one document and waits for it.
func (h *Hindsight) RetainDocument(ctx context.Context, guildID string, item RetainItem) error {
	if err := h.ensureBank(ctx, guildID); err != nil {
		return err
	}
	body := map[string]any{"items": []RetainItem{item}, "async": false}
	return h.call(ctx, http.MethodPost, "/v1/default/banks/"+BankID(guildID)+"/memories", body, nil)
}
