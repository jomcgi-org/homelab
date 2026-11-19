package main

import (
	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"

	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
	v1 "k8s.io/api/core/v1"
	meta "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/kubernetes/fake"
	k8stesting "k8s.io/client-go/testing"
)

func TestRevertConfirmation(t *testing.T) {
	target := revertTarget{"locked", "executor", "factory-r-homelab", "homelab-codex-deadbeef-abcde", "uid", "factory-connector"}
	submitted := 0
	a := revertAction{tickets: map[string]preview{}, targets: func(context.Context) ([]revertTarget, error) { return []revertTarget{target}, nil }, submit: func(_ context.Context, user string, tgt revertTarget) error {
		if user != "joe" || tgt != target {
			t.Fatal(user, tgt)
		}
		submitted++
		return nil
	}}
	request := func(method, user, form string) int {
		r := httptest.NewRequest(method, actionPath, strings.NewReader(form))
		r.Header.Set("X-Runbook-User", user)
		r.Header.Set("Origin", "https://factory-runbooks.jomcgi.dev")
		r.Header.Set("Content-Type", "application/x-www-form-urlencoded")
		w := httptest.NewRecorder()
		a.ServeHTTP(w, r)
		if method == "GET" && !strings.Contains(w.Body.String(), target.UID) {
			t.Fatal(w.Body.String())
		}
		return w.Code
	}
	if request("GET", "joe", "") != 200 || len(a.tickets) != 1 || submitted != 0 {
		t.Fatal("preview")
	}
	for ticket := range a.tickets {
		form := "ticket=" + ticket
		if request("POST", "other", form) != 409 || request("POST", "joe", form+"&pod=other") != 400 || submitted != 0 {
			t.Fatal("unreviewed execution")
		}
		if request("POST", "joe", form) != 202 || submitted != 1 || request("POST", "joe", form) != 409 {
			t.Fatal("confirmation or replay")
		}
	}
}

type lockedSession struct {
	state      kagent.Session
	reverts    int
	lookupErr  error
	workerGone func(context.Context) bool
}

func (s *lockedSession) GetSession(ctx context.Context, id string) (*kagent.Session, error) {
	if id != s.state.ID {
		return nil, kagent.ErrSessionNotFound
	}
	if s.reverts == 0 && s.workerGone(ctx) {
		s.state.FailureReason = kagent.ReasonRuntimeCrashed
	}
	return &s.state, s.lookupErr
}

func (s *lockedSession) RevertSession(ctx context.Context, id string) (*kagent.Session, error) {
	if id != s.state.ID || !s.workerGone(ctx) || !s.state.Crashed() {
		return nil, errors.New("not crashed")
	}
	s.state.State = kagent.RuntimeReady
	s.state.FailureReason = ""
	s.reverts++
	return nil, errors.New("lost response after successful revert")
}

func TestDurableRecoveryAndAuditRetry(t *testing.T) {
	for _, target := range []revertTarget{
		{"locked", "executor", "factory-r-homelab", "homelab-codex-deadbeef-abcde", "uid", "factory-connector"},
		{"locked", "executor", "factory-r-homelab-review", "homelab-reviewer-deadbeef-abcde", "uid", "factory-reviewer"},
	} {
		t.Run(target.Creator, func(t *testing.T) { testConfirmedRecovery(t, target) })
	}
}

