package meter

import (
	"net/http/httptest"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/testutil"
)

func TestClientClass(t *testing.T) {
	cases := []struct {
		ip, ua, want string
	}{
		{"160.79.104.10", "", ClassAnthropic},
		{"160.79.105.255", "", ClassAnthropic},
		{"160.79.106.1", "", ClassDirect},
		{"2607:6bc0::1", "", ClassAnthropic},
		{"2607:6bc1::1", "", ClassDirect},
		{"203.0.113.9", "Mozilla/5.0 (compatible; ChatGPT-User/1.0)", ClassOpenAI},
		{"203.0.113.9", "curl/8", ClassDirect},
		{"not-an-ip", "", ClassUnknown},
	}
	for _, c := range cases {
		r := httptest.NewRequest("GET", "/v1/x", nil)
		r.Header.Set("CF-Connecting-IP", c.ip)
		r.Header.Set("User-Agent", c.ua)
		if got := ClientClass(r); got != c.want {
			t.Errorf("ClientClass(%q, %q) = %s, want %s", c.ip, c.ua, got, c.want)
		}
	}
	// No edge header at all: in-cluster traffic.
	if got := ClientClass(httptest.NewRequest("GET", "/v1/x", nil)); got != ClassDirect {
		t.Errorf("no header = %s", got)
	}
}

func TestRecord(t *testing.T) {
	reg := prometheus.NewRegistry()
	m := New(reg)
	m.Record("plan", "mcp", ClassAnthropic, "ok", 5, time.Millisecond)
	m.Record("plan", "mcp", ClassAnthropic, "ok", 0, time.Millisecond)
	if got := testutil.ToFloat64(m.calls.WithLabelValues("plan", "mcp", ClassAnthropic, "ok")); got != 2 {
		t.Fatalf("calls = %v", got)
	}
	if got := testutil.ToFloat64(m.results.WithLabelValues("plan", "mcp", ClassAnthropic)); got != 5 {
		t.Fatalf("results = %v", got)
	}
}
