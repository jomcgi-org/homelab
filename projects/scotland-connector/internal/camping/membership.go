// Package camping holds producer-computed point membership, never polygons.
package camping

// Membership is additive snapshot metadata. Nil, or missing lists, means
// unknown. Present empty lists mean a point was checked and is outside.
// A National Park is independent of a camping-management zone.
type Membership struct {
	Zones []string `json:"zones"`
	Parks []string `json:"parks"`
}

func (m *Membership) Known() bool {
	return m != nil && m.Zones != nil && m.Parks != nil
}
