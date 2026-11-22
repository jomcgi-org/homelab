// The reviewed kagent revert action runs as grafana/runbook-kagent-revert.
package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"html/template"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/internal/reaper"
	"github.com/jomcgi/homelab/projects/factory/connector/kagent"

	v1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	meta "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	core "k8s.io/client-go/kubernetes/typed/core/v1"
	"k8s.io/client-go/rest"
)

const actionPath = "/actions/kagent-revert"

type (
	revertTarget struct{ Session, Executor, Namespace, Pod, UID, Creator string }
	preview      struct {
		User   string
		Target revertTarget
		Until  time.Time
	}
	revertAction struct {
		targets func(context.Context) ([]revertTarget, error)
		submit  func(context.Context, string, revertTarget) error
		mu      sync.Mutex
		tickets map[string]preview
	}
)

var confirmPage = template.Must(template.New("confirm").Parse(`<!doctype html><title>Review kagent revert</title><h1>Review kagent revert</h1>{{range $token,$p := .}}<form action="/actions/kagent-revert" method="post"><p>Session {{$p.Target.Session}}, executor {{$p.Target.Executor}}. Delete {{$p.Target.Namespace}}/{{$p.Target.Pod}} (UID {{$p.Target.UID}}), then revert as factory/{{$p.Target.Creator}}.</p><button name="ticket" value="{{$token}}">Confirm revert</button></form>{{else}}No eligible locked sessions.{{end}}`))

func (a *revertAction) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("Content-Security-Policy", "frame-ancestors 'none'; form-action 'self'")
	user := r.Header.Get("X-Runbook-User")
	if user == "" {
		http.Error(w, "Operator identity required", 403)
		return
	}
	if r.URL.Path != actionPath || r.URL.RawQuery != "" {
		http.NotFound(w, r)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 90*time.Second)
	defer cancel()
	switch r.Method {
	case "GET":
		targets, err := a.targets(ctx)
		if err != nil {
			http.Error(w, "Discovery unavailable", 503)
			return
		}
		page := map[string]preview{}
		a.mu.Lock()
		for k, p := range a.tickets {
			if !time.Now().Before(p.Until) {
				delete(a.tickets, k)
			}
		}
		for _, t := range targets {
			k := rand.Text()
			p := preview{user, t, time.Now().Add(2 * time.Minute)}
			a.tickets[k] = p
			page[k] = p
		}
		a.mu.Unlock()
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		confirmPage.Execute(w, page)
	case "POST":
		r.Body = http.MaxBytesReader(w, r.Body, 1024)
		if r.Header.Get("Origin") != "https://factory-runbooks.jomcgi.dev" || r.ParseForm() != nil || len(r.PostForm) != 1 || len(r.PostForm["ticket"]) != 1 {
			http.Error(w, "Invalid confirmation", 400)
			return
		}
		k := r.PostForm.Get("ticket")
		a.mu.Lock()
		p, ok := a.tickets[k]
		if ok && p.User == user {
			delete(a.tickets, k)
		}
		a.mu.Unlock()
		if !ok || p.User != user || !time.Now().Before(p.Until) {
			http.Error(w, "Preview again", 409)
			return
		}
		if err := a.submit(ctx, user, p.Target); err != nil {
			http.Error(w, "Recovery queue unavailable or busy; preview again", 409)
			return
		}
		w.WriteHeader(http.StatusAccepted)
		fmt.Fprintln(w, "Recovery recorded. The result will be logged and sent to Discord.")
	default:
		w.WriteHeader(405)
	}
}

type revertBackend struct {
	discover func(context.Context) ([]revertTarget, error)
	actors   *sql.DB
	pods     core.CoreV1Interface
	sessions interface {
		GetSession(context.Context, string) (*kagent.Session, error)
		RevertSession(context.Context, string) (*kagent.Session, error)
	}
}

var workerName = regexp.MustCompile(`^homelab-(codex|reviewer)-[0-9a-f]{8,10}-[a-z0-9]{5}$`)

func lockedClaimQuery() string {
	// Require a recent sample and a distinct observation at least 60s older.
	const metric = `cnpg_quiescence_pending{job="cnpg",namespace="kagent-pg",cluster="kagent-pg"}`
	return "(time() - timestamp(" + metric + ") < 35) and (" + metric + " == 1) and ((timestamp(" + metric + ") - timestamp(" + metric + " offset 75s)) >= 60) and (" + metric + " offset 75s == 1)"
}

