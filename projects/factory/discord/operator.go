package discord

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"slices"
	"sync"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
)

// Operator places Joe's operator session (HOME-314): each thread under one
// of Channels in Guild is one session. UserID is Joe's Discord user ID. It
// comes from a Secret file, not the policy. A session's context holds only
// messages whose author is UserID. This is a session-specific exception to
// HOME-162 rules 1 and 4: no other author, bot, webhook, forward, reply
// preview, channel history or channel memory reaches the operator Agent.
type Operator struct {
	Guild    string
	Channels []string
	UserID   string
}

func (o Operator) enabled() bool { return o.Guild != "" && len(o.Channels) > 0 }

// ReplyOperatorThread answers Joe in an operator channel outside a thread.
const ReplyOperatorThread = "Start a thread in this channel to open an operator session."

// Thread channel types: announcement, public and private threads.
var threadTypes = map[int]bool{10: true, 11: true, 12: true}

// Bounds on the operator path.
const (
	// maxPending is the most messages held for one channel while the bridge
	// reads its parent.
	maxPending = 16
	// maxRestored and maxRestoredChars bound the earlier messages a new
	// session gets from its thread.
	maxRestored      = 30
	maxRestoredChars = 8000
)

// ChannelReader reads one channel or thread.
type ChannelReader interface {
	Channel(ctx context.Context, id string) (Channel, error)
}

// ThreadHistory reads a thread's messages older than a message.
type ThreadHistory interface {
	MessagesBefore(ctx context.Context, channelID, before string) ([]HistoryMessage, error)
}

// Channel reads a channel or thread.
func (r *REST) Channel(ctx context.Context, id string) (Channel, error) {
	var c Channel
	err := r.do(ctx, http.MethodGet, "/channels/"+id, nil, &c)
	return c, err
}

// OperatorSession is a thread's kagent session. Fresh is true when the
// session holds no turn yet, so the bridge restores the thread's earlier
// messages into its first turn.
type OperatorSession struct {
	ContextID string
	Fresh     bool
}

// OperatorAgent runs operator turns: one persistent session per thread.
type OperatorAgent interface {
	Session(ctx context.Context, threadID string) (OperatorSession, error)
	Turn(ctx context.Context, threadID, messageID, prompt string) (string, error)
}

// operatorScope reports whether m is in the operator's channels, and then
// handles it. A message there is never ingested and never starts a public
// turn. A channel whose parent is not known yet is held until the bridge
// reads it, so nothing passes before the bridge knows where it is.
func (b *Bridge) operatorScope(ctx context.Context, m Message) bool {
	o := b.Config.Operator
	if !o.enabled() || m.GuildID != o.Guild {
		return false
	}
	if contains(o.Channels, m.ChannelID) {
		if b.fromOperator(m) && m.MentionsUser(b.bot()) {
			if notices, ok := b.admit(ctx, m); ok {
				b.queueNotice(ctx, notices, m, ReplyOperatorThread)
			}
		}
		return true
	}
	b.mu.Lock()
	parent, known := b.parents[m.ChannelID]
	if !known {
		if b.Channels == nil {
			b.mu.Unlock()
			return true // Nothing can place the channel: hold nothing, pass nothing.
		}
		if b.pending == nil {
			b.pending = map[string][]Message{}
		}
		held, reading := b.pending[m.ChannelID]
		if len(held) < maxPending {
			held = append(held, m)
		}
		b.pending[m.ChannelID] = held
		if !reading {
			b.wg.Add(1)
			go b.place(ctx, m.ChannelID)
		}
		b.mu.Unlock()
		return true
	}
	b.mu.Unlock()
	if !contains(o.Channels, parent) {
		return false
	}
	b.operatorMessage(ctx, m)
	return true
}

