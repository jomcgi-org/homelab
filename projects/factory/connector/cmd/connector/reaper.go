package main

import (
	"context"
	"database/sql"
	"errors"
	"log/slog"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"google.golang.org/protobuf/encoding/protowire"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	core "k8s.io/client-go/kubernetes/typed/core/v1"
	"k8s.io/client-go/rest"
)

const actorDB = "postgresql://factory_reaper@postgres.ate-system.svc:5432/atepg?sslmode=verify-full&sslrootcert=/var/run/ate/ca.pem&sslcert=/var/run/ate/client.pem&sslkey=/var/run/ate/client.pem"

type placement struct{ namespace, pod, uid string }

// Decode the small subset of the pinned ateapi.v1.Actor wire schema we need.
// Absent, unknown or malformed fields cannot authorize an action.
func field(msg []byte, number protowire.Number, typ protowire.Type) []byte {
	for len(msg) > 0 {
		n, t, k := protowire.ConsumeTag(msg)
		if k < 0 {
			return nil
		}
		msg = msg[k:]
		size := protowire.ConsumeFieldValue(n, t, msg)
		if size < 0 {
			return nil
		}
		if n == number && t == typ {
			if t == protowire.BytesType {
				b, _ := protowire.ConsumeBytes(msg)
				return b
			}
			return msg[:size]
		}
		msg = msg[size:]
	}
	return nil
}
func bytesField(msg []byte, n protowire.Number) []byte { return field(msg, n, protowire.BytesType) }
func intField(msg []byte, n protowire.Number) uint64 {
	v, _ := protowire.ConsumeVarint(field(msg, n, protowire.VarintType))
	return v
}

func actorPlacement(proto []byte) (placement, uint64, time.Time) {
	status := bytesField(proto, 5)
	w := bytesField(status, 2)
	updated := bytesField(bytesField(proto, 1), 6)
	return placement{string(bytesField(w, 1)), string(bytesField(w, 3)), string(bytesField(w, 4))}, intField(status, 1), time.Unix(int64(intField(updated, 1)), int64(intField(updated, 2)))
}

func (p placement) allowed() bool {
	return p.uid != "" && ((p.namespace == "factory-r-homelab" && strings.HasPrefix(p.pod, "homelab-codex-")) || (p.namespace == "factory-r-homelab-review" && strings.HasPrefix(p.pod, "homelab-reviewer-")))
}

func suspendExpired(p placement, state uint64, updated, now time.Time) bool {
	return p.allowed() && state == 3 && updated.Unix() > 0 && now.Sub(updated) > 5*time.Minute
}

type reaper struct {
	actors, journal *sql.DB
	pods            core.PodsGetter
}

func (r reaper) scan(ctx context.Context) error {
	rows, err := r.actors.QueryContext(ctx, `SELECT name,proto FROM public.reaper_actors()`)
	if err != nil {
		return err
	}
	defer rows.Close()
	var errs []error
	for rows.Next() {
		var name string
		var proto []byte
		if err := rows.Scan(&name, &proto); err != nil {
			return err
		}
		p, state, updated := actorPlacement(proto)
		if !p.allowed() {
			continue
		}
		session := strings.TrimPrefix(name, "session-")
		_, err := r.journal.ExecContext(ctx, `INSERT INTO worker_fence(attempt_id,session_id,namespace,pod,uid)
    SELECT id,context_id,$2,$3,$4 FROM delivery_attempt WHERE context_id=$1 AND state IN ('accepted','ambiguous')
    ON CONFLICT(attempt_id) DO UPDATE SET namespace=excluded.namespace,pod=excluded.pod,uid=excluded.uid
    WHERE worker_fence.gone_at IS NULL AND worker_fence.uid<>excluded.uid`, session, p.namespace, p.pod, p.uid)
		if err != nil {
			errs = append(errs, err)
		}
		if suspendExpired(p, state, updated, time.Now()) {
			// Re-read the row immediately before acting. Both the state and the
			// assignment must still be the revision that exceeded the deadline.
			var current bool
			err = r.actors.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM public.reaper_actors() WHERE name=$1 AND proto=$2)`, name, proto).Scan(&current)
			if err == nil && current {
				uid := types.UID(p.uid)
				err = r.pods.Pods(p.namespace).Delete(ctx, p.pod, metav1.DeleteOptions{Preconditions: &metav1.Preconditions{UID: &uid}})
			}
			if err != nil && !apierrors.IsNotFound(err) {
				errs = append(errs, err)
			}
		}
	}
	if err := rows.Err(); err != nil {
		return err
	}
	evidence, err := r.journal.QueryContext(ctx, `SELECT f.attempt_id,f.namespace,f.pod,f.uid FROM worker_fence f JOIN delivery_attempt a ON a.id=f.attempt_id WHERE f.gone_at IS NULL AND a.state IN ('accepted','ambiguous')`)
	if err != nil {
		return err
	}
	defer evidence.Close()
	for evidence.Next() {
		var id int64
		var p placement
		if err := evidence.Scan(&id, &p.namespace, &p.pod, &p.uid); err != nil {
			return err
		}
		pod, err := r.pods.Pods(p.namespace).Get(ctx, p.pod, metav1.GetOptions{})
		if apierrors.IsNotFound(err) || (err == nil && string(pod.UID) != p.uid) {
			_, err = r.journal.ExecContext(ctx, `UPDATE worker_fence SET gone_at=now() WHERE attempt_id=$1 AND uid=$2 AND gone_at IS NULL`, id, p.uid)
		}
		if err != nil {
			errs = append(errs, err)
		}
	}
	return errors.Join(append(errs, evidence.Err())...)
}

func runReaper(log *slog.Logger) error {
	cfg, err := rest.InClusterConfig()
	if err != nil {
		return err
	}
	cfg.Timeout = 5 * time.Second
	client, err := core.NewForConfig(cfg)
	if err != nil {
		return err
	}
	st, err := operatorStore()
	if err != nil {
		return err
	}
	defer st.DB.Close()
	actors, err := sql.Open("postgres", actorDB)
	if err != nil {
		return err
	}
	defer actors.Close()
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	r := reaper{actors, st.DB, client}
	ticker := time.NewTicker(15 * time.Second)
	defer ticker.Stop()
	for {
		pass, cancel := context.WithTimeout(ctx, 45*time.Second)
		err := r.scan(pass)
		cancel()
		if err != nil {
			log.Error("worker reaper scan", "error", err)
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-ticker.C:
		}
	}
}
