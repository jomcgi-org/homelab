package discord

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"math/rand/v2"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"nhooyr.io/websocket"
)

// Intents: guilds and guild messages only. Without the privileged message
// content intent, Discord still sends the text of a message that tags the
// bot, which is all the bridge reads (HOME-162 contract rule 5).
const Intents = 1<<0 | 1<<9

// Gateway opcodes.
const (
	opDispatch     = 0
	opHeartbeat    = 1
	opIdentify     = 2
	opResume       = 6
	opReconnect    = 7
	opInvalid      = 9
	opHello        = 10
	opHeartbeatACK = 11
)

const maxEventBytes = 8 << 20

// FatalError is a close code that another connection cannot fix, such as a
// bad token or intents the application may not use.
type FatalError struct{ Code websocket.StatusCode }

func (e *FatalError) Error() string {
	return fmt.Sprintf("discord: gateway closed with fatal code %d", e.Code)
}

var fatalCodes = map[websocket.StatusCode]bool{4004: true, 4010: true, 4011: true, 4012: true, 4013: true, 4014: true}

// Codes after which the session cannot resume.
var freshCodes = map[websocket.StatusCode]bool{4007: true, 4009: true}

// Gateway holds one connection to Discord's gateway and reconnects.
type Gateway struct {
	REST          *REST
	Token         func() (string, error)
	OnInteraction func(context.Context, Interaction)
	OnReady       func(User)
	OnMessage     func(Message)
	Log           *slog.Logger
	// Dial opens the connection; websocket.Dial when nil (tests replace it).
	Dial func(ctx context.Context, url string) (*websocket.Conn, error)
}

type payload struct {
	Op int             `json:"op"`
	D  json.RawMessage `json:"d,omitempty"`
	S  *int64          `json:"s,omitempty"`
	T  string          `json:"t,omitempty"`
}

type session struct {
	id        string
	resumeURL string
	seq       atomic.Int64
}

func (s *session) reset() {
	s.id, s.resumeURL = "", ""
	s.seq.Store(0)
}

// Run connects and reconnects until ctx ends or Discord sends a fatal code.
func (g *Gateway) Run(ctx context.Context) error {
	var s session
	backoff := time.Second
	for {
		url := s.resumeURL
		if url == "" {
			var err error
			if url, err = g.REST.GatewayURL(ctx); err != nil {
				g.Log.WarnContext(ctx, "gateway URL", "error", err)
				if err := sleep(ctx, backoff); err != nil {
					return err
				}
				backoff = min(backoff*2, 2*time.Minute)
				continue
			}
		}
		started := time.Now()
		err := g.connect(ctx, url, &s)
		if ctx.Err() != nil {
			return ctx.Err()
		}
		var fatal *FatalError
		if errors.As(err, &fatal) {
			return err
		}
		if time.Since(started) > 5*time.Minute {
			backoff = time.Second
		}
		g.Log.InfoContext(ctx, "gateway reconnecting", "error", err, "resume", s.id != "", "wait", backoff)
		if err := sleep(ctx, backoff); err != nil {
			return err
		}
		backoff = min(backoff*2, 2*time.Minute)
	}
}

func (g *Gateway) dial(ctx context.Context, url string) (*websocket.Conn, error) {
	if g.Dial != nil {
		return g.Dial(ctx, url)
	}
	c, _, err := websocket.Dial(ctx, url, nil)
	return c, err
}

