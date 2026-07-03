package whatsapp

import (
	"context"
	"database/sql"
	"fmt"
	"log/slog"
	"sync"
	"time"

	// pgx's database/sql adapter registers the "pgx" driver that the whatsmeow
	// sqlstore and the outbox notifier both open.
	_ "github.com/jackc/pgx/v5/stdlib"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
)

// Session is the minimal slice of the whatsmeow client the gateway state machine
// depends on. Narrowing to this interface lets the state machine be unit-tested
// with a fake (no live WhatsApp, no real DB); the production implementation
// (whatsmeowSession) wraps a *whatsmeow.Client.
type Session interface {
	// IsLoggedIn reports whether a device session is already stored (Store.ID is
	// set), i.e. the gateway can resume without re-pairing.
	IsLoggedIn() bool
	// Connect opens the WhatsApp websocket. For phone-code pairing whatsmeow
	// requires a live connection before a code can be requested.
	Connect() error
	// RequestPairingCode requests a phone-number pairing code for the configured
	// bot number and returns the code string for the operator to enter.
	RequestPairingCode(ctx context.Context) (string, error)
	// Disconnect tears the socket down (used when parking).
	Disconnect()
	// AddEventHandler registers a handler for whatsmeow events.
	AddEventHandler(func(evt any))
}

// Notifier delivers ops alerts (the pairing code and the logout/ban alert). The
// production implementation inserts into chat.discord_outbox so the existing
// Discord bot drain posts them; tests inject a fake that records calls.
type Notifier interface {
	Notify(ctx context.Context, level, content string) error
}

// Gateway is the transport-only WhatsApp channel gateway state machine. It owns
// no business logic: it pairs or resumes, tracks connection state, logs
// allow-listed group messages, and parks on logout or ban.
type Gateway struct {
	cfg       Config
	log       *slog.Logger
	session   Session
	notifier  Notifier
	forwarder Forwarder
	state     *stateHolder

	// baseCtx is the process context captured in Start. The event goroutine (which
	// receives no ctx of its own) derives bounded notify contexts from it so an
	// unbounded Postgres INSERT cannot wedge the whatsmeow event loop.
	baseCtx context.Context

	// allow is the set of group JIDs whose messages are forwarded; everything
	// else is dropped without forwarding.
	allow map[string]bool

	// parkOnce guards the parked-state transition so the alert fires exactly once
	// even if several logout/ban events arrive.
	parkOnce sync.Once
}

// NewGateway builds a gateway over the given session, notifier, and forwarder. It
// starts in the pairing state; Start drives the transition to connected (or
// parked). A nil forwarder disables forwarding (the message is only logged), used
// by tests that do not exercise the inbound path.
func NewGateway(cfg Config, log *slog.Logger, session Session, notifier Notifier, forwarder Forwarder) *Gateway {
	allow := make(map[string]bool, len(cfg.GroupJIDs))
	for _, jid := range cfg.GroupJIDs {
		allow[jid] = true
	}
	return &Gateway{
		cfg:       cfg,
		log:       log,
		session:   session,
		notifier:  notifier,
		forwarder: forwarder,
		state:     newStateHolder(StatePairing),
		allow:     allow,
	}
}

// State returns the current lifecycle state (also surfaced on /healthz).
func (g *Gateway) State() State { return g.state.get() }

// Start registers the event handler and drives the connect-or-pair state
// machine. On a stored session it just connects and waits for the Connected
// event; with no session it connects, requests a pairing code, and delivers it
// via the notifier exactly once. It does not block: the connection lifecycle is
// then driven by events into handleEvent.
func (g *Gateway) Start(ctx context.Context) error {
	g.baseCtx = ctx
	g.session.AddEventHandler(g.handleEvent)

	if g.session.IsLoggedIn() {
		g.log.Info("resuming stored whatsapp session")
		if err := g.session.Connect(); err != nil {
			return fmt.Errorf("connect (resume): %w", err)
		}
		return nil
	}

	g.log.Info("no stored session; requesting pairing code", "bot_number", g.cfg.BotNumber)
	g.state.set(StatePairing)
	if err := g.session.Connect(); err != nil {
		return fmt.Errorf("connect (pairing): %w", err)
	}
	code, err := g.session.RequestPairingCode(ctx)
	if err != nil {
		return fmt.Errorf("request pairing code: %w", err)
	}
	g.log.Info("pairing code issued", "bot_number", g.cfg.BotNumber, "code", code)
	msg := fmt.Sprintf("WhatsApp pairing code for %s: %s (enter it on the bot phone)", g.cfg.BotNumber, code)
	if err := g.notifier.Notify(ctx, "info", msg); err != nil {
		// Log-only fallback: the code is already in the gateway logs, so a failed
		// notify does not block pairing.
		g.log.Error("failed to deliver pairing code via notifier; see the code in the log above", "err", err)
	}
	return nil
}

