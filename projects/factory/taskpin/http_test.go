package taskpin

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"slices"
	"strings"
	"testing"

	"github.com/jomcgi/homelab/projects/factory/broker/actorjwt"
	"github.com/jomcgi/homelab/projects/factory/broker/auth"
	authenticationv1 "k8s.io/api/authentication/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/kubernetes/fake"
	ktesting "k8s.io/client-go/testing"
)

type verifyFunc func(context.Context, string) (actorjwt.Identity, error)

func (f verifyFunc) Verify(ctx context.Context, raw string) (actorjwt.Identity, error) {
	return f(ctx, raw)
}

func TestBrokerUsesOnlyVerifiedActorIdentity(t *testing.T) {
	const subject = "system:serviceaccount:factory:factory-broker"
	const ownerToken = "projected-broker-token"
	kube := fake.NewSimpleClientset()
	kube.PrependReactor("create", "tokenreviews", func(action ktesting.Action) (bool, runtime.Object, error) {
		in := action.(ktesting.CreateAction).GetObject().(*authenticationv1.TokenReview)
		if in.Spec.Token != ownerToken || !slices.Equal(in.Spec.Audiences, []string{Audience}) {
			return true, &authenticationv1.TokenReview{}, nil
		}
		return true, &authenticationv1.TokenReview{Status: authenticationv1.TokenReviewStatus{
			Authenticated: true,
			Audiences:     []string{Audience},
			User:          authenticationv1.UserInfo{Username: subject},
		}}, nil
	})
	const namespace = "factory-r-homelab"
	main := strings.Repeat("a", 40)
	reviewer := &auth.Reviewer{Client: kube, Audience: Audience}
	owner := httptest.NewServer(reviewer.Require([]string{subject}, Owner(func(_ context.Context, wantNamespace, session string) (string, error) {
		if wantNamespace != namespace || session != "working-1" {
			return "", sql.ErrNoRows
		}
		return main, nil
	})))
	defer owner.Close()
	verify := verifyFunc(func(_ context.Context, token string) (actorjwt.Identity, error) {
		switch token {
		case "working":
			return actorjwt.Identity{Atespace: namespace, ActorName: "session-Working-1"}, nil
		case "stale":
			return actorjwt.Identity{Atespace: namespace, ActorName: "session-stale-1"}, nil
		case "other-role":
			return actorjwt.Identity{Atespace: namespace + "-review", ActorName: "session-working-1"}, nil
		default:
			return actorjwt.Identity{}, errors.New("signature rejected")
		}
	})
	broker := Broker(verify, []string{namespace}, owner.URL, func() (string, error) { return ownerToken, nil })
	for _, tc := range []struct {
		name, token, query string
		status             int
	}{
		{"working", "working", "", http.StatusOK},
		{"missing token", "", "", http.StatusUnauthorized},
		{"invalid signature", "forged", "", http.StatusUnauthorized},
		{"wrong namespace", "other-role", "", http.StatusForbidden},
		{"terminal task", "stale", "", http.StatusNotFound},
		{"caller input", "working", "?session=someone-else", http.StatusBadRequest},
	} {
		t.Run(tc.name, func(t *testing.T) {
			req := httptest.NewRequest(http.MethodGet, Path+tc.query, nil)
			if tc.token != "" {
				req.Header.Set("Authorization", "Bearer "+tc.token)
			}
			req.Header.Set("Session_id", "someone-else")
			rec := httptest.NewRecorder()
			broker.ServeHTTP(rec, req)
			if rec.Code != tc.status {
				t.Fatalf("status %d, want %d: %s", rec.Code, tc.status, rec.Body.String())
			}
			if tc.status == http.StatusOK {
				var got response
				if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil || got.MainSHA != main ||
					rec.Header().Get("Cache-Control") != "no-store" {
					t.Fatalf("invalid pin response: %q, %v", rec.Body.String(), err)
				}
			}
		})
	}
}

func TestOwnerRejectsUnexpectedInput(t *testing.T) {
	owner := Owner(func(context.Context, string, string) (string, error) {
		t.Fatal("invalid input reached the pin resolver")
		return "", nil
	})
	req := httptest.NewRequest(http.MethodPost, Path, strings.NewReader(
		`{"namespace":"factory-r-homelab","session":"working-1","main_sha":"supplied-by-caller"}`))
	rec := httptest.NewRecorder()
	owner.ServeHTTP(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status %d, want %d", rec.Code, http.StatusBadRequest)
	}
}
