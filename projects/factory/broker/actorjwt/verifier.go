// Package actorjwt verifies the actor JWTs Substrate's egress gateway injects
// for kagent ModelConfigs with actorJwt, against the issuer's published keys.
// See the kagent-dev/substrate guide docs/guides/actor-jwts.md.
package actorjwt

import (
	"context"
	"crypto"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"math/big"
	"net/http"
	"net/url"
	"os"
	"strings"
	"sync"
	"time"
)

// Identity is the actor a verified token names.
type Identity struct {
	Subject   string
	Atespace  string
	ActorName string
	ActorUID  string
}

// Error is a rejected token. Reason is a small fixed set, safe as a metric
// label; it never carries token bytes.
type Error struct{ Reason string }

func (e *Error) Error() string { return "actor JWT rejected: " + e.Reason }

const (
	ReasonMalformed       = "malformed"
	ReasonAlgorithm       = "algorithm"
	ReasonUnknownKey      = "unknown_key"
	ReasonSignature       = "signature"
	ReasonIssuer          = "issuer"
	ReasonAudience        = "audience"
	ReasonExpired         = "expired"
	ReasonNotYetValid     = "not_yet_valid"
	ReasonSubject         = "subject"
	ReasonAtespace        = "atespace"
	ReasonKeysUnavailable = "keys_unavailable"
)

func reject(reason string) error { return &Error{Reason: reason} }

// clockSkew tolerates small clock differences; Substrate already sets nbf
// five minutes before iat.
const clockSkew = 30 * time.Second

// fetchTimeout bounds one discovery and key set fetch.
const fetchTimeout = 10 * time.Second

// maxTokenBytes bounds work done on an unauthenticated header.
const maxTokenBytes = 16 << 10

type Options struct {
	Issuer   string
	Audience string
	// CAFile verifies the issuer's certificate; empty uses system roots.
	CAFile string
	// HTTPClient overrides the client built from CAFile (tests).
	HTTPClient *http.Client
	// RefreshInterval refreshes keys in Run.
	RefreshInterval time.Duration
	// MinRefetch is the least time between key set fetches that an unknown
	// kid may trigger, so invalid tokens cannot overload the issuer.
	MinRefetch       time.Duration
	AllowedAtespaces []string
	Logger           *slog.Logger
	// Now overrides the clock (tests).
	Now func() time.Time
}

type publicKey struct {
	alg string
	key crypto.PublicKey
}

type Verifier struct {
	opts    Options
	client  *http.Client
	allowed map[string]bool

	fetchMu sync.Mutex // one key set fetch at a time

	mu        sync.RWMutex
	keys      map[string]publicKey
	lastFetch time.Time
}

func New(opts Options) (*Verifier, error) {
	if opts.Issuer == "" || opts.Audience == "" {
		return nil, errors.New("actor JWT issuer and audience are required")
	}
	if opts.Now == nil {
		opts.Now = time.Now
	}
	if opts.Logger == nil {
		opts.Logger = slog.Default()
	}
	if opts.RefreshInterval <= 0 {
		opts.RefreshInterval = 5 * time.Minute
	}
	if opts.MinRefetch <= 0 {
		opts.MinRefetch = 30 * time.Second
	}
	client := opts.HTTPClient
	if client == nil {
		tlsConfig := &tls.Config{MinVersion: tls.VersionTLS12}
		if opts.CAFile != "" {
			pem, err := os.ReadFile(opts.CAFile)
			if err != nil {
				return nil, fmt.Errorf("read actor JWT CA: %w", err)
			}
			pool := x509.NewCertPool()
			if !pool.AppendCertsFromPEM(pem) {
				return nil, errors.New("actor JWT CA file holds no certificates")
			}
			tlsConfig.RootCAs = pool
		}
		transport := http.DefaultTransport.(*http.Transport).Clone()
		transport.TLSClientConfig = tlsConfig
		client = &http.Client{
			Transport: transport, Timeout: 10 * time.Second,
			CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
		}
	}
	allowed := map[string]bool{}
	for _, a := range opts.AllowedAtespaces {
		allowed[a] = true
	}
	return &Verifier{opts: opts, client: client, allowed: allowed}, nil
}

// Run refreshes the key set every RefreshInterval until ctx ends, so keys
// published ahead of rotation arrive without an unknown-kid refetch.
func (v *Verifier) Run(ctx context.Context) {
	if err := v.Refresh(ctx); err != nil {
		v.opts.Logger.Error("actor JWT key set fetch failed", "err", err)
	}
	t := time.NewTicker(v.opts.RefreshInterval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			if err := v.Refresh(ctx); err != nil {
				v.opts.Logger.Error("actor JWT key set refresh failed", "err", err)
			}
		}
	}
}

// Refresh fetches discovery and the key set. On failure the previous keys
// stay in use.
func (v *Verifier) Refresh(ctx context.Context) error {
	v.fetchMu.Lock()
	defer v.fetchMu.Unlock()
	return v.fetchLocked(ctx)
}