// connect runs one connection: Hello, Identify or Resume, heartbeats and
// dispatches. It returns why the connection ended.
func (g *Gateway) connect(ctx context.Context, url string, s *session) error {
	sep := "?"
	if strings.Contains(url, "?") {
		sep = "&"
	}
	conn, err := g.dial(ctx, url+sep+"v=10&encoding=json")
	if err != nil {
		return err
	}
	defer conn.CloseNow()
	conn.SetReadLimit(maxEventBytes)

	hello, err := read(ctx, conn)
	if err != nil {
		return err
	}
	if hello.Op != opHello {
		return fmt.Errorf("discord: expected hello, got op %d", hello.Op)
	}
	var h struct {
		Interval int64 `json:"heartbeat_interval"`
	}
	if err := json.Unmarshal(hello.D, &h); err != nil || h.Interval <= 0 {
		return fmt.Errorf("discord: bad hello: %v", err)
	}
	token, err := g.Token()
	if err != nil {
		return fmt.Errorf("discord: bot token: %w", err)
	}
	if s.id != "" {
		err = write(ctx, conn, opResume, map[string]any{"token": token, "session_id": s.id, "seq": s.seq.Load()})
	} else {
		err = write(ctx, conn, opIdentify, map[string]any{
			"token":   token,
			"intents": Intents,
			"properties": map[string]string{
				"os": "linux", "browser": "homelab-discord-bridge", "device": "homelab-discord-bridge",
			},
		})
	}
	if err != nil {
		return err
	}

	beatCtx, stop := context.WithCancel(ctx)
	defer stop()
	var acked atomic.Bool
	acked.Store(true)
	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		interval := time.Duration(h.Interval) * time.Millisecond
		// Discord asks for a random delay before the first heartbeat.
		wait := time.Duration(rand.Float64() * float64(interval))
		for {
			if sleep(beatCtx, wait) != nil {
				return
			}
			wait = interval
			if !acked.Swap(false) {
				// No ACK since the last beat: a zombie connection.
				conn.Close(4000, "heartbeat not acknowledged")
				return
			}
			if write(beatCtx, conn, opHeartbeat, seqOrNil(s)) != nil {
				return
			}
		}
	}()
	defer wg.Wait()
	defer stop()

	for {
		p, err := read(ctx, conn)
		if err != nil {
			code := websocket.CloseStatus(err)
			if fatalCodes[code] {
				return &FatalError{Code: code}
			}
			if freshCodes[code] {
				s.reset()
			}
			return err
		}
		if p.S != nil {
			s.seq.Store(*p.S)
		}
		switch p.Op {
		case opDispatch:
			g.dispatch(ctx, s, p)
		case opHeartbeat:
			if err := write(ctx, conn, opHeartbeat, seqOrNil(s)); err != nil {
				return err
			}
		case opHeartbeatACK:
			acked.Store(true)
		case opReconnect:
			return errors.New("discord: gateway asked to reconnect")
		case opInvalid:
			var resumable bool
			_ = json.Unmarshal(p.D, &resumable)
			if !resumable {
				s.reset()
			}
			// Discord asks for a wait of one to five seconds.
			if err := sleep(ctx, time.Second+time.Duration(rand.IntN(4000))*time.Millisecond); err != nil {
				return err
			}
			return errors.New("discord: invalid session")
		}
	}
}

func (g *Gateway) dispatch(ctx context.Context, s *session, p payload) {
	switch p.T {
	case "READY":
		var r struct {
			SessionID string `json:"session_id"`
			ResumeURL string `json:"resume_gateway_url"`
			User      User   `json:"user"`
		}
		if err := json.Unmarshal(p.D, &r); err != nil {
			g.Log.WarnContext(ctx, "bad READY", "error", err)
			return
		}
		s.id, s.resumeURL = r.SessionID, r.ResumeURL
		g.Log.InfoContext(ctx, "gateway ready", "bot", r.User.ID)
		if g.OnReady != nil {
			g.OnReady(r.User)
		}
	case "INTERACTION_CREATE":
		var i Interaction
		if g.OnInteraction != nil && json.Unmarshal(p.D, &i) == nil {
			go g.OnInteraction(ctx, i)
		}
	case "RESUMED":
		g.Log.InfoContext(ctx, "gateway resumed")
	case "MESSAGE_CREATE":
		var m Message
		if err := json.Unmarshal(p.D, &m); err != nil {
			g.Log.WarnContext(ctx, "bad MESSAGE_CREATE", "error", err)
			return
		}
		if g.OnMessage != nil {
			g.OnMessage(m)
		}
	}
}

func seqOrNil(s *session) any {
	if n := s.seq.Load(); n > 0 {
		return n
	}
	return nil
}

func read(ctx context.Context, conn *websocket.Conn) (payload, error) {
	var p payload
	_, data, err := conn.Read(ctx)
	if err != nil {
		return p, err
	}
	return p, json.Unmarshal(data, &p)
}

func write(ctx context.Context, conn *websocket.Conn, op int, d any) error {
	data, err := json.Marshal(map[string]any{"op": op, "d": d})
	if err != nil {
		return err
	}
	return conn.Write(ctx, websocket.MessageText, data)
}
