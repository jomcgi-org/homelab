package actorjwt

import (
	"context"
	"encoding/base64"
	"errors"
	"sync"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/broker/internal/fakes"
)

type clock struct {
	mu  sync.Mutex
	now time.Time
}

func (c *clock) Now() time.Time { c.mu.Lock(); defer c.mu.Unlock(); return c.now }
func (c *clock) advance(d time.Duration) {
	c.mu.Lock()
	c.now = c.now.Add(d)
	c.mu.Unlock()
}

const audience = "factory-broker"

func claimsAt(idp *fakes.IdP, now time.Time) map[string]any {
	return fakes.ActorClaims(idp.URL, audience, "factory-homelab", "session-1", now)
}

func newVerifier(t *testing.T, idp *fakes.IdP, c *clock, allowed ...string) *Verifier {
	t.Helper()
	v, err := New(Options{Issuer: idp.URL, Audience: audience, HTTPClient: idp.Client(), Now: c.Now, MinRefetch: 30 * time.Second, AllowedAtespaces: allowed})
	if err != nil {
		t.Fatal(err)
	}
	return v
}

func reason(err error) string {
	var e *Error
	if errors.As(err, &e) {
		return e.Reason
	}
	if err == nil {
		return ""
	}
	return "other: " + err.Error()
}

func b64(s string) string { return base64.RawURLEncoding.EncodeToString([]byte(s)) }

func TestVerify(t *testing.T) {
	idp := fakes.NewIdP(t)
	c := &clock{now: time.Unix(1_800_000_000, 0)}
	key := fakes.NewECSigner(t, "k1")
	rs := fakes.NewRSASigner(t, "r1")
	idp.Publish(key, rs)
	v := newVerifier(t, idp, c)
	ctx := context.Background()

	id, err := v.Verify(ctx, key.Sign(t, claimsAt(idp, c.Now())))
	if err != nil {
		t.Fatalf("valid token: %v", err)
	}
	if id.Atespace != "factory-homelab" || id.ActorName != "session-1" || id.ActorUID != "uid-session-1" || id.Subject != "actor/factory-homelab/session-1" {
		t.Fatalf("identity = %+v", id)
	}
	if _, err := v.Verify(ctx, rs.Sign(t, claimsAt(idp, c.Now()))); err != nil {
		t.Fatalf("valid RS256 token: %v", err)
	}

	mutate := func(f func(map[string]any)) string {
		claims := claimsAt(idp, c.Now())
		f(claims)
		return key.Sign(t, claims)
	}
	other := fakes.NewECSigner(t, "k1") // same kid, different key
	tampered := key.Sign(t, claimsAt(idp, c.Now()))
	tampered = tampered[:len(tampered)-4] + "AAAA"
	for name, tc := range map[string]struct {
		token, want string
	}{
		"expired":         {mutate(func(m map[string]any) { m["exp"] = c.Now().Add(-time.Minute).Unix() }), ReasonExpired},
		"no exp":          {mutate(func(m map[string]any) { delete(m, "exp") }), ReasonExpired},
		"not yet valid":   {mutate(func(m map[string]any) { m["nbf"] = c.Now().Add(time.Hour).Unix() }), ReasonNotYetValid},
		"wrong issuer":    {mutate(func(m map[string]any) { m["iss"] = "https://idp.other.svc" }), ReasonIssuer},
		"wrong audience":  {mutate(func(m map[string]any) { m["aud"] = []string{"someone-else"} }), ReasonAudience},
		"string audience": {mutate(func(m map[string]any) { m["aud"] = "someone-else" }), ReasonAudience},
		"worker-for-actor": {mutate(func(m map[string]any) {
			m["sub"] = "worker-for-actor/factory-homelab/session-1"
			m["worker-for-actor.ate.dev"] = m["actor.ate.dev"]
			delete(m, "actor.ate.dev")
		}), ReasonSubject},
		"subject mismatch":  {mutate(func(m map[string]any) { m["sub"] = "actor/other/session-1" }), ReasonSubject},
		"wrong signing key": {other.Sign(t, claimsAt(idp, c.Now())), ReasonSignature},
		"tampered":          {tampered, ReasonSignature},
		"alg none":          {b64(`{"alg":"none","kid":"k1"}`) + "." + b64(`{}`) + ".", ReasonAlgorithm},
		"garbage":           {"not-a-jwt", ReasonMalformed},
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := v.Verify(ctx, tc.token); reason(err) != tc.want {
				t.Fatalf("Verify() reason = %q, want %q", reason(err), tc.want)
			}
		})
	}

	if _, err := v.Verify(ctx, mutate(func(m map[string]any) { m["aud"] = audience })); err != nil {
		t.Fatalf("matching string audience: %v", err)
	}
	// An RS256 header cannot select an ES256 key.
	confused := fakes.Signer{Kid: "k1", RSA: rs.RSA}
	if _, err := v.Verify(ctx, confused.Sign(t, claimsAt(idp, c.Now()))); reason(err) != ReasonAlgorithm {
		t.Fatalf("algorithm confusion reason = %q", reason(err))
	}

	restricted := newVerifier(t, idp, c, "factory-other")
	if _, err := restricted.Verify(ctx, key.Sign(t, claimsAt(idp, c.Now()))); reason(err) != ReasonAtespace {
		t.Fatalf("disallowed atespace reason = %q", reason(err))
	}
}

