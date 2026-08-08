// Package snapshot translates a control-plane desired-state document into the
// go-control-plane resource types the ADS server serves to node Envoys.
//
// The sidecar holds NO durable state and makes NO decisions: the control plane
// PUTs a full desired-state document (never a delta) and this package renders it
// into CDS/RDS/EDS resources (and, since R4, LDS TCP-proxy listeners) that are
// swapped atomically into the snapshot cache. The node Envoy's HTTP listener +
// connection manager stay STATIC in the bootstrap (byte-identical to R3); the
// only listeners rendered here are the R4 stateful TCP-proxy listeners the
// control plane publishes per workload, served dynamically over LDS on the same
// ADS stream. A document with no `listeners` renders
// exactly the R3 three-type snapshot (proven by a regression test). Keeping the
// translation pure (stdlib + go-control-plane only) makes it unit-testable on a
// workstation without an Envoy or a running server.
package snapshot

import (
	"errors"
	"fmt"
	"time"

	clusterv3 "github.com/envoyproxy/go-control-plane/envoy/config/cluster/v3"
	corev3 "github.com/envoyproxy/go-control-plane/envoy/config/core/v3"
	endpointv3 "github.com/envoyproxy/go-control-plane/envoy/config/endpoint/v3"
	listenerv3 "github.com/envoyproxy/go-control-plane/envoy/config/listener/v3"
	routev3 "github.com/envoyproxy/go-control-plane/envoy/config/route/v3"
	tcpproxyv3 "github.com/envoyproxy/go-control-plane/envoy/extensions/filters/network/tcp_proxy/v3"
	cachetypes "github.com/envoyproxy/go-control-plane/pkg/cache/types"
	cachev3 "github.com/envoyproxy/go-control-plane/pkg/cache/v3"
	resourcev3 "github.com/envoyproxy/go-control-plane/pkg/resource/v3"
	"google.golang.org/protobuf/types/known/anypb"
	"google.golang.org/protobuf/types/known/durationpb"
	"google.golang.org/protobuf/types/known/wrapperspb"
)

// routeConfigName is the single RouteConfiguration name the static node-Envoy
// bootstrap references over RDS. The bootstrap HTTP connection manager is wired
// with rds.route_config_name = this constant (Task 6), so every route the
// control plane PUTs lands in one RouteConfiguration served under this name.
const routeConfigName = "embervm-serving"

// defaultConnectTimeout is applied to a cluster whose ConnectTimeoutMs is unset
// or non-positive. Envoy rejects a cluster with a zero connect_timeout, so a
// desired-state document that omits it still yields a valid cluster.
const defaultConnectTimeout = 5 * time.Second

// Desired is the full desired-state document the control plane PUTs to the
// sidecar for one Envoy node. It is decoded straight from the request JSON; the
// field tags are the wire contract with the (PR-4) Elixir publisher.
type Desired struct {
	// Version is a caller-supplied monotonic string (the control plane's own
	// counter). go-control-plane treats the snapshot version opaquely; a strictly
	// increasing value guarantees a re-push after a control-plane restart (with a
	// higher counter) always converges Envoy off its last-ACKed config.
	Version string `json:"version"`

	Clusters []Cluster `json:"clusters"`
	Routes   []Route   `json:"routes"`

	// Listeners are the R4 stateful TCP-proxy listeners, one per stateful
	// workload, served over LDS. Absent/empty on a document that carries only the
	// R3 serving surface, in which case no Listener resource is rendered and the
	// snapshot is the byte-identical R3 three-type shape.
	Listeners []Listener `json:"listeners,omitempty"`
}

// Cluster is one upstream: a name, its serving-VM endpoints, and a connect
// timeout. Endpoints are served as a separate EDS resource (ClusterLoadAssignment
// keyed by the cluster name), so endpoint churn does not re-push the CDS entry.
type Cluster struct {
	Name             string     `json:"name"`
	Endpoints        []Endpoint `json:"endpoints"`
	ConnectTimeoutMs int        `json:"connect_timeout_ms"`
}

// Endpoint is one serving-VM tap IP + port. In v1 these are node-local routable
// tap addresses the node Envoy dials directly over pod networking (PR-2 bridge).
type Endpoint struct {
	IP   string `json:"ip"`
	Port int    `json:"port"`
}

