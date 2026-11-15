package store

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"strings"
	"time"
)

// ErrRecoveryConflict rejects stale, premature, or already used attestations.
var ErrRecoveryConflict = errors.New("store: recovery evidence is stale, premature, or already used")

// RecoveryAttempt identifies every execution the operator must fence.
type RecoveryAttempt struct {
	ID, RunID, WorkItemEpoch            int64
	RunAttempt, CurrentRunAttempt       int
	WriterEpoch                         int64
	KagentNamespace, KagentAgent        string
	MessageID, ContextID, TaskID, State string
	Issues                              int
	SentAt                              time.Time
}

// RecoverySnapshot excludes polling timestamps and includes an unsent target.
// An empty CallerIdentity explicitly records legacy, unproven ownership.
type RecoverySnapshot struct {
	WorkID, WorkEpoch                                         int64
	SessionID                                                 string
	SessionGeneration                                         int
	RunID                                                     int64
	RunAttempt                                                int
	AttemptID                                                 int64
	MessageID, ContextID, TaskID, TargetState, CallerIdentity string
	Attempts                                                  []RecoveryAttempt
}

// Recovery is immutable evidence plus its one-way attestation lifecycle.
type Recovery struct {
	ID                                               int64
	Fingerprint, Reason, Action, By, Evidence, State string
	Snapshot                                         RecoverySnapshot
	NotBefore                                        time.Time
	AttestedAt                                       *time.Time
}

// RecoverySnapshotFor locks work, then its runs, then attempts. Parent row locks
// also serialize foreign-key inserts, so a new predecessor cannot escape the set.
func (s *Store) RecoverySnapshotFor(ctx context.Context, tx *sql.Tx, targetID int64) (RecoverySnapshot, error) {
	var out RecoverySnapshot
	var workID, runID int64
	if err := tx.QueryRowContext(ctx, `SELECT r.work_item_id, a.run_id FROM delivery_attempt a
		JOIN run r ON r.id = a.run_id WHERE a.id = $1`, targetID).Scan(&workID, &runID); err != nil {
		return out, err
	}
	w, err := scanWork(tx.QueryRowContext(ctx, `SELECT `+workCols+` FROM work_item WHERE id = $1 FOR UPDATE`, workID))
	if err != nil {
		return out, err
	}
	rows, err := tx.QueryContext(ctx, `SELECT id, attempt FROM run WHERE work_item_id = $1 ORDER BY id FOR UPDATE`, workID)
	if err != nil {
		return out, err
	}
	runs := map[int64]int{}
	for rows.Next() {
		var id int64
		var attempt int
		if err := rows.Scan(&id, &attempt); err != nil {
			rows.Close()
			return out, err
		}
		runs[id] = attempt
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return out, err
	}
	if _, ok := runs[runID]; !ok {
		return out, ErrRecoveryConflict
	}
	rows, err = tx.QueryContext(ctx, `SELECT `+attemptCols+` FROM delivery_attempt
		WHERE run_id IN (SELECT id FROM run WHERE work_item_id = $1) ORDER BY id FOR UPDATE`, workID)
	if err != nil {
		return out, err
	}
	defer rows.Close()
	out = RecoverySnapshot{
		WorkID: w.ID, WorkEpoch: w.Epoch, SessionID: w.SessionID,
		SessionGeneration: w.SessionGeneration, RunID: runID, RunAttempt: runs[runID],
		AttemptID: targetID, CallerIdentity: w.CallerIdentity, Attempts: []RecoveryAttempt{},
	}
	found := false
	for rows.Next() {
		a, err := scanAttempt(rows)
		if err != nil {
			return out, err
		}
		if a.ID == targetID {
			if a.RunID != runID {
				return out, ErrRecoveryConflict
			}
			found = true
			out.MessageID, out.ContextID, out.TaskID, out.TargetState = a.MessageID, a.ContextID, a.TaskID, a.State
		} else if a.Issues == 0 || a.State == AttemptCompleted || (a.State == AttemptCanceled && a.TaskID != "") {
			continue
		}
		out.Attempts = append(out.Attempts, RecoveryAttempt{
			ID: a.ID, RunID: a.RunID,
			RunAttempt: a.RunAttempt, CurrentRunAttempt: runs[a.RunID], WriterEpoch: a.WriterEpoch,
			KagentNamespace: a.KagentNamespace, KagentAgent: a.KagentAgent,
			WorkItemEpoch: a.WorkItemEpoch, MessageID: a.MessageID,
			ContextID: a.ContextID, TaskID: a.TaskID, State: a.State, Issues: a.Issues, SentAt: a.SentAt.UTC(),
		})
	}
	if err := rows.Err(); err != nil {
		return out, err
	}
	if !found {
		return out, ErrRecoveryConflict
	}
	return out, nil
}

