package aurora

import (
	"bufio"
	"bytes"
	"crypto/sha256"
	"fmt"
	"math"
	"strconv"
)

const (
	TerrainAlgorithm = "north-rays-v1-20km-100m-5deg-no-refraction"
	MaxTerrainBytes  = 128 << 20
)

// Grid is a north-to-south ESRI ASCII raster in WGS84 geographic degrees,
// heights in metres in the input source datum. GMTED2010's vertical reference
// varies by input source; preserve and validate the source metadata separately.
// Preprocess only local GMTED2010 mean tiles.
type Grid struct {
	Cols, Rows                int
	West, South, Cell, NoData float64
	Values                    []float64
	SHA256                    string
}

func ParseGrid(body []byte) (*Grid, error) {
	if len(body) == 0 || len(body) > MaxTerrainBytes {
		return nil, fmt.Errorf("terrain: raster size outside limit")
	}
	s := bufio.NewScanner(bytes.NewReader(body))
	s.Split(bufio.ScanWords)
	next := func() (string, error) {
		if !s.Scan() {
			return "", fmt.Errorf("terrain: truncated raster")
		}
		return s.Text(), nil
	}
	values := make([]float64, 6)
	for i, key := range []string{"ncols", "nrows", "xllcorner", "yllcorner", "cellsize", "NODATA_value"} {
		k, err := next()
		if err != nil || k != key {
			return nil, fmt.Errorf("terrain: expected header %s", key)
		}
		raw, err := next()
		if err != nil {
			return nil, err
		}
		v, err := strconv.ParseFloat(raw, 64)
		if err != nil || !finite(v) {
			return nil, fmt.Errorf("terrain: invalid header number")
		}
		values[i] = v
	}
	cols, rows := values[0], values[1]
	if cols < 1 || rows < 1 || cols > 4000000 || rows > 4000000 || cols != math.Trunc(cols) || rows != math.Trunc(rows) || cols*rows > 4000000 || values[4] <= 0 || values[4] > .002084 || values[2] < -180 || values[2]+cols*values[4] > 180 || values[3] < -90 || values[3]+rows*values[4] > 90 {
		return nil, fmt.Errorf("terrain: dimensions, resolution or bounds invalid")
	}
	g := &Grid{Cols: int(cols), Rows: int(rows), West: values[2], South: values[3], Cell: values[4], NoData: values[5], SHA256: fmt.Sprintf("%x", sha256.Sum256(body))}
	for i := 0; i < g.Cols*g.Rows; i++ {
		raw, err := next()
		if err != nil {
			return nil, err
		}
		v, err := strconv.ParseFloat(raw, 64)
		if err != nil || !finite(v) || (v != g.NoData && (v < -500 || v > 9000)) {
			return nil, fmt.Errorf("terrain: invalid elevation")
		}
		g.Values = append(g.Values, v)
	}
	if s.Scan() || s.Err() != nil {
		return nil, fmt.Errorf("terrain: extra/invalid raster data")
	}
	return g, nil
}

func (g *Grid) height(lat, lon float64) (float64, bool) {
	x := int(math.Floor((lon - g.West) / g.Cell))
	y := g.Rows - 1 - int(math.Floor((lat-g.South)/g.Cell))
	if x < 0 || x >= g.Cols || y < 0 || y >= g.Rows {
		return 0, false
	}
	v := g.Values[y*g.Cols+x]
	return v, v != g.NoData
}

// Profile samples 19 great-circle rays out to 20 km at 100 m spacing.
// Earth curvature is subtracted without refraction. Every missing cell makes
// its ray unknown. This sampled coarse model is not a guaranteed clear view.
func (g *Grid) Profile(lat, lon float64) *Terrain {
	tr := &Terrain{Source: "USGS GMTED2010 mean 7.5 arc-second", SHA256: g.SHA256, Algorithm: TerrainAlgorithm, North: make([]*float64, 19)}
	base, ok := g.height(lat, lon)
	if !ok {
		return tr
	}
	r := math.Pi / 180
	phi, lambda := lat*r, lon*r
	for i := range tr.North {
		bearing := float64(i*5-45) * r
		high := 0.0
		valid := true
		for metres := 100.0; metres <= 20000; metres += 100 {
			arc := metres / 6371008.8
			p := math.Asin(math.Sin(phi)*math.Cos(arc) + math.Cos(phi)*math.Sin(arc)*math.Cos(bearing))
			l := lambda + math.Atan2(math.Sin(bearing)*math.Sin(arc)*math.Cos(phi), math.Cos(arc)-math.Sin(phi)*math.Sin(p))
			h, ok := g.height(p/r, l/r)
			if !ok {
				valid = false
				break
			}
			angle := math.Atan2(h-base-metres*metres/(2*6371008.8), metres) / r
			high = math.Max(high, angle)
		}
		if valid {
			v := high
			tr.North[i] = &v
		}
	}
	return tr
}
