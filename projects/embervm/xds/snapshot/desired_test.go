package snapshot

import (
	"testing"

	clusterv3 "github.com/envoyproxy/go-control-plane/envoy/config/cluster/v3"
	corev3 "github.com/envoyproxy/go-control-plane/envoy/config/core/v3"
	endpointv3 "github.com/envoyproxy/go-control-plane/envoy/config/endpoint/v3"
	listenerv3 "github.com/envoyproxy/go-control-plane/envoy/config/listener/v3"
	routev3 "github.com/envoyproxy/go-control-plane/envoy/config/route/v3"
	tcpproxyv3 "github.com/envoyproxy/go-control-plane/envoy/extensions/filters/network/tcp_proxy/v3"
	resourcev3 "github.com/envoyproxy/go-control-plane/pkg/resource/v3"
)

func TestBuild_translatesClustersEndpointsRoutes(t *testing.T) {
	d := &Desired{
		Version: "0000000001",
		Clusters: []Cluster{
			{
				Name:             "fn-hello",
				ConnectTimeoutMs: 250,
				Endpoints: []Endpoint{
					{IP: "10.42.0.5", Port: 8080},
					{IP: "10.42.0.6", Port: 8080},
				},
			},
		},
		Routes: []Route{
			{
				Host:           "serving-hello.private.jomcgi.dev",
				PathPrefix:     "/",
				Cluster:        "fn-hello",
				RequestHeaders: map[string]string{"x-ember-function": "hello"},
			},
		},
	}

	snap, err := Build(d)
	if err != nil {
		t.Fatalf("Build: %v", err)
	}

	// CDS: one EDS-type cluster with the converted connect timeout.
	clusters := snap.GetResources(resourcev3.ClusterType)
	if len(clusters) != 1 {
		t.Fatalf("want 1 cluster, got %d", len(clusters))
	}
	cl, ok := clusters["fn-hello"].(*clusterv3.Cluster)
	if !ok {
		t.Fatalf("cluster fn-hello missing or wrong type: %T", clusters["fn-hello"])
	}
	if got := cl.GetType(); got != clusterv3.Cluster_EDS {
		t.Errorf("discovery type = %v, want EDS", got)
	}
	if got := cl.GetConnectTimeout().AsDuration().Milliseconds(); got != 250 {
		t.Errorf("connect timeout = %dms, want 250ms", got)
	}
	if got := cl.GetEdsClusterConfig().GetServiceName(); got != "fn-hello" {
		t.Errorf("eds service name = %q, want fn-hello", got)
	}
	if cl.GetEdsClusterConfig().GetEdsConfig().GetAds() == nil {
		t.Error("eds config should use ADS")
	}

	// EDS: one ClusterLoadAssignment with both endpoints.
	eds := snap.GetResources(resourcev3.EndpointType)
	cla, ok := eds["fn-hello"].(*endpointv3.ClusterLoadAssignment)
	if !ok {
		t.Fatalf("endpoint assignment fn-hello missing: %T", eds["fn-hello"])
	}
	if cla.GetClusterName() != "fn-hello" {
		t.Errorf("cla cluster name = %q, want fn-hello", cla.GetClusterName())
	}
	lbs := cla.GetEndpoints()[0].GetLbEndpoints()
	if len(lbs) != 2 {
		t.Fatalf("want 2 lb endpoints, got %d", len(lbs))
	}
	addr := lbs[0].GetEndpoint().GetAddress().GetSocketAddress()
	if addr.GetAddress() != "10.42.0.5" || addr.GetPortValue() != 8080 {
		t.Errorf("endpoint[0] = %s:%d, want 10.42.0.5:8080", addr.GetAddress(), addr.GetPortValue())
	}

	// RDS: one route config named for the bootstrap RDS reference, one vhost with
	// the injected request header.
	rds := snap.GetResources(resourcev3.RouteType)
	rc, ok := rds[routeConfigName].(*routev3.RouteConfiguration)
	if !ok {
		t.Fatalf("route config %q missing: %v", routeConfigName, rds)
	}
	if len(rc.GetVirtualHosts()) != 1 {
		t.Fatalf("want 1 vhost, got %d", len(rc.GetVirtualHosts()))
	}
	vh := rc.GetVirtualHosts()[0]
	if got := vh.GetDomains(); len(got) != 1 || got[0] != "serving-hello.private.jomcgi.dev" {
		t.Errorf("vhost domains = %v", got)
	}
	route := vh.GetRoutes()[0]
	if got := route.GetRoute().GetCluster(); got != "fn-hello" {
		t.Errorf("route cluster = %q, want fn-hello", got)
	}
	if got := route.GetMatch().GetPrefix(); got != "/" {
		t.Errorf("route prefix = %q, want /", got)
	}
	hdrs := route.GetRequestHeadersToAdd()
	if len(hdrs) != 1 || hdrs[0].GetHeader().GetKey() != "x-ember-function" || hdrs[0].GetHeader().GetValue() != "hello" {
		t.Errorf("request headers = %v, want x-ember-function=hello", hdrs)
	}
	if hdrs[0].GetAppendAction() != corev3.HeaderValueOption_OVERWRITE_IF_EXISTS_OR_ADD {
		t.Errorf("header append action = %v, want OVERWRITE_IF_EXISTS_OR_ADD", hdrs[0].GetAppendAction())
	}
}

