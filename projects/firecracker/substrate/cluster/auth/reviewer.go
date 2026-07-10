package auth

import (
	"context"
	"fmt"

	authnv1 "k8s.io/api/authentication/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
)

// tokenReviewClient is the narrow slice of the Kubernetes AuthenticationV1
// client this package needs. It is an interface so the Reviewer can be tested
// against a fake without a live API server.
type tokenReviewClient interface {
	Create(ctx context.Context, tr *authnv1.TokenReview, opts metav1.CreateOptions) (*authnv1.TokenReview, error)
}

// clusterReviewer authenticates bearer tokens against the Kubernetes
// TokenReview API. It requires the daemon's ServiceAccount to hold `create` on
// tokenreviews.authentication.k8s.io, granted in the homelab by binding the SA
// to the built-in system:auth-delegator ClusterRole (see the chart's rbac.yaml).
type clusterReviewer struct {
	reviews tokenReviewClient
}

// NewClusterReviewer builds a Reviewer backed by the in-cluster TokenReview API.
// It uses the pod's own ServiceAccount credentials (in-cluster REST config), so
// it only works when running inside Kubernetes; callers gate on
// FC_INVOKE_ALLOWED_CALLERS being set before constructing one.
func NewClusterReviewer() (Reviewer, error) {
	restCfg, err := rest.InClusterConfig()
	if err != nil {
		return nil, fmt.Errorf("in-cluster config: %w", err)
	}
	// client-go's default client-side rate limiter is 5 QPS / burst 10. Since
	// every /invoke does one TokenReview, that default bucket silently capped the
	// daemon's whole throughput at ~5 invokes/s (a saturating drain queued for
	// seconds in this limiter, upstream of the concurrency semaphore and every
	// span but auth_tokenreview). Raise it well above the invoke rate; the
	// caching wrapper below keeps the actual TokenReview call rate near zero, so
	// this is headroom for cache misses (hourly token rotation), not sustained
	// API-server load.
	restCfg.QPS = 100
	restCfg.Burst = 200
	clientset, err := kubernetes.NewForConfig(restCfg)
	if err != nil {
		return nil, fmt.Errorf("kubernetes client: %w", err)
	}
	cluster := &clusterReviewer{reviews: clientset.AuthenticationV1().TokenReviews()}
	return newCachingReviewer(cluster, defaultReviewTTL), nil
}

// Review submits token to the TokenReview API and returns the authenticated
// username. A TokenReview with no explicit audiences validates the token
// against the API server's default audiences, which the caller's
// default-mounted ServiceAccount token carries; scoping to a dedicated
// audience (a projected token minted with audience "fc-invoke") is a future
// hardening that would additionally bind the token to this daemon.
func (r *clusterReviewer) Review(ctx context.Context, token string) (string, error) {
	res, err := r.reviews.Create(ctx, &authnv1.TokenReview{
		Spec: authnv1.TokenReviewSpec{Token: token},
	}, metav1.CreateOptions{})
	if err != nil {
		return "", fmt.Errorf("token review request: %w", err)
	}
	if !res.Status.Authenticated {
		// Status.Error is set by the API server when the token is malformed or
		// expired; surface it for operator logs (it carries no secret material).
		if res.Status.Error != "" {
			return "", fmt.Errorf("token not authenticated: %s", res.Status.Error)
		}
		return "", fmt.Errorf("token not authenticated")
	}
	return res.Status.User.Username, nil
}
