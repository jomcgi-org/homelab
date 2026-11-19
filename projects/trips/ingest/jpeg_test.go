package main

import (
	"bytes"
	"math"
	"os"
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
		if err := verifyJPEG(bytes.NewReader(ok)); err != nil {
			t.Fatalf("complete JPEG rejected: %v", err)
		}
	}
	scan := bytes.Index(photo, []byte{255, 0xda})
	for _, cut := range []int{len(photo) - 2, (scan + len(photo)) / 2, scan} {
		if verifyJPEG(bytes.NewReader(photo[:cut])) == nil {
			t.Fatalf("JPEG truncated at %d of %d accepted", cut, len(photo))
		}
	}
}
