package aurora_test

import (
	"fmt"
	"math"
	"strings"
	"testing"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/aurora"
)

func TestTerrainGeometry(t *testing.T) {
	var b strings.Builder
	fmt.Fprint(&b, "ncols 400\nnrows 400\nxllcorner -4.4\nyllcorner 57.8\ncellsize 0.002\nNODATA_value -9999\n")
	b.WriteString(strings.Repeat("100 ", 160000))
	g, err := aurora.ParseGrid([]byte(b.String()))
	if err != nil {
		t.Fatal(err)
	}
	p := g.Profile(58, -4)
	for _, h := range p.North {
		if h == nil || *h != 0 {
			t.Fatal("flat horizon", p)
		}
	}
	// Constant-height east/west ridge at latitude 58.01. At the north ray's
	// 1200 m sample the rise is 300 m, giving atan2(300-curvature,1200).
	y := g.Rows - 1 - int(math.Floor((58.01-g.South)/g.Cell))
	for x := 0; x < g.Cols; x++ {
		g.Values[y*g.Cols+x] = 400
	}
	p = g.Profile(58, -4)
	want := math.Atan2(300-1200.0*1200/(2*6371008.8), 1200) * 180 / math.Pi
	if p.North[9] == nil || math.Abs(*p.North[9]-want) > .001 {
		t.Fatalf("ridge = %v want %v", p.North[9], want)
	}
	for x := 0; x < g.Cols; x++ {
		g.Values[y*g.Cols+x] = -9999
	}
	p = g.Profile(58, -4)
	for _, h := range p.North {
		if h != nil {
			t.Fatal("nodata became clear")
		}
	}
	p = g.Profile(60, -4)
	for _, h := range p.North {
		if h != nil {
			t.Fatal("outside raster became clear")
		}
	}
}

func TestTerrainParsingBounds(t *testing.T) {
	good := "ncols 1\nnrows 1\nxllcorner -4\nyllcorner 58\ncellsize 0.002\nNODATA_value -9999\n100"
	for _, bad := range []string{"", strings.Replace(good, "ncols 1", "ncols 4000001", 1), strings.Replace(good, "0.002", "0.02", 1), strings.Replace(good, "ncols 1", "ncols 1.5", 1), strings.Replace(good, "100", "NaN", 1), good + " 2", strings.TrimSuffix(good, "100")} {
		if _, err := aurora.ParseGrid([]byte(bad)); err == nil {
			t.Fatal("accepted", bad)
		}
	}
}