// Route maps an inbound host + path prefix to a cluster, optionally injecting a
// fixed set of request headers on match (the seam the control plane later uses
// for per-tenant / per-function routing metadata).
type Route struct {
	Host           string            `json:"host"`
	PathPrefix     string            `json:"path_prefix"`
	Cluster        string            `json:"cluster"`
	RequestHeaders map[string]string `json:"request_headers"`
}

// Listener is one R4 stateful TCP-proxy listener: a name, the pod port it binds
// on the node Envoy, and the cluster it proxies raw TCP bytes to. Opaque L4 (no
// routing, no protocol awareness): the listener port IS the workload identity
// (decision 5), and the cluster (state|<workload>) resolves to the live VM or,
// when empty, the control-plane TCP activator (the fallback endpoint the
// publisher installs). One listener per stateful workload.
type Listener struct {
	Name    string `json:"name"`
	Port    int    `json:"port"`
	Cluster string `json:"cluster"`
}

// Build validates the desired-state document and renders it into a
// go-control-plane snapshot carrying CDS + EDS + RDS resources under the
// caller-supplied version. A malformed document returns an error (the HTTP layer
// maps this to a 400); the snapshot is never partially applied.
func Build(d *Desired) (*cachev3.Snapshot, error) {
	if err := d.validate(); err != nil {
		return nil, err
	}

	clusters := make([]cachetypes.Resource, 0, len(d.Clusters))
	endpoints := make([]cachetypes.Resource, 0, len(d.Clusters))
	for i := range d.Clusters {
		c := &d.Clusters[i]
		clusters = append(clusters, buildCluster(c))
		endpoints = append(endpoints, buildEndpoint(c))
	}

	routeConfig := buildRouteConfig(d.Routes)

	// LDS TCP-proxy listeners (R4). Empty for a document with no `listeners`, in
	// which case the ListenerType slice is empty and Envoy keeps its static
	// bootstrap listeners untouched (the byte-identical R3 path).
	listeners := make([]cachetypes.Resource, 0, len(d.Listeners))
	for i := range d.Listeners {
		l, err := buildListener(&d.Listeners[i])
		if err != nil {
			return nil, fmt.Errorf("listener[%d] %q: %w", i, d.Listeners[i].Name, err)
		}
		listeners = append(listeners, l)
	}

	// One RouteConfiguration resource, named for the bootstrap RDS reference.
	return cachev3.NewSnapshot(d.Version, map[resourcev3.Type][]cachetypes.Resource{
		resourcev3.ClusterType:  clusters,
		resourcev3.EndpointType: endpoints,
		resourcev3.RouteType:    {routeConfig},
		resourcev3.ListenerType: listeners,
	})
}

// validate enforces the desired-state invariants translation depends on:
// a version string, unique non-empty cluster names, endpoints with a host and a
// port in range, and routes whose cluster references resolve to a defined
// cluster. These are the errors a mis-built control-plane document produces; the
// HTTP layer returns them as 400s so a bad PUT never swaps a broken snapshot in.
func (d *Desired) validate() error {
	if d.Version == "" {
		return errors.New("version is required")
	}

	seen := make(map[string]struct{}, len(d.Clusters))
	for i := range d.Clusters {
		c := &d.Clusters[i]
		if c.Name == "" {
			return fmt.Errorf("cluster[%d]: name is required", i)
		}
		if _, dup := seen[c.Name]; dup {
			return fmt.Errorf("cluster[%d]: duplicate cluster name %q", i, c.Name)
		}
		seen[c.Name] = struct{}{}
		for j := range c.Endpoints {
			e := &c.Endpoints[j]
			if e.IP == "" {
				return fmt.Errorf("cluster[%d].endpoints[%d]: ip is required", i, j)
			}
			if e.Port < 1 || e.Port > 65535 {
				return fmt.Errorf("cluster[%d].endpoints[%d]: port %d out of range", i, j, e.Port)
			}
		}
	}

	for i := range d.Routes {
		r := &d.Routes[i]
		if r.Host == "" {
			return fmt.Errorf("route[%d]: host is required", i)
		}
		if r.Cluster == "" {
			return fmt.Errorf("route[%d]: cluster is required", i)
		}
		if _, ok := seen[r.Cluster]; !ok {
			return fmt.Errorf("route[%d]: references undefined cluster %q", i, r.Cluster)
		}
	}

	// Listeners: a name, a bind port in range, and a cluster reference that
	// resolves to a defined cluster (which may carry zero endpoints, the
	// activator-fallback case). Listener names must be unique so two listeners
	// never collide on the LDS resource name.
	lseen := make(map[string]struct{}, len(d.Listeners))
	for i := range d.Listeners {
		l := &d.Listeners[i]
		if l.Name == "" {
			return fmt.Errorf("listener[%d]: name is required", i)
		}
		if _, dup := lseen[l.Name]; dup {
			return fmt.Errorf("listener[%d]: duplicate listener name %q", i, l.Name)
		}
		lseen[l.Name] = struct{}{}
		if l.Port < 1 || l.Port > 65535 {
			return fmt.Errorf("listener[%d]: port %d out of range", i, l.Port)
		}
		if l.Cluster == "" {
			return fmt.Errorf("listener[%d]: cluster is required", i)
		}
		if _, ok := seen[l.Cluster]; !ok {
			return fmt.Errorf("listener[%d]: references undefined cluster %q", i, l.Cluster)
		}
	}

	return nil
}

