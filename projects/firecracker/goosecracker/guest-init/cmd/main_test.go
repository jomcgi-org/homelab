package main

import (
	"net/netip"
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
	res := newSynthResolver()
	r := buildDNSResponse(dnsQuery("model.example.com", 1), res)
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
	// The A RDATA is the name's synthetic 127.0.0.0/8 address, not a fixed
	// 127.0.0.1, and it must equal the address the resolver hands out for the name.
	rd := r[len(r)-4:]
	if rd[0] != 127 {
		t.Errorf("A RDATA = %v, want a 127.0.0.0/8 address", rd)
	}
	want := res.forName("model.example.com").As4()
	if [4]byte{rd[0], rd[1], rd[2], rd[3]} != want {
		t.Errorf("A RDATA = %v, want synthetic %v", rd, want)
	}
}

func TestBuildDNSResponseAAAAIsNoData(t *testing.T) {
	r := buildDNSResponse(dnsQuery("example.com", 28), newSynthResolver()) // AAAA
	if r == nil {
		t.Fatal("nil response for AAAA query")
	}
	if an := int(r[6])<<8 | int(r[7]); an != 0 {
		t.Errorf("ANCOUNT = %d, want 0 (NODATA so resolver falls back to A)", an)
	}
}

func TestBuildDNSResponseMalformed(t *testing.T) {
	if got := buildDNSResponse([]byte{0x00, 0x01}, newSynthResolver()); got != nil {
		t.Errorf("want nil for a too-short packet, got %v", got)
	}
}

func TestSynthResolverStableAndReversible(t *testing.T) {
	res := newSynthResolver()
	a := res.forName("api.github.com")
	// Same name is stable; case/trailing-dot normalise to the same entry.
	if b := res.forName("API.github.com."); a != b {
		t.Errorf("forName not stable/normalised: %v vs %v", a, b)
	}
	// Different names get different addresses.
	c := res.forName("openrouter.ai")
	if a == c {
		t.Errorf("distinct names share an address: %v", a)
	}
	if a.As4()[0] != 127 || c.As4()[0] != 127 {
		t.Errorf("synthetic addresses not in 127/8: %v %v", a, c)
	}
	if a == netip.MustParseAddr("127.0.0.1") {
		t.Error("allocated 127.0.0.1, which is reserved for DNS + capture listener")
	}
	// Reverse map recovers the original name.
	if n, ok := res.name(a); !ok || n != "api.github.com" {
		t.Errorf("reverse map = (%q, %v), want api.github.com", n, ok)
	}
	if _, ok := res.name(netip.MustParseAddr("127.9.9.9")); ok {
		t.Error("reverse map returned a name for an unallocated address")
	}
}
