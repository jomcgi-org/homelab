package main

import (
	"bytes"
	"math"
	"os"
	"strings"
	"testing"
)

func TestHubPhoto(t *testing.T) {
	f, err := os.Open("../../monolith/trips/testdata/geotagged.jpg")
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	c, err := jpegCamera(f)
	if err != nil {
		t.Fatal(err)
	}
	if math.Abs(*c.GPSLatitude-50.6745) > 0.000005 || *c.GPSLongitude != -120 || c.DateTimeOriginal != "2024:06:15 14:30:00" {
		t.Fatalf("unexpected location or camera time: %+v", c)
	}
	if c.ISO == nil || *c.ISO != 393 || c.FNumber == nil || *c.FNumber != 2.5 || c.ExposureTime == nil || *c.ExposureTime != 1.0/240 || c.FocalLengthIn35mmFormat == nil || *c.FocalLengthIn35mmFormat != 24 {
		t.Fatal("missing camera optics from hub JPEG")
	}
}

func TestJPEGRaster(t *testing.T) {
	photo, err := os.ReadFile("../../monolith/trips/testdata/geotagged.jpg")
	if err != nil {
		t.Fatal(err)
	}
	padded := append(append([]byte{}, photo...), make([]byte, 64)...)
	for _, ok := range [][]byte{photo, padded} {
		if err := decodeJPEG(bytes.NewReader(ok)); err != nil {
			t.Fatalf("complete JPEG rejected: %v", err)
		}
	}
	scan := bytes.Index(photo, []byte{255, 0xda})
	for _, cut := range []int{len(photo) - 2, (scan + len(photo)) / 2, scan + 16} {
		if decodeJPEG(bytes.NewReader(photo[:cut])) == nil {
			t.Fatalf("JPEG truncated at %d of %d accepted", cut, len(photo))
		}
		if cut == len(photo)-2 {
			continue // Appending the end marker restores the original.
		}
		// A valid end marker after too little entropy-coded data is still rejected.
		ended := append(append([]byte{}, photo[:cut]...), 255, 0xd9)
		if decodeJPEG(bytes.NewReader(ended)) == nil {
			t.Fatalf("JPEG truncated at %d of %d with an end marker accepted", cut, len(photo))
		}
	}
}

// A frame header above the pixel bound is rejected before the raster decodes.
func TestJPEGPixelBound(t *testing.T) {
	photo, err := os.ReadFile("../../monolith/trips/testdata/geotagged.jpg")
	if err != nil {
		t.Fatal(err)
	}
	big := append([]byte{}, photo...)
	sof := bytes.Index(big, []byte{255, 0xc0})
	if sof < 0 {
		t.Fatal("fixture has no baseline frame")
	}
	// SOFn: length(2) precision(1) height(2) width(2).
	copy(big[sof+5:], []byte{0xff, 0xff, 0xff, 0xff})
	if err := decodeJPEG(bytes.NewReader(big)); err == nil || !strings.Contains(err.Error(), "pixel bound") {
		t.Fatalf("oversized frame: %v", err)
	}
}
