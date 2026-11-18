package main

import (
	"bytes"
	"context"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"image"
	"image/color"
	"image/draw"
	"image/png"
	"io"
	"math"
	"net/http"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"time"
)

// Printable ASCII from the public-domain X11 misc-fixed 6x13 bitmap font.
//
//go:embed ascii.bin
var glyphs []byte

type (
	sample struct{ t, v float64 }
	series struct {
		name   string
		points []sample
	}
)

func finite(v float64) bool { return !math.IsNaN(v) && !math.IsInf(v, 0) }

func (r relay) graph(ctx context.Context, a alert, end time.Time) ([]byte, error) {
	start := end.Add(-6 * time.Hour).Unix()
	q := url.Values{"query": {a.Annotations["graph"]}, "start": {strconv.FormatInt(start, 10)}, "end": {strconv.FormatInt(end.Unix(), 10)}, "step": {"60"}}
	req, err := http.NewRequestWithContext(ctx, "GET", strings.TrimRight(r.prometheus, "/")+"/api/v1/query_range?"+q.Encode(), nil)
	if err != nil {
		return nil, err
	}
	resp, err := r.client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("Prometheus returned %d", resp.StatusCode)
	}
	var matrix struct {
		Status string
		Data   struct {
			ResultType string
			Result     []struct {
				Metric map[string]string
				Values [][2]json.RawMessage
			}
		}
	}
	if err = json.NewDecoder(io.LimitReader(resp.Body, 8<<20)).Decode(&matrix); err != nil {
		return nil, err
	}
	if matrix.Status != "success" || matrix.Data.ResultType != "matrix" || len(matrix.Data.Result) > 256 {
		return nil, errors.New("unusable query result")
	}
	var lines []series
	for _, row := range matrix.Data.Result {
		var names []string
		for k, v := range row.Metric {
			names = append(names, k+"="+v)
		}
		sort.Strings(names)
		s := series{name: strings.Join(names, ", ")}
		if s.name == "" {
			s.name = "value"
		}
		for _, pair := range row.Values {
			var t float64
			var raw string
			if json.Unmarshal(pair[0], &t) != nil || json.Unmarshal(pair[1], &raw) != nil {
				continue
			}
			v, err := strconv.ParseFloat(raw, 64)
			if err == nil && finite(v) && t >= float64(start) && t <= float64(end.Unix()) {
				s.points = append(s.points, sample{t, v})
			}
		}
		if len(s.points) > 0 {
			lines = append(lines, s)
		}
	}
	if len(lines) == 0 {
		return nil, errors.New("no graph samples")
	}
	threshold, err := strconv.ParseFloat(a.Annotations["threshold"], 64)
	if err != nil {
		threshold = math.NaN()
	}
	return render(lines, start, a.StartsAt.Unix(), threshold)
}

func label(img *image.RGBA, x, y int, s string, c color.RGBA) {
	for _, r := range s {
		if r < 32 || r > 126 {
			r = '?'
		}
		for dy, b := range glyphs[(r-32)*13 : (r-31)*13] {
			for dx := 0; dx < 6; dx++ {
				if b&(128>>dx) != 0 {
					img.SetRGBA(x+dx, y+dy, c)
				}
			}
		}
		x += 6
		if x >= img.Bounds().Max.X {
			return
		}
	}
}

func line(img *image.RGBA, x, y, xx, yy int, c color.RGBA) {
	dx, dy := xx-x, yy-y
	n := max(1, int(math.Abs(float64(dx))), int(math.Abs(float64(dy))))
	for i := 0; i <= n; i++ {
		img.SetRGBA(x+dx*i/n, y+dy*i/n, c)
	}
}

func render(lines []series, start, marker int64, threshold float64) ([]byte, error) {
	const left, right, top, bottom = 84, 740, 18, 214
	img := image.NewRGBA(image.Rect(0, 0, 760, 248+16*len(lines)))
	draw.Draw(img, img.Bounds(), image.NewUniform(color.White), image.Point{}, draw.Src)
	ink := color.RGBA{45, 48, 55, 255}
	lo, hi := math.Inf(1), math.Inf(-1)
	for _, s := range lines {
		for _, p := range s.points {
			lo = math.Min(lo, p.v)
			hi = math.Max(hi, p.v)
		}
	}
	if finite(threshold) {
		lo = math.Min(lo, threshold)
		hi = math.Max(hi, threshold)
	}
	y := func(v float64) int {
		if lo == hi {
			return (bottom + top) / 2
		}
		return bottom - int((v/2-lo/2)/(hi/2-lo/2)*(bottom-top))
	}
	x := func(t float64) int { return left + int((t-float64(start))/21600*(right-left)) }
	for _, v := range []float64{hi, lo} {
		label(img, 2, y(v)-6, strconv.FormatFloat(v, 'g', 5, 64), ink)
	}
	for h := 0; h <= 6; h++ {
		xx := x(float64(start + int64(h*3600)))
		line(img, xx, top, xx, bottom, color.RGBA{222, 225, 229, 255})
		label(img, xx-10, bottom+3, time.Unix(start+int64(h*3600), 0).UTC().Format("15:04"), ink)
	}
	if marker >= start && marker <= start+21600 {
		xx := x(float64(marker))
		line(img, xx, top, xx, bottom, ink)
		label(img, min(xx, right-28), 2, "start", ink)
	}
	if finite(threshold) {
		yy := y(threshold)
		for xx := left; xx < right; xx += 10 {
			line(img, xx, yy, xx+5, yy, color.RGBA{205, 65, 50, 255})
		}
	}
	palette := []color.RGBA{{30, 100, 180, 255}, {25, 140, 86, 255}, {145, 65, 165, 255}, {190, 115, 20, 255}}
	for i, s := range lines {
		c := palette[i%len(palette)]
		for j, p := range s.points {
			prev := s.points[max(0, j-1)]
			line(img, x(prev.t), y(prev.v), x(p.t), y(p.v), c)
		}
		line(img, left, 246+16*i, left+18, 246+16*i, c)
		label(img, left+24, 240+16*i, s.name, ink)
	}
	var out bytes.Buffer
	err := png.Encode(&out, img)
	return out.Bytes(), err
}
