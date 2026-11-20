package dispatch

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/json"
	"fmt"
	"strings"

	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

const (
	maxDelta     = 12_000 // itsaplan DELTA_LIMIT
	maxIDLen     = 200
	maxBatch     = 200 // itsaplan events per batch
	maxOutput    = 10_000
	maxErrorText = 500
)

// Events renders complete history messages as AG-UI text events. Growing
// artifacts are reconciled separately through the durable transcript journal.
func Events(t *kagent.Task) []itsaplan.Event {
	var out []itsaplan.Event
	for _, m := range t.History {
		if m == nil || m.Role == "ROLE_USER" {
			continue
		}
		text := messageText(m)
		if text == "" {
			continue
		}
		id := clip(m.MessageID, maxIDLen)
		out = append(out, itsaplan.Event{Type: "TEXT_MESSAGE_START", MessageID: id, Role: "assistant"})
		out = appendContent(out, id, text)
		out = append(out, itsaplan.Event{Type: "TEXT_MESSAGE_END", MessageID: id})
	}
	return out
}

func appendContent(out []itsaplan.Event, id, text string) []itsaplan.Event {
	for len(text) > 0 {
		n := min(len(text), maxDelta)
		// Keep UTF-8 sequences whole.
		for n < len(text) && n > 0 && !utf8Start(text[n]) {
			n--
		}
		out = append(out, itsaplan.Event{Type: "TEXT_MESSAGE_CONTENT", MessageID: id, Delta: text[:n]})
		text = text[n:]
	}
	return out
}

// transcriptState freezes event boundaries across growing snapshots, lost
// acknowledgements and restarts. Artifact byte cursors advance in the same
// transaction as the events they produce, independently of delivery progress.
type transcriptState struct {
	ArtifactText  bool             `json:"artifactText"`
	Events        []itsaplan.Event `json:"events"`
	HistoryEvents int              `json:"historyEvents"`
	Artifacts     []artifactCursor `json:"artifacts"`
}

type artifactCursor struct {
	ID     string `json:"id"`
	Bytes  int    `json:"bytes"`
	Hash   string `json:"hash"`
	Closed bool   `json:"closed"`
}

func artifactMessageID(taskID, artifactID string) string {
	// Hash the full identities; clipping long IDs can merge distinct artifacts.
	return fmt.Sprintf("artifact/%x", sha256.Sum256([]byte(taskID+"\x00"+artifactID)))
}

func textHash(text string) string { return fmt.Sprintf("%x", sha256.Sum256([]byte(text))) }

func (s *transcriptState) appendTask(t *kagent.Task) error {
	// History messages are complete and append-only. Retain the old rendering
	// for rollout compatibility with offsets acknowledged before journals existed.
	history := Events(t)
	if len(history) < s.HistoryEvents {
		return fmt.Errorf("task history shrank below the persisted transcript cursor")
	}
	s.Events = append(s.Events, history[s.HistoryEvents:]...)
	s.HistoryEvents = len(history)
	if !s.ArtifactText {
		return nil
	}
	// GetTask snapshots must retain every artifact whose text we have already
	// journaled. Missing artifacts are incomplete reads, including at completion.
	present := make(map[string]bool, len(t.Artifacts))
	for _, a := range t.Artifacts {
		if a != nil {
			present[a.ArtifactID] = true
		}
	}
	for _, cursor := range s.Artifacts {
		if !present[cursor.ID] {
			return fmt.Errorf("snapshot omitted persisted artifact %q", cursor.ID)
		}
	}
	for _, a := range t.Artifacts {
		if a == nil {
			continue
		}
		var b strings.Builder
		for _, p := range a.Parts {
			b.WriteString(p.Text)
		}
		text := b.String()
		var cursor *artifactCursor
		for i := range s.Artifacts {
			if s.Artifacts[i].ID == a.ArtifactID {
				cursor = &s.Artifacts[i]
				break
			}
		}
		if cursor == nil {
			if text == "" {
				continue
			}
			if a.ArtifactID == "" {
				return fmt.Errorf("text artifact has no stable identity")
			}
			s.Artifacts = append(s.Artifacts, artifactCursor{ID: a.ArtifactID, Hash: textHash("")})
			cursor = &s.Artifacts[len(s.Artifacts)-1]
			s.Events = append(s.Events, itsaplan.Event{
				Type: "TEXT_MESSAGE_START", MessageID: artifactMessageID(t.ID, cursor.ID), Role: "assistant",
			})
		}
		if len(text) < cursor.Bytes || textHash(text[:cursor.Bytes]) != cursor.Hash {
			return fmt.Errorf("artifact %q changed its persisted text prefix", a.ArtifactID)
		}
		if len(text) == cursor.Bytes {
			continue
		}
		if cursor.Closed {
			return fmt.Errorf("artifact %q grew after its transcript ended", a.ArtifactID)
		}
		s.Events = appendContent(s.Events, artifactMessageID(t.ID, cursor.ID), text[cursor.Bytes:])
		cursor.Bytes, cursor.Hash = len(text), textHash(text)
	}
	// GetTask does not expose artifact LastChunk. Only quiescence proves all
	// artifacts have finished; emit each END once and never between snapshots.
	if kagent.Quiescent(t.Status.State) {
		for i := range s.Artifacts {
			cursor := &s.Artifacts[i]
			if !cursor.Closed {
				s.Events = append(s.Events, itsaplan.Event{
					Type: "TEXT_MESSAGE_END", MessageID: artifactMessageID(t.ID, cursor.ID),
				})
				cursor.Closed = true
			}
		}
	}
	return nil
}

