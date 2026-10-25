package meter

import (
	"strings"
	"testing"
)

func TestPolicyRejectsUnsupportedTables(t *testing.T) {
	for _, s := range []string{"null", "[]", "{}", DefaultPrices + "{}", strings.Replace(DefaultPrices, `"check"`, `"plan"`, 1), strings.Replace(DefaultPrices, `"check"`, `"other"`, 1), strings.Replace(DefaultPrices, `check_invocation`, `bad-unit`, 1)} {
		if _, err := ParsePolicy(s); err == nil {
			t.Errorf("accepted %s", s)
		}
	}
	for _, price := range []string{`"1"`, `"-1"`, `"NaN"`, `"Inf"`, `"0.0"`, `"00"`, `0`, `null`, `"0.000001"`} {
		if _, err := ParsePolicy(strings.Replace(DefaultPrices, `"0"`, price, 1)); err == nil {
			t.Errorf("accepted price %s", price)
		}
	}
	p, err := ParsePolicy(DefaultPrices)
	if err != nil {
		t.Fatal(err)
	}
	for _, tool := range []string{"plan", "check"} {
		price, units := p.Quote(tool)
		if units != 1 || price.PriceMicros != "0" {
			t.Fatal("nonzero quote")
		}
	}
	if _, units := p.Quote("unknown"); units != 0 {
		t.Fatal("unknown tool units")
	}
}