func recoveryFingerprint(snapshot RecoverySnapshot, notBefore time.Time, reason string) (string, []byte) {
	data, _ := json.Marshal(snapshot)
	bound, _ := json.Marshal(struct {
		Snapshot  RecoverySnapshot
		NotBefore time.Time
		Reason    string
	}{snapshot, notBefore.UTC(), reason})
	hash := sha256.Sum256(bound)
	return hex.EncodeToString(hash[:]), data
}

const recoveryCols = `id, fingerprint, reason, coalesce(action, ''), coalesce(attested_by, ''),
	coalesce(evidence, ''), state, snapshot, not_before, attested_at`

func scanRecovery(row interface{ Scan(...any) error }) (*Recovery, error) {
	r := &Recovery{}
	var data []byte
	if err := row.Scan(&r.ID, &r.Fingerprint, &r.Reason, &r.Action, &r.By, &r.Evidence,
		&r.State, &data, &r.NotBefore, &r.AttestedAt); err != nil {
		return nil, err
	}
	if err := json.Unmarshal(data, &r.Snapshot); err != nil {
		return nil, err
	}
	r.NotBefore = r.NotBefore.UTC()
	return r, nil
}

// EnsureRecovery replaces obsolete unconsumed holds with fresh immutable evidence.
// Consumed evidence is never reactivated; an invalidated occurrence gets a new ID.
func (s *Store) EnsureRecovery(ctx context.Context, tx *sql.Tx, targetID int64, reason string, window time.Duration) (*Recovery, error) {
	if window < 0 || strings.TrimSpace(reason) == "" {
		return nil, ErrRecoveryConflict
	}
	snapshot, err := s.RecoverySnapshotFor(ctx, tx, targetID)
	if err != nil {
		return nil, err
	}
	notBefore := time.Unix(0, 0).UTC()
	for _, a := range snapshot.Attempts {
		if a.Issues > 0 && a.SentAt.Add(window).After(notBefore) {
			notBefore = a.SentAt.Add(window)
		}
	}
	fingerprint, data := recoveryFingerprint(snapshot, notBefore, reason)
	if _, err := tx.ExecContext(ctx, `UPDATE recovery_hold SET state = 'invalidated'
		WHERE target_attempt_id = $1 AND state IN ('pending', 'attested') AND fingerprint <> $2`, targetID, fingerprint); err != nil {
		return nil, err
	}
	r, err := scanRecovery(tx.QueryRowContext(ctx, `SELECT `+recoveryCols+` FROM recovery_hold
		WHERE fingerprint = $1 AND state IN ('pending', 'attested', 'consumed') ORDER BY id DESC LIMIT 1 FOR UPDATE`, fingerprint))
	if !errors.Is(err, sql.ErrNoRows) {
		return r, err
	}
	return scanRecovery(tx.QueryRowContext(ctx, `INSERT INTO recovery_hold
		(target_attempt_id, work_item_id, fingerprint, snapshot, reason, not_before)
		VALUES ($1, $2, $3, $4, $5, $6) RETURNING `+recoveryCols,
		targetID, snapshot.WorkID, fingerprint, string(data), reason, notBefore))
}

