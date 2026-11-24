package main

import (
	"regexp"
	"strings"
	"sync"

	authv3 "github.com/envoyproxy/go-control-plane/envoy/service/auth/v3"
)

// Metric label values come from untrusted requests (the Host header) and from
// peers (a SPIFFE ID). A label with an unbounded value set would grow the series
// of every metric that carries it, so each value passes a guard: an allowlist, or
// a hard cap on the number of distinct values, and everything else is "other".
// A label never holds request content: no path, query, header value or match.

const (
	labelOther   = "other"
	labelUnknown = "unknown"
	// defaultMaxLabelValues is the cap on distinct values of one label when no
	// allowlist is set.
	defaultMaxLabelValues = 32
	// actorSPIFFEPrefix is the identity of a Substrate actor's worker:
	// spiffe://substrate-actor.local/worker-for-actor/<atespace>/<name>. Only the
	// atespace is kept. The actor name is per actor and never a label.
	actorSPIFFEPrefix = "spiffe://substrate-actor.local/worker-for-actor/"
	// ContextRoute is the key of the static per-route context that the gateway
	// policy sends (context_extensions). It is set by the gateway, not by a client.
	contextRoute = "route"
	// metadataActor is the key of the CEL metadata of the gateway policy.
	metadataActor = "actor"
)

var (
	hostPattern = regexp.MustCompile(`^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?(:[0-9]{1,5})?$`)
	namePattern = regexp.MustCompile(`^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$`)
)

// labeler turns a CheckRequest into the bounded route and source label values.
type labeler struct {
	// routes is the allowlist of destination host[:port] values. Empty: a cap on
	// distinct values instead.
	routes map[string]bool
	max    int

	mu      sync.Mutex
	routeN  map[string]bool
	sourceN map[string]bool
}

func newLabeler(routes []string, max int) *labeler {
	if max <= 0 {
		max = defaultMaxLabelValues
	}
	l := &labeler{max: max, routeN: map[string]bool{}, sourceN: map[string]bool{}}
	if len(routes) > 0 {
		l.routes = map[string]bool{}
		for _, r := range routes {
			if r = normalizeHost(r); r != "" {
				l.routes[r] = true
			}
		}
	}
	return l
}

// routeValues lists the values to create series for at start.
func (l *labeler) routeValues() []string {
	values := []string{labelOther}
	for r := range l.routes {
		values = append(values, r)
	}
	return values
}

// labels returns the route and source values of one check.
func (l *labeler) labels(check *authv3.CheckRequest) (route, source string) {
	if l == nil {
		return labelOther, labelUnknown
	}
	attrs := check.GetAttributes()
	route = l.route(attrs.GetRequest().GetHttp().GetHost())
	if route == labelOther {
		// The Host header comes from the client. A scanned request with an
		// unexpected Host still carries the route name that the gateway set.
		if name := attrs.GetContextExtensions()[contextRoute]; namePattern.MatchString(name) {
			route = l.admit(l.routeN, "route:"+name)
		}
	}
	source = l.source(attrs)
	return route, source
}

// route is the normalized destination host[:port]: lower case, no path, no
// query, no user information.
func (l *labeler) route(host string) string {
	host = normalizeHost(host)
	if host == "" {
		return labelOther
	}
	if l.routes != nil {
		if l.routes[host] {
			return host
		}
		return labelOther
	}
	return l.admit(l.routeN, host)
}

// source is the atespace of the actor when the gateway gives it, else the static
// route name of the gateway policy, else "unknown". Never an actor name.
func (l *labeler) source(attrs *authv3.AttributeContext) string {
	if atespace := atespaceOf(attrs.GetSource().GetPrincipal()); atespace != "" {
		return l.admit(l.sourceN, atespace)
	}
	if meta := attrs.GetMetadataContext().GetFilterMetadata()[metadataActor]; meta != nil {
		if v := meta.GetFields()["spiffe"]; v != nil {
			if atespace := atespaceOf(v.GetStringValue()); atespace != "" {
				return l.admit(l.sourceN, atespace)
			}
		}
	}
	if name := attrs.GetContextExtensions()[contextRoute]; namePattern.MatchString(name) {
		return l.admit(l.sourceN, "route:"+name)
	}
	return labelUnknown
}

func (l *labeler) admit(seen map[string]bool, value string) string {
	l.mu.Lock()
	defer l.mu.Unlock()
	if seen[value] {
		return value
	}
	if len(seen) >= l.max {
		return labelOther
	}
	seen[value] = true
	return value
}

// atespaceOf returns the atespace of an actor's worker SPIFFE ID, or "".
func atespaceOf(spiffeID string) string {
	rest, ok := strings.CutPrefix(spiffeID, actorSPIFFEPrefix)
	if !ok {
		return ""
	}
	atespace, name, ok := strings.Cut(rest, "/")
	if !ok || !namePattern.MatchString(atespace) || !namePattern.MatchString(name) {
		return ""
	}
	return atespace
}

// normalizeHost lower-cases a host[:port], drops a trailing dot, and returns ""
// for anything that is not a plain host[:port] (a path, user information, a
// space, a control byte, an over-long value).
func normalizeHost(host string) string {
	host = strings.ToLower(strings.TrimSpace(host))
	hostOnly, port, hasPort := strings.Cut(host, ":")
	hostOnly = strings.TrimSuffix(hostOnly, ".")
	if hasPort {
		host = hostOnly + ":" + port
	} else {
		host = hostOnly
	}
	if len(host) > 255 || !hostPattern.MatchString(host) {
		return ""
	}
	return host
}