func (v *Verifier) fetchLocked(ctx context.Context) error {
	v.mu.Lock()
	v.lastFetch = v.opts.Now()
	v.mu.Unlock()
	var discovery struct {
		Issuer  string `json:"issuer"`
		JWKSURI string `json:"jwks_uri"`
	}
	if err := v.getJSON(ctx, strings.TrimRight(v.opts.Issuer, "/")+"/.well-known/openid-configuration", &discovery); err != nil {
		return fmt.Errorf("discovery: %w", err)
	}
	if discovery.Issuer != v.opts.Issuer {
		return errors.New("discovery issuer does not match the configured issuer")
	}
	if u, err := url.Parse(discovery.JWKSURI); err != nil || u.Scheme != "https" || u.Host == "" {
		return errors.New("discovery jwks_uri is not an https URL")
	}
	var set struct {
		Keys []jsonKey `json:"keys"`
	}
	if err := v.getJSON(ctx, discovery.JWKSURI, &set); err != nil {
		return fmt.Errorf("key set: %w", err)
	}
	keys := make(map[string]publicKey, len(set.Keys))
	for _, k := range set.Keys {
		parsed, err := k.parse()
		if err != nil {
			v.opts.Logger.Warn("actor JWT key skipped", "kid", k.Kid, "err", err)
			continue
		}
		keys[k.Kid] = parsed
	}
	if len(keys) == 0 {
		return errors.New("key set has no usable keys")
	}
	v.mu.Lock()
	v.keys = keys
	v.mu.Unlock()
	return nil
}

func (v *Verifier) getJSON(ctx context.Context, rawURL string, out any) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, rawURL, nil)
	if err != nil {
		return err
	}
	resp, err := v.client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	return json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(out)
}

// key returns the key for kid, refetching the key set at most once per
// MinRefetch when kid is unknown.
func (v *Verifier) key(ctx context.Context, kid string) (publicKey, error) {
	v.mu.RLock()
	k, ok := v.keys[kid]
	v.mu.RUnlock()
	if ok {
		return k, nil
	}
	v.fetchMu.Lock()
	defer v.fetchMu.Unlock()
	v.mu.RLock()
	k, ok = v.keys[kid]
	last, loaded := v.lastFetch, v.keys != nil
	v.mu.RUnlock()
	if ok {
		return k, nil
	}
	if !last.IsZero() && v.opts.Now().Sub(last) < v.opts.MinRefetch {
		if !loaded {
			return publicKey{}, reject(ReasonKeysUnavailable)
		}
		return publicKey{}, reject(ReasonUnknownKey)
	}
	// Detached from the request: a cancelled caller must neither abort
	// the fetch nor spend the refetch slot on nothing.
	fetchCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), fetchTimeout)
	err := v.fetchLocked(fetchCtx)
	cancel()
	if err != nil {
		v.opts.Logger.Error("actor JWT key set fetch failed", "err", err)
	}
	v.mu.RLock()
	k, ok = v.keys[kid]
	loaded = v.keys != nil
	v.mu.RUnlock()
	switch {
	case ok:
		return k, nil
	case !loaded:
		return publicKey{}, reject(ReasonKeysUnavailable)
	default:
		return publicKey{}, reject(ReasonUnknownKey)
	}
}

type header struct {
	Alg string `json:"alg"`
	Kid string `json:"kid"`
	Typ string `json:"typ"`
}

type substrateClaims struct {
	Atespace  string `json:"atespace"`
	ActorName string `json:"actorName"`
	ActorUID  string `json:"actorUID"`
}

type claims struct {
	Issuer         string           `json:"iss"`
	Subject        string           `json:"sub"`
	Audience       json.RawMessage  `json:"aud"`
	Expiry         *float64         `json:"exp"`
	NotBefore      *float64         `json:"nbf"`
	Actor          *substrateClaims `json:"actor.ate.dev"`
	WorkerForActor json.RawMessage  `json:"worker-for-actor.ate.dev"`
}

