package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"path"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	jose "github.com/go-jose/go-jose/v4"
	"github.com/go-jose/go-jose/v4/jwt"
	"golang.org/x/time/rate"
)

const (
	issuer   = "https://factory-auth.jomcgi.dev/application/o/factory-explore/"
	audience = "factory-explore"
)

type verifier struct {
	jwksURL string
	sync.Mutex
	keys             jose.JSONWebKeySet
	refresh, expires time.Time
}

func (v *verifier) subject(raw string) (string, error) {
	token, err := jwt.ParseSigned(raw, []jose.SignatureAlgorithm{jose.RS256})
	if err != nil {
		return "", err
	}
	kid := token.Headers[0].KeyID
	if kid == "" {
		return "", errors.New("missing kid")
	}
	v.Lock()
	now := time.Now()
	keys := v.keys.Key(kid)
	if (len(keys) == 0 || !now.Before(v.expires)) && !now.Before(v.refresh) {
		v.refresh = now.Add(10 * time.Second)
		client := http.Client{Timeout: 4 * time.Second}
		resp, e := client.Get(v.jwksURL)
		if e == nil {
			defer resp.Body.Close()
			var set jose.JSONWebKeySet
			if resp.StatusCode == 200 && json.NewDecoder(resp.Body).Decode(&set) == nil {
				v.keys = set
				v.expires = now.Add(5 * time.Minute)
				keys = set.Key(kid)
			}
		}
	}
	valid := time.Now().Before(v.expires)
	v.Unlock()
	// Mirror the factory-explore issuer rules in factory-cluster/nodes/authentication-config.yaml.
	// All accepted identities receive the same group-only explorer RBAC grants.
	var identity struct {
		Username string   `json:"preferred_username"`
		Issued   *float64 `json:"iat"`
		Expires  *float64 `json:"exp"`
	}
	var c jwt.Claims
	if !valid || len(keys) != 1 || token.Claims(keys[0].Key, &c, &identity) != nil || identity.Username == "" || identity.Issued == nil || identity.Expires == nil || *identity.Expires-*identity.Issued > 600 || c.Expiry == nil || c.Subject == "" || c.ValidateWithLeeway(jwt.Expected{Issuer: issuer, AnyAudience: jwt.Audience{audience}, Time: time.Now()}, 0) != nil {
		return "", errors.New("invalid token")
	}
	return c.Subject, nil
}

type (
	window struct {
		start   time.Time
		rate    *rate.Limiter
		watches int
	}
	proxy struct {
		sync.Mutex
		cache            readCache
		users            map[string]*window
		nextSweep        time.Time
		watches, reads   int
		now              func() time.Time
		subject          func(string) (string, error)
		upstream         http.Handler
		passed, rejected atomic.Uint64
	}
)

func allowed(method, p string) bool {
	if method == "GET" {
		return true
	}
	if method != "POST" {
		return false
	}
	switch p {
	case "/apis/authentication.k8s.io/v1/selfsubjectreviews", "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews", "/apis/authorization.k8s.io/v1/selfsubjectrulesreviews":
		return true
	}
	return false
}