// handleEvent is the whatsmeow event sink. It flips lifecycle state on
// connection events, parks on logout or ban, and logs allow-listed group
// messages. It takes `any` so the real whatsmeow event structs can be fed
// directly (including from unit tests, which construct real events).
func (g *Gateway) handleEvent(evt any) {
	// Parked is terminal until an operator re-pairs: ignore any later events
	// (a stray Connected or Message must not resurrect a logged-out gateway).
	if g.state.get() == StateParked {
		return
	}
	switch e := evt.(type) {
	case *events.Connected:
		g.state.set(StateConnected)
		g.log.Info("whatsapp connected")
	case *events.PairSuccess:
		g.log.Info("whatsapp pairing succeeded", "device_jid", e.ID.String())
		nctx, cancel := g.notifyCtx()
		if err := g.notifier.Notify(nctx, "info", "WhatsApp pairing succeeded; the gateway is connecting."); err != nil {
			g.log.Warn("failed to deliver pairing-success notice", "err", err)
		}
		cancel()
	case *events.LoggedOut:
		g.park(fmt.Sprintf("logged out (on_connect=%v reason=%v)", e.OnConnect, e.Reason))
	case *events.TemporaryBan:
		g.park(fmt.Sprintf("temporarily banned: %s", e.String()))
	case *events.Message:
		g.onMessage(e)
	}
}

// onMessage forwards a message from an allow-listed group to the monolith
// inbound endpoint (ADR 039 spec section 2), preserving per-group order via the
// forwarder. Messages from any other chat (a DM to the bot number, an unknown
// group) are dropped silently. A nil forwarder (tests, or forwarding not
// configured) falls back to log-only.
func (g *Gateway) onMessage(e *events.Message) {
	if !e.Info.IsGroup {
		return
	}
	groupJID := e.Info.Chat.String()
	if !g.allow[groupJID] {
		return
	}
	g.log.Info("group message",
		"group_jid", groupJID,
		"sender_jid", e.Info.Sender.String(),
		"sender_name", e.Info.PushName,
		"message_id", e.Info.ID,
	)
	if g.forwarder == nil {
		return
	}
	g.forwarder.Forward(InboundPayload{
		GroupJID:        groupJID,
		SenderJID:       e.Info.Sender.String(),
		SenderName:      e.Info.PushName,
		MessageID:       e.Info.ID,
		Text:            messageText(e),
		QuotedMessageID: quotedMessageID(e),
		Timestamp:       e.Info.Timestamp.UTC().Format(time.RFC3339),
	})
}

// quotedMessageID returns the id of the message this one replies to, or "" when
// it is not a reply. WhatsApp carries it in the ExtendedTextMessage ContextInfo.
func quotedMessageID(e *events.Message) string {
	if e.Message == nil {
		return ""
	}
	ci := e.Message.GetExtendedTextMessage().GetContextInfo()
	if ci == nil {
		return ""
	}
	return ci.GetStanzaID()
}

// notifyCtx returns a 10s-bounded context derived from the captured process
// context (falling back to Background if Start has not run) so a notify INSERT
// on the event goroutine cannot block the whatsmeow event loop indefinitely.
// The caller must invoke the returned cancel.
func (g *Gateway) notifyCtx() (context.Context, context.CancelFunc) {
	base := g.baseCtx
	if base == nil {
		base = context.Background()
	}
	return context.WithTimeout(base, 10*time.Second)
}

// park moves the gateway to the parked state: it stops work, fires exactly one
// error alert naming the cause, and disconnects. It does not crash-loop or retry
// registration; re-pairing is an operator runbook.
func (g *Gateway) park(cause string) {
	g.parkOnce.Do(func() {
		g.state.set(StateParked)
		g.log.Error("whatsapp gateway parked", "cause", cause)
		nctx, cancel := g.notifyCtx()
		if err := g.notifier.Notify(nctx, "error", "WhatsApp gateway parked: "+cause+". Re-pairing is required (runbook)."); err != nil {
			g.log.Error("failed to deliver parked alert", "err", err, "cause", cause)
		}
		cancel()
		g.session.Disconnect()
	})
}

