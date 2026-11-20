package main

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	authenticationv1 "k8s.io/api/authentication/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/kubernetes/fake"
	k8stesting "k8s.io/client-go/testing"
)

func TestDecisionAccess(t *testing.T) {
	client := fake.NewSimpleClientset()
	client.PrependReactor("create", "tokenreviews", func(action k8stesting.Action) (bool, runtime.Object, error) {
		review := action.(k8stesting.CreateAction).GetObject().(*authenticationv1.TokenReview).DeepCopy()
		if len(review.Spec.Audiences) != 1 || review.Spec.Audiences[0] != "factory-decide" {
			t.Error("wrong requested audience")
		}
		review.Status = authenticationv1.TokenReviewStatus{Authenticated: true, Audiences: review.Spec.Audiences, User: authenticationv1.UserInfo{Username: review.Spec.Token}}
		return true, review, nil
	})
	handler := decisionAccess(client, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(204) }))
	for user, want := range map[string]int{"": 401, "system:serviceaccount:factory:factory-connector": 403, "system:serviceaccount:factory-discord-agent:discord-bridge": 403, "system:serviceaccount:factory-discord:discord-bridge": 204, "system:serviceaccount:factory-landing:factory-landing": 204} {
		req := httptest.NewRequest("POST", "/decide", nil)
		if user != "" {
			req.Header.Set("Authorization", "Bearer "+user)
		}
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, req)
		if rec.Code != want {
			t.Errorf("subject %q: got %d, want %d", user, rec.Code, want)
		}
	}
}

func TestTokenReviewFence(t *testing.T) {
	sent := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-Request-ID") != "decision/tokenreview" {
			t.Error("missing journal ID")
		}
		sent++
	}))
	defer srv.Close()
	lost := errors.New("writer lost")
	fail := true
	attempts := 0
	transport := tokenReviewTransport{next: srv.Client().Transport, begin: func(_ context.Context, id string) error {
		attempts++
		if id != "decision/tokenreview" {
			t.Error("unexpected journal ID")
		}
		if fail {
			return lost
		}
		return nil
	}}
	client := http.Client{Transport: transport}
	for _, token := range []string{"first", "rotated"} {
		resp, err := client.Post(srv.URL, "application/json", strings.NewReader(token))
		if fail {
			if !errors.Is(err, lost) || sent != 0 {
				t.Fatal("request escaped writer fence")
			}
			fail = false
		} else if err != nil {
			t.Fatal(err)
		} else {
			resp.Body.Close()
		}
	}
	if sent != 1 || attempts != 2 {
		t.Fatalf("sent=%d attempts=%d", sent, attempts)
	}
}
