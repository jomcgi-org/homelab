package whatsapp

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
)

// strptr returns a pointer to s. The waE2E protobuf message fields are *string;
// this local helper avoids a direct dependency on google.golang.org/protobuf
// (proto.String) just for pointer wrapping, which would need a MODULE.bazel
// use_repo entry for an otherwise-indirect module.
func strptr(s string) *string { return &s }

const (
	// outboxMaxAttempts parks a row after this many failed sends so one bad row
	// (e.g. an unresolvable group) is not retried forever. Mirrors the Discord
	// drain's _MAX_ATTEMPTS.
	outboxMaxAttempts = 5
	// outboxBatch caps how many pending rows one drain tick claims. Mirrors the
	// Discord drain's _BATCH.
	outboxBatch = 20
	// outboxPollInterval is how often the drain wakes to look for pending rows.
	outboxPollInterval = 3 * time.Second
)

// sender translates an outbox row into a whatsmeow send. It is an interface so
// the drain can be unit-tested with a fake (no live WhatsApp); the production
// implementation (whatsmeowSender) wraps a *whatsmeow.Client. Each method
// returns the sent message id (empty for edits/reactions, which do not mint a
// new addressable id the outbox needs to keep).
type sender interface {
	// Ready reports whether the underlying socket is live (connected and logged
	// in) and can actually send. The gateway state machine tracks a coarse
	// StateConnected that a transient reconnect does not flip, so the drain checks
	// the live client here to avoid burning rows against a momentarily-dead socket.
	Ready() bool
	// SendText sends content to groupJID; when quotedMessageID is set it is sent
	// as a reply to that message. Returns the new message id.
	SendText(ctx context.Context, groupJID, content, quotedMessageID string) (string, error)
	// SendEdit edits targetMessageID in groupJID to newContent.
	SendEdit(ctx context.Context, groupJID, targetMessageID, newContent string) error
	// SendReaction reacts to targetMessageID (whose sender is targetSenderJID)
	// with reaction; an empty reaction clears a previous one.
	SendReaction(ctx context.Context, groupJID, targetMessageID, targetSenderJID, reaction string) error
}

// outboxRow is one claimed pending row. NULLable columns use sql.Null* so the
// per-kind shape (guaranteed by the DB CHECK) can be read back safely.
type outboxRow struct {
	id              int64
	groupJID        string
	kind            string
	content         sql.NullString
	quotedMessageID sql.NullString
	editOf          sql.NullInt64
	targetMessageID sql.NullString
	targetSenderJID sql.NullString
	reaction        sql.NullString
	reactionRemove  bool
}

// store is the drain's persistence seam. Production is pgStore over a *sql.DB;
// tests use an in-memory fake so the drain's ordering, parking, and stamping can
// be asserted without a Postgres (there is no local test DB, and the pgx driver
// cannot talk to SQLite). Every query the pgStore runs fully-qualifies
// chat.whatsapp_outbox because the gateway's own connection uses
// search_path=whatsapp for the whatsmeow tables.
type store interface {
	// claimPending returns the oldest unposted, not-yet-parked rows ordered
	// (group_jid, created_at, id) so each group drains oldest-first.
	claimPending(ctx context.Context) ([]outboxRow, error)
	// sentMessageID resolves an edit_of reference to the original send's
	// sent_message_id; empty means the original has not been sent yet.
	sentMessageID(ctx context.Context, id int64) (string, error)
	// markPosted stamps posted_at; a non-empty sentMessageID is recorded (an
	// empty one preserves any existing value, so edits/reactions do not clear it).
	markPosted(ctx context.Context, id int64, sentMessageID string) error
	// markFailed increments attempts and records a truncated last_error.
	markFailed(ctx context.Context, id int64, errMsg string) error
	// markEditExpired consumes an edit whose ~15-minute window has closed: it
	// stamps posted_at (so it is not retried) with last_error='edit_window_expired'
	// so the monolith can detect it and repost a fresh message.
	markEditExpired(ctx context.Context, id int64) error
}

// OutboxDrain polls chat.whatsapp_outbox and sends pending rows via whatsmeow.
// It runs only while the gateway is connected (stateFn); pairing/parked ticks are
// skipped so rows are not burned against a socket that cannot send.
type OutboxDrain struct {
	store   store
	sender  sender
	stateFn func() State
	log     *slog.Logger
}

