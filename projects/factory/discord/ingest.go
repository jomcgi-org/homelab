package discord

import (
	"context"
	"log/slog"
	"slices"
	"strings"
	"sync"
	"time"
)

// Ingest defaults. A retain runs the LLM's extraction, so the ingestor
// batches a channel's messages instead of retaining each one.
const (
	defaultIngestWindow   = 5 * time.Minute
	defaultIngestLines    = 50
	defaultIngestBytes    = 16 << 10
	defaultIngestBuffered = 500
	ingestTick            = 10 * time.Second
	ingestWorkers         = 4
	ingestFinalFlush      = 30 * time.Second
)

// Ingestor stores every message of a server whose policy sets `ingest`
// (Joe, 2026-11-24, HOME-833). It batches a channel's lines and retains them
// into the channel's document for the day, with the channel's tag, in the
// server's bank. Add never blocks the gateway's reader.
type Ingestor struct {
	Memory Memory
	Log    *slog.Logger
	Now    func() time.Time
	// Window is how long a channel's first buffered line waits for more.
	Window time.Duration
	// MaxLines and MaxBytes flush a channel early.
	MaxLines, MaxBytes int
	// MaxBuffered bounds one channel's lines when the memory is down; the
	// oldest lines drop first.
	MaxBuffered int

	mu    sync.Mutex
	chans map[string]*ingestChannel
	seen  map[string]time.Time
}

type ingestChannel struct {
	guild, channel string
	lines          []ingestLine
	// sending holds the lines of the flush in progress, so Recent still
	// sees them before the store has them.
	sending []ingestLine
	bytes   int
	first   time.Time
	flush   sync.Mutex // one flush per channel at a time, in order
}

type ingestLine struct {
	id   string
	at   time.Time
	text string
}

func (i *Ingestor) now() time.Time {
	if i.Now != nil {
		return i.Now()
	}
	return time.Now()
}

func (i *Ingestor) window() time.Duration { return orDuration(i.Window, defaultIngestWindow) }
func (i *Ingestor) maxLines() int         { return orInt(i.MaxLines, defaultIngestLines) }
func (i *Ingestor) maxBytes() int         { return orInt(i.MaxBytes, defaultIngestBytes) }
func (i *Ingestor) maxBuffered() int      { return orInt(i.MaxBuffered, defaultIngestBuffered) }

func orDuration(v, d time.Duration) time.Duration {
	if v > 0 {
		return v
	}
	return d
}

func orInt(v, d int) int {
	if v > 0 {
		return v
	}
	return d
}

func channelKey(guildID, channelID string) string { return guildID + "/" + channelID }

// Add buffers one line. A message ID seen before adds nothing, so a resumed
// gateway cannot store a message twice. An empty ID is never a repeat.
func (i *Ingestor) Add(guildID, channelID, messageID string, at time.Time, text string) {
	now := i.now()
	i.mu.Lock()
	defer i.mu.Unlock()
	if messageID != "" {
		if i.seen == nil {
			i.seen = map[string]time.Time{}
		}
		if _, dup := i.seen[messageID]; dup {
			return
		}
		i.seen[messageID] = now
		if len(i.seen)%1024 == 0 {
			for id, t := range i.seen {
				if now.Sub(t) > dedupeWindow {
					delete(i.seen, id)
				}
			}
		}
	}
	c := i.channelLocked(guildID, channelID)
	if len(c.lines) == 0 {
		c.first = now
	}
	c.lines = append(c.lines, ingestLine{id: messageID, at: at, text: text})
	c.bytes += len(text)
	i.trimLocked(c)
}

func (i *Ingestor) channelLocked(guildID, channelID string) *ingestChannel {
	if i.chans == nil {
		i.chans = map[string]*ingestChannel{}
	}
	k := channelKey(guildID, channelID)
	c, ok := i.chans[k]
	if !ok {
		c = &ingestChannel{guild: guildID, channel: channelID}
		i.chans[k] = c
	}
	return c
}

// trimLocked drops the oldest lines past MaxBuffered.
func (i *Ingestor) trimLocked(c *ingestChannel) {
	over := len(c.lines) - i.maxBuffered()
	if over <= 0 {
		return
	}
	for _, l := range c.lines[:over] {
		c.bytes -= len(l.text)
	}
	c.lines = append([]ingestLine(nil), c.lines[over:]...)
	if i.Log != nil {
		i.Log.Warn("ingest buffer full; oldest lines dropped", "guild", c.guild, "channel", c.channel, "dropped", over)
	}
}

// Flush retains a channel's buffered lines and waits for the store. A
// failure puts the lines back, ahead of any added since.
func (i *Ingestor) Flush(ctx context.Context, guildID, channelID string) error {
	i.mu.Lock()
	c, ok := i.chans[channelKey(guildID, channelID)]
	i.mu.Unlock()
	if !ok {
		return nil
	}
	return i.flush(ctx, c)
}