func testConfirmedRecovery(t *testing.T, target revertTarget) {
	ctx := t.Context()
	t.Setenv("KAGENT_AUTHORITY", "factory-home")
	claims := fmt.Sprintf(`{"iss":"cluster","sub":"system:serviceaccount:factory:%[1]s","aud":["kagent-invocation"],"kubernetes.io":{"namespace":"factory","serviceaccount":{"name":"%[1]s","uid":"creator-uid"}}}`, target.Creator)
	source := &kagent.TokenSource{Path: filepath.Join(t.TempDir(), "token")}
	if err := os.WriteFile(source.Path, []byte("mock."+base64.RawURLEncoding.EncodeToString([]byte(claims))+".mock"), 0o600); err != nil {
		t.Fatal(err)
	}
	sessions := &lockedSession{state: kagent.Session{ID: target.Session, Creator: (kagent.CallerIdentity{Authority: "factory-home", ServiceAccountUID: "creator-uid"}).PrincipalID()}}
	client := fake.NewSimpleClientset(&v1.ConfigMap{ObjectMeta: meta.ObjectMeta{Name: journalName(target.Creator), Namespace: "grafana"}}, &v1.Pod{ObjectMeta: meta.ObjectMeta{Name: target.Pod, Namespace: target.Namespace, UID: "uid"}})
	sessions.workerGone = func(ctx context.Context) bool {
		_, err := client.CoreV1().Pods(target.Namespace).Get(ctx, target.Pod, meta.GetOptions{})
		return apierrors.IsNotFound(err)
	}
	backend := func() revertBackend {
		return revertBackend{pods: client.CoreV1(), sessions: sessions, discover: func(context.Context) ([]revertTarget, error) { return []revertTarget{target}, nil }}
	}
	// Drive the same preview and confirmation POST used by the runbook button.
	action := revertAction{targets: backend().discover, submit: backend().submit, tickets: map[string]preview{}}
	previewReq := httptest.NewRequest("GET", actionPath, nil)
	previewReq.Header.Set("X-Runbook-User", "joe")
	page := httptest.NewRecorder()
	action.ServeHTTP(page, previewReq)
	ticket := regexp.MustCompile(`name="ticket" value="([^"]+)"`).FindStringSubmatch(page.Body.String())
	if page.Code != 200 || len(ticket) != 2 || !strings.Contains(page.Body.String(), target.Pod) {
		t.Fatal(page.Body.String())
	}
	confirm := httptest.NewRequest("POST", actionPath, strings.NewReader("ticket="+ticket[1]))
	confirm.Header.Set("Origin", "https://factory-runbooks.jomcgi.dev")
	confirm.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	confirm.Header.Set("X-Runbook-User", "joe")
	response := httptest.NewRecorder()
	action.ServeHTTP(response, confirm)
	if response.Code != 202 {
		t.Fatal(response.Body.String())
	}
	// The front action must not read pods; only the creator observes them.
	front := false
	client.PrependReactor("get", "pods", func(k8stesting.Action) (bool, runtime.Object, error) {
		if front {
			t.Fatal("front action read a pod")
		}
		return false, nil, nil
	})
	auditIDs := []string{}
	auditCount := 0
	audit := func(id, user string, got revertTarget, result string) error {
		if user != "joe" || got != target || result != "reverted" {
			t.Fatal(user, got, result)
		}
		auditIDs = append(auditIDs, id)
		auditCount++
		if auditCount == 1 {
			return errors.New("Discord offline")
		}
		return nil
	}
	step := func(token *kagent.TokenSource, phase string) {
		t.Helper()
		b := backend()
		front = token == nil
		defer func() { front = false }()
		if err := b.advance(ctx, target.Creator, token, audit); err != nil {
			t.Fatal(err)
		}
		_, op, err := b.load(ctx, target.Creator)
		if err != nil || op.Phase != phase {
			t.Fatal(op, err)
		}
	}
	step(source, "authorized")
	step(nil, "deleting")
	// The delete landed: the front waits for the creator to observe it.
	step(nil, "deleting")
	step(source, "gone")
	if err := backend().advance(ctx, target.Creator, source, nil); err == nil {
		t.Fatal("expected lost response")
	}
	cm, pending, err := backend().load(ctx, target.Creator)
	if err != nil || pending.Phase != "reverting" || pending.Epoch != 2 || pending.ID == "" {
		t.Fatal(pending, err)
	}
	// A replacement process reconciles the intent once the previous lease expires.
	pending.LeaseUntil = time.Now().Add(-time.Second)
	if err := backend().save(ctx, cm, pending); err != nil {
		t.Fatal(err)
	}
	step(source, "audit")
	front = true
	if err := backend().advance(ctx, target.Creator, nil, audit); err == nil {
		t.Fatal("expected delivery failure")
	}
	front = false
	// The failed send holds a fenced claim; no writer resends until it expires.
	cm, sent, err := backend().load(ctx, target.Creator)
	if err != nil || sent.Phase != "auditing" || sent.LeaseUntil.IsZero() {
		t.Fatal(sent, err)
	}
	step(nil, "auditing")
	if auditCount != 1 {
		t.Fatal("resent during the claim", auditCount)
	}
	sent.LeaseUntil = time.Now().Add(-time.Second)
	if err := backend().save(ctx, cm, sent); err != nil {
		t.Fatal(err)
	}
	step(nil, "delivered")
	if sessions.reverts != 1 || auditCount != 2 || auditIDs[0] != pending.ID || auditIDs[1] != pending.ID {
		t.Fatal(sessions.reverts, auditCount, auditIDs)
	}
	for _, phase := range []string{"gone", "reverting"} {
		if err := backend().submit(ctx, "joe", target); err != nil {
			t.Fatal(err)
		}
		cm, op, err := backend().load(ctx, target.Creator)
		if err != nil {
			t.Fatal(err)
		}
		op.Phase = phase
		op.Deadline = time.Now().Add(-time.Minute)
		if err := backend().save(ctx, cm, op); err != nil {
			t.Fatal(err)
		}
		sessions.lookupErr = kagent.ErrSessionNotFound
		if err := backend().advance(ctx, target.Creator, source, nil); err != nil {
			t.Fatal(err)
		}
		sink := func(_, user string, got revertTarget, result string) error {
			if result != "session missing or unrecoverable" || user != "joe" || got != target {
				t.Fatal(result, user, got)
			}
			return nil
		}
		if err := backend().advance(ctx, target.Creator, nil, sink); err != nil {
			t.Fatal(err)
		}
		_, op, err = backend().load(ctx, target.Creator)
		if err != nil || op.Phase != "delivered" {
			t.Fatal(op, err)
		}
	}
}