// NewOutboxDrain builds the production drain over a *sql.DB and the gateway's
// whatsmeow session. It opens no new connection: the caller's db is the base DSN
// (no search_path), and the queries fully-qualify chat.whatsapp_outbox. It
// returns an error if the session is not the production whatsmeow-backed one
// (tests build the drain with newOutboxDrain and a fake sender directly).
func NewOutboxDrain(db *sql.DB, session Session, stateFn func() State, log *slog.Logger) (*OutboxDrain, error) {
	holder, ok := session.(clientHolder)
	if !ok {
		return nil, fmt.Errorf("outbox drain needs the whatsmeow-backed session")
	}
	return newOutboxDrain(&pgStore{db: db}, &whatsmeowSender{client: holder.waClient()}, stateFn, log), nil
}

// newOutboxDrain is the injectable constructor used by tests (fake store, fake
// sender) and by NewOutboxDrain.
func newOutboxDrain(st store, snd sender, stateFn func() State, log *slog.Logger) *OutboxDrain {
	return &OutboxDrain{store: st, sender: snd, stateFn: stateFn, log: log}
}

// Run drains on a ticker until ctx is cancelled. It skips a tick unless the
// gateway is connected so pairing/parked states do not send.
func (d *OutboxDrain) Run(ctx context.Context) {
	d.log.Info("whatsapp outbox drain started", "poll", outboxPollInterval.String())
	ticker := time.NewTicker(outboxPollInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			d.log.Info("whatsapp outbox drain stopped")
			return
		case <-ticker.C:
			if d.stateFn() != StateConnected {
				continue
			}
			if err := d.drainOnce(ctx); err != nil {
				d.log.Error("whatsapp outbox drain tick failed", "err", err)
			}
		}
	}
}

// drainOnce sends every currently-pending row it can. Rows are processed in
// (group_jid, created_at, id) order; a failure blocks only that group for the
// rest of the tick (so a poison row does not reorder its group's later sends,
// but never stalls another group). A parked row (attempts >= max) is excluded by
// claimPending, which unblocks its group on the next tick.
func (d *OutboxDrain) drainOnce(ctx context.Context) error {
	// Gate on the live socket, not just the coarse gateway state: a transient
	// reconnect leaves StateConnected set (handleEvent does not track
	// Disconnected/StreamReplaced), so without this check every row would take a
	// not-connected error each tick and eventually park. Skip the tick until the
	// client is genuinely connected and logged in; the rows stay pending.
	if !d.sender.Ready() {
		return nil
	}
	rows, err := d.store.claimPending(ctx)
	if err != nil {
		return err
	}
	blocked := make(map[string]bool)
	for _, row := range rows {
		if blocked[row.groupJID] {
			continue
		}
		if failed := d.processRow(ctx, row); failed {
			blocked[row.groupJID] = true
		}
	}
	return nil
}

