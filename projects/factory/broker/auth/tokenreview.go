// Package auth authenticates in-cluster callers by their projected
// ServiceAccount token through the Kubernetes TokenReview API.
package auth

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"slices"
	"strings"

	authenticationv1 "k8s.io/api/authentication/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
)

var ErrUnauthenticated = errors.New("unauthenticated")

// Reviewer validates bearer tokens for one audience.
type Reviewer struct {
	Client   kubernetes.Interface
	Audience string
	Logger   *slog.Logger
}

// Review returns the authenticated username, such as
// system:serviceaccount:<namespace>:<name>.
func (r *Reviewer) Review(ctx context.Context, token string) (string, error) {
	if token == "" {
		return "", ErrUnauthenticated
	}
	review, err := r.Client.AuthenticationV1().TokenReviews().Create(ctx, &authenticationv1.TokenReview{
		Spec: authenticationv1.TokenReviewSpec{Token: token, Audiences: []string{r.Audience}},
	}, metav1.CreateOptions{})
	if err != nil {
		return "", err
	}
	status := review.Status
	if !status.Authenticated || status.Error != "" || status.User.Username == "" {
		return "", ErrUnauthenticated
	}
	// The API server returns the audiences it checked. An authenticator
	// that ignores audiences returns none, so require ours explicitly.
	if !slices.Contains(status.Audiences, r.Audience) {
		return "", ErrUnauthenticated
	}
	return status.User.Username, nil
}

// Require wraps next so that only the listed usernames reach it.
func (r *Reviewer) Require(allowed []string, next http.Handler) http.Handler {
	set := map[string]bool{}
	for _, a := range allowed {
		set[a] = true
	}
	logger := r.Logger
	if logger == nil {
		logger = slog.Default()
	}
	return http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		token, ok := BearerToken(req)
		if !ok {
			writeError(w, http.StatusUnauthorized, "unauthenticated")
			return
		}
		user, err := r.Review(req.Context(), token)
		switch {
		case errors.Is(err, ErrUnauthenticated):
			writeError(w, http.StatusUnauthorized, "unauthenticated")
			return
		case err != nil:
			logger.Error("token review failed", "err", err)
			writeError(w, http.StatusServiceUnavailable, "token_review_unavailable")
			return
		case !set[user]:
			logger.Warn("caller not authorized", "user", user, "path", req.URL.Path)
			writeError(w, http.StatusForbidden, "forbidden")
			return
		}
		next.ServeHTTP(w, req.WithContext(withUser(req.Context(), user)))
	})
}

type userKey struct{}

func withUser(ctx context.Context, user string) context.Context {
	return context.WithValue(ctx, userKey{}, user)
}

// User returns the username Require authenticated.
func User(ctx context.Context) string { u, _ := ctx.Value(userKey{}).(string); return u }

// BearerToken extracts the Authorization bearer token.
func BearerToken(r *http.Request) (string, bool) {
	scheme, token, ok := strings.Cut(r.Header.Get("Authorization"), " ")
	if !ok || !strings.EqualFold(scheme, "Bearer") || strings.TrimSpace(token) == "" {
		return "", false
	}
	return strings.TrimSpace(token), true
}

func writeError(w http.ResponseWriter, status int, reason string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write([]byte(`{"factory_error":"` + reason + `"}` + "\n"))
}