// Verify checks the token's signature, issuer, audience, lifetime and actor
// subject. Worker-for-actor tokens share the signing keys and are rejected.
func (v *Verifier) Verify(ctx context.Context, raw string) (Identity, error) {
	if len(raw) == 0 || len(raw) > maxTokenBytes {
		return Identity{}, reject(ReasonMalformed)
	}
	parts := strings.Split(raw, ".")
	if len(parts) != 3 {
		return Identity{}, reject(ReasonMalformed)
	}
	var h header
	if err := decodeSegment(parts[0], &h); err != nil {
		return Identity{}, reject(ReasonMalformed)
	}
	if h.Alg != "ES256" && h.Alg != "RS256" {
		return Identity{}, reject(ReasonAlgorithm)
	}
	sig, err := base64.RawURLEncoding.DecodeString(parts[2])
	if err != nil {
		return Identity{}, reject(ReasonMalformed)
	}
	key, err := v.key(ctx, h.Kid)
	if err != nil {
		return Identity{}, err
	}
	if key.alg != h.Alg {
		return Identity{}, reject(ReasonAlgorithm)
	}
	digest := sha256.Sum256([]byte(parts[0] + "." + parts[1]))
	if !verifySignature(key, digest[:], sig) {
		return Identity{}, reject(ReasonSignature)
	}
	var c claims
	if err := decodeSegment(parts[1], &c); err != nil {
		return Identity{}, reject(ReasonMalformed)
	}
	if c.Issuer != v.opts.Issuer {
		return Identity{}, reject(ReasonIssuer)
	}
	if !audienceContains(c.Audience, v.opts.Audience) {
		return Identity{}, reject(ReasonAudience)
	}
	now := v.opts.Now()
	if c.Expiry == nil || !now.Before(unix(*c.Expiry).Add(clockSkew)) {
		return Identity{}, reject(ReasonExpired)
	}
	if c.NotBefore != nil && now.Add(clockSkew).Before(unix(*c.NotBefore)) {
		return Identity{}, reject(ReasonNotYetValid)
	}
	if len(c.WorkerForActor) != 0 || c.Actor == nil || c.Actor.Atespace == "" || c.Actor.ActorName == "" ||
		c.Subject != "actor/"+c.Actor.Atespace+"/"+c.Actor.ActorName {
		return Identity{}, reject(ReasonSubject)
	}
	if len(v.allowed) != 0 && !v.allowed[c.Actor.Atespace] {
		return Identity{}, reject(ReasonAtespace)
	}
	return Identity{Subject: c.Subject, Atespace: c.Actor.Atespace, ActorName: c.Actor.ActorName, ActorUID: c.Actor.ActorUID}, nil
}

func unix(seconds float64) time.Time {
	return time.Unix(0, 0).Add(time.Duration(seconds * float64(time.Second)))
}

func decodeSegment(segment string, out any) error {
	b, err := base64.RawURLEncoding.DecodeString(segment)
	if err != nil {
		return err
	}
	return json.Unmarshal(b, out)
}

func audienceContains(raw json.RawMessage, want string) bool {
	var list []string
	if err := json.Unmarshal(raw, &list); err != nil {
		var one string
		if json.Unmarshal(raw, &one) != nil {
			return false
		}
		list = []string{one}
	}
	for _, a := range list {
		if a == want {
			return true
		}
	}
	return false
}

func verifySignature(k publicKey, digest, sig []byte) bool {
	switch pub := k.key.(type) {
	case *ecdsa.PublicKey:
		if len(sig) != 64 {
			return false
		}
		r, s := new(big.Int).SetBytes(sig[:32]), new(big.Int).SetBytes(sig[32:])
		return ecdsa.Verify(pub, digest, r, s)
	case *rsa.PublicKey:
		return rsa.VerifyPKCS1v15(pub, crypto.SHA256, digest, sig) == nil
	}
	return false
}

type jsonKey struct {
	Kty string `json:"kty"`
	Kid string `json:"kid"`
	Use string `json:"use"`
	Alg string `json:"alg"`
	Crv string `json:"crv"`
	X   string `json:"x"`
	Y   string `json:"y"`
	N   string `json:"n"`
	E   string `json:"e"`
}

func (k jsonKey) parse() (publicKey, error) {
	if k.Kid == "" {
		return publicKey{}, errors.New("no kid")
	}
	if k.Use != "" && k.Use != "sig" {
		return publicKey{}, errors.New("not a signing key")
	}
	switch k.Kty {
	case "EC":
		if k.Crv != "P-256" || (k.Alg != "" && k.Alg != "ES256") {
			return publicKey{}, errors.New("unsupported EC key")
		}
		x, errX := base64.RawURLEncoding.DecodeString(k.X)
		y, errY := base64.RawURLEncoding.DecodeString(k.Y)
		if errX != nil || errY != nil || len(x) != 32 || len(y) != 32 {
			return publicKey{}, errors.New("invalid EC coordinates")
		}
		point := append(append([]byte{4}, x...), y...)
		pub, err := ecdsa.ParseUncompressedPublicKey(elliptic.P256(), point)
		if err != nil {
			return publicKey{}, err
		}
		return publicKey{alg: "ES256", key: pub}, nil
	case "RSA":
		if k.Alg != "" && k.Alg != "RS256" {
			return publicKey{}, errors.New("unsupported RSA algorithm")
		}
		n, errN := base64.RawURLEncoding.DecodeString(k.N)
		e, errE := base64.RawURLEncoding.DecodeString(k.E)
		if errN != nil || errE != nil || len(e) == 0 || len(e) > 4 {
			return publicKey{}, errors.New("invalid RSA key")
		}
		pub := &rsa.PublicKey{N: new(big.Int).SetBytes(n), E: int(new(big.Int).SetBytes(e).Int64())}
		if pub.N.BitLen() < 2048 || pub.E < 3 {
			return publicKey{}, errors.New("weak RSA key")
		}
		return publicKey{alg: "RS256", key: pub}, nil
	}
	return publicKey{}, fmt.Errorf("unsupported key type %q", k.Kty)
}
