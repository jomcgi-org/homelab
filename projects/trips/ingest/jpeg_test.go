package main

import (
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
