package meter

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"regexp"
)

// UnitPrice uses integer millionths of a currency unit, encoded as decimal text.
// Only exact zero is supported until a separately designed payment policy exists.
type UnitPrice struct {
	Tool        string `json:"tool"`
	Unit        string `json:"unit"`
	PriceMicros string `json:"price_micros"`
}

type Policy struct{ prices map[string]UnitPrice }

const DefaultPrices = `[{"tool":"plan","unit":"plan_invocation","price_micros":"0"},{"tool":"check","unit":"check_invocation","price_micros":"0"}]`

var unitName = regexp.MustCompile(`^[a-z][a-z0-9_]{0,63}$`)

// ParsePolicy rejects incomplete tables and unsupported payment activation.
func ParsePolicy(raw string) (*Policy, error) {
	var entries []UnitPrice
	d := json.NewDecoder(bytes.NewBufferString(raw))
	d.DisallowUnknownFields()
	if d.Decode(&entries) != nil || d.Decode(new(any)) != io.EOF {
		return nil, errors.New("invalid unit price table")
	}
	p := &Policy{prices: make(map[string]UnitPrice)}
	for _, e := range entries {
		if (e.Tool != "plan" && e.Tool != "check") || !unitName.MatchString(e.Unit) || e.PriceMicros != "0" {
			return nil, errors.New("unit prices require registered tools, bounded unit names and exact zero price_micros")
		}
		if _, exists := p.prices[e.Tool]; exists {
			return nil, errors.New("duplicate tool price")
		}
		p.prices[e.Tool] = e
	}
	if len(p.prices) != 2 {
		return nil, errors.New("missing tool price")
	}
	return p, nil
}

// Quote is the future billing-policy boundary. No payment client or enforcement
// exists. A recognized origin invocation consumes one unit, regardless of its
// outcome. Discovery, protocol failures and unknown names consume no units.
func (p *Policy) Quote(tool string) (UnitPrice, int64) {
	price, ok := p.prices[tool]
	if !ok {
		return UnitPrice{}, 0
	}
	return price, 1
}
