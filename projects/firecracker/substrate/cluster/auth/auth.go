// Package auth is the caller-authentication control for the fc-invoke ingress
// (the STPA "unauthenticated /invoke" high-severity UCA). The daemon enforces
// authentication itself, in application code, rather than delegating it to the
// deployment substrate, so the security property is portable to any cluster and
// does not evaporate when a service mesh is absent. In the homelab a Linkerd
// AuthorizationPolicy is layered on top as defence-in-depth, but this middleware
// is the substrate-independent guarantee.
//
// The mechanism is the Kubernetes TokenReview API: a caller presents its
// ServiceAccount bearer token, and the daemon asks the API server to
// authenticate it and return the caller identity, then checks that identity
// against an explicit allow-list. There is no long-lived shared secret to
// provision or rotate, and the caller identity is verified cryptographically by
// the API server rather than asserted by a replayable string.
package auth

import (
	"context"
	"log/slog"
	"net/http"
	"strings"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/propagation"
)

// tracer spans the TokenReview round-trip. Auth is OUTER middleware that runs
// before the ingress handler opens the root fc_invoke span, so this span cannot
// nest under fc_invoke. Instead it is started from the context carrying the
// caller's extracted W3C parent, so it lands in the SAME trace as fc_invoke (a
// sibling under the caller's remote parent) rather than in a disconnected one.
var tracer = otel.Tracer("fc-invoke/auth")

// Reviewer verifies a bearer token and returns the authenticated caller's
// username (for a ServiceAccount token this is
// "system:serviceaccount:<namespace>:<name>"). It is the seam that lets the
// middleware be unit-tested without a live Kubernetes API server: production
// uses the TokenReview-backed implementation (see reviewer.go), tests inject a
// fake.
type Reviewer interface {
	// Review validates token and returns the authenticated username. A non-nil
	// error means the token could not be validated (an API failure) or was
	// rejected as unauthenticated; the caller maps either to a 401.
	Review(ctx context.Context, token string) (username string, err error)
}

// healthPath is exempt from authentication: kubelet liveness/readiness probes
// hit the app port directly and carry no bearer token, so authenticating them
// would fail every probe and crash-loop the pod. The Linkerd layer likewise
// auto-authorizes kubelet probes, so this exemption keeps the two layers
// consistent.
const healthPath = "/healthz"

// middleware wraps a handler, admitting a request only after its bearer token
// authenticates to an allow-listed caller identity.
type middleware struct {
	next     http.Handler
	reviewer Reviewer
	allowed  map[string]struct{}
	logger   *slog.Logger
}

// Middleware returns an http.Handler that authenticates every request (except
// the unauthenticated liveness path) with reviewer, admitting it only when the
// verified caller identity is in allowed. allowed holds full usernames, e.g.
// "system:serviceaccount:monolith:monolith". An empty allowed list is a
// programming error: callers gate on len(allowed) > 0 and skip the middleware
// entirely when authentication is intentionally disabled, so the daemon never
// silently authorizes everyone here.
func Middleware(next http.Handler, reviewer Reviewer, allowed []string, logger *slog.Logger) http.Handler {
	if logger == nil {
		logger = slog.Default()
	}
	set := make(map[string]struct{}, len(allowed))
	for _, a := range allowed {
		if a = strings.TrimSpace(a); a != "" {
			set[a] = struct{}{}
		}
	}
	return &middleware{next: next, reviewer: reviewer, allowed: set, logger: logger}
}

func (m *middleware) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	// Liveness probes carry no credential and must never be gated.
	if r.Method == http.MethodGet && r.URL.Path == healthPath {
		m.next.ServeHTTP(w, r)
		return
	}

	token, ok := bearerToken(r)
	if !ok {
		http.Error(w, "missing or malformed Authorization: Bearer token", http.StatusUnauthorized)
		return
	}

	// Start the TokenReview span from the caller's extracted trace context so it
	// joins the same trace as the downstream fc_invoke span (see tracer doc).
	reviewCtx := otel.GetTextMapPropagator().Extract(r.Context(), propagation.HeaderCarrier(r.Header))
	reviewCtx, span := tracer.Start(reviewCtx, "auth_tokenreview")
	username, err := m.reviewer.Review(reviewCtx, token)
	if err != nil {
		span.RecordError(err)
		span.SetStatus(codes.Error, err.Error())
	}
	span.End()
	if err != nil {
		// Do not echo the failure detail to the caller (it can carry token
		// state); log it for operators and return a bare 401.
		m.logger.Warn("invoke: caller token rejected", "err", err)
		http.Error(w, "unauthenticated", http.StatusUnauthorized)
		return
	}

	if _, allowed := m.allowed[username]; !allowed {
		m.logger.Warn("invoke: caller not authorized", "caller", username)
		http.Error(w, "forbidden", http.StatusForbidden)
		return
	}

	m.next.ServeHTTP(w, r)
}

// bearerToken extracts the token from an "Authorization: Bearer <token>"
// header. The scheme match is case-insensitive per RFC 7235; the token must be
// non-empty.
func bearerToken(r *http.Request) (string, bool) {
	h := r.Header.Get("Authorization")
	if h == "" {
		return "", false
	}
	const prefix = "bearer "
	if len(h) <= len(prefix) || !strings.EqualFold(h[:len(prefix)], prefix) {
		return "", false
	}
	token := strings.TrimSpace(h[len(prefix):])
	if token == "" {
		return "", false
	}
	return token, true
}