func TestBuild_defaultsConnectTimeoutAndPathPrefix(t *testing.T) {
	d := &Desired{
		Version:  "1",
		Clusters: []Cluster{{Name: "c1", Endpoints: []Endpoint{{IP: "10.0.0.1", Port: 80}}}},
		Routes:   []Route{{Host: "h", Cluster: "c1"}},
	}
	snap, err := Build(d)
	if err != nil {
		t.Fatalf("Build: %v", err)
	}
	cl := snap.GetResources(resourcev3.ClusterType)["c1"].(*clusterv3.Cluster)
	if got := cl.GetConnectTimeout().AsDuration(); got != defaultConnectTimeout {
		t.Errorf("default connect timeout = %v, want %v", got, defaultConnectTimeout)
	}
	rc := snap.GetResources(resourcev3.RouteType)[routeConfigName].(*routev3.RouteConfiguration)
	if got := rc.GetVirtualHosts()[0].GetRoutes()[0].GetMatch().GetPrefix(); got != "/" {
		t.Errorf("default path prefix = %q, want /", got)
	}
}

func TestBuild_emptyDesiredServesEmptyResources(t *testing.T) {
	snap, err := Build(&Desired{Version: "1"})
	if err != nil {
		t.Fatalf("Build empty: %v", err)
	}
	if n := len(snap.GetResources(resourcev3.ClusterType)); n != 0 {
		t.Errorf("clusters = %d, want 0", n)
	}
	// An empty desired-state still yields a (single, empty) RouteConfiguration so
	// the bootstrap RDS reference resolves rather than warming with a NACK.
	if n := len(snap.GetResources(resourcev3.RouteType)); n != 1 {
		t.Errorf("route configs = %d, want 1 (empty)", n)
	}
	// R4 regression: a document with no `listeners` renders ZERO Listener
	// resources, so Envoy keeps its static bootstrap listeners and the R3 path is
	// byte-identical. The ListenerType surface only ever carries stateful listeners.
	if n := len(snap.GetResources(resourcev3.ListenerType)); n != 0 {
		t.Errorf("listeners = %d, want 0 for a document with no listeners", n)
	}
}

func TestBuild_translatesStatefulTcpListeners(t *testing.T) {
	d := &Desired{
		Version: "1",
		Clusters: []Cluster{
			// The cluster the listener proxies to. In production its sole endpoint
			// is the live VM or the TCP activator; here one endpoint suffices.
			{Name: "state|scratch-postgres", Endpoints: []Endpoint{{IP: "10.42.0.9", Port: 15432}}},
		},
		Listeners: []Listener{
			{Name: "state-5400", Port: 5400, Cluster: "state|scratch-postgres"},
		},
	}

	snap, err := Build(d)
	if err != nil {
		t.Fatalf("Build: %v", err)
	}

	listeners := snap.GetResources(resourcev3.ListenerType)
	if len(listeners) != 1 {
		t.Fatalf("want 1 listener, got %d", len(listeners))
	}
	l, ok := listeners["state-5400"].(*listenerv3.Listener)
	if !ok {
		t.Fatalf("listener state-5400 missing or wrong type: %T", listeners["state-5400"])
	}

	// Bound on 0.0.0.0:5400 over TCP.
	sa := l.GetAddress().GetSocketAddress()
	if sa.GetAddress() != "0.0.0.0" || sa.GetPortValue() != 5400 || sa.GetProtocol() != corev3.SocketAddress_TCP {
		t.Errorf("listener address = %s:%d/%v, want 0.0.0.0:5400/TCP", sa.GetAddress(), sa.GetPortValue(), sa.GetProtocol())
	}

	// One filter chain, one tcp_proxy filter routing to the cluster.
	chains := l.GetFilterChains()
	if len(chains) != 1 || len(chains[0].GetFilters()) != 1 {
		t.Fatalf("want one filter chain with one filter, got %d chains", len(chains))
	}
	f := chains[0].GetFilters()[0]
	if f.GetName() != tcpProxyFilterName {
		t.Errorf("filter name = %q, want %q", f.GetName(), tcpProxyFilterName)
	}
	var tp tcpproxyv3.TcpProxy
	if err := f.GetTypedConfig().UnmarshalTo(&tp); err != nil {
		t.Fatalf("unmarshal tcp_proxy config: %v", err)
	}
	if tp.GetCluster() != "state|scratch-postgres" {
		t.Errorf("tcp_proxy cluster = %q, want state|scratch-postgres", tp.GetCluster())
	}
	if tp.GetStatPrefix() != "state-5400" {
		t.Errorf("tcp_proxy stat_prefix = %q, want state-5400", tp.GetStatPrefix())
	}
	// Idle timeout disabled (0): long-lived DB connections are never severed.
	if tp.GetIdleTimeout().AsDuration() != 0 {
		t.Errorf("tcp_proxy idle_timeout = %v, want 0 (disabled)", tp.GetIdleTimeout().AsDuration())
	}
}

