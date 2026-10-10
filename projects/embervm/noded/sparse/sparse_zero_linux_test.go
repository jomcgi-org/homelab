//go:build linux

package sparse

import "testing"

func TestIsZeroBlock(t *testing.T) {
	block := make([]byte, holeBlockSize)
	if !isZeroBlock(block) {
		t.Fatal("all-zero block judged non-zero")
	}
	for _, i := range []int{0, 1, int(holeBlockSize) / 2, int(holeBlockSize) - 1} {
		b := make([]byte, holeBlockSize)
		b[i] = 1
		if isZeroBlock(b) {
			t.Fatalf("block with byte %d set judged zero", i)
		}
	}
	if isZeroBlock(block[:holeBlockSize-1]) {
		t.Fatal("a short block must never compare equal to a full zero block")
	}
}

func BenchmarkIsZeroBlock(b *testing.B) {
	block := make([]byte, holeBlockSize)
	b.SetBytes(holeBlockSize)
	for i := 0; i < b.N; i++ {
		if !isZeroBlock(block) {
			b.Fatal("zero block judged non-zero")
		}
	}
}
