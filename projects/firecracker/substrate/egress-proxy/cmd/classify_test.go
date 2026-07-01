package main

import (
	"errors"
	"log/slog"
	"net"
	"testing"
)

// Cluster service names used as test fixtures (not runtime defaults, so the
// no-hardcoded-k8s-service-url rule does not apply; annotated once here and
// referenced by name below).
const (
	svcInference = "inference.inference.svc.cluster.local" // nosemgrep: no-hardcoded-k8s-service-url
	svcK8sAPI    = "kubernetes.default.svc.cluster.local"  // nosemgrep: no-hardcoded-k8s-service-url
)

// fakeResolver returns a fixed IP set per host, or an error for unknown hosts.
func fakeResolver(table map[string][]net.IP) func(string) ([]net.IP, error) {
	return func(host string) ([]net.IP, error) {
		if ips, ok := table[host]; ok {
			return ips, nil
		}
		return nil, errors.New("no such host")
	}
}

func TestIsInternal(t *testing.T) {
	_, extra, _ := net.ParseCIDR("100.64.0.0/10") // CGNAT, not covered by stdlib predicates
	p := &proxy{extraInternalNets: []*net.IPNet{extra}, logger: slog.Default()}
	cases := []struct {
		ip   string
		want bool
	}{
		{"10.42.3.46", true},      // pod CIDR (RFC1918)
		{"10.43.0.1", true},       // service CIDR (RFC1918)
		{"192.168.1.195", true},   // node net (RFC1918)
		{"172.16.5.5", true},      // RFC1918
		{"127.0.0.1", true},       // loopback
		{"169.254.169.254", true}, // cloud metadata (link-local)
		{"0.0.0.0", true},         // unspecified
		{"fc00::1", true},         // IPv6 ULA
		{"fe80::1", true},         // IPv6 link-local
		{"100.64.1.1", true},      // configured extra CIDR
		{"140.82.112.3", false},   // public (github)
		{"8.8.8.8", false},        // public
	}
	for _, c := range cases {
		ip := net.ParseIP(c.ip)
		if ip == nil {
			t.Fatalf("bad test IP %q", c.ip)
		}
		if got := p.isInternal(ip); got != c.want {
			t.Errorf("isInternal(%s) = %v, want %v", c.ip, got, c.want)
		}
	}
}

func TestRoute(t *testing.T) {
	resolver := fakeResolver(map[string][]net.IP{
		"api.github.com":     {net.ParseIP("140.82.112.3")},
		svcInference:         {net.ParseIP("10.43.0.9")},
		svcK8sAPI:            {net.ParseIP("10.43.0.1")},
		"sneaky.example.com": {net.ParseIP("10.43.0.1")}, // public name, internal IP
	})
	base := func() *proxy {
		return &proxy{
			externalAllow:        true,
			internalDefaultAllow: false,
			internalAllowlist:    []string{svcInference + ":8080"},
			lookupIP:             resolver,
			logger:               slog.Default(),
		}
	}

	t.Run("external public allowed and pinned", func(t *testing.T) {
		p := base()
		dial, ok := p.route("api.github.com", "443")
		if !ok || dial != "140.82.112.3:443" {
			t.Fatalf("route(api.github.com:443) = (%q, %v), want (140.82.112.3:443, true)", dial, ok)
		}
	})

	t.Run("internal allowlisted allowed and pinned", func(t *testing.T) {
		p := base()
		dial, ok := p.route(svcInference, "8080")
		if !ok || dial != "10.43.0.9:8080" {
			t.Fatalf("route(inference:8080) = (%q, %v), want (10.43.0.9:8080, true)", dial, ok)
		}
	})

	t.Run("internal not allowlisted denied", func(t *testing.T) {
		p := base()
		if dial, ok := p.route(svcK8sAPI, "443"); ok {
			t.Fatalf("k8s API should be denied, got dial=%q", dial)
		}
	})

	t.Run("wrong port on allowlisted internal denied", func(t *testing.T) {
		p := base()
		if _, ok := p.route(svcInference, "9999"); ok {
			t.Fatal("wrong port on an allowlisted internal host should be denied")
		}
	})

	t.Run("public name resolving to internal IP denied (SSRF-by-name)", func(t *testing.T) {
		p := base()
		if _, ok := p.route("sneaky.example.com", "443"); ok {
			t.Fatal("a public name resolving to an internal IP must be denied")
		}
	})

	t.Run("literal internal IP denied", func(t *testing.T) {
		p := base()
		if _, ok := p.route("10.43.0.1", "443"); ok {
			t.Fatal("a literal internal IP must be denied")
		}
	})

	t.Run("literal public IP allowed", func(t *testing.T) {
		p := base()
		dial, ok := p.route("8.8.8.8", "443")
		if !ok || dial != "8.8.8.8:443" {
			t.Fatalf("route(8.8.8.8:443) = (%q, %v), want (8.8.8.8:443, true)", dial, ok)
		}
	})

	t.Run("external deny closes the open path", func(t *testing.T) {
		p := base()
		p.externalAllow = false
		if _, ok := p.route("api.github.com", "443"); ok {
			t.Fatal("external=deny should block public destinations")
		}
	})

	t.Run("internal default allow permits any internal", func(t *testing.T) {
		p := base()
		p.internalDefaultAllow = true
		if _, ok := p.route(svcK8sAPI, "443"); !ok {
			t.Fatal("internalDefaultAllow should permit any internal destination")
		}
	})

	t.Run("unresolvable host denied", func(t *testing.T) {
		p := base()
		if _, ok := p.route("does-not-exist.invalid", "443"); ok {
			t.Fatal("an unresolvable host must be denied")
		}
	})
}