func (i *Ingestor) flush(ctx context.Context, c *ingestChannel) error {
	c.flush.Lock()
	defer c.flush.Unlock()
	i.mu.Lock()
	lines := c.lines
	c.lines, c.bytes, c.first = nil, 0, time.Time{}
	c.sending = lines
	i.mu.Unlock()
	defer func() {
		i.mu.Lock()
		c.sending = nil
		i.mu.Unlock()
	}()
	groups := byDay(lines)
	for n, group := range groups {
		var b strings.Builder
		for _, l := range group {
			b.WriteString(l.text)
			b.WriteByte('\n')
		}
		err := i.Memory.Retain(ctx, c.guild, c.channel, group[0].at, strings.TrimRight(b.String(), "\n"))
		if err != nil {
			i.requeue(c, flatten(groups[n:]))
			return err
		}
	}
	return nil
}

// Recent returns a channel's lines that the store may not hold yet, oldest
// first: the newest at most maxLines lines and maxChars bytes, without the
// line of message skipID. A turn puts them in its prompt, so it need not
// wait for the store (HOME-909).
func (i *Ingestor) Recent(guildID, channelID, skipID string, maxLines, maxChars int) []string {
	i.mu.Lock()
	defer i.mu.Unlock()
	c, ok := i.chans[channelKey(guildID, channelID)]
	if !ok {
		return nil
	}
	all := append(append([]ingestLine(nil), c.sending...), c.lines...)
	var out []string
	chars := 0
	for n := len(all) - 1; n >= 0 && len(out) < maxLines; n-- {
		l := all[n]
		if skipID != "" && l.id == skipID {
			continue
		}
		if chars+len(l.text) > maxChars {
			break
		}
		chars += len(l.text)
		out = append(out, l.text)
	}
	slices.Reverse(out)
	return out
}

// requeue puts unsent lines back ahead of the lines added since.
func (i *Ingestor) requeue(c *ingestChannel, lines []ingestLine) {
	i.mu.Lock()
	defer i.mu.Unlock()
	c.sending = nil // The lines are back in c.lines; Recent must not see them twice.
	c.lines = append(lines, c.lines...)
	c.bytes = 0
	for _, l := range c.lines {
		c.bytes += len(l.text)
	}
	if c.first.IsZero() {
		c.first = i.now()
	}
	i.trimLocked(c)
}

// byDay groups consecutive lines by UTC day, so a document holds one day.
func byDay(lines []ingestLine) [][]ingestLine {
	var out [][]ingestLine
	for _, l := range lines {
		if n := len(out); n > 0 && sameDay(out[n-1][0].at, l.at) {
			out[n-1] = append(out[n-1], l)
			continue
		}
		out = append(out, []ingestLine{l})
	}
	return out
}

func sameDay(a, b time.Time) bool {
	return a.UTC().Format(time.DateOnly) == b.UTC().Format(time.DateOnly)
}

func flatten(groups [][]ingestLine) []ingestLine {
	var out []ingestLine
	for _, g := range groups {
		out = append(out, g...)
	}
	return out
}

// due lists the channels to flush: a first line older than Window, or a
// buffer past MaxLines or MaxBytes. all lists every channel with lines.
func (i *Ingestor) due(all bool) []*ingestChannel {
	now := i.now()
	i.mu.Lock()
	defer i.mu.Unlock()
	var out []*ingestChannel
	for _, c := range i.chans {
		if len(c.lines) == 0 {
			continue
		}
		if all || now.Sub(c.first) >= i.window() || len(c.lines) >= i.maxLines() || c.bytes >= i.maxBytes() {
			out = append(out, c)
		}
	}
	return out
}

// Run flushes due channels every ingestTick until ctx ends, then flushes
// every channel once more.
func (i *Ingestor) Run(ctx context.Context) {
	t := time.NewTicker(ingestTick)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			final, cancel := context.WithTimeout(context.WithoutCancel(ctx), ingestFinalFlush)
			defer cancel()
			i.flushAll(final, i.due(true))
			return
		case <-t.C:
			i.flushAll(ctx, i.due(false))
		}
	}
}

func (i *Ingestor) flushAll(ctx context.Context, channels []*ingestChannel) {
	sem := make(chan struct{}, ingestWorkers)
	var wg sync.WaitGroup
	for _, c := range channels {
		sem <- struct{}{}
		wg.Add(1)
		go func() {
			defer wg.Done()
			defer func() { <-sem }()
			if err := i.flush(ctx, c); err != nil && i.Log != nil {
				i.Log.WarnContext(ctx, "ingest retain failed", "guild", c.guild, "channel", c.channel, "error", err)
			}
		}()
	}
	wg.Wait()
}
