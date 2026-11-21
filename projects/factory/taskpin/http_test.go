package taskpin

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/jomcgi/homelab/projects/factory/broker/actorjwt"
)

type verifyFunc func(context.Context, string) (actorjwt.Identity, error)

func (f verifyFunc) Verify(ctx context.Context, raw string) (actorjwt.Identity, error) {
	return f(ctx, raw)
}

func TestBrokerUsesOnlyVerifiedActorIdentity(t *testing.T) {
	const namespace = "factory-r-homelab"
	main := strings.Repeat("a", 40)
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
	owner := httptest.NewServer(ownerHandler(verify, func(_ context.Context, wantNamespace, session string) (string, error) {
		if wantNamespace != namespace || session != "working-1" {
			return "", sql.ErrNoRows
		}
		return main, nil
	}))
	defer owner.Close()
	broker := Broker(verify, map[string]string{namespace: owner.URL})
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
	verify := verifyFunc(func(context.Context, string) (actorjwt.Identity, error) {
		return actorjwt.Identity{}, errors.New("invalid token")
	})
	owner := ownerHandler(verify, func(context.Context, string, string) (string, error) {
		t.Fatal("unverified request reached the pin resolver")
		return "", nil
	})
	for _, tc := range []struct {
		path   string
		status int
	}{
		{Path + "?task=caller-choice", http.StatusBadRequest},
		{Path, http.StatusUnauthorized},
	} {
		req := httptest.NewRequest(http.MethodGet, tc.path, nil)
		req.Header.Set("Authorization", "Bearer forged")
		rec := httptest.NewRecorder()
		owner.ServeHTTP(rec, req)
		if rec.Code != tc.status {
			t.Fatalf("status %d, want %d", rec.Code, tc.status)
		}
	}
}
