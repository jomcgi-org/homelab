package main

import (
	"reflect"
	"strings"
	"testing"
)

// dnsQuery builds a minimal single-question DNS query for name and qtype.
func dnsQuery(name string, qtype uint16) []byte {
	b := []byte{
		0x12, 0x34, // ID
		0x01, 0x00, // flags: RD
		0x00, 0x01, // QDCOUNT
		0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // AN, NS, AR
	}
	for _, label := range strings.Split(name, ".") {
		b = append(b, byte(len(label)))
		b = append(b, label...)
	}
	b = append(b, 0x00)                        // root label
	b = append(b, byte(qtype>>8), byte(qtype)) // QTYPE
	b = append(b, 0x00, 0x01)                  // QCLASS IN
	return b
}

func TestBuildDNSResponseA(t *testing.T) {
	r := buildDNSResponse(dnsQuery("model.example.com", 1))
	if r == nil {
		t.Fatal("nil response for A query")
	}
	if r[0] != 0x12 || r[1] != 0x34 {
		t.Errorf("ID not echoed: %#v", r[:2])
	}
	if r[2]&0x80 == 0 {
		t.Error("QR bit not set in response")
	}
	if an := int(r[6])<<8 | int(r[7]); an != 1 {
		t.Fatalf("ANCOUNT = %d, want 1", an)
	}
	if rd := r[len(r)-4:]; rd[0] != 127 || rd[1] != 0 || rd[2] != 0 || rd[3] != 1 {
		t.Errorf("A RDATA = %v, want 127.0.0.1", rd)
	}
}

func TestBuildDNSResponseAAAAIsNoData(t *testing.T) {
	r := buildDNSResponse(dnsQuery("example.com", 28)) // AAAA
	if r == nil {
		t.Fatal("nil response for AAAA query")
	}
	if an := int(r[6])<<8 | int(r[7]); an != 0 {
		t.Errorf("ANCOUNT = %d, want 0 (NODATA so resolver falls back to A)", an)
	}
}

func TestBuildDNSResponseMalformed(t *testing.T) {
	if got := buildDNSResponse([]byte{0x00, 0x01}); got != nil {
		t.Errorf("want nil for a too-short packet, got %v", got)
	}
}

func TestParseEgressPorts(t *testing.T) {
	tests := []struct {
		name string
		in   string
		want []int
	}{
		{name: "empty falls back to default", in: "", want: defaultEgressPorts},
		{name: "all invalid falls back to default", in: "bad,-1,70000", want: defaultEgressPorts},
		{name: "parsed and trimmed", in: "80, 443 ,9090", want: []int{80, 443, 9090}},
		{name: "invalid entries dropped", in: "80,oops,443", want: []int{80, 443}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := parseEgressPorts(tt.in); !reflect.DeepEqual(got, tt.want) {
				t.Errorf("parseEgressPorts(%q) = %v, want %v", tt.in, got, tt.want)
			}
		})
	}
}
