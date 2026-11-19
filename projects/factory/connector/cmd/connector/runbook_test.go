package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
	"google.golang.org/grpc/metadata"
	meta "k8s.io/apimachinery/pkg/apis/meta/v1"
	core "k8s.io/client-go/kubernetes/typed/core/v1"
	"k8s.io/client-go/rest"
)

func TestRevertConfirmation(t *testing.T) {
	target := revertTarget{"locked", "executor", "factory-r-homelab", "homelab-codex-deadbeef-abcde", "uid", "factory-connector"}
	locked := true
	audits := 0
	a := revertAction{tickets: map[string]preview{}, targets: func(context.Context) ([]revertTarget, error) { return []revertTarget{target}, nil }, execute: func(_ context.Context, got revertTarget) error {
		if got != target {
			t.Fatal(got)
		}
		locked = false
		return nil
	}, audit: func(user string, got revertTarget, result string) error {
		if user != "joe" || got != target || result != "reverted" {
			t.Fatal(user, got, result)
		}
		audits++
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
	if request("GET", "joe", "") != 200 || len(a.tickets) != 1 || !locked {
		t.Fatal("preview")
	}
	for ticket := range a.tickets {
		form := "ticket=" + ticket
		if request("POST", "other", form) != 409 || request("POST", "joe", form+"&pod=other") != 400 || !locked || audits != 0 {
			t.Fatal("unreviewed execution")
		}
		if request("POST", "joe", form) != 200 || locked || audits != 1 || request("POST", "joe", form) != 409 {
			t.Fatal("confirmation or replay")
		}
	}
}

type lockedSession struct {
	target         revertTarget
	token, creator string
	deleted        atomic.Bool
	released       bool
}

func (s *lockedSession) GetSession(ctx context.Context, id string) (*kagent.Session, error) {
	md, _ := metadata.FromOutgoingContext(ctx)
	if id != s.target.Session || len(md.Get("authorization")) != 1 || md.Get("authorization")[0] != "Bearer "+s.token {
		return nil, fmt.Errorf("wrong identity or session")
	}
	state := &kagent.Session{ID: id, Creator: s.creator}
	if s.deleted.Load() {
		state.FailureReason = kagent.ReasonRuntimeCrashed
	}
	return state, nil
}

func (s *lockedSession) RevertSession(ctx context.Context, id string) (*kagent.Session, error) {
	state, err := s.GetSession(ctx, id)
	if err != nil || !s.deleted.Load() {
		return nil, fmt.Errorf("premature revert")
	}
	state.State = kagent.RuntimeReady
	state.FailureReason = ""
	s.released = true
	return state, nil
}

func TestDeleteThenRevertAsCreator(t *testing.T) {
	t.Setenv("KAGENT_AUTHORITY", "factory-home")
	target := revertTarget{"locked", "executor", "factory-r-homelab", "homelab-codex-deadbeef-abcde", "uid", "factory-connector"}
	claims := `{"iss":"cluster","sub":"system:serviceaccount:factory:factory-connector","aud":["kagent-invocation"],"kubernetes.io":{"namespace":"factory","serviceaccount":{"name":"factory-connector","uid":"creator-uid"}}}`
	session := &lockedSession{target: target, token: "mock." + base64.RawURLEncoding.EncodeToString([]byte(claims)) + ".mock", creator: (kagent.CallerIdentity{Authority: "factory-home", ServiceAccountUID: "creator-uid"}).PrincipalID()}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.Method == "POST" && r.URL.Path == "/api/v1/namespaces/factory/serviceaccounts/factory-connector/token":
			fmt.Fprintf(w, `{"apiVersion":"authentication.k8s.io/v1","kind":"TokenRequest","status":{"token":%q}}`, session.token)
		case r.Method == "DELETE" && r.URL.Path == "/api/v1/namespaces/"+target.Namespace+"/pods/"+target.Pod:
			var opts meta.DeleteOptions
			if json.NewDecoder(r.Body).Decode(&opts) != nil || opts.Preconditions == nil || opts.Preconditions.UID == nil || string(*opts.Preconditions.UID) != target.UID {
				t.Error("missing UID precondition")
				w.WriteHeader(409)
				return
			}
			session.deleted.Store(true)
			fmt.Fprint(w, `{"kind":"Status","apiVersion":"v1","status":"Success"}`)
		case r.Method == "GET" && session.deleted.Load():
			w.WriteHeader(404)
			fmt.Fprint(w, `{"kind":"Status","apiVersion":"v1","reason":"NotFound","code":404}`)
		default:
			t.Errorf("unexpected %s %s", r.Method, r.URL.Path)
			w.WriteHeader(403)
		}
	}))
	defer server.Close()
	pods, err := core.NewForConfig(&rest.Config{Host: server.URL, ContentConfig: rest.ContentConfig{ContentType: "application/json"}})
	if err != nil {
		t.Fatal(err)
	}
	b := revertBackend{pods: pods, sessions: session}
	if err = b.execute(t.Context(), target); err != nil || !session.released {
		t.Fatal("session still locked", err)
	}
}
