package kagent

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"strings"
	"time"
)

// ErrCallerIdentity means the projected credential or configured identity
// boundary cannot establish a caller witness. Its errors contain no token data.
var ErrCallerIdentity = errors.New("kagent: caller identity unavailable")

// CallerIdentity is the non-secret, stable identity boundary of an invocation.
// The UID survives projected-token rotation, but changes when the ServiceAccount
// is recreated. The issuer and endpoints also fence a restored or moved cluster.
// Authority must be the controller's configured serviceAccount.authority.
type CallerIdentity struct {
	Issuer            string `json:"issuer"`
	ServiceAccountUID string `json:"serviceAccountUID"`
	Authority         string `json:"authority"`
	A2AEndpoint       string `json:"a2aEndpoint"`
	GRPCTarget        string `json:"grpcTarget"`
}

// Valid reports whether all parts of the identity boundary are present.
func (i CallerIdentity) Valid() bool {
	for _, v := range []string{i.Issuer, i.ServiceAccountUID, i.Authority, i.A2AEndpoint, i.GRPCTarget} {
		if v == "" || strings.TrimSpace(v) != v {
			return false
		}
	}
	return true
}

// Key is a deterministic, non-secret representation suitable for persistence.
func (i CallerIdentity) Key() string {
	b, _ := json.Marshal(i) // a struct containing only strings cannot fail
	return string(b)
}

// PrincipalID is kagent v1.0.1's creator ID for this ServiceAccount. It is
// compared with a readable Session.Creator when adopting legacy work.
func (i CallerIdentity) PrincipalID() string {
	encode := base64.RawURLEncoding.EncodeToString
	return encode([]byte("kubernetes:"+i.Authority)) + "." + encode([]byte(i.ServiceAccountUID))
}

// InvocationCredentials pins one projected credential to a reconciliation or
// send sequence. Parsing establishes continuity only: kagent still authenticates
// the pinned bytes through Kubernetes TokenReview on each request.
type InvocationCredentials struct {
	Tokens      *TokenSource
	Authority   string
	A2AEndpoint string
	GRPCTarget  string
}

type pinnedCredentialKey struct{}

type pinnedCredential struct {
	source   *TokenSource
	token    string
	identity CallerIdentity
}

// Pin reads the current projected token, bypassing TokenSource's ordinary
// minute cache so a replaced ServiceAccount is noticed before another send.
// Both HTTP and gRPC use these exact bytes until this context is discarded.
func (c *InvocationCredentials) Pin(ctx context.Context) (context.Context, CallerIdentity, error) {
	if c == nil || c.Tokens == nil {
		return ctx, CallerIdentity{}, fmt.Errorf("%w: no projected credential", ErrCallerIdentity)
	}
	if pinned, ok := ctx.Value(pinnedCredentialKey{}).(pinnedCredential); ok && pinned.source == c.Tokens {
		if pinned.identity.Authority != c.Authority || pinned.identity.A2AEndpoint != c.A2AEndpoint || pinned.identity.GRPCTarget != c.GRPCTarget {
			return ctx, CallerIdentity{}, fmt.Errorf("%w: pinned identity boundary changed", ErrCallerIdentity)
		}
		return ctx, pinned.identity, nil
	}
	token, err := c.Tokens.freshToken()
	if err != nil {
		return ctx, CallerIdentity{}, fmt.Errorf("%w: cannot read projected credential", ErrCallerIdentity)
	}
	issuer, uid, err := projectedIdentity(token)
	if err != nil {
		return ctx, CallerIdentity{}, err
	}
	identity := CallerIdentity{
		Issuer: issuer, ServiceAccountUID: uid, Authority: c.Authority,
		A2AEndpoint: c.A2AEndpoint, GRPCTarget: c.GRPCTarget,
	}
	if !identity.Valid() {
		return ctx, CallerIdentity{}, fmt.Errorf("%w: incomplete identity boundary", ErrCallerIdentity)
	}
	return context.WithValue(ctx, pinnedCredentialKey{}, pinnedCredential{source: c.Tokens, token: token, identity: identity}), identity, nil
}

// tokenForContext uses a pin only for the source that established it. A broker
// token source cannot accidentally borrow the connector's kagent credential.
func tokenForContext(ctx context.Context, source *TokenSource) (string, error) {
	if pinned, ok := ctx.Value(pinnedCredentialKey{}).(pinnedCredential); ok && pinned.source == source {
		return pinned.token, nil
	}
	return source.Token()
}

func (s *TokenSource) freshToken() (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	b, err := os.ReadFile(s.Path)
	if err != nil {
		return "", err
	}
	s.token, s.readAt = strings.TrimSpace(string(b)), time.Now()
	return s.token, nil
}

func projectedIdentity(token string) (issuer, uid string, err error) {
	invalid := func() (string, string, error) {
		return "", "", fmt.Errorf("%w: invalid projected ServiceAccount identity", ErrCallerIdentity)
	}
	parts := strings.Split(token, ".")
	if len(parts) != 3 || parts[0] == "" || parts[1] == "" || parts[2] == "" || len(parts[1]) > 64<<10 {
		return invalid()
	}
	payload, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return invalid()
	}
	var claims struct {
		Issuer     string          `json:"iss"`
		Subject    string          `json:"sub"`
		Audience   json.RawMessage `json:"aud"`
		Kubernetes struct {
			Namespace      string `json:"namespace"`
			ServiceAccount struct {
				Name string `json:"name"`
				UID  string `json:"uid"`
			} `json:"serviceaccount"`
		} `json:"kubernetes.io"`
	}
	if json.Unmarshal(payload, &claims) != nil || claims.Issuer == "" || claims.Kubernetes.ServiceAccount.UID == "" {
		return invalid()
	}
	subject := strings.Split(claims.Subject, ":")
	if len(subject) != 4 || subject[0] != "system" || subject[1] != "serviceaccount" || subject[2] == "" || subject[3] == "" {
		return invalid()
	}
	if claims.Kubernetes.Namespace != subject[2] || claims.Kubernetes.ServiceAccount.Name != subject[3] {
		return invalid()
	}
	var audiences []string
	if json.Unmarshal(claims.Audience, &audiences) != nil {
		var single string
		if json.Unmarshal(claims.Audience, &single) != nil {
			return invalid()
		}
		audiences = []string{single}
	}
	for _, audience := range audiences {
		if audience == "kagent-invocation" {
			return claims.Issuer, claims.Kubernetes.ServiceAccount.UID, nil
		}
	}
	return invalid()
}