func TestBuild_rejectsMalformedListeners(t *testing.T) {
	defined := []Cluster{{Name: "state|wl", Endpoints: []Endpoint{{IP: "10.0.0.1", Port: 5432}}}}
	cases := []struct {
		name string
		d    *Desired
	}{
		{"listener missing name", &Desired{Version: "1", Clusters: defined, Listeners: []Listener{{Port: 5400, Cluster: "state|wl"}}}},
		{"duplicate listener name", &Desired{Version: "1", Clusters: defined, Listeners: []Listener{{Name: "l", Port: 5400, Cluster: "state|wl"}, {Name: "l", Port: 5401, Cluster: "state|wl"}}}},
		{"listener port zero", &Desired{Version: "1", Clusters: defined, Listeners: []Listener{{Name: "l", Port: 0, Cluster: "state|wl"}}}},
		{"listener port too high", &Desired{Version: "1", Clusters: defined, Listeners: []Listener{{Name: "l", Port: 70000, Cluster: "state|wl"}}}},
		{"listener missing cluster", &Desired{Version: "1", Clusters: defined, Listeners: []Listener{{Name: "l", Port: 5400}}}},
		{"listener to undefined cluster", &Desired{Version: "1", Listeners: []Listener{{Name: "l", Port: 5400, Cluster: "nope"}}}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := Build(tc.d); err == nil {
				t.Fatalf("want error for %s, got nil", tc.name)
			}
		})
	}
}

func TestBuild_acceptsValidStatefulListeners(t *testing.T) {
	// A valid L4 stateful listener referencing a defined cluster is accepted (the
	// control plane can publish stateful clusters + listeners without the PUT
	// 400ing). LDS rendering itself lands with the wake-on-connect task; here we
	// only assert the document validates and the cluster still renders.
	d := &Desired{
		Version:   "1",
		Clusters:  []Cluster{{Name: "state|wl-s", Endpoints: []Endpoint{{IP: "10.99.0.7", Port: 6000}}}},
		Listeners: []Listener{{Name: "state-9100", Port: 9100, Cluster: "state|wl-s"}},
	}
	snap, err := Build(d)
	if err != nil {
		t.Fatalf("Build valid stateful listener: %v", err)
	}
	if _, ok := snap.GetResources(resourcev3.ClusterType)["state|wl-s"]; !ok {
		t.Errorf("stateful cluster not rendered")
	}
}

func TestBuild_rejectsMalformed(t *testing.T) {
	cases := []struct {
		name string
		d    *Desired
	}{
		{"missing version", &Desired{Clusters: []Cluster{{Name: "c"}}}},
		{"missing cluster name", &Desired{Version: "1", Clusters: []Cluster{{Name: ""}}}},
		{"duplicate cluster name", &Desired{Version: "1", Clusters: []Cluster{{Name: "c"}, {Name: "c"}}}},
		{"endpoint missing ip", &Desired{Version: "1", Clusters: []Cluster{{Name: "c", Endpoints: []Endpoint{{Port: 80}}}}}},
		{"endpoint port zero", &Desired{Version: "1", Clusters: []Cluster{{Name: "c", Endpoints: []Endpoint{{IP: "1.1.1.1", Port: 0}}}}}},
		{"endpoint port too high", &Desired{Version: "1", Clusters: []Cluster{{Name: "c", Endpoints: []Endpoint{{IP: "1.1.1.1", Port: 70000}}}}}},
		{"route missing host", &Desired{Version: "1", Clusters: []Cluster{{Name: "c"}}, Routes: []Route{{Cluster: "c"}}}},
		{"route missing cluster", &Desired{Version: "1", Routes: []Route{{Host: "h"}}}},
		{"route to undefined cluster", &Desired{Version: "1", Routes: []Route{{Host: "h", Cluster: "nope"}}}},
		{"listener missing name", &Desired{Version: "1", Clusters: []Cluster{{Name: "c"}}, Listeners: []Listener{{Port: 9100, Cluster: "c"}}}},
		{"listener port zero", &Desired{Version: "1", Clusters: []Cluster{{Name: "c"}}, Listeners: []Listener{{Name: "l", Port: 0, Cluster: "c"}}}},
		{"listener port too high", &Desired{Version: "1", Clusters: []Cluster{{Name: "c"}}, Listeners: []Listener{{Name: "l", Port: 70000, Cluster: "c"}}}},
		{"listener missing cluster", &Desired{Version: "1", Listeners: []Listener{{Name: "l", Port: 9100}}}},
		{"listener to undefined cluster", &Desired{Version: "1", Listeners: []Listener{{Name: "l", Port: 9100, Cluster: "nope"}}}},
		{"duplicate listener name", &Desired{Version: "1", Clusters: []Cluster{{Name: "c"}}, Listeners: []Listener{{Name: "l", Port: 9100, Cluster: "c"}, {Name: "l", Port: 9101, Cluster: "c"}}}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := Build(tc.d); err == nil {
				t.Fatalf("want error for %s, got nil", tc.name)
			}
		})
	}
}