func TestUnknownKidThenRotation(t *testing.T) {
	idp := fakes.NewIdP(t)
	c := &clock{now: time.Unix(1_800_000_000, 0)}
	oldKey, newKey := fakes.NewECSigner(t, "old"), fakes.NewECSigner(t, "new")
	idp.Publish(oldKey)
	v := newVerifier(t, idp, c)
	ctx := context.Background()
	if _, err := v.Verify(ctx, oldKey.Sign(t, claimsAt(idp, c.Now()))); err != nil {
		t.Fatal(err)
	}
	fetches := idp.JWKSCalls()

	// The new key signs before it is published: unknown kid, and repeated
	// misses inside MinRefetch must not refetch the key set.
	for i := 0; i < 5; i++ {
		if _, err := v.Verify(ctx, newKey.Sign(t, claimsAt(idp, c.Now()))); reason(err) != ReasonUnknownKey {
			t.Fatalf("unknown kid reason = %q", reason(err))
		}
	}
	if got := idp.JWKSCalls() - fetches; got != 0 {
		t.Fatalf("unknown kid caused %d key set fetches inside MinRefetch", got)
	}

	// Rotation publishes both keys. After the rate limit one refetch picks
	// the new key up, and the old key keeps verifying during the overlap.
	idp.Publish(oldKey, newKey)
	c.advance(31 * time.Second)
	if _, err := v.Verify(ctx, newKey.Sign(t, claimsAt(idp, c.Now()))); err != nil {
		t.Fatalf("rotated key: %v", err)
	}
	if got := idp.JWKSCalls() - fetches; got != 1 {
		t.Fatalf("rotation took %d key set fetches, want 1", got)
	}
	if _, err := v.Verify(ctx, oldKey.Sign(t, claimsAt(idp, c.Now()))); err != nil {
		t.Fatalf("old key during rotation overlap: %v", err)
	}

	// Removing the old key stops its tokens after the next refresh.
	idp.Publish(newKey)
	if err := v.Refresh(ctx); err != nil {
		t.Fatal(err)
	}
	if _, err := v.Verify(ctx, oldKey.Sign(t, claimsAt(idp, c.Now()))); reason(err) != ReasonUnknownKey {
		t.Fatalf("removed key reason = %q", reason(err))
	}
	if _, err := v.Verify(ctx, newKey.Sign(t, claimsAt(idp, c.Now()))); err != nil {
		t.Fatalf("new key after rotation: %v", err)
	}
}

func TestDiscoveryIssuerMismatch(t *testing.T) {
	idp := fakes.NewIdP(t)
	key := fakes.NewECSigner(t, "k")
	idp.Publish(key)
	v, err := New(Options{Issuer: idp.URL + "/", Audience: audience, HTTPClient: idp.Client()})
	if err != nil {
		t.Fatal(err)
	}
	if err := v.Refresh(context.Background()); err == nil {
		t.Fatal("Refresh accepted a discovery document naming another issuer")
	}
	if _, err := v.Verify(context.Background(), key.Sign(t, claimsAt(idp, time.Now()))); reason(err) != ReasonKeysUnavailable {
		t.Fatalf("no keys reason = %q", reason(err))
	}
}
