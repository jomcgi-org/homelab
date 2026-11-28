// Package requests is bosun-requests: Bosun's private factory MCP tools
// (HOME-941). create_request records a factory request from Joe's Discord
// turn; the connector files it as a HOME issue, follows it, and replies to
// the message when it is Done or Canceled (connector/dispatch/bosun_requests.go).
// request_status reads that progress. The service's database login may
// only insert a request and read the status view.
package requests

import (
	"context"
	"database/sql"
	"errors"
	"strconv"
	"time"
	"unicode/utf8"

	"github.com/lib/pq"
)

// Bounds match the bosun_request table's checks.
const (
	MaxTitle       = 120
	MaxDescription = 4000
	// recent is how many requests request_status lists.
	recent = 10
)

// Origin is the Discord turn a request came from, as the bridge recorded it.
type Origin struct {
	Guild, Channel, Message, Author string
}

// PR is one pull request that delivers a request.
type PR struct {
	Number int    `json:"number"`
	URL    string `json:"url"`
	State  string `json:"state"`
	Merged bool   `json:"merged"`
}

// Status is what request_status returns for one request.
type Status struct {
	ID        int64      `json:"request_id"`
	Title     string     `json:"title"`
	State     string     `json:"state"`
	Ticket    string     `json:"ticket,omitempty"`
	TicketURL string     `json:"ticket_url,omitempty"`
	Column    string     `json:"column,omitempty"`
	Work      string     `json:"work_state,omitempty"`
	PRs       []PR       `json:"prs"`
	Created   time.Time  `json:"created_at"`
	Finished  *time.Time `json:"finished_at,omitempty"`
	Replied   bool       `json:"replied"`
}

// Store reads and writes the connector's bosun_request table.
type Store struct {
	DB *sql.DB
}

// ParamError is an invalid tool argument.
type ParamError struct{ Param, Reason string }

func (e *ParamError) Error() string { return e.Param + ": " + e.Reason }

// ErrNotFound is a request ID that is not in this server.
var ErrNotFound = errors.New("requests: no such request in this server")

// Create records a request once per Discord message: a second call in the
// same turn returns the first request.
func (s *Store) Create(ctx context.Context, o Origin, title, description string) (*Status, error) {
	if n := utf8.RuneCountInString(title); n < 1 || n > MaxTitle {
		return nil, &ParamError{"title", "must be 1 to " + strconv.Itoa(MaxTitle) + " characters"}
	}
	if n := utf8.RuneCountInString(description); n < 1 || n > MaxDescription {
		return nil, &ParamError{"description", "must be 1 to " + strconv.Itoa(MaxDescription) + " characters"}
	}
	var id int64
	err := s.DB.QueryRowContext(ctx, `INSERT INTO bosun_request (guild_id, channel_id, message_id, author_id, title, description)
		VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT (message_id) DO NOTHING RETURNING id`,
		o.Guild, o.Channel, o.Message, o.Author, title, description).Scan(&id)
	if errors.Is(err, sql.ErrNoRows) {
		err = s.DB.QueryRowContext(ctx, `SELECT id FROM bosun_request WHERE message_id = $1`, o.Message).Scan(&id)
	}
	if err != nil {
		return nil, err
	}
	return s.Get(ctx, o.Guild, id)
}

// Get returns one request of the server.
func (s *Store) Get(ctx context.Context, guild string, id int64) (*Status, error) {
	out, err := s.query(ctx, `WHERE guild_id = $1 AND id = $2`, guild, id)
	if err != nil {
		return nil, err
	}
	if len(out) == 0 {
		return nil, ErrNotFound
	}
	return &out[0], nil
}

// Recent returns the server's latest requests, newest first.
func (s *Store) Recent(ctx context.Context, guild string) ([]Status, error) {
	return s.query(ctx, `WHERE guild_id = $1 AND id IN (
		SELECT DISTINCT id FROM bosun_request_status WHERE guild_id = $1 ORDER BY id DESC LIMIT `+strconv.Itoa(recent)+`)`, guild)
}

