package server

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"testing"
	"time"
)

func mintCapability(t *testing.T, macKey, dataKey []byte, expiry time.Time, scope capabilityScope) []byte {
	t.Helper()
	tuple, err := json.Marshal(scope)
	if err != nil {
		t.Fatal(err)
	}
	var framed bytes.Buffer
	framed.WriteByte(0x01)
	var n [8]byte
	binary.BigEndian.PutUint64(n[:], uint64(expiry.UnixMilli()))
	framed.Write(n[:])
	var short [2]byte
	binary.BigEndian.PutUint16(short[:], uint16(len(dataKey)))
	framed.Write(short[:])
	framed.Write(dataKey)
	binary.BigEndian.PutUint16(short[:], uint16(len(tuple)))
	framed.Write(short[:])
	framed.Write(tuple)
	h := hmac.New(sha256.New, macKey)
	h.Write([]byte(restoreCapabilityLabel))
	h.Write(framed.Bytes())
	framed.Write(h.Sum(nil))
	return framed.Bytes()
}

func TestParseAndVerifyCapability(t *testing.T) {
	macKey := []byte("shared-bearer")
	dataKey := bytes.Repeat([]byte{0x5a}, 32)
	now := time.UnixMilli(1_800_000_000_000)
	want := capabilityScope{
		Principal: "acct:alice", Lineage: "lineage-1", Node: "node-a", PodUID: "pod-a",
		Workload: "sandbox-session", Ref: "session-1", Kind: "session", Generation: 9,
	}
	valid := mintCapability(t, macKey, dataKey, now.Add(time.Minute), want)

	got, err := parseAndVerifyCapability(valid, macKey, now, want)
	if err != nil || !bytes.Equal(got, dataKey) {
		t.Fatalf("valid capability = (%x, %v), want data key", got, err)
	}

	tests := []struct {
		name    string
		raw     func() []byte
		macKey  []byte
		want    capabilityScope
		wantErr error
	}{
		{name: "no_mac_key", raw: func() []byte { return valid }, want: want, wantErr: ErrNoCapabilityKey},
		{name: "missing", raw: func() []byte { return nil }, macKey: macKey, want: want, wantErr: ErrMissingCapability},
		{name: "malformed", raw: func() []byte { return []byte{0x01} }, macKey: macKey, want: want, wantErr: ErrMalformedCapability},
		{name: "bad_version", raw: func() []byte { b := append([]byte(nil), valid...); b[0] = 2; return b }, macKey: macKey, want: want, wantErr: ErrBadCapabilityVersion},
		{name: "mac_mismatch", raw: func() []byte { b := append([]byte(nil), valid...); b[len(b)-1] ^= 1; return b }, macKey: macKey, want: want, wantErr: ErrCapabilityMACMismatch},
		{name: "expired", raw: func() []byte { return mintCapability(t, macKey, dataKey, now, want) }, macKey: macKey, want: want, wantErr: ErrCapabilityExpired},
		{name: "wrong_node", raw: func() []byte { return valid }, macKey: macKey, want: withScope(want, func(s *capabilityScope) { s.Node = "node-b" }), wantErr: ErrCapabilityNodeMismatch},
		{name: "wrong_pod_uid", raw: func() []byte { return valid }, macKey: macKey, want: withScope(want, func(s *capabilityScope) { s.PodUID = "pod-b" }), wantErr: ErrCapabilityPodUIDMismatch},
		{name: "wrong_workload", raw: func() []byte { return valid }, macKey: macKey, want: withScope(want, func(s *capabilityScope) { s.Workload = "other" }), wantErr: ErrCapabilityWorkloadMismatch},
		{name: "wrong_ref", raw: func() []byte { return valid }, macKey: macKey, want: withScope(want, func(s *capabilityScope) { s.Ref = "other" }), wantErr: ErrCapabilityRefMismatch},
		{name: "wrong_kind", raw: func() []byte { return valid }, macKey: macKey, want: withScope(want, func(s *capabilityScope) { s.Kind = "stateful" }), wantErr: ErrCapabilityKindMismatch},
		{name: "wrong_generation", raw: func() []byte { return valid }, macKey: macKey, want: withScope(want, func(s *capabilityScope) { s.Generation++ }), wantErr: ErrCapabilityGenerationMismatch},
		{name: "zero_generation_is_wildcard", raw: func() []byte {
			return mintCapability(t, macKey, dataKey, now.Add(time.Minute), withScope(want, func(s *capabilityScope) { s.Generation = 0 }))
		}, macKey: macKey, want: withScope(want, func(s *capabilityScope) { s.Generation = 123 }), wantErr: nil},
		{name: "wrong_principal", raw: func() []byte { return valid }, macKey: macKey, want: withScope(want, func(s *capabilityScope) { s.Principal = "acct:bob" }), wantErr: ErrCapabilityPrincipalMismatch},
		{name: "wrong_lineage", raw: func() []byte { return valid }, macKey: macKey, want: withScope(want, func(s *capabilityScope) { s.Lineage = "lineage-2" }), wantErr: ErrCapabilityLineageMismatch},
		{name: "bad_key_length", raw: func() []byte { return mintCapability(t, macKey, dataKey[:31], now.Add(time.Minute), want) }, macKey: macKey, want: want, wantErr: ErrCapabilityKeyLength},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			_, err := parseAndVerifyCapability(tt.raw(), tt.macKey, now, tt.want)
			if !errors.Is(err, tt.wantErr) {
				t.Fatalf("error = %v, want %v", err, tt.wantErr)
			}
		})
	}
}

func withScope(scope capabilityScope, change func(*capabilityScope)) capabilityScope {
	change(&scope)
	return scope
}

func TestCapabilityGoldenVector(t *testing.T) {
	macKey := []byte("golden-shared-bearer")
	dataKey := make([]byte, 32)
	for i := range dataKey {
		dataKey[i] = byte(i)
	}
	scope := capabilityScope{
		Principal: "acct:alice", Lineage: "lineage-42", Node: "node-a", PodUID: "uid-a",
		Workload: "sandbox-session", Ref: "sess-42", Kind: "session", Generation: 7,
	}
	expiry := time.UnixMilli(1_893_456_000_123)
	got := hex.EncodeToString(mintCapability(t, macKey, dataKey, expiry, scope))
	// V1 embervm-restore-cap-v1 cross-language golden capability hex:
	const goldenCapabilityHex = "01000001b8dac5b47b0020000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f00a07b227072696e636970616c223a22616363743a616c696365222c226c696e65616765223a226c696e656167652d3432222c226e6f6465223a226e6f64652d61222c22706f645f756964223a227569642d61222c22776f726b6c6f6164223a2273616e64626f782d73657373696f6e222c22726566223a22736573732d3432222c226b696e64223a2273657373696f6e222c2267656e65726174696f6e223a377d856a4e670db0f0fdd70b88aafbe5ab9b8411b21cf0c19cc82c8ffe3ffaf9adc8"
	if got != goldenCapabilityHex {
		t.Fatalf("golden capability changed:\n got %s\nwant %s", got, goldenCapabilityHex)
	}
}