// FindRecovery returns the current unconsumed hold, or nil.
func (s *Store) FindRecovery(ctx context.Context, tx *sql.Tx, targetID int64) (*Recovery, error) {
	if _, err := s.RecoverySnapshotFor(ctx, tx, targetID); err != nil {
		return nil, err
	}
	r, err := scanRecovery(tx.QueryRowContext(ctx, `SELECT `+recoveryCols+` FROM recovery_hold
		WHERE target_attempt_id = $1 AND state IN ('pending', 'attested') ORDER BY id DESC LIMIT 1 FOR UPDATE`, targetID))
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	return r, err
}

func (s *Store) InvalidateRecovery(ctx context.Context, tx *sql.Tx, targetID int64) error {
	if _, err := s.RecoverySnapshotFor(ctx, tx, targetID); err != nil {
		return err
	}
	_, err := tx.ExecContext(ctx, `UPDATE recovery_hold SET state = 'invalidated'
		WHERE target_attempt_id = $1 AND state IN ('pending', 'attested')`, targetID)
	return err
}

// InspectRecoveries returns the issue's complete audit trail, newest first.
func (s *Store) InspectRecoveries(ctx context.Context, issueKey string) ([]*Recovery, error) {
	rows, err := s.DB.QueryContext(ctx, `SELECT `+recoveryCols+` FROM recovery_hold
		WHERE work_item_id IN (SELECT id FROM work_item WHERE issue_key = $1) ORDER BY id DESC`, issueKey)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []*Recovery{}
	for rows.Next() {
		r, err := scanRecovery(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

// lockRecovery uses the same work-before-hold lock order as EnsureRecovery.
func (s *Store) lockRecovery(ctx context.Context, tx *sql.Tx, id int64, fingerprint string) (*Recovery, error) {
	var targetID int64
	if err := tx.QueryRowContext(ctx, `SELECT target_attempt_id FROM recovery_hold WHERE id = $1`, id).Scan(&targetID); err != nil {
		return nil, err
	}
	snapshot, err := s.RecoverySnapshotFor(ctx, tx, targetID)
	if err != nil {
		return nil, err
	}
	r, err := scanRecovery(tx.QueryRowContext(ctx, `SELECT `+recoveryCols+` FROM recovery_hold WHERE id = $1 FOR UPDATE`, id))
	if err != nil {
		return nil, err
	}
	current, _ := recoveryFingerprint(snapshot, r.NotBefore, r.Reason)
	if r.Fingerprint != fingerprint || current != fingerprint {
		return nil, ErrRecoveryConflict
	}
	return r, nil
}

// AttestRecovery records an operator's external fencing evidence once.
func (s *Store) AttestRecovery(ctx context.Context, id int64, fingerprint, action, by, evidence string) error {
	if (action != "retry" && action != "cancel") || strings.TrimSpace(by) == "" || strings.TrimSpace(evidence) == "" {
		return ErrRecoveryConflict
	}
	return s.Tx(ctx, func(tx *sql.Tx) error {
		r, err := s.lockRecovery(ctx, tx, id, fingerprint)
		if err != nil {
			return err
		}
		if r.State != "pending" || s.now().Before(r.NotBefore) {
			return ErrRecoveryConflict
		}
		_, err = tx.ExecContext(ctx, `UPDATE recovery_hold SET state = 'attested', action = $2,
			attested_by = $3, evidence = $4, attested_at = now() WHERE id = $1`, id, action, by, evidence)
		return err
	})
}

// CompleteRecovery consumes an attestation in the writer's action transaction.
func (s *Store) CompleteRecovery(ctx context.Context, tx *sql.Tx, id int64, fingerprint string) error {
	r, err := s.lockRecovery(ctx, tx, id, fingerprint)
	if err != nil {
		return err
	}
	if r.State != "attested" || s.now().Before(r.NotBefore) {
		return ErrRecoveryConflict
	}
	res, err := tx.ExecContext(ctx, `UPDATE recovery_hold SET state = 'consumed', consumed_at = now()
		WHERE id = $1 AND fingerprint = $2 AND state = 'attested'`, id, fingerprint)
	if err != nil {
		return err
	}
	n, err := res.RowsAffected()
	if err == nil && n != 1 {
		return ErrRecoveryConflict
	}
	return err
}