// place reads a channel's parent, then handles the messages held for it. A
// failed read drops them, and the channel's next message reads it again.
func (b *Bridge) place(ctx context.Context, id string) {
	defer b.wg.Done()
	rctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	c, err := b.Channels.Channel(rctx, id)
	cancel()
	b.mu.Lock()
	held := b.pending[id]
	delete(b.pending, id)
	if err == nil {
		if b.parents == nil {
			b.parents = map[string]string{}
		}
		b.parents[id] = ""
		if threadTypes[c.Type] {
			b.parents[id] = c.ParentID
		}
	}
	b.mu.Unlock()
	if err != nil {
		b.Log.WarnContext(ctx, "channel lookup failed; messages dropped", "channel", id, "messages", len(held), "error", err)
		return
	}
	for _, m := range held {
		b.Handle(ctx, m)
	}
}

func (b *Bridge) bot() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.botID
}

// fromOperator reports whether Joe wrote m himself: his user ID, not a bot
// or webhook, a plain message or reply, and not a forward.
func (b *Bridge) fromOperator(m Message) bool {
	uid := b.Config.Operator.UserID
	return uid != "" && m.Author.ID == uid && uid != b.bot() && !m.Author.Bot && m.WebhookID == "" &&
		conversationTypes[m.Type] && !m.Forwarded()
}

