// Command stars-aurora is an offline-first, additive stars snapshot producer.
// It never uploads. Collection requires an explicit flag and is not scheduled.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/aurora"
)

func read(path string, limit int64) ([]byte, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	b, err := io.ReadAll(io.LimitReader(f, limit+1))
	if int64(len(b)) > limit {
		return nil, fmt.Errorf("input exceeds byte limit")
	}
	return b, err
}

func main() {
	input := flag.String("input", "", "local provider input JSON")
	grid := flag.String("grid", "", "local WGS84 GMTED2010 mean ESRI ASCII raster; outputs site profiles")
	sites := flag.String("sites", "", "local site manifest JSON (array of WeatherInput)")
	collect := flag.Bool("collect", false, "explicitly collect NOAA/MET for sites; never publishes")
	flag.Parse()
	if err := run(*input, *grid, *sites, *collect, os.Stdout); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func run(input, grid, sites string, collect bool, out io.Writer) error {
	if (input != "" && (grid != "" || collect || sites != "")) || (grid != "" && collect) {
		return fmt.Errorf("choose exactly one mode: input, grid+sites, or collect+sites")
	}
	var b []byte
	var err error
	if grid != "" || collect {
		raw, err := read(sites, aurora.MaxBytes)
		if err != nil {
			return err
		}
		var points []aurora.WeatherInput
		if err = aurora.Decode(raw, &points); err != nil {
			return err
		}
		if len(points) == 0 || len(points) > aurora.MaxSites {
			return fmt.Errorf("site count outside limit")
		}
		if grid != "" {
			raw, err = read(grid, aurora.MaxTerrainBytes)
			if err != nil {
				return err
			}
			g, err := aurora.ParseGrid(raw)
			if err != nil {
				return err
			}
			for i, p := range points {
				if p.Lat == nil || p.Lon == nil || p.ID == "" || *p.Lat < 54.5 || *p.Lat > 61 || *p.Lon < -9 || *p.Lon > -.5 {
					return fmt.Errorf("site identity/coordinate missing or outside Scotland")
				}
				points[i].Terrain = g.Profile(*p.Lat, *p.Lon)
			}
			return json.NewEncoder(out).Encode(points)
		}
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Minute)
		defer cancel()
		in, err := aurora.Collect(ctx, &http.Client{}, points, time.Now)
		if err != nil {
			return err
		}
		b, err = aurora.Materialize(in)
		if err != nil {
			return err
		}
	} else {
		raw, err := read(input, aurora.MaxBytes)
		if err != nil {
			return err
		}
		var in aurora.Inputs
		if err = aurora.Decode(raw, &in); err != nil {
			return err
		}
		b, err = aurora.Materialize(in)
		if err != nil {
			return err
		}
	}
	_, err = out.Write(append(b, '\n'))
	return err
}
