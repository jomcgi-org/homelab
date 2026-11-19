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
	"sync"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/internal/reaper"
	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	auth "k8s.io/api/authentication/v1"
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
		execute func(context.Context, revertTarget) error
		audit   func(string, revertTarget, string) error
		mu      sync.Mutex
		serial  sync.Mutex
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
		// Finish and audit a confirmed operation even if the browser disconnects.
		run, stop := context.WithTimeout(context.WithoutCancel(ctx), 4*time.Minute)
		defer stop()
		a.serial.Lock()
		defer a.serial.Unlock()
		result := "target changed"
		targets, err := a.targets(run)
		if err != nil {
			result = "discovery failed"
		}
		for _, t := range targets {
			if err == nil && t == p.Target {
				if err = a.execute(run, t); err != nil {
					result = err.Error()
				} else {
					result = "reverted"
				}
				break
			}
		}
		if a.audit(user, p.Target, result) != nil {
			http.Error(w, "Discord audit failed: "+result, 502)
			return
		}
		if result != "reverted" {
			http.Error(w, result, 409)
			return
		}
		fmt.Fprintln(w, result)
	default:
		w.WriteHeader(405)
	}
}

type revertBackend struct {
	actors   *sql.DB
	pods     core.CoreV1Interface
	sessions interface {
		GetSession(context.Context, string) (*kagent.Session, error)
		RevertSession(context.Context, string) (*kagent.Session, error)
	}
}

var workerName = regexp.MustCompile(`^homelab-(codex|reviewer)-[0-9a-f]{8,10}-[a-z0-9]{5}$`)

func (b revertBackend) targets(ctx context.Context) ([]revertTarget, error) {
	// Require a recent sample and a distinct observation at least 60s older.
	const metric = `cnpg_quiescence_pending{job="cnpg",namespace="kagent-pg",cluster="kagent-pg"}`
	expr := "(time() - timestamp(" + metric + ") < 35) and (" + metric + " == 1) and ((timestamp(" + metric + ") - timestamp(" + metric + " offset 75s)) >= 60) and (" + metric + " offset 75s == 1)"
	req, err := http.NewRequestWithContext(ctx, "GET", "http://prometheus.prometheus.svc:9090/api/v1/query?query="+url.QueryEscape(expr), nil)
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

func (b revertBackend) execute(ctx context.Context, t revertTarget) error {
	ttl := int64(600)
	tok, err := b.pods.ServiceAccounts("factory").CreateToken(ctx, t.Creator, &auth.TokenRequest{Spec: auth.TokenRequestSpec{Audiences: []string{"kagent-invocation"}, ExpirationSeconds: &ttl}}, meta.CreateOptions{})
	if err != nil {
		return errors.New("creator credential unavailable")
	}
	ctx = metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+tok.Status.Token)
	principal, err := kagent.TokenPrincipal(tok.Status.Token, os.Getenv("KAGENT_AUTHORITY"))
	if err != nil {
		return errors.New("invalid creator credential")
	}
	session, err := b.sessions.GetSession(ctx, t.Session)
	if err != nil || session.ID != t.Session || session.Creator != principal {
		return errors.New("creator cannot read session")
	}
	uid := types.UID(t.UID)
	pods := b.pods.Pods(t.Namespace)
	if err = pods.Delete(ctx, t.Pod, meta.DeleteOptions{Preconditions: &meta.Preconditions{UID: &uid}}); err != nil {
		return errors.New("worker deletion refused")
	}
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return errors.New("worker deletion requested; recovery timed out")
		case <-ticker.C:
		}
		p, err := pods.Get(ctx, t.Pod, meta.GetOptions{})
		if err != nil && !apierrors.IsNotFound(err) {
			return errors.New("worker deletion requested; inspection failed")
		}
		if err == nil && p.UID == uid {
			continue
		}
		s, err := b.sessions.GetSession(ctx, t.Session)
		if err != nil || !s.Crashed() {
			continue
		}
		restored, err := b.sessions.RevertSession(ctx, t.Session)
		if err != nil {
			return errors.New("worker gone; revert failed")
		}
		if restored.ID != t.Session || restored.State != kagent.RuntimeReady {
			return errors.New("worker gone; session readiness unconfirmed")
		}
		return nil
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
	db, err := sql.Open("postgres", actorDB)
	if err != nil {
		return err
	}
	defer db.Close()
	conn, err := grpc.NewClient("kagent-controller.kagent.svc:8083", grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		return err
	}
	defer conn.Close()
	b := revertBackend{db, pods, kagent.NewSessions(conn)}
	audit := func(user string, t revertTarget, result string) error {
		log.Info("runbook action", "action", "kagent-revert", "user", user, "target", t, "result", result)
		msg := fmt.Sprintf("kagent-revert by %s: session %s executor %s worker %s/%s UID %s as factory/%s: %s", user, t.Session, t.Executor, t.Namespace, t.Pod, t.UID, t.Creator, result)
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
	srv := http.Server{Addr: ":8080", ReadTimeout: 10 * time.Second, Handler: &revertAction{targets: b.targets, execute: b.execute, audit: audit, tickets: map[string]preview{}}}
	return srv.ListenAndServe()
}
