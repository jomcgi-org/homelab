package snapshot

import (
	"testing"

	clusterv3 "github.com/envoyproxy/go-control-plane/envoy/config/cluster/v3"
	corev3 "github.com/envoyproxy/go-control-plane/envoy/config/core/v3"
	endpointv3 "github.com/envoyproxy/go-control-plane/envoy/config/endpoint/v3"
	routev3 "github.com/envoyproxy/go-control-plane/envoy/config/route/v3"
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
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := Build(tc.d); err == nil {
				t.Fatalf("want error for %s, got nil", tc.name)
			}
		})
	}
}