// Evaluate the production query against scrapes. Prometheus lookback can
// return one old sample at multiple evaluation times.
func TestLockedClaimDiscovery(t *testing.T) {
	tool := os.Getenv("PROMTOOL")
	if tool == "" {
		var err error
		tool, err = exec.LookPath("promtool")
		if err != nil {
			t.Skip("promtool is supplied by the Bazel target")
		}
	}
	if !filepath.IsAbs(tool) && os.Getenv("TEST_SRCDIR") != "" {
		tool = filepath.Join(os.Getenv("TEST_SRCDIR"), os.Getenv("TEST_WORKSPACE"), tool)
	}
	labels := `job="cnpg",namespace="kagent-pg",cluster="kagent-pg"`
	suite := fmt.Sprintf(`evaluation_interval: 30s
tests:
- interval: 30s
  input_series:
  - series: 'cnpg_quiescence_pending{%[1]s,session="locked",executor="one"}'
    values: '1 1 1 1 1'
  - series: 'cnpg_quiescence_pending{%[1]s,session="new",executor="one"}'
    values: '_ _ _ _ 1'
  - series: 'cnpg_quiescence_pending{%[1]s,session="changed",executor="old"}'
    values: '1 1 stale _ _'
  - series: 'cnpg_quiescence_pending{%[1]s,session="changed",executor="new"}'
    values: '_ _ _ 1 1'
  - series: 'cnpg_quiescence_pending{%[1]s,session="missing",executor="one"}'
    values: '_ 1 _ _ _'
  promql_expr_test:
  - expr: '%[2]s'
    eval_time: 2m
    exp_samples:
    - labels: '{%[1]s,session="locked",executor="one"}'
      value: 0
`, labels, lockedClaimQuery())
	file := filepath.Join(t.TempDir(), "claims.yml")
	if err := os.WriteFile(file, []byte(suite), 0o600); err != nil {
		t.Fatal(err)
	}
	if output, err := exec.Command(tool, "test", "rules", file).CombinedOutput(); err != nil {
		t.Fatalf("promtool: %v: %s", err, output)
	}
}
