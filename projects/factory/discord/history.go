package discord

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"net/url"
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

// Text channels whose history the import reads: text, announcement, and
// public and announcement threads.
var historyChannelTypes = map[int]bool{0: true, 5: true, 10: true, 11: true}

// Message types that are conversation: default and reply. Joins, pins and
// other system messages are left out.
var conversationTypes = map[int]bool{0: true, 19: true}

// Attachment is a message attachment; the import keeps only its file name.
type Attachment struct {
	Filename string `json:"filename"`
}

// HistoryMessage is a message read from a channel's history.
type HistoryMessage struct {
	Message
	Type        int          `json:"type"`
	Attachments []Attachment `json:"attachments"`
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
		if c.Type != 0 && c.Type != 5 {
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
// (HOME-164): one document per channel and UTC day, in the shape the bridge
// appends to (DocumentID), so history and live memory share a structure.
// Days already stored are skipped, so a re-run adds no documents, and today
// is left to the live bridge.
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
	Log     *slog.Logger
	Now     func() time.Time

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

// Run imports every allowlisted server marked for history.
func (im *Importer) Run(ctx context.Context) (ImportStats, error) {
	var stats ImportStats
	today := im.now().UTC().Truncate(24 * time.Hour)
	type job struct {
		guildID string
		channel Channel
	}
	var jobs []job
	guilds := make([]string, 0, len(im.Config.Guilds))
	for id := range im.Config.Guilds {
		guilds = append(guilds, id)
	}
	sort.Strings(guilds)
	for _, gid := range guilds {
		g := im.Config.Guilds[gid]
		if !g.History {
			continue
		}
		channels, err := im.Source.GuildChannels(ctx, gid)
		if err != nil {
			return stats, fmt.Errorf("guild %s channels: %w", gid, err)
		}
		threads, err := im.Source.Threads(ctx, gid, channels)
		if err != nil {
			return stats, fmt.Errorf("guild %s threads: %w", gid, err)
		}
		for _, c := range append(channels, threads...) {
			if !historyChannelTypes[c.Type] {
				continue
			}
			// A thread belongs to its parent's listed channel.
			if len(g.Channels) > 0 && !contains(g.Channels, c.ID) && !contains(g.Channels, c.ParentID) {
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
				retained, skipped, err := im.channel(runCtx, j.guildID, j.channel, today)
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
		return nil
	}
	before := ""
	for {
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
	return strings.TrimSpace(m.Content) != "" || len(m.Attachments) > 0
}

// store retains one channel-day unless it is already stored. lines are
// newest first, as Discord returns them.
func (im *Importer) store(ctx context.Context, guildID string, c Channel, day time.Time, lines []HistoryMessage) (bool, error) {
	id := DocumentID(c.ID, day)
	exists, err := im.Store.DocumentExists(ctx, guildID, id)
	if err != nil {
		return false, err
	}
	if exists {
		return false, nil
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
		fmt.Fprintf(&b, "%s (%s): %s", m.Author.Username, SentAt(m.Message).UTC().Format(time.RFC3339), strings.TrimSpace(m.Content))
		for _, a := range m.Attachments {
			fmt.Fprintf(&b, " [attachment: %s]", a.Filename)
		}
		b.WriteString("\n")
	}
	item := NewRetain(c.ID, SentAt(lines[len(lines)-1].Message), strings.TrimRight(b.String(), "\n"))
	item.UpdateMode = "replace"
	item.Context = "Discord #" + c.Name
	if err := im.Store.RetainDocument(ctx, guildID, item); err != nil {
		return false, err
	}
	return true, nil
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