// buildCluster renders one CDS entry as an EDS-type cluster: the endpoints are
// carried by a same-named ClusterLoadAssignment (buildEndpoint) served over EDS,
// so a cluster definition is stable across endpoint churn. EdsConfig ADS means
// Envoy fetches the assignment over the same aggregated stream.
func buildCluster(c *Cluster) *clusterv3.Cluster {
	timeout := defaultConnectTimeout
	if c.ConnectTimeoutMs > 0 {
		timeout = time.Duration(c.ConnectTimeoutMs) * time.Millisecond
	}
	return &clusterv3.Cluster{
		Name:                 c.Name,
		ConnectTimeout:       durationpb.New(timeout),
		ClusterDiscoveryType: &clusterv3.Cluster_Type{Type: clusterv3.Cluster_EDS},
		LbPolicy:             clusterv3.Cluster_ROUND_ROBIN,
		EdsClusterConfig: &clusterv3.Cluster_EdsClusterConfig{
			EdsConfig: &corev3.ConfigSource{
				ResourceApiVersion: corev3.ApiVersion_V3,
				ConfigSourceSpecifier: &corev3.ConfigSource_Ads{
					Ads: &corev3.AggregatedConfigSource{},
				},
			},
			// Fetch the ClusterLoadAssignment named after the cluster.
			ServiceName: c.Name,
		},
	}
}

// buildEndpoint renders the EDS ClusterLoadAssignment for one cluster: a single
// locality carrying every endpoint as a TCP SocketAddress. The assignment's
// ClusterName MUST equal the cluster's EdsClusterConfig.ServiceName so Envoy
// pairs them.
func buildEndpoint(c *Cluster) *endpointv3.ClusterLoadAssignment {
	lbEndpoints := make([]*endpointv3.LbEndpoint, 0, len(c.Endpoints))
	for i := range c.Endpoints {
		e := &c.Endpoints[i]
		lbEndpoints = append(lbEndpoints, &endpointv3.LbEndpoint{
			HostIdentifier: &endpointv3.LbEndpoint_Endpoint{
				Endpoint: &endpointv3.Endpoint{
					Address: &corev3.Address{
						Address: &corev3.Address_SocketAddress{
							SocketAddress: &corev3.SocketAddress{
								Protocol: corev3.SocketAddress_TCP,
								Address:  e.IP,
								PortSpecifier: &corev3.SocketAddress_PortValue{
									PortValue: uint32(e.Port),
								},
							},
						},
					},
				},
			},
		})
	}
	return &endpointv3.ClusterLoadAssignment{
		ClusterName: c.Name,
		Endpoints: []*endpointv3.LocalityLbEndpoints{
			{LbEndpoints: lbEndpoints},
		},
	}
}

