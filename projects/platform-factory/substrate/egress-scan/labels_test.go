package main

import (
	"fmt"
	"strings"
	"testing"

	"context"

	corev3 "github.com/envoyproxy/go-control-plane/envoy/config/core/v3"
	authv3 "github.com/envoyproxy/go-control-plane/envoy/service/auth/v3"
	"github.com/prometheus/client_golang/prometheus"
	"google.golang.org/protobuf/types/known/structpb"
)

func checkWith(host, principal string, extensions map[string]string, spiffe string) *authv3.CheckRequest {
	attrs := &authv3.AttributeContext{
		Request:           &authv3.AttributeContext_Request{Http: &authv3.AttributeContext_HttpRequest{Host: host}},
		Source:            &authv3.AttributeContext_Peer{Principal: principal},
		ContextExtensions: extensions,
	}
	if spiffe != "" {
		meta, err := structpb.NewStruct(map[string]any{"spiffe": spiffe})
		if err != nil {
			panic(err)
		}
		attrs.MetadataContext = &corev3.Metadata{FilterMetadata: map[string]*structpb.Struct{metadataActor: meta}}
	}
	return &authv3.CheckRequest{Attributes: attrs}
}

func TestRouteIsTheNormalizedHostAndBounded(t *testing.T) {
	l := newLabeler([]string{"Hindsight-API.hindsight.svc.cluster.local:8888", "example.com"}, 0)
	for host, want := range map[string]string{
		"hindsight-api.hindsight.svc.cluster.local:8888":  "hindsight-api.hindsight.svc.cluster.local:8888",
		"HINDSIGHT-API.hindsight.svc.cluster.local:8888":  "hindsight-api.hindsight.svc.cluster.local:8888",
		"hindsight-api.hindsight.svc.cluster.local:8888.": labelOther,
		"example.com.":                    "example.com",
		"example.com:443":                 labelOther, // not on the list: the port is part of the value
		"unlisted.example.net":            labelOther,
		"example.com/path?token=secret":   labelOther, // never a path or a query
		"user:pass@example.com":           labelOther,
		"example.com\r\nx: y":             labelOther,
		"":                                labelOther,
		strings.Repeat("a", 300) + ".com": labelOther,
	} {
		if got := l.route(host); got != want {
			t.Errorf("route(%q) = %q, want %q", host, got, want)
		}
	}
}

// A client picks the Host header. A scanned request with a Host that is not on the
// list keeps the route name of the gateway policy, so it does not hide in other.
func TestUnlistedHostKeepsTheGatewayRouteName(t *testing.T) {
	l := newLabeler([]string{"hindsight-api.hindsight.svc.cluster.local:8888"}, 0)
	ext := map[string]string{contextRoute: "hindsight"}
	if got, _ := l.labels(checkWith("evil.example.net", "", ext, "")); got != "route:hindsight" {
		t.Errorf("route = %q, want route:hindsight", got)
	}
	if got, _ := l.labels(checkWith("evil.example.net", "", nil, "")); got != labelOther {
		t.Errorf("route without a gateway name = %q, want other", got)
	}
	if got, _ := l.labels(checkWith("hindsight-api.hindsight.svc.cluster.local:8888", "", ext, "")); got != "hindsight-api.hindsight.svc.cluster.local:8888" {
		t.Errorf("listed host route = %q", got)
	}
}

func TestRouteCapWithoutAnAllowlist(t *testing.T) {
	l := newLabeler(nil, 3)
	seen := map[string]bool{}
	for i := range 10 {
		seen[l.route(fmt.Sprintf("host%d.example.com", i))] = true
	}
	// 3 distinct values and "other": a hard cap, whatever the requests send.
	if len(seen) != 4 || !seen[labelOther] {
		t.Fatalf("distinct route values: %v", seen)
	}
	// A value already admitted stays admitted.
	if got := l.route("host0.example.com"); got != "host0.example.com" {
		t.Errorf("admitted value changed to %q", got)
	}
}

