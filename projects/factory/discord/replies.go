package discord

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"time"
)

// RequestReplies answers Joe's finished factory requests (HOME-943). It
// polls bosun-requests for requests whose ticket reached Done or Canceled,
// replies to each request's Discord message with the ticket and PR links,
// and records the reply as sent. A request whose server is not allowlisted,
// or whose channel is in another server, gets no reply; it is recorded as
// sent too, so it cannot hold the poll's window.
type RequestReplies struct {
	URL     string // DISCORD_REQUESTS_URL, the bosun-requests Service
	HTTP    *http.Client
	REST    *REST
	Allowed func(guild string) bool
	Every   time.Duration
	Log     *slog.Logger
}

// RequestReply is one finished request, as bosun-requests lists it.
type RequestReply struct {
	ID        int64  `json:"request_id"`
	Guild     string `json:"guild_id"`
	Channel   string `json:"channel_id"`
	Message   string `json:"message_id"`
	Title     string `json:"title"`
	State     string `json:"state"`
	TicketURL string `json:"ticket_url"`
	PRs       []struct {
		URL    string `json:"url"`
		Merged bool   `json:"merged"`
	} `json:"prs"`
}

// Run polls until ctx ends.
func (q *RequestReplies) Run(ctx context.Context) {
	every := q.Every
	if every <= 0 {
		every = time.Minute
	}
	t := time.NewTicker(every)
	defer t.Stop()
	for {
		if err := q.Poll(ctx); err != nil && ctx.Err() == nil {
			q.Log.WarnContext(ctx, "request replies failed", "error", err)
		}
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
	}
}

// Poll answers each listed request once.
func (q *RequestReplies) Poll(ctx context.Context) error {
	var list struct {
		Replies []RequestReply `json:"replies"`
	}
	if err := q.do(ctx, http.MethodGet, "/replies", &list); err != nil {
		return err
	}
	bot := ""
	for _, r := range list.Replies {
		l := q.Log.With("request", r.ID, "guild", r.Guild, "channel", r.Channel)
		skip := ""
		if q.Allowed == nil || !q.Allowed(r.Guild) {
			skip = "server not allowlisted"
		} else if guild, err := q.REST.ChannelGuild(ctx, r.Channel); permanent(err) {
			skip = "channel unreadable: " + err.Error()
		} else if err != nil {
			return err
		} else if guild != r.Guild {
			skip = "channel is in another server"
		}
		if skip != "" {
			l.WarnContext(ctx, "request reply skipped", "reason", skip)
			if err := q.ack(ctx, r.ID); err != nil {
				return err
			}
			continue
		}
		if bot == "" {
			me, err := q.REST.Me(ctx)
			if err != nil || me.ID == "" {
				return fmt.Errorf("bot user: %w", err)
			}
			bot = me.ID
		}
		hash := sha256.Sum256([]byte("bosun-request/" + strconv.FormatInt(r.ID, 10)))
		nonce := hex.EncodeToString(hash[:12])
		ref := "-# ref " + nonce
		// A reply sent before a lost acknowledgement is not sent again.
		posted, err := postedToday(ctx, q.REST, r.Channel, bot, ref, time.Now().UTC().Add(-48*time.Hour))
		if err != nil {
			return err
		}
		if !posted {
			reply := NewReply(RequestReplyText(r), r.Message)
			reply.Components = append(reply.Components, ReplyComponent{Type: componentText, Content: ref})
			reply.Nonce, reply.EnforceNonce = nonce, true
			// Discord posts it unreferenced if the message is gone. A
			// refusal for this channel must not hold the later replies.
			if err := q.REST.Send(ctx, r.Channel, reply); permanent(err) {
				l.WarnContext(ctx, "request reply refused", "error", err)
			} else if err != nil {
				return err
			}
		}
		if err := q.ack(ctx, r.ID); err != nil {
			return err
		}
		l.InfoContext(ctx, "request answered", "state", r.State, "already_posted", posted)
	}
	return nil
}

// RequestReplyText is the reply: the outcome, the ticket and each PR.
func RequestReplyText(r RequestReply) string {
	var b strings.Builder
	if r.State == "canceled" {
		b.WriteString("Request canceled: ")
	} else {
		b.WriteString("Request done: ")
	}
	b.WriteString(r.Title)
	if r.TicketURL != "" {
		b.WriteString("\nTicket: " + r.TicketURL)
	}
	for _, pr := range r.PRs {
		state := "open"
		if pr.Merged {
			state = "merged"
		}
		b.WriteString("\nPR (" + state + "): " + pr.URL)
	}
	return b.String()
}

// permanent reports a Discord refusal that a retry does not change: a 4xx
// other than 429.
func permanent(err error) bool {
	var he *HTTPError
	return errors.As(err, &he) && he.Status >= 400 && he.Status < 500 && he.Status != 429
}

func (q *RequestReplies) ack(ctx context.Context, id int64) error {
	return q.do(ctx, http.MethodPost, "/replies/"+strconv.FormatInt(id, 10)+"/sent", nil)
}

func (q *RequestReplies) do(ctx context.Context, method, path string, out any) error {
	client := q.HTTP
	if client == nil {
		client = &http.Client{Timeout: 10 * time.Second}
	}
	req, err := http.NewRequestWithContext(ctx, method, strings.TrimRight(q.URL, "/")+path, nil)
	if err != nil {
		return err
	}
	resp, err := client.Do(req)
	if err != nil {
		return fmt.Errorf("bosun-requests: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode/100 != 2 {
		return fmt.Errorf("bosun-requests %s %s: %s", method, path, resp.Status)
	}
	if out == nil {
		return nil
	}
	return json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(out)
}
