package main

import (
	"context"
	"encoding/base64"
	"errors"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
	v1 "k8s.io/api/core/v1"
	meta "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes/fake"
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
	state   kagent.Session
	reverts int
}

func (s *lockedSession) GetSession(context.Context, string) (*kagent.Session, error) {
	return &s.state, nil
}

func (s *lockedSession) RevertSession(context.Context, string) (*kagent.Session, error) {
	if !s.state.Crashed() {
		return nil, errors.New("not crashed")
	}
	s.state.State = kagent.RuntimeReady
	s.state.FailureReason = ""
	s.reverts++
	return nil, errors.New("lost response after successful revert")
}

func TestDurableRecoveryAndAuditRetry(t *testing.T) {
	ctx := t.Context()
	t.Setenv("KAGENT_AUTHORITY", "factory-home")
	target := revertTarget{"locked", "executor", "factory-r-homelab", "homelab-codex-deadbeef-abcde", "uid", "factory-connector"}
	claims := `{"iss":"cluster","sub":"system:serviceaccount:factory:factory-connector","aud":["kagent-invocation"],"kubernetes.io":{"namespace":"factory","serviceaccount":{"name":"factory-connector","uid":"creator-uid"}}}`
	source := &kagent.TokenSource{Path: filepath.Join(t.TempDir(), "token")}
	if err := os.WriteFile(source.Path, []byte("mock."+base64.RawURLEncoding.EncodeToString([]byte(claims))+".mock"), 0o600); err != nil {
		t.Fatal(err)
	}
	sessions := &lockedSession{state: kagent.Session{ID: target.Session, Creator: (kagent.CallerIdentity{Authority: "factory-home", ServiceAccountUID: "creator-uid"}).PrincipalID()}}
	client := fake.NewSimpleClientset(&v1.ConfigMap{ObjectMeta: meta.ObjectMeta{Name: journalName(target.Creator), Namespace: "grafana"}}, &v1.Pod{ObjectMeta: meta.ObjectMeta{Name: target.Pod, Namespace: target.Namespace, UID: "uid"}})
	backend := func() revertBackend {
		return revertBackend{pods: client.CoreV1(), sessions: sessions, discover: func(context.Context) ([]revertTarget, error) { return []revertTarget{target}, nil }}
	}
	if err := backend().submit(ctx, "joe", target); err != nil {
		t.Fatal(err)
	}
	auditCount := 0
	audit := func(user string, got revertTarget, result string) error {
		if user != "joe" || got != target || result != "reverted" {
			t.Fatal(user, got, result)
		}
		auditCount++
		if auditCount == 1 {
			return errors.New("Discord offline")
		}
		return nil
	}
	step := func(token *kagent.TokenSource, phase string) {
		t.Helper()
		b := backend()
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
	step(nil, "gone")
	sessions.state.FailureReason = kagent.ReasonRuntimeCrashed
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
	if err := backend().advance(ctx, target.Creator, nil, audit); err == nil {
		t.Fatal("expected delivery failure")
	}
	step(nil, "delivered")
	if sessions.reverts != 1 || auditCount != 2 {
		t.Fatal(sessions.reverts, auditCount)
	}
}
