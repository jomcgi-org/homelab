package auth

import (
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"

	authenticationv1 "k8s.io/api/authentication/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/kubernetes/fake"
	k8stesting "k8s.io/client-go/testing"
)

// fakeTokenReview answers TokenReviews from a token table, honouring the
// requested audience the way the API server does.
func fakeTokenReview(users map[string]string, audiences map[string][]string, fail bool) *fake.Clientset {
	client := fake.NewSimpleClientset()
	client.PrependReactor("create", "tokenreviews", func(action k8stesting.Action) (bool, runtime.Object, error) {
		if fail {
			return true, nil, errors.New("apiserver down")
		}
		review := action.(k8stesting.CreateAction).GetObject().(*authenticationv1.TokenReview).DeepCopy()
		user, ok := users[review.Spec.Token]
		if !ok {
			return true, review, nil
		}
		var matched []string
		for _, want := range review.Spec.Audiences {
			for _, has := range audiences[review.Spec.Token] {
				if want == has {
					matched = append(matched, want)
				}
			}
		}
		if len(matched) == 0 {
			review.Status = authenticationv1.TokenReviewStatus{Error: "token audiences do not match"}
			return true, review, nil
		}
		review.Status = authenticationv1.TokenReviewStatus{Authenticated: true, Audiences: matched, User: authenticationv1.UserInfo{Username: user}}
		return true, review, nil
	})
	return client
}

func TestRequire(t *testing.T) {
	const connector = "system:serviceaccount:factory:factory-connector"
	client := fakeTokenReview(
		map[string]string{"good": connector, "other": "system:serviceaccount:factory:other", "wrong-aud": connector},
		map[string][]string{"good": {"factory-broker"}, "other": {"factory-broker"}, "wrong-aud": {"https://kubernetes.default.svc"}},
		false,
	)
	r := &Reviewer{Client: client, Audience: "factory-broker"}
	handler := r.Require([]string{connector}, http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		_, _ = w.Write([]byte(User(req.Context())))
	}))
	for name, tc := range map[string]struct {
		header string
		status int
	}{
		"allowed":        {"Bearer good", http.StatusOK},
		"no header":      {"", http.StatusUnauthorized},
		"basic":          {"Basic good", http.StatusUnauthorized},
		"unknown token":  {"Bearer nope", http.StatusUnauthorized},
		"wrong audience": {"Bearer wrong-aud", http.StatusUnauthorized},
		"not allowed":    {"Bearer other", http.StatusForbidden},
	} {
		t.Run(name, func(t *testing.T) {
			req := httptest.NewRequest(http.MethodGet, "/", nil)
			if tc.header != "" {
				req.Header.Set("Authorization", tc.header)
			}
			rec := httptest.NewRecorder()
			handler.ServeHTTP(rec, req)
			if rec.Code != tc.status {
				t.Fatalf("status = %d, want %d (%s)", rec.Code, tc.status, rec.Body)
			}
			if tc.status == http.StatusOK && rec.Body.String() != connector {
				t.Fatalf("user = %q", rec.Body)
			}
		})
	}

	down := &Reviewer{Client: fakeTokenReview(nil, nil, true), Audience: "factory-broker"}
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/", nil)
	req.Header.Set("Authorization", "Bearer good")
	down.Require([]string{connector}, http.NotFoundHandler()).ServeHTTP(rec, req)
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("API failure status = %d", rec.Code)
	}
}