// buildRouteConfig renders the single RouteConfiguration: one virtual host per
// desired host, each with one prefix route to its cluster and any request-header
// injection. Hosts are exact (the domains array is the host); a catch-all
// wildcard is intentionally not added, so an unmatched host returns Envoy's 404
// rather than silently falling through to an arbitrary cluster.
func buildRouteConfig(routes []Route) *routev3.RouteConfiguration {
	vhosts := make([]*routev3.VirtualHost, 0, len(routes))
	for i := range routes {
		r := &routes[i]
		prefix := r.PathPrefix
		if prefix == "" {
			prefix = "/"
		}

		var headers []*corev3.HeaderValueOption
		for k, v := range r.RequestHeaders {
			headers = append(headers, &corev3.HeaderValueOption{
				Header: &corev3.HeaderValue{Key: k, Value: v},
				// Overwrite: a routing header the control plane sets is authoritative,
				// never appended to a client-supplied value of the same name.
				AppendAction:   corev3.HeaderValueOption_OVERWRITE_IF_EXISTS_OR_ADD,
				KeepEmptyValue: false,
			})
		}

		vhosts = append(vhosts, &routev3.VirtualHost{
			// Unique per index: two routes for the same host would otherwise collide
			// on the virtual-host name. The host string itself is the match domain.
			Name:    fmt.Sprintf("vh-%d-%s", i, r.Host),
			Domains: []string{r.Host},
			Routes: []*routev3.Route{
				{
					Match: &routev3.RouteMatch{
						PathSpecifier: &routev3.RouteMatch_Prefix{Prefix: prefix},
					},
					Action: &routev3.Route_Route{
						Route: &routev3.RouteAction{
							ClusterSpecifier: &routev3.RouteAction_Cluster{Cluster: r.Cluster},
						},
					},
					RequestHeadersToAdd: headers,
				},
			},
		})
	}
	return &routev3.RouteConfiguration{
		Name:         routeConfigName,
		VirtualHosts: vhosts,
		// Envoy validates that route clusters exist in CDS before serving; the
		// snapshot's consistency check (below, in the server) enforces the same at
		// push time so a route to a missing cluster is rejected as a 400.
		ValidateClusters: wrapperspb.Bool(false),
	}
}

// tcpProxyFilterName is the Envoy network filter name for the TCP proxy, the
// only filter in a stateful listener's chain (opaque L4: no protocol filters).
const tcpProxyFilterName = "envoy.filters.network.tcp_proxy"

// buildListener renders one LDS TCP-proxy Listener bound on 0.0.0.0:port, whose
// single network filter proxies raw bytes to the named cluster. The config is
// deliberately minimal (decision 4, opaque L4): a per-listener stat prefix (the
// source of the downstream_cx_active / downstream_cx_total counters the Task 9
// idle-signal scrape and the Task 10 metrics read) and the idle timeout disabled
// (long-lived DB connections are legitimate). Returns an error only if the typed
// filter config cannot be marshaled (never for a valid input).
func buildListener(l *Listener) (*listenerv3.Listener, error) {
	tcpProxy := &tcpproxyv3.TcpProxy{
		// The stat prefix names this listener's stats bucket; the control plane's
		// TCP idle scrape keys on it, so it must be stable and per-listener.
		StatPrefix:       l.Name,
		ClusterSpecifier: &tcpproxyv3.TcpProxy_Cluster{Cluster: l.Cluster},
		// 0 disables the idle timeout: a stateful workload's connections (e.g. a
		// pooled Postgres session) may sit idle indefinitely and must not be
		// severed by the proxy (decision 7: never sever a live connection).
		IdleTimeout: durationpb.New(0),
	}
	tcpAny, err := anypb.New(tcpProxy)
	if err != nil {
		return nil, err
	}
	return &listenerv3.Listener{
		Name: l.Name,
		Address: &corev3.Address{
			Address: &corev3.Address_SocketAddress{
				SocketAddress: &corev3.SocketAddress{
					Protocol: corev3.SocketAddress_TCP,
					Address:  "0.0.0.0",
					PortSpecifier: &corev3.SocketAddress_PortValue{
						PortValue: uint32(l.Port),
					},
				},
			},
		},
		FilterChains: []*listenerv3.FilterChain{
			{
				Filters: []*listenerv3.Filter{
					{
						Name:       tcpProxyFilterName,
						ConfigType: &listenerv3.Filter_TypedConfig{TypedConfig: tcpAny},
					},
				},
			},
		},
	}, nil
}