func (b revertBackend) targets(ctx context.Context) ([]revertTarget, error) {
	req, err := http.NewRequestWithContext(ctx, "GET", "http://prometheus.prometheus.svc:9090/api/v1/query?query="+url.QueryEscape(lockedClaimQuery()), nil)
	if err != nil {
		return nil, err
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var data struct {
		Status string
		Data   struct {
			Result []struct{ Metric map[string]string }
		}
	}
	if resp.StatusCode != 200 || json.NewDecoder(resp.Body).Decode(&data) != nil || data.Status != "success" {
		return nil, errors.New("claim query failed")
	}
	var out []revertTarget
	for _, row := range data.Data.Result {
		t := revertTarget{Session: row.Metric["session"], Executor: row.Metric["executor"]}
		if t.Session == "" || t.Executor == "" {
			continue
		}
		var proto []byte
		err = b.actors.QueryRowContext(ctx, "SELECT proto FROM public.reaper_actors() WHERE name=$1", "session-"+t.Session).Scan(&proto)
		if errors.Is(err, sql.ErrNoRows) {
			continue
		}
		if err != nil {
			return nil, err
		}
		var ok bool
		t.Namespace, t.Pod, t.UID, ok = reaper.SuspendingWorker(proto)
		if !ok || !workerName.MatchString(t.Pod) {
			continue
		}
		t.Creator = "factory-connector"
		if t.Namespace == "factory-r-homelab-review" {
			t.Creator = "factory-reviewer"
		}
		out = append(out, t)
	}
	return out, nil
}

// Each creator has a pre-provisioned journal slot. resourceVersion is the
// writer fence: every transition must replace exactly the version observed.
type operation struct {
	ID, User, Phase, Result string
	Target                  revertTarget
	Deadline, LeaseUntil    time.Time
	Epoch                   uint64
}

func journalName(owner string) string { return "runbook-revert-" + owner }
func (b revertBackend) save(ctx context.Context, cm *v1.ConfigMap, op operation) error {
	t := op.Target
	cm.Data = map[string]string{
		"session": t.Session, "executor": t.Executor, "namespace": t.Namespace, "pod": t.Pod, "uid": t.UID, "creator": t.Creator,
		"id": op.ID, "user": op.User, "phase": op.Phase, "result": op.Result,
		"deadline": op.Deadline.Format(time.RFC3339Nano), "lease": op.LeaseUntil.Format(time.RFC3339Nano), "epoch": strconv.FormatUint(op.Epoch, 10),
	}
	_, err := b.pods.ConfigMaps("grafana").Update(ctx, cm, meta.UpdateOptions{})
	return err
}

func (b revertBackend) load(ctx context.Context, owner string) (*v1.ConfigMap, operation, error) {
	cm, err := b.pods.ConfigMaps("grafana").Get(ctx, journalName(owner), meta.GetOptions{})
	var op operation
	if err == nil && len(cm.Data) > 0 {
		d := cm.Data
		op.ID, op.User, op.Phase, op.Result = d["id"], d["user"], d["phase"], d["result"]
		op.Target = revertTarget{Session: d["session"], Executor: d["executor"], Namespace: d["namespace"], Pod: d["pod"], UID: d["uid"], Creator: d["creator"]}
		if op.Deadline, err = time.Parse(time.RFC3339Nano, d["deadline"]); err != nil {
			return nil, op, err
		}
		if op.LeaseUntil, err = time.Parse(time.RFC3339Nano, d["lease"]); err != nil {
			return nil, op, err
		}
		op.Epoch, err = strconv.ParseUint(d["epoch"], 10, 64)
	}
	return cm, op, err
}

func (b revertBackend) submit(ctx context.Context, user string, t revertTarget) error {
	cm, prev, err := b.load(ctx, t.Creator)
	if err != nil {
		return err
	}
	if prev.Phase != "" && prev.Phase != "delivered" {
		return errors.New("busy")
	}
	return b.save(ctx, cm, operation{ID: rand.Text(), User: user, Target: t, Phase: "confirmed", Deadline: time.Now().Add(6 * time.Minute)})
}

// auditSink sends one outcome. The first argument is the journal operation
// ID, so a resend after a lost response carries the same identity.
type auditSink func(id, user string, t revertTarget, result string) error

func (b revertBackend) advance(ctx context.Context, owner string, credential *kagent.TokenSource, audit auditSink) error {
	cm, op, err := b.load(ctx, owner)
	if err != nil {
		return err
	}
	if op.Phase == "" || op.Phase == "delivered" || op.Target.Creator != owner {
		return nil
	}
	t := op.Target
	finish := func(result string) error { op.Result = result; op.Phase = "audit"; return b.save(ctx, cm, op) }
	// A fresh CAS before each external call records a new writer epoch.
	// Bound the RPC deadline inside the lease so another process cannot race it.
	claim := func(phase string) error {
		op.Phase = phase
		op.Epoch++
		op.LeaseUntil = time.Now().Add(160 * time.Second)
		if err := b.save(ctx, cm, op); err != nil {
			return err
		}
		var current operation
		cm, current, err = b.load(ctx, owner)
		if err != nil {
			return err
		}
		if current.ID != op.ID || current.Epoch != op.Epoch || current.Phase != phase {
			return errors.New("lost recovery lease")
		}
		op.LeaseUntil = time.Time{}
		return nil
	}
	if time.Now().Before(op.LeaseUntil) {
		return nil
	}
	if op.Phase == "audit" || op.Phase == "auditing" {
		// Only the front sends audits. It claims the journal before each send,
		// so one writer holds each attempt and a retry waits for the lease.
		if audit == nil {
			return nil
		}
		if err := claim("auditing"); err != nil {
			return err
		}
		if err := audit(op.ID, op.User, t, op.Result); err != nil {
			return err
		}
		op.Phase = "delivered"
		return b.save(ctx, cm, op)
	}
	if time.Now().After(op.Deadline) && (op.Phase == "confirmed" || op.Phase == "authorized") {
		if audit != nil {
			return finish("recovery deadline exceeded during " + op.Phase)
		}
		return nil
	}
	if credential != nil {
		if op.Phase != "confirmed" && op.Phase != "deleting" && op.Phase != "gone" && op.Phase != "reverting" {
			return nil
		}
		token, err := credential.Token()
		if err != nil {
			return err
		}
		principal, err := kagent.TokenPrincipal(token, os.Getenv("KAGENT_AUTHORITY"))
		if err != nil {
			return err
		}
		session, err := b.sessions.GetSession(ctx, t.Session)
		if errors.Is(err, kagent.ErrSessionNotFound) || errors.Is(err, kagent.ErrSessionUnrecoverable) {
			return finish("session missing or unrecoverable")
		}
		if err != nil {
			return err
		}
		if session.Creator != principal || session.ID != t.Session {
			return finish("creator mismatch")
		}
		if !workerName.MatchString(t.Pod) || t.UID == "" || !(owner == "factory-connector" && t.Namespace == "factory-r-homelab" && strings.HasPrefix(t.Pod, "homelab-codex-") || owner == "factory-reviewer" && t.Namespace == "factory-r-homelab-review" && strings.HasPrefix(t.Pod, "homelab-reviewer-")) {
			return finish("invalid worker assignment")
		}
		if op.Phase == "confirmed" {
			if !time.Now().Before(op.Deadline) || op.Deadline.After(time.Now().Add(6*time.Minute)) {
				return finish("invalid confirmation deadline")
			}
			targets, err := b.discover(ctx)
			if err != nil {
				return err
			}
			if !slices.Contains(targets, t) {
				return finish("target changed")
			}
			op.Phase = "authorized"
			return b.save(ctx, cm, op)
		}
		// The creator observes its own worker pod. The front action reads no
		// pods, so its ServiceAccount holds only fenced deletes.
		pod, err := b.pods.Pods(t.Namespace).Get(ctx, t.Pod, meta.GetOptions{})
		if err != nil && !apierrors.IsNotFound(err) {
			return err
		}
		present := err == nil && string(pod.UID) == t.UID
		if op.Phase == "deleting" {
			if !present {
				op.Phase = "gone"
				return b.save(ctx, cm, op)
			}
			if pod.DeletionTimestamp == nil && time.Now().After(op.Deadline) {
				return finish("worker still present; confirmation expired")
			}
			return nil
		}
		if present {
			return finish("confirmed worker still present")
		}
		if op.Phase == "reverting" && session.State == kagent.RuntimeReady {
			return finish("reverted")
		}
		if !session.Crashed() {
			if time.Now().After(op.Deadline) {
				return finish("worker gone; session no longer awaiting revert")
			}
			return nil
		}
		if err := claim("reverting"); err != nil {
			return err
		}
		restored, err := b.sessions.RevertSession(ctx, t.Session)
		if errors.Is(err, kagent.ErrSessionNotFound) || errors.Is(err, kagent.ErrSessionUnrecoverable) {
			return finish("session missing or unrecoverable")
		}
		if err != nil {
			return errors.New("revert outcome pending")
		}
		if restored.ID != t.Session || restored.State != kagent.RuntimeReady {
			return finish("session readiness unconfirmed")
		}
		return finish("reverted")
	}
	if op.Phase != "authorized" && op.Phase != "deleting" {
		return nil
	}
	// The front deletes without reading the pod. The UID precondition and the
	// admission fence keep the delete on the confirmed worker. The creator
	// observes the result and moves the journal to gone, or audits a worker
	// still present at the deadline. In the deleting phase a cleared lease
	// means the delete landed; an expired lease means an attempt may not have
	// landed, and deleting a terminating pod again is harmless.
	if time.Now().After(op.Deadline) || op.Phase == "deleting" && op.LeaseUntil.IsZero() {
		return nil
	}
	// Revalidate the locked claim before every delete, including a retry
	// whose first attempt has an unknown result. Admission checks the journal
	// and UID, not the claim, so a retry after the claim cleared must not
	// delete the still-live worker. The creator then audits it at the deadline.
	targets, err := b.discover(ctx)
	if err != nil {
		return err
	}
	if !slices.Contains(targets, t) {
		if op.Phase == "authorized" {
			return finish("target changed")
		}
		return nil
	}
	first := op.Phase == "authorized"
	if err := claim("deleting"); err != nil {
		return err
	}
	uid := types.UID(t.UID)
	err = b.pods.Pods(t.Namespace).Delete(ctx, t.Pod, meta.DeleteOptions{Preconditions: &meta.Preconditions{UID: &uid}})
	if first && (apierrors.IsNotFound(err) || apierrors.IsConflict(err)) {
		return finish("target changed")
	}
	if err != nil && !apierrors.IsNotFound(err) && !apierrors.IsConflict(err) {
		return err
	}
	return b.save(ctx, cm, op)
}

func (b revertBackend) poll(owner string, source *kagent.TokenSource, audit auditSink, log *slog.Logger) {
	for {
		ctx, cancel := context.WithTimeout(context.Background(), 150*time.Second)
		err := b.advance(ctx, owner, source, audit)
		cancel()
		if err != nil {
			log.Warn("runbook recovery retry", "creator", owner, "error", err)
		}
		time.Sleep(3 * time.Second)
	}
}

func runRevert(log *slog.Logger) error {
	cfg, err := rest.InClusterConfig()
	if err != nil {
		return err
	}
	pods, err := core.NewForConfig(cfg)
	if err != nil {
		return err
	}
	db := openActorDB()
	defer db.Close()
	if owner := os.Getenv("RUNBOOK_OWNER"); owner != "" {
		source := &kagent.TokenSource{Path: "/var/run/secrets/kagent/token"}
		sessions, err := kagent.DialSessions("kagent-controller.kagent.svc:8083", source)
		if err != nil {
			return err
		}
		defer sessions.Close()
		backend := revertBackend{actors: db, pods: pods, sessions: sessions}
		backend.discover = backend.targets
		backend.poll(owner, source, nil, log)
		return nil
	}
	b := revertBackend{actors: db, pods: pods}
	b.discover = b.targets
	audit := func(id, user string, t revertTarget, result string) error {
		log.Info("runbook action", "action", "kagent-revert", "operation", id, "user", user, "target", t, "result", result)
		msg := fmt.Sprintf("kagent-revert %s by %s: session %s executor %s worker %s/%s UID %s as factory/%s: %s", id, user, t.Session, t.Executor, t.Namespace, t.Pod, t.UID, t.Creator, result)
		payload, _ := json.Marshal(map[string]any{"content": msg, "allowed_mentions": map[string]any{"parse": []string{}}})
		client := http.Client{Timeout: 10 * time.Second}
		resp, err := client.Post(os.Getenv("DISCORD_WEBHOOK"), "application/json", bytes.NewReader(payload))
		if err != nil {
			return errors.New("Discord unavailable")
		}
		defer resp.Body.Close()
		if resp.StatusCode/100 != 2 {
			return errors.New("Discord rejected audit")
		}
		return nil
	}
	for _, owner := range []string{"factory-connector", "factory-reviewer"} {
		go b.poll(owner, nil, audit, log)
	}
	srv := http.Server{Addr: ":8080", ReadTimeout: 10 * time.Second, Handler: &revertAction{targets: b.targets, submit: b.submit, tickets: map[string]preview{}}}
	return srv.ListenAndServe()
}