// processRow sends one row and records the outcome. It returns true when the row
// failed in a way that should block the group for the rest of the tick (a
// transient send error). A consumed row (posted, or an edit whose window
// expired) returns false: the group may keep draining.
func (d *OutboxDrain) processRow(ctx context.Context, row outboxRow) bool {
	switch row.kind {
	case "message":
		id, err := d.sender.SendText(ctx, row.groupJID, row.content.String, row.quotedMessageID.String)
		if err != nil {
			if transient(err) {
				// Connection-class failure: leave the row pending (attempts
				// unchanged) and block the group for this tick so it retries in order
				// once the socket returns. Only genuine rejections burn the budget.
				d.log.Warn("whatsapp send deferred; socket not ready", "id", row.id, "err", err)
				return true
			}
			d.fail(ctx, row.id, err)
			return true
		}
		if d.post(ctx, row.id, id) {
			// The send succeeded but the posted stamp did not land; block the group
			// so a later row cannot be sent before this one is marked posted (which
			// would break the per-group ordering guarantee). Retried next tick.
			return true
		}
		return false

	case "edit":
		if !row.editOf.Valid {
			// Malformed (the CHECK should prevent this); park rather than loop.
			d.fail(ctx, row.id, fmt.Errorf("edit row has no edit_of"))
			return true
		}
		orig, err := d.store.sentMessageID(ctx, row.editOf.Int64)
		if err != nil {
			d.fail(ctx, row.id, fmt.Errorf("resolve edit_of: %w", err))
			return true
		}
		if orig == "" {
			// The original has not been sent yet; retry on a later tick.
			d.fail(ctx, row.id, fmt.Errorf("edit target not sent yet"))
			return true
		}
		if err := d.sender.SendEdit(ctx, row.groupJID, orig, row.content.String); err != nil {
			if transient(err) {
				// Socket down mid-flight: do not consume the edit as window-expired;
				// leave it pending and block the group so it retries when connected.
				d.log.Warn("whatsapp edit deferred; socket not ready", "id", row.id, "err", err)
				return true
			}
			// whatsmeow does not surface a distinct "edit window expired" error
			// (the server silently ignores a late edit), so a send error cannot be
			// cleanly attributed. Per the plan we treat any edit failure as window
			// expiry after one attempt: consume the row so it is not retried, and
			// let the monolith repost a fresh message. Reposting is safe even if the
			// failure was transient.
			d.log.Warn("whatsapp edit failed; consuming as window-expired", "id", row.id, "err", err)
			if e := d.store.markEditExpired(ctx, row.id); e != nil {
				d.log.Error("mark edit expired failed", "id", row.id, "err", e)
			}
			return false
		}
		// An edit keeps the original message id; do not overwrite sent_message_id.
		d.post(ctx, row.id, "")
		return false

	case "reaction":
		reaction := row.reaction.String
		if row.reactionRemove {
			reaction = ""
		}
		if err := d.sender.SendReaction(ctx, row.groupJID, row.targetMessageID.String, row.targetSenderJID.String, reaction); err != nil {
			if transient(err) {
				d.log.Warn("whatsapp reaction deferred; socket not ready", "id", row.id, "err", err)
				return true
			}
			d.fail(ctx, row.id, err)
			return true
		}
		d.post(ctx, row.id, "")
		return false

	default:
		// Unknown kind (the CHECK should prevent this); park it.
		d.fail(ctx, row.id, fmt.Errorf("unknown kind %q", row.kind))
		return true
	}
}

// post stamps the row posted. It returns true when the stamp failed, so the
// caller can decide whether to hold the group (a message must be marked posted
// before a later same-group row is sent).
func (d *OutboxDrain) post(ctx context.Context, id int64, sentMessageID string) bool {
	if err := d.store.markPosted(ctx, id, sentMessageID); err != nil {
		d.log.Error("mark posted failed", "id", id, "err", err)
		return true
	}
	return false
}

// transient reports whether a send error is a connection-class failure that must
// NOT consume the row's attempt budget: the socket is down or the device logged
// out mid-flight, so the row stays pending to retry when connectivity returns.
// Only genuine send rejections burn attempts and eventually park the row.
func transient(err error) bool {
	return errors.Is(err, whatsmeow.ErrNotConnected) || errors.Is(err, whatsmeow.ErrNotLoggedIn)
}

func (d *OutboxDrain) fail(ctx context.Context, id int64, cause error) {
	msg := cause.Error()
	if len(msg) > 500 {
		msg = msg[:500]
	}
	d.log.Warn("whatsapp outbox row failed", "id", id, "err", msg)
	if err := d.store.markFailed(ctx, id, msg); err != nil {
		d.log.Error("mark failed failed", "id", id, "err", err)
	}
}

// --- Production store ------------------------------------------------------

// pgStore is the Postgres-backed store. Every statement fully-qualifies
// chat.whatsapp_outbox so the gateway's search_path=whatsapp (used for the
// whatsmeow tables) does not resolve the wrong table.
type pgStore struct {
	db *sql.DB
}