func TestSourceIsTheAtespaceNeverTheActor(t *testing.T) {
	l := newLabeler(nil, 2)
	actor := actorSPIFFEPrefix + "factory-r-homelab/homelab-codex-ea46f555b35a"
	for _, tc := range []struct {
		name string
		req  *authv3.CheckRequest
		want string
	}{
		{"actor principal", checkWith("h", actor, nil, ""), "factory-r-homelab"},
		{"another atespace", checkWith("h", actorSPIFFEPrefix+"factory-discord-agent/bosun-99fa46c84dfa", nil, ""), "factory-discord-agent"},
		{"cap reached", checkWith("h", actorSPIFFEPrefix+"third-atespace/some-actor", nil, ""), labelOther},
		{"not an actor identity", checkWith("h", "spiffe://cluster.local/ns/ate-system/sa/atenet-client", nil, ""), labelUnknown},
		{"malformed actor identity", checkWith("h", actorSPIFFEPrefix+"Bad_Space/x", nil, ""), labelUnknown},
		{"actor name only", checkWith("h", actorSPIFFEPrefix+"factory-r-homelab", nil, ""), labelUnknown},
		{"no identity", checkWith("h", "", nil, ""), labelUnknown},
	} {
		if got := l.source(tc.req.GetAttributes()); got != tc.want {
			t.Errorf("%s: source = %q, want %q", tc.name, got, tc.want)
		}
	}
	for _, v := range []string{"homelab-codex-ea46f555b35a", "bosun-99fa46c84dfa"} {
		for _, req := range []*authv3.CheckRequest{checkWith("h", actor, nil, "")} {
			if got := l.source(req.GetAttributes()); strings.Contains(got, v) {
				t.Errorf("the source label holds an actor name: %q", got)
			}
		}
	}
}

func TestSourceFromGatewayMetadataAndStaticContext(t *testing.T) {
	l := newLabeler(nil, 8)
	// The CEL metadata of the gateway policy (source.spiffeId) works like the peer
	// principal: only the atespace is kept.
	req := checkWith("h", "", nil, actorSPIFFEPrefix+"factory-r-homelab/a-codex")
	if got := l.source(req.GetAttributes()); got != "factory-r-homelab" {
		t.Errorf("metadata source = %q", got)
	}
	// The static route name of the gateway policy is the fallback.
	fallback := checkWith("h", "", map[string]string{"route": "hindsight"}, "")
	if got := l.source(fallback.GetAttributes()); got != "route:hindsight" {
		t.Errorf("static context source = %q", got)
	}
	// A client cannot choose it: only a plain name is accepted.
	hostile := checkWith("h", "", map[string]string{"route": "a b/../c\n"}, "")
	if got := l.source(hostile.GetAttributes()); got != labelUnknown {
		t.Errorf("hostile context source = %q", got)
	}
}

func TestNoLabelHoldsRequestContent(t *testing.T) {
	l := newLabeler([]string{"allowed.example.com"}, 0)
	secret := "gh" + "p_" + "8Jdm9u1Qa7bWdF2p7XZ9sWy7Qnf2gXMbRFqa"
	for _, host := range []string{"x.example.com/" + secret, "x.example.com?t=" + secret, secret + ".example.com", secret} {
		route := l.route(host)
		if strings.Contains(route, secret) {
			t.Errorf("route label holds request content for host %q", host)
		}
	}
	if got := l.route(secret + ".example.com"); got != labelOther {
		t.Errorf("an unlisted host must be other, got %q", got)
	}
}

// Checks for three hosts, one of them unlisted, land in the series of their
// route, and the unlisted one in other. The series count stays bounded.
func TestChecksAreCountedByRouteWithTheAllowlist(t *testing.T) {
	registry := prometheus.NewRegistry()
	backend := newTestScanner(t, "gitleaks", nil)
	backend.labels = newLabeler([]string{"hindsight-api.hindsight.svc.cluster.local:8888", "other-listed.example.com"}, 0)
	checked := newMeteredScanner(backend, registry)
	for _, host := range []string{
		"hindsight-api.hindsight.svc.cluster.local:8888", "HINDSIGHT-API.hindsight.svc.cluster.local:8888",
		"other-listed.example.com", "unlisted-1.example.net", "unlisted-2.example.net", "unlisted-3.example.net",
	} {
		_, err := checked.Check(context.Background(), request(&authv3.AttributeContext_HttpRequest{Method: "GET", Path: "/", Host: host}))
		if err != nil {
			t.Fatal(err)
		}
	}
	families, err := registry.Gather()
	if err != nil {
		t.Fatal(err)
	}
	got := map[string]float64{}
	series := 0
	for _, family := range families {
		if family.GetName() != "factory_egress_scan_checks_total" {
			continue
		}
		for _, sample := range family.GetMetric() {
			series++
			labels := map[string]string{}
			for _, l := range sample.GetLabel() {
				labels[l.GetName()] = l.GetValue()
			}
			if labels["outcome"] == "allowed" {
				got[labels["route"]] += sample.GetCounter().GetValue()
			}
		}
	}
	want := map[string]float64{"hindsight-api.hindsight.svc.cluster.local:8888": 2, "other-listed.example.com": 1, labelOther: 3}
	for route, n := range want {
		if got[route] != n {
			t.Errorf("allowed checks for route %q = %v, want %v (all: %v)", route, got[route], n, got)
		}
	}
	// 7 outcomes x (2 listed routes + other), one source: the cap, not the traffic.
	if series != 7*3 {
		t.Errorf("checks_total has %d series, want %d", series, 7*3)
	}
}