// messageText extracts the plain text of a message: a simple conversation body,
// or the text of an extended-text message. Media (v1 out of scope) yields "".
func messageText(e *events.Message) string {
	if e.Message == nil {
		return ""
	}
	if c := e.Message.GetConversation(); c != "" {
		return c
	}
	return e.Message.GetExtendedTextMessage().GetText()
}

// --- Production adapters ---------------------------------------------------

// whatsmeowSession is the production Session backed by a *whatsmeow.Client whose
// device store lives in the `whatsapp` Postgres schema.
type whatsmeowSession struct {
	client  *whatsmeow.Client
	cfg     Config
	log     *slog.Logger
	qrReady chan struct{} // closed on the first QR event so pairing can proceed
	qrOnce  sync.Once
}

// NewWhatsmeowSession builds the sqlstore-backed whatsmeow client. It opens the
// device store on the `whatsapp` schema (via search_path), takes the first
// stored device or a fresh one, and registers an internal handler that signals
// when the connection is established enough to request a pairing code.
func NewWhatsmeowSession(ctx context.Context, cfg Config, log *slog.Logger) (Session, error) {
	dbLog := waLog.Stdout("whatsmeow-db", "WARN", false)
	container, err := sqlstore.New(ctx, "pgx", cfg.DSNWithSchema(), dbLog)
	if err != nil {
		return nil, fmt.Errorf("open whatsmeow sqlstore: %w", err)
	}
	device, err := container.GetFirstDevice(ctx)
	if err != nil {
		return nil, fmt.Errorf("get whatsmeow device: %w", err)
	}
	client := whatsmeow.NewClient(device, waLog.Stdout("whatsmeow", "WARN", false))
	s := &whatsmeowSession{
		client:  client,
		cfg:     cfg,
		log:     log,
		qrReady: make(chan struct{}),
	}
	// Internal handler: the first QR event means the pairing websocket is live,
	// which is whatsmeow's precondition for requesting a phone-code (see
	// Client.PairPhone docs).
	client.AddEventHandler(func(evt any) {
		if _, ok := evt.(*events.QR); ok {
			s.qrOnce.Do(func() { close(s.qrReady) })
		}
	})
	return s, nil
}

func (s *whatsmeowSession) IsLoggedIn() bool {
	return s.client.Store != nil && s.client.Store.ID != nil
}

func (s *whatsmeowSession) Connect() error { return s.client.Connect() }

func (s *whatsmeowSession) Disconnect() { s.client.Disconnect() }

func (s *whatsmeowSession) AddEventHandler(h func(evt any)) { s.client.AddEventHandler(h) }

// RequestPairingCode waits briefly for the pairing websocket to be ready, then
// asks whatsmeow for a phone-number pairing code for the bot number.
func (s *whatsmeowSession) RequestPairingCode(ctx context.Context) (string, error) {
	// Give the connection up to 10s to surface a QR event; fall back to
	// proceeding anyway (whatsmeow docs note a short sleep also suffices).
	select {
	case <-s.qrReady:
	case <-time.After(10 * time.Second):
		s.log.Warn("proceeding to request pairing code without a QR-ready signal")
	case <-ctx.Done():
		return "", ctx.Err()
	}
	code, err := s.client.PairPhone(ctx, s.cfg.BotNumber, true, whatsmeow.PairClientChrome, "Chrome (Linux)")
	if err != nil {
		return "", err
	}
	return code, nil
}

// pgNotifier is the production Notifier: it inserts an ops alert into
// chat.discord_outbox (fully schema-qualified, so the gateway's whatsapp
// search_path does not matter), which the monolith's Discord bot drains and
// posts. This reuses the existing outbox drain, so the gateway needs no HTTP
// endpoint or bot token of its own.
type pgNotifier struct {
	db        *sql.DB
	channelID string
}

// NewPGNotifier opens a Postgres connection on the same DSN and returns a
// Notifier targeting the given Discord channel. The caller owns closing the DB.
func NewPGNotifier(dsn, channelID string) (*pgNotifier, *sql.DB, error) {
	db, err := sql.Open("pgx", dsn)
	if err != nil {
		return nil, nil, fmt.Errorf("open notifier db: %w", err)
	}
	return &pgNotifier{db: db, channelID: channelID}, db, nil
}

func (n *pgNotifier) Notify(ctx context.Context, level, content string) error {
	_, err := n.db.ExecContext(ctx,
		`INSERT INTO chat.discord_outbox (channel_id, content, level) VALUES ($1, $2, $3)`,
		n.channelID, content, level)
	return err
}