func (s *pgStore) claimPending(ctx context.Context) ([]outboxRow, error) {
	rows, err := s.db.QueryContext(ctx, `
		SELECT id, group_jid, kind, content, quoted_message_id, edit_of,
		       target_message_id, target_sender_jid, reaction, reaction_remove
		FROM chat.whatsapp_outbox
		WHERE posted_at IS NULL AND attempts < $1
		ORDER BY group_jid, created_at, id
		LIMIT $2`, outboxMaxAttempts, outboxBatch)
	if err != nil {
		return nil, fmt.Errorf("claim pending: %w", err)
	}
	defer rows.Close()
	var out []outboxRow
	for rows.Next() {
		var r outboxRow
		if err := rows.Scan(&r.id, &r.groupJID, &r.kind, &r.content, &r.quotedMessageID,
			&r.editOf, &r.targetMessageID, &r.targetSenderJID, &r.reaction, &r.reactionRemove); err != nil {
			return nil, fmt.Errorf("scan pending: %w", err)
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

func (s *pgStore) sentMessageID(ctx context.Context, id int64) (string, error) {
	var v sql.NullString
	err := s.db.QueryRowContext(ctx,
		`SELECT sent_message_id FROM chat.whatsapp_outbox WHERE id = $1`, id).Scan(&v)
	if err != nil {
		return "", err
	}
	return v.String, nil
}

func (s *pgStore) markPosted(ctx context.Context, id int64, sentMessageID string) error {
	// NULLIF preserves an existing sent_message_id when an empty one is passed
	// (edits/reactions do not mint an id the outbox needs to keep).
	_, err := s.db.ExecContext(ctx, `
		UPDATE chat.whatsapp_outbox
		SET posted_at = now(),
		    sent_message_id = COALESCE(NULLIF($2, ''), sent_message_id)
		WHERE id = $1`, id, sentMessageID)
	return err
}

func (s *pgStore) markFailed(ctx context.Context, id int64, errMsg string) error {
	_, err := s.db.ExecContext(ctx, `
		UPDATE chat.whatsapp_outbox
		SET attempts = attempts + 1, last_error = $2
		WHERE id = $1`, id, errMsg)
	return err
}

func (s *pgStore) markEditExpired(ctx context.Context, id int64) error {
	_, err := s.db.ExecContext(ctx, `
		UPDATE chat.whatsapp_outbox
		SET posted_at = now(), last_error = 'edit_window_expired'
		WHERE id = $1`, id)
	return err
}

// --- Production sender -----------------------------------------------------

// clientHolder exposes the underlying *whatsmeow.Client from the production
// session so NewOutboxDrain can build a whatsmeowSender. Kept unexported: only
// this package builds the production drain.
type clientHolder interface {
	waClient() *whatsmeow.Client
}

func (s *whatsmeowSession) waClient() *whatsmeow.Client { return s.client }

// whatsmeowSender sends outbox rows over a live whatsmeow client.
type whatsmeowSender struct {
	client *whatsmeow.Client
}

// Ready reports whether the live socket can send: connected and logged in.
func (w *whatsmeowSender) Ready() bool {
	return w.client.IsConnected() && w.client.IsLoggedIn()
}

func (w *whatsmeowSender) SendText(ctx context.Context, groupJID, content, quotedMessageID string) (string, error) {
	chat, err := types.ParseJID(groupJID)
	if err != nil {
		return "", fmt.Errorf("parse group jid: %w", err)
	}
	var msg *waE2E.Message
	if quotedMessageID != "" {
		// Best-effort reply: the outbox row carries only the quoted message id, not
		// the quoted sender or body, so the ContextInfo sets StanzaID (and the group
		// as RemoteJID). WhatsApp threads the reply from the StanzaID; the quoted
		// preview may be sparse without Participant/QuotedMessage, which the outbox
		// does not have.
		msg = &waE2E.Message{
			ExtendedTextMessage: &waE2E.ExtendedTextMessage{
				Text: strptr(content),
				ContextInfo: &waE2E.ContextInfo{
					StanzaID:  strptr(quotedMessageID),
					RemoteJID: strptr(chat.String()),
				},
			},
		}
	} else {
		msg = &waE2E.Message{Conversation: strptr(content)}
	}
	resp, err := w.client.SendMessage(ctx, chat, msg)
	if err != nil {
		return "", err
	}
	return resp.ID, nil
}

func (w *whatsmeowSender) SendEdit(ctx context.Context, groupJID, targetMessageID, newContent string) error {
	chat, err := types.ParseJID(groupJID)
	if err != nil {
		return fmt.Errorf("parse group jid: %w", err)
	}
	edit := w.client.BuildEdit(chat, targetMessageID, &waE2E.Message{
		Conversation: strptr(newContent),
	})
	_, err = w.client.SendMessage(ctx, chat, edit)
	return err
}

func (w *whatsmeowSender) SendReaction(ctx context.Context, groupJID, targetMessageID, targetSenderJID, reaction string) error {
	chat, err := types.ParseJID(groupJID)
	if err != nil {
		return fmt.Errorf("parse group jid: %w", err)
	}
	senderJID, err := types.ParseJID(targetSenderJID)
	if err != nil {
		return fmt.Errorf("parse target sender jid: %w", err)
	}
	react := w.client.BuildReaction(chat, senderJID, targetMessageID, reaction)
	_, err = w.client.SendMessage(ctx, chat, react)
	return err
}
