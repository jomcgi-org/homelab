package archive

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"
	"unicode"

	"github.com/lib/pq"
)

// Caps on one history_sql call.
const (
	MaxSQLQuery = 10000
	MaxSQLRows  = 2000
	// MaxSQLBytes bounds the JSON of all rows of one answer.
	MaxSQLBytes = 60000
	SQLTimeout  = 10 * time.Second
)

// sqlTimeout is SQLTimeout; tests shorten it.
var sqlTimeout = SQLTimeout

// SQLArgs are history_sql's arguments.
type SQLArgs struct {
	Server string `json:"server"`
	Query  string `json:"query"`
}

// SQLResult is a history_sql answer.
type SQLResult struct {
	Server  string   `json:"server"`
	Columns []string `json:"columns"`
	Rows    [][]any  `json:"rows"`
	// Truncated marks an answer cut at MaxSQLRows or MaxSQLBytes.
	Truncated bool   `json:"truncated"`
	Note      string `json:"note"`
}

// QueryError is PostgreSQL's answer to a bad query; it goes back to the
// model so it can fix the query.
type QueryError struct{ Message string }

func (e *QueryError) Error() string { return e.Message }

// SQL runs one read-only statement on the server's own login. The login is
// a member of the history_sql group only, and row-level security shows it
// only its server's rows (Migrate). The statement runs in a READ ONLY
// transaction with a statement timeout, and is prepared (extended
// protocol), so PostgreSQL refuses more than one statement.
func (r *Reader) SQL(ctx context.Context, a SQLArgs) (SQLResult, error) {
	if err := r.server(a.Server); err != nil {
		return SQLResult{}, err
	}
	db := r.SQLDB[a.Server]
	if db == nil {
		return SQLResult{}, &ParamError{"server", "history_sql is not set up for this server"}
	}
	q := strings.TrimSpace(a.Query)
	switch {
	case q == "":
		return SQLResult{}, &ParamError{"query", "give one SQL SELECT statement"}
	case len(q) > MaxSQLQuery:
		return SQLResult{}, &ParamError{"query", "must be at most 10000 characters"}
	case !readStatement(q):
		return SQLResult{}, &ParamError{"query", "must be one SELECT (or WITH, VALUES, TABLE) statement"}
	}
	// The context ends the call if PostgreSQL's own timeout does not.
	ctx, cancel := context.WithTimeout(ctx, sqlTimeout+5*time.Second)
	defer cancel()
	tx, err := db.BeginTx(ctx, &sql.TxOptions{ReadOnly: true})
	if err != nil {
		return SQLResult{}, err
	}
	// Rollback also undoes any set_config the query calls.
	defer func() { _ = tx.Rollback() }()
	if _, err := tx.ExecContext(ctx, fmt.Sprintf("SET LOCAL statement_timeout = %d", sqlTimeout.Milliseconds())); err != nil {
		return SQLResult{}, err
	}
	// Cancelling qctx when an answer is cut stops the query, so closing the
	// rows does not wait for the rest of a large result.
	qctx, stop := context.WithCancel(ctx)
	defer stop()
	stmt, err := tx.PrepareContext(qctx, q)
	if err != nil {
		return SQLResult{}, queryError(err)
	}
	defer stmt.Close()
	rows, err := stmt.QueryContext(qctx)
	if err != nil {
		return SQLResult{}, queryError(err)
	}
	defer rows.Close()
	cols, err := rows.Columns()
	if err != nil {
		return SQLResult{}, err
	}
	res := SQLResult{Server: a.Server, Columns: cols, Rows: [][]any{}, Note: untrusted}
	size := 0
	for rows.Next() {
		if len(res.Rows) == MaxSQLRows {
			res.Truncated = true
			break
		}
		vals := make([]any, len(cols))
		ptrs := make([]any, len(cols))
		for i := range vals {
			ptrs[i] = &vals[i]
		}
		if err := rows.Scan(ptrs...); err != nil {
			return SQLResult{}, queryError(err)
		}
		for i, v := range vals {
			vals[i] = cell(v)
		}
		b, _ := json.Marshal(vals)
		size += len(b) + 1
		if size > MaxSQLBytes && len(res.Rows) > 0 {
			res.Truncated = true
			break
		}
		res.Rows = append(res.Rows, vals)
	}
	if res.Truncated {
		stop()
		return res, nil
	}
	if err := rows.Err(); err != nil {
		return SQLResult{}, queryError(err)
	}
	return res, nil
}

// cell makes a scanned value JSON-friendly: text for bytes, RFC 3339 for
// times, text cut at MaxText.
func cell(v any) any {
	switch x := v.(type) {
	case []byte:
		s, _ := cut(string(x), MaxText)
		return s
	case string:
		s, _ := cut(x, MaxText)
		return s
	case time.Time:
		return x.UTC().Format(time.RFC3339)
	}
	return v
}

// queryError keeps PostgreSQL's message for the model; anything else is an
// archive error.
func queryError(err error) error {
	var pe *pq.Error
	if !errors.As(err, &pe) {
		return err
	}
	switch pe.Code {
	case "57014":
		return &QueryError{fmt.Sprintf("the query ran longer than %s; add a time filter or simplify it", sqlTimeout)}
	case "08000", "08003", "08006", "53300", "57P01":
		return err
	}
	msg := pe.Message
	if pe.Hint != "" {
		msg += " (hint: " + pe.Hint + ")"
	}
	return &QueryError{msg}
}

var readKeywords = map[string]bool{"select": true, "with": true, "values": true, "table": true}

// readStatement reports whether the statement's first word, after
// comments and opening parentheses, starts a query. Privileges and the
// READ ONLY transaction are the controls; this only gives a clear error
// for SET, EXPLAIN, DO and the like.
func readStatement(q string) bool {
	for {
		q = strings.TrimLeftFunc(q, func(r rune) bool { return unicode.IsSpace(r) || r == '(' })
		switch {
		case strings.HasPrefix(q, "--"):
			i := strings.IndexByte(q, '\n')
			if i < 0 {
				return false
			}
			q = q[i+1:]
		case strings.HasPrefix(q, "/*"):
			i := strings.Index(q, "*/")
			if i < 0 {
				return false
			}
			q = q[i+2:]
		default:
			end := strings.IndexFunc(q, func(r rune) bool { return !unicode.IsLetter(r) })
			if end < 0 {
				end = len(q)
			}
			return readKeywords[strings.ToLower(q[:end])]
		}
	}
}