func (s *Store) query(ctx context.Context, where string, args ...any) ([]Status, error) {
	rows, err := s.DB.QueryContext(ctx, `SELECT id, title, state, coalesce(issue_key, ''), coalesce(column_name, ''),
			coalesce(work_state, ''), created_at, finished_at, replied_at IS NOT NULL,
			coalesce(pr_number, 0), coalesce(pr_state, ''), merged_at IS NOT NULL
		FROM bosun_request_status `+where+` ORDER BY id DESC, pr_number`, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Status
	for rows.Next() {
		var st Status
		var pr PR
		var finished sql.NullTime
		if err := rows.Scan(&st.ID, &st.Title, &st.State, &st.Ticket, &st.Column, &st.Work, &st.Created, &finished,
			&st.Replied, &pr.Number, &pr.State, &pr.Merged); err != nil {
			return nil, err
		}
		if n := len(out); n > 0 && out[n-1].ID == st.ID {
			st = out[n-1]
			out = out[:n-1]
		} else {
			st.PRs = []PR{}
			if finished.Valid {
				st.Finished = &finished.Time
			}
			if st.Ticket != "" {
				st.TicketURL = "https://factory-plan.jomcgi.dev/factory/issue/" + st.Ticket
			}
		}
		if pr.Number > 0 {
			pr.URL = "https://github.com/jomcgi-org/homelab/pull/" + strconv.Itoa(pr.Number)
			st.PRs = append(st.PRs, pr)
		}
		out = append(out, st)
	}
	return out, rows.Err()
}

// Reply is a finished request the bridge has yet to answer.
type Reply struct {
	ID        int64  `json:"request_id"`
	Guild     string `json:"guild_id"`
	Channel   string `json:"channel_id"`
	Message   string `json:"message_id"`
	Title     string `json:"title"`
	State     string `json:"state"`
	TicketURL string `json:"ticket_url"`
	PRs       []PR   `json:"prs"`
}

// maxReplies bounds one poll.
const maxReplies = 20

// Unreplied lists finished requests of the servers whose reply is not sent.
func (s *Store) Unreplied(ctx context.Context, servers []string) ([]Reply, error) {
	rows, err := s.DB.QueryContext(ctx, `SELECT id, guild_id, channel_id, message_id, title, state, coalesce(issue_key, ''),
			coalesce(pr_number, 0), coalesce(pr_state, ''), merged_at IS NOT NULL
		FROM bosun_request_status
		WHERE state IN ('done', 'canceled') AND replied_at IS NULL AND guild_id = ANY($1) AND id IN (
			SELECT DISTINCT id FROM bosun_request_status
			WHERE state IN ('done', 'canceled') AND replied_at IS NULL AND guild_id = ANY($1)
			ORDER BY id LIMIT `+strconv.Itoa(maxReplies)+`)
		ORDER BY id, pr_number`, pq.Array(servers))
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Reply
	for rows.Next() {
		var r Reply
		var key string
		var pr PR
		if err := rows.Scan(&r.ID, &r.Guild, &r.Channel, &r.Message, &r.Title, &r.State, &key, &pr.Number, &pr.State, &pr.Merged); err != nil {
			return nil, err
		}
		if n := len(out); n > 0 && out[n-1].ID == r.ID {
			r = out[n-1]
			out = out[:n-1]
		} else {
			r.PRs = []PR{}
			if key != "" {
				r.TicketURL = "https://factory-plan.jomcgi.dev/factory/issue/" + key
			}
		}
		if pr.Number > 0 {
			pr.URL = "https://github.com/jomcgi-org/homelab/pull/" + strconv.Itoa(pr.Number)
			r.PRs = append(r.PRs, pr)
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

// MarkReplied records that the bridge answered a finished request. The
// table's check refuses it for a request that is not finished.
func (s *Store) MarkReplied(ctx context.Context, id int64) error {
	_, err := s.DB.ExecContext(ctx, `UPDATE bosun_request SET replied_at = now() WHERE id = $1 AND replied_at IS NULL`, id)
	return err
}