// prepareEvents persists new events before the first HTTP call, including the
// task's base offset even if the first batch's acknowledgement is lost.
func (e *Engine) prepareEvents(ctx context.Context, r *store.Run, t *kagent.Task, base int) ([]itsaplan.Event, int, error) {
	var state transcriptState
	err := e.w.Tx(ctx, func(tx *sql.Tx) error {
		var attempt int
		if err := tx.QueryRowContext(ctx, `SELECT attempt FROM run WHERE id = $1 FOR UPDATE`, r.ID).Scan(&attempt); err != nil {
			return err
		}
		if attempt != r.Attempt {
			return fmt.Errorf("run attempt changed before recording transcript")
		}
		journal, err := e.st.GetTranscript(ctx, tx, r.ID, r.Attempt, t.ID)
		if err != nil {
			return err
		}
		fresh := journal == nil
		if fresh {
			journal = &store.Transcript{Base: base}
		} else if err := json.Unmarshal(journal.State, &state); err != nil {
			return fmt.Errorf("decode transcript: %w", err)
		}
		base = journal.Base
		// Admission is sticky per task. A rollback to a release with admission
		// off must reconcile existing artifact journals, including
		// unacknowledged events, even though it admits no artifact text on
		// other tasks.
		admitted := state.ArtifactText
		state.ArtifactText = admitted || e.cfg.ArtifactTranscripts || len(state.Artifacts) > 0
		previous := len(state.Events)
		if err := state.appendTask(t); err != nil {
			return err
		}
		if !fresh && len(state.Events) == previous && admitted == state.ArtifactText {
			return nil
		}
		journal.State, err = json.Marshal(state)
		if err != nil {
			return err
		}
		return e.st.SaveTranscript(ctx, tx, r.ID, r.Attempt, t.ID, journal)
	})
	return state.Events, base, err
}

func utf8Start(b byte) bool { return b&0xC0 != 0x80 }

// itsaplanText replaces NUL with the Unicode replacement character. itsaplan's
// trace events and our result outbox use JSONB, which cannot represent U+0000.
// Apply this at delivery, after chunking, so legacy history offsets stay exact.
// Replacing one code point with one code point preserves itsaplan's DELTA_LIMIT
// (UTF-16 units), although the UTF-8 byte count can grow. Journals keep raw text.
func itsaplanText(text string) string { return strings.ReplaceAll(text, "\x00", "\uFFFD") }

func messageText(m *kagent.Message) string {
	var b strings.Builder
	for _, p := range m.Parts {
		if p.Text == "" {
			continue
		}
		if b.Len() > 0 {
			b.WriteString("\n")
		}
		b.WriteString(p.Text)
	}
	return b.String()
}

// Output is the turn's final answer: the status message, else the last agent
// message, else the artifacts' text.
func Output(t *kagent.Task) string {
	return clip(itsaplanText(taskAnswer(t)), maxOutput)
}

func taskAnswer(t *kagent.Task) string {
	if t.Status.Message != nil {
		if s := messageText(t.Status.Message); s != "" {
			return s
		}
	}
	for i := len(t.History) - 1; i >= 0; i-- {
		if m := t.History[i]; m != nil && m.Role != "ROLE_USER" {
			if s := messageText(m); s != "" {
				return s
			}
		}
	}
	var b strings.Builder
	for _, a := range t.Artifacts {
		if a == nil {
			continue
		}
		for _, p := range a.Parts {
			b.WriteString(p.Text)
		}
	}
	return b.String()
}

// StatusText is the task's status message text, for error classification.
func StatusText(t *kagent.Task) string {
	if t.Status.Message == nil {
		return ""
	}
	return messageText(t.Status.Message)
}

func clip(s string, n int) string {
	if len(s) <= n {
		return s
	}
	for n > 0 && !utf8Start(s[n]) {
		n--
	}
	return s[:n]
}