func (p *proxy) deny(w http.ResponseWriter, code int, reason string) {
	p.rejected.Add(1)
	if code == 429 {
		w.Header().Set("Retry-After", "1")
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(map[string]any{"apiVersion": "v1", "kind": "Status", "status": "Failure", "reason": reason, "code": code})
}

func (p *proxy) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	now := p.now()
	w.Header().Set("X-Explore-Window-End", now.UTC().Format(time.RFC3339))
	auth := strings.Fields(r.Header.Get("Authorization"))
	if len(auth) != 2 || !strings.EqualFold(auth[0], "Bearer") {
		p.deny(w, 401, "Unauthorized")
		return
	}
	sub, err := p.subject(auth[1])
	if err != nil {
		p.deny(w, 401, "Unauthorized")
		return
	}
	p.Lock()
	now = p.now()
	if !now.Before(p.nextSweep) {
		for id, u := range p.users {
			if !now.Before(u.start.Add(time.Hour)) {
				delete(p.users, id)
			}
		}
		p.nextSweep = now.Add(time.Minute)
	}
	u := p.users[sub]
	if u == nil || !now.Before(u.start.Add(time.Hour)) {
		u = &window{start: now, rate: rate.NewLimiter(10, 40)}
		p.users[sub] = u
	}
	end := u.start.Add(10 * time.Minute)
	w.Header().Set("X-Explore-Window-End", end.UTC().Format(time.RFC3339))
	if !now.Before(end) {
		p.Unlock()
		w.Header().Set("X-Explore-Next-Window", u.start.Add(time.Hour).UTC().Format(time.RFC3339))
		p.deny(w, 403, "Forbidden")
		return
	}
	if !u.rate.AllowN(now, 1) {
		p.Unlock()
		p.deny(w, 429, "TooManyRequests")
		return
	}
	if !allowed(r.Method, r.URL.Path) || r.Header.Get("Upgrade") != "" || r.URL.RawPath != "" || path.Clean(r.URL.Path) != strings.TrimSuffix(r.URL.Path, "/") && r.URL.Path != "/" {
		p.Unlock()
		p.deny(w, 403, "Forbidden")
		return
	}
	q, err := url.ParseQuery(r.URL.RawQuery)
	if err != nil || len(q["watch"]) > 1 {
		p.Unlock()
		p.deny(w, 400, "BadRequest")
		return
	}
	watch := strings.Contains(r.URL.Path, "/watch/") || q.Has("watch") && q.Get("watch") != "false" && q.Get("watch") != "0"
	p.Unlock()
	// Bound writes to a stalled downstream as well as the upstream request.
	controller := http.NewResponseController(w)
	if controller.SetWriteDeadline(end) == nil {
		defer controller.SetWriteDeadline(time.Time{})
	}
	ctx, cancel := context.WithDeadline(r.Context(), end)
	defer cancel()
	r = r.WithContext(ctx)
	if !watch && r.Method == http.MethodGet {
		expired := func() bool {
			if p.now().Before(end) && time.Now().Before(end) {
				return false
			}
			_ = controller.SetWriteDeadline(time.Time{})
			w.Header().Set("X-Explore-Next-Window", u.start.Add(time.Hour).UTC().Format(time.RFC3339))
			p.deny(w, http.StatusForbidden, "Forbidden")
			return true
		}
		p.cache.serve(w, r, p.now, expired, func(out http.ResponseWriter) { p.forward(out, r, u, false) })
		return
	}
	p.forward(w, r, u, watch)
}

func (p *proxy) forward(w http.ResponseWriter, r *http.Request, u *window, watch bool) {
	p.Lock()
	if watch && (u.watches >= 8 || p.watches >= 200) || !watch && p.reads >= 32 {
		p.Unlock()
		p.deny(w, 429, "TooManyRequests")
		return
	}
	if watch {
		u.watches++
		p.watches++
	} else {
		p.reads++
	}
	p.Unlock()
	defer func() {
		p.Lock()
		defer p.Unlock()
		if watch {
			u.watches--
			p.watches--
		} else {
			p.reads--
		}
	}()
	p.passed.Add(1)
	p.upstream.ServeHTTP(w, r)
}

func apiProxy(target *url.URL, transport http.RoundTripper) http.Handler {
	return &httputil.ReverseProxy{Transport: transport, FlushInterval: -1, Rewrite: func(r *httputil.ProxyRequest) {
		r.SetURL(target)
		r.Out.Header = make(http.Header)
		for _, key := range []string{"Authorization", "Accept", "Content-Type", "User-Agent"} {
			r.Out.Header.Set(key, r.In.Header.Get(key))
		}
	}}
}

func main() {
	ca, err := os.ReadFile("/etc/kubernetes/ca.crt")
	if err != nil {
		log.Fatal(err)
	}
	roots := x509.NewCertPool()
	if !roots.AppendCertsFromPEM(ca) {
		log.Fatal("invalid Kubernetes CA")
	}
	target, _ := url.Parse("https://kubernetes.default.svc")
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil
	transport.TLSClientConfig = &tls.Config{RootCAs: roots, MinVersion: tls.VersionTLS12}
	p := &proxy{users: make(map[string]*window), now: time.Now, subject: (&verifier{jwksURL: issuer + "jwks/"}).subject, upstream: apiProxy(target, transport)}
	mux := http.NewServeMux()
	mux.HandleFunc("/metrics", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain; version=0.0.4")
		fmt.Fprintf(w, "explore_proxy_forwarded_total %d\nexplore_proxy_rejected_total %d\n", p.passed.Load(), p.rejected.Load())
		p.cache.metrics(w)
	})
	go func() { log.Fatal(http.ListenAndServe(":9090", mux)) }()
	server := http.Server{Addr: ":8080", Handler: p, ReadHeaderTimeout: 5 * time.Second, IdleTimeout: time.Minute}
	log.Fatal(server.ListenAndServe())
}