// admit records m as seen, or reports a repeat or a message older than the
// dedupe window.
func (b *Bridge) admit(ctx context.Context, m Message) (chan notice, bool) {
	now := b.now()
	if now.Sub(SentAt(m)) > dedupeWindow {
		return nil, false
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.draining {
		b.Log.InfoContext(ctx, "drain: message not taken", "channel", m.ChannelID, "message", m.ID)
		return nil, false
	}
	b.start(ctx)
	if _, dup := b.seen[m.ID]; dup {
		return nil, false
	}
	b.seen[m.ID] = now
	b.prune(now)
	return b.notices, true
}

// operatorMessage queues Joe's message in a session thread. The public
// limits do not apply; the thread's queue depth does.
func (b *Bridge) operatorMessage(ctx context.Context, m Message) {
	if b.Operator == nil || !b.fromOperator(m) || b.stripMention(m.Content) == "" {
		return
	}
	notices, ok := b.admit(ctx, m)
	if !ok {
		return
	}
	key := "operator:" + m.ChannelID
	b.mu.Lock()
	q, exists := b.queues[key]
	if !exists {
		q = make(chan Message, max(b.Config.QueueDepth, 1))
		b.queues[key] = q
		b.wg.Add(1)
		go b.worker(ctx, q, b.operatorTurn)
	}
	b.mu.Unlock()
	b.enqueue(ctx, notices, q, m)
}

// operatorTurn runs one operator turn in the thread's session. It recalls
// and retains nothing: Bosun's channel memory stays out of the session.
func (b *Bridge) operatorTurn(ctx context.Context, m Message) {
	l := b.Log.With("guild", m.GuildID, "thread", m.ChannelID, "message", m.ID)
	s, err := b.Operator.Session(ctx, m.ChannelID)
	if err != nil {
		l.WarnContext(ctx, "operator session unavailable", "error", err)
		b.reply(ctx, m, ReplyFailed)
		return
	}
	var earlier []OperatorLine
	if s.Fresh {
		earlier = b.restore(ctx, m)
	}
	answer, err := b.Operator.Turn(ctx, m.ChannelID, m.ID, OperatorPrompt(m, b.Places.Resolve(m.GuildID, b.stripMention(m.Content), m.Mentions), earlier))
	if err != nil {
		l.WarnContext(ctx, "operator turn failed", "error", err)
		b.reply(ctx, m, ReplyFailed)
		return
	}
	text, _, files := Visuals(ctx, b.Render, answer)
	b.replyWith(ctx, m, text, files)
}

// OperatorLine is one of Joe's earlier messages in a session thread.
type OperatorLine struct {
	SentAt  string `json:"sent_at"`
	Message string `json:"message"`
}

// restore reads Joe's own earlier messages in m's thread, oldest first, for
// a session that holds no turn yet. Every other message is left out.
func (b *Bridge) restore(ctx context.Context, m Message) []OperatorLine {
	if b.History == nil {
		return nil
	}
	msgs, err := b.History.MessagesBefore(ctx, m.ChannelID, m.ID)
	if err != nil {
		b.Log.WarnContext(ctx, "operator thread history unavailable", "thread", m.ChannelID, "error", err)
		return nil
	}
	var out []OperatorLine
	chars := 0
	for _, h := range msgs { // Newest first.
		if (h.ChannelID != "" && h.ChannelID != m.ChannelID) || !b.fromOperator(h.Message) {
			continue
		}
		text := b.Places.Resolve(m.GuildID, b.stripMention(h.Content), h.Mentions)
		if text == "" {
			continue
		}
		if len(out) == maxRestored || chars+len(text) > maxRestoredChars {
			break
		}
		chars += len(text)
		out = append(out, OperatorLine{SentAt: SentAt(h.Message).UTC().Format(time.RFC3339), Message: text})
	}
	slices.Reverse(out)
	return out
}

// OperatorPrompt is an operator turn's message to the agent: Joe's message
// and, for a new session, his earlier messages in the thread.
func OperatorPrompt(m Message, text string, earlier []OperatorLine) string {
	data := map[string]any{
		"sent_at": SentAt(m).UTC().Format(time.RFC3339),
		"message": text,
	}
	if len(earlier) > 0 {
		data["earlier_messages"] = earlier
	}
	raw, _ := json.Marshal(data)
	return "Joe wrote this message in his Discord operator session. The bridge checked that Joe is " +
		"its author. The JSON holds only his own words: the message and, when this session is new, " +
		"his earlier messages in this thread. Answer the message.\n\n" + string(raw)
}

// OperatorSessions creates and repairs kagent sessions.
type OperatorSessions interface {
	CreateSession(ctx context.Context, agent kagent.AgentRef, requestID string) (*kagent.Session, error)
	RevertSession(ctx context.Context, id string) (*kagent.Session, error)
}

// maxSessionGenerations bounds the replacements of a thread's session.
const maxSessionGenerations = 16

// OperatorRequestID is the request ID of a thread's session. kagent returns
// the same session for the same request ID, so a restarted bridge finds the
// thread's session again. A deleted session moves the thread to the next
// generation.
func OperatorRequestID(threadID string, generation int) string {
	if generation == 0 {
		return "discord-operator-" + threadID
	}
	return fmt.Sprintf("discord-operator-%s-%d", threadID, generation)
}

// ChannelRequestID is the request ID of a server agent's session for one
// channel (HOME-909): each channel, a thread included, has its own.
func ChannelRequestID(channelID string, generation int) string {
	if generation == 0 {
		return "discord-channel-" + channelID
	}
	return fmt.Sprintf("discord-channel-%s-%d", channelID, generation)
}

// EpochChannelRequestID names channel sessions with epoch in the ID:
// discord-channel-<epoch>-<channel>. kagent pins a session to the Agent
// revision that created it, so a new epoch gives every channel a new session
// on the current revision. An empty epoch is ChannelRequestID.
func EpochChannelRequestID(epoch string) func(string, int) string {
	if epoch == "" {
		return ChannelRequestID
	}
	return func(channelID string, generation int) string {
		return ChannelRequestID(epoch+"-"+channelID, generation)
	}
}

// ServerRequestID is the request ID of a server agent's session for a whole
// server, before sessions moved to one per channel (HOME-909).
func ServerRequestID(guildID string, generation int) string {
	if generation == 0 {
		return "discord-server-" + guildID
	}
	return fmt.Sprintf("discord-server-%s-%d", guildID, generation)
}

func (k *KagentOperator) requestID(key string, generation int) string {
	if k.RequestID != nil {
		return k.RequestID(key, generation)
	}
	return OperatorRequestID(key, generation)
}

// KagentOperator runs operator turns on one kagent Agent. Each thread keeps
// one session across turns and bridge restarts; nothing deletes it.
type KagentOperator struct {
	A2A      *kagent.A2A
	Sessions OperatorSessions
	Agent    kagent.AgentRef
	Poll     time.Duration
	Timeout  time.Duration
	// RequestID names a key's session; nil names a thread's
	// (OperatorRequestID). A server agent uses ServerRequestID, so its
	// sessions never meet a thread's.
	RequestID func(key string, generation int) string

	mu    sync.Mutex
	known map[string]string // thread -> context ID
}

// Session finds or creates the thread's session.
func (k *KagentOperator) Session(ctx context.Context, threadID string) (OperatorSession, error) {
	k.mu.Lock()
	id, ok := k.known[threadID]
	k.mu.Unlock()
	if ok {
		return OperatorSession{ContextID: id}, nil
	}
	for gen := range maxSessionGenerations {
		s, err := k.Sessions.CreateSession(ctx, k.Agent, k.requestID(threadID, gen))
		if errors.Is(err, kagent.ErrSessionNotFound) || errors.Is(err, kagent.ErrSessionUnrecoverable) {
			continue
		}
		if err != nil {
			return OperatorSession{}, err
		}
		if s == nil || s.ContextID == "" || s.ID != s.ContextID || s.AgentNamespace != k.Agent.Namespace || s.AgentName != k.Agent.Name {
			return OperatorSession{}, errors.New("discord: session does not belong to the Agent")
		}
		if s.Gone() {
			continue
		}
		if s.Crashed() {
			if s, err = k.Sessions.RevertSession(ctx, s.ID); err != nil {
				return OperatorSession{}, err
			}
			if s == nil || s.Crashed() {
				return OperatorSession{}, errors.New("discord: operator session did not revert")
			}
		}
		page, err := k.A2A.ListTasks(ctx, k.Agent, s.ContextID, "", "")
		if err != nil {
			return OperatorSession{}, err
		}
		k.mu.Lock()
		if k.known == nil {
			k.known = map[string]string{}
		}
		k.known[threadID] = s.ContextID
		k.mu.Unlock()
		return OperatorSession{ContextID: s.ContextID, Fresh: page.TotalSize == 0 && len(page.Tasks) == 0}, nil
	}
	return OperatorSession{}, fmt.Errorf("discord: thread %s has no usable session after %d generations", threadID, maxSessionGenerations)
}

// Forget drops a key's cached session, so its next turn looks it up again.
func (k *KagentOperator) Forget(key string) {
	k.mu.Lock()
	delete(k.known, key)
	k.mu.Unlock()
}

// Turn sends prompt in the thread's session and waits for the answer. The
// Discord message ID makes the A2A message ID, so a resend after an
// ambiguous outcome is the same message. A failed turn makes the next turn
// look the session up again.
func (k *KagentOperator) Turn(ctx context.Context, threadID, messageID, prompt string) (string, error) {
	s, err := k.Session(ctx, threadID)
	if err != nil {
		return "", err
	}
	answer, err := k.turn(ctx, s.ContextID, messageID, prompt)
	if err != nil {
		k.mu.Lock()
		delete(k.known, threadID)
		k.mu.Unlock()
	}
	return answer, err
}

func (k *KagentOperator) turn(ctx context.Context, contextID, messageID, prompt string) (string, error) {
	timeout := k.Timeout
	if timeout <= 0 {
		timeout = 10 * time.Minute
	}
	poll := k.Poll
	if poll <= 0 {
		poll = 2 * time.Second
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	msg := kagent.Message{MessageID: "discord-" + messageID, ContextID: contextID, Parts: []kagent.Part{{Text: prompt}}}
	task, err := k.A2A.Send(ctx, k.Agent, msg)
	if errors.Is(err, kagent.ErrAmbiguous) {
		task, err = k.A2A.Send(ctx, k.Agent, msg)
	}
	if err != nil {
		return "", err
	}
	if task.ContextID != contextID {
		return "", errors.New("discord: kagent answered from another session")
	}
	return awaitAnswer(ctx, k.A2A, k.Agent, task, poll)
}
