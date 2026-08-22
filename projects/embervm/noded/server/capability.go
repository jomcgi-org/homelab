package server

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"time"
)

const restoreCapabilityLabel = "embervm-restore-cap-v1"

var (
	ErrNoCapabilityKey              = errors.New("restore capability bearer key is not configured")
	ErrMissingCapability            = errors.New("restore capability is missing")
	ErrMalformedCapability          = errors.New("restore capability has malformed framing")
	ErrBadCapabilityVersion         = errors.New("restore capability has an unsupported version")
	ErrCapabilityMACMismatch        = errors.New("restore capability MAC mismatch")
	ErrCapabilityExpired            = errors.New("restore capability is expired")
	ErrCapabilityNodeMismatch       = errors.New("restore capability node mismatch")
	ErrCapabilityPodUIDMismatch     = errors.New("restore capability pod_uid mismatch")
	ErrCapabilityWorkloadMismatch   = errors.New("restore capability workload mismatch")
	ErrCapabilityRefMismatch        = errors.New("restore capability ref mismatch")
	ErrCapabilityKindMismatch       = errors.New("restore capability kind mismatch")
	ErrCapabilityGenerationMismatch = errors.New("restore capability generation mismatch")
	ErrCapabilityPrincipalMismatch  = errors.New("restore capability principal mismatch")
	ErrCapabilityLineageMismatch    = errors.New("restore capability lineage mismatch")
	ErrCapabilityKeyLength          = errors.New("restore capability data key length is not 32 bytes")
)

// capabilityScope is the v1 MAC-authenticated tuple. Field order is part of the
// cross-language golden vector because tuple_json is carried verbatim in the
// capability framing.
type capabilityScope struct {
	Principal  string `json:"principal"`
	Lineage    string `json:"lineage"`
	Node       string `json:"node"`
	PodUID     string `json:"pod_uid"`
	Workload   string `json:"workload"`
	Ref        string `json:"ref"`
	Kind       string `json:"kind"`
	Generation uint64 `json:"generation"`
}

// parseAndVerifyCapability validates the v1 framing and HMAC before trusting
// the embedded data key or scope. The framing is:
//
//	0x01 || expiry_unix_ms::uint64 BE || key_len::uint16 BE || key ||
//	tuple_len::uint16 BE || tuple_json || mac::32
//
// mac is HMAC-SHA256(macKey, "embervm-restore-cap-v1" || all bytes before mac).
func parseAndVerifyCapability(raw []byte, macKey []byte, now time.Time, want capabilityScope) ([]byte, error) {
	if len(macKey) == 0 {
		return nil, ErrNoCapabilityKey
	}
	if len(raw) == 0 {
		return nil, ErrMissingCapability
	}
	if raw[0] != 0x01 {
		return nil, fmt.Errorf("%w: got %d", ErrBadCapabilityVersion, raw[0])
	}
	const fixedBeforeKey = 1 + 8 + 2
	const macLen = sha256.Size
	if len(raw) < fixedBeforeKey+2+macLen {
		return nil, ErrMalformedCapability
	}
	expiry := binary.BigEndian.Uint64(raw[1:9])
	keyLen := int(binary.BigEndian.Uint16(raw[9:11]))
	keyStart := fixedBeforeKey
	keyEnd := keyStart + keyLen
	macStart := len(raw) - macLen
	if keyEnd+2 > macStart {
		return nil, ErrMalformedCapability
	}
	tupleLen := int(binary.BigEndian.Uint16(raw[keyEnd : keyEnd+2]))
	tupleStart := keyEnd + 2
	tupleEnd := tupleStart + tupleLen
	if tupleEnd != macStart {
		return nil, ErrMalformedCapability
	}

	h := hmac.New(sha256.New, macKey)
	_, _ = h.Write([]byte(restoreCapabilityLabel))
	_, _ = h.Write(raw[:macStart])
	if !hmac.Equal(h.Sum(nil), raw[macStart:]) {
		return nil, ErrCapabilityMACMismatch
	}
	if keyLen != 32 {
		return nil, fmt.Errorf("%w: got %d", ErrCapabilityKeyLength, keyLen)
	}
	if now.UnixMilli() >= 0 && expiry <= uint64(now.UnixMilli()) {
		return nil, ErrCapabilityExpired
	}

	var got capabilityScope
	if err := json.Unmarshal(raw[tupleStart:tupleEnd], &got); err != nil {
		return nil, fmt.Errorf("%w: tuple JSON: %v", ErrMalformedCapability, err)
	}
	if got.Node != want.Node {
		return nil, ErrCapabilityNodeMismatch
	}
	if got.PodUID != want.PodUID {
		return nil, ErrCapabilityPodUIDMismatch
	}
	if got.Workload != want.Workload {
		return nil, ErrCapabilityWorkloadMismatch
	}
	if got.Ref != want.Ref {
		return nil, ErrCapabilityRefMismatch
	}
	if got.Kind != want.Kind {
		return nil, ErrCapabilityKindMismatch
	}
	if got.Generation != want.Generation {
		return nil, ErrCapabilityGenerationMismatch
	}
	if want.Principal != "" && got.Principal != want.Principal {
		return nil, ErrCapabilityPrincipalMismatch
	}
	if want.Lineage != "" && got.Lineage != want.Lineage {
		return nil, ErrCapabilityLineageMismatch
	}
	return append([]byte(nil), raw[keyStart:keyEnd]...), nil
}
