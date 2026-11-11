package verdict

import "testing"

func TestRankOrder(t *testing.T) {
	for i, v := range All {
		if v.Rank() != i {
			t.Errorf("%s ranks %d, want %d", v, v.Rank(), i)
		}
	}
}
