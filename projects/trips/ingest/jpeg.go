package main

import (
	"bufio"
	"bytes"
	"encoding/binary"
	"errors"
	"io"
	"math"
	"strings"
)

// JPEG segments are at most 64 KiB. Skip unrelated markers without copying the
// raster and retain just the EXIF segment while resolving TIFF offsets.
func jpegCamera(r io.Reader) (camera, error) {
	var sig [2]byte
	if _, err := io.ReadFull(r, sig[:]); err != nil || sig != [2]byte{255, 216} {
		return camera{}, errors.New("not JPEG")
	}
	for {
		if _, err := io.ReadFull(r, sig[:]); err != nil {
			return camera{}, err
		}
		if sig[0] != 255 || sig[1] == 0xda || sig[1] == 0xd9 {
			return camera{}, errors.New("missing EXIF")
		}
		for sig[1] == 255 {
			if _, err := io.ReadFull(r, sig[1:]); err != nil {
				return camera{}, err
			}
		}
		marker := sig[1]
		if _, err := io.ReadFull(r, sig[:]); err != nil {
			return camera{}, err
		}
		size := int(binary.BigEndian.Uint16(sig[:])) - 2
		if size < 0 {
			return camera{}, errors.New("invalid JPEG segment")
		}
		if marker != 0xe1 {
			if _, err := io.CopyN(io.Discard, r, int64(size)); err != nil {
				return camera{}, err
			}
			continue
		}
		data := make([]byte, size)
		if _, err := io.ReadFull(r, data); err != nil {
			return camera{}, err
		}
		if bytes.HasPrefix(data, []byte("Exif\x00\x00")) {
			return readTIFF(data[6:])
		}
	}
}

// verifyJPEG walks every segment and entropy-coded scan up to the end marker
// with a small buffer. A truncated upload, or one with no frame or scan, has no
// usable raster. Bytes after the end marker are allowed, as cameras append data.
func verifyJPEG(r io.Reader) error {
	b := bufio.NewReader(r)
	var sig [2]byte
	if _, err := io.ReadFull(b, sig[:]); err != nil || sig != [2]byte{255, 216} {
		return errors.New("not JPEG")
	}
	frame, scanned, inScan := false, false, false
	for {
		// A scan ends on a marker whose 0xFF it has already read.
		marker := byte(255)
		if !inScan {
			var err error
			if marker, err = b.ReadByte(); err != nil {
				return err
			}
		}
		inScan = false
		var err error
		if marker != 255 {
			return errors.New("invalid JPEG marker")
		}
		for marker == 255 {
			if marker, err = b.ReadByte(); err != nil {
				return err
			}
		}
		switch {
		case marker == 0xd9:
			if !frame || !scanned {
				return errors.New("JPEG has no raster")
			}
			return nil
		case marker >= 0xd0 && marker <= 0xd7, marker == 0x01:
			continue
		}
		if _, err := io.ReadFull(b, sig[:]); err != nil {
			return err
		}
		size := int64(binary.BigEndian.Uint16(sig[:])) - 2
		if size < 0 {
			return errors.New("invalid JPEG segment")
		}
		if _, err := io.CopyN(io.Discard, b, size); err != nil {
			return err
		}
		if marker >= 0xc0 && marker <= 0xcf && marker != 0xc4 && marker != 0xc8 && marker != 0xcc {
			frame = true
		}
		if marker != 0xda {
			continue
		}
		if !frame {
			return errors.New("JPEG scan before frame")
		}
		// Skip entropy-coded data. 0xFF00 is a stuffed byte and RSTn markers
		// stay inside the scan; any other marker ends it.
		data := 0
		for {
			c, err := b.ReadByte()
			if err != nil {
				return err
			}
			if c != 255 {
				data++
				continue
			}
			next, err := b.Peek(1)
			if err != nil {
				return err
			}
			if next[0] == 0 || (next[0] >= 0xd0 && next[0] <= 0xd7) {
				_, _ = b.ReadByte()
				data++
				continue
			}
			inScan = true
			break
		}
		if data > 0 {
			scanned = true
		}
	}
}

type field struct {
	order binary.ByteOrder
	kind  uint16
	data  []byte
}

func (f field) integer() *int {
	var n int
	switch {
	case f.kind == 3 && len(f.data) >= 2:
		n = int(f.order.Uint16(f.data))
	case f.kind == 4 && len(f.data) >= 4:
		n = int(f.order.Uint32(f.data))
	default:
		return nil
	}
	return &n
}

func (f field) rational(i int) *float64 {
	if len(f.data) < (i+1)*8 {
		return nil
	}
	data := f.data[i*8:]
	if f.kind == 12 {
		n := math.Float64frombits(f.order.Uint64(data))
		if math.IsNaN(n) || math.IsInf(n, 0) {
			return nil
		}
		return &n
	}
	if f.kind != 5 {
		return nil
	}
	denominator := f.order.Uint32(data[4:])
	if denominator == 0 {
		return nil
	}
	n := float64(f.order.Uint32(data)) / float64(denominator)
	return &n
}

func (f field) text() string {
	if f.kind != 2 {
		return ""
	}
	return strings.TrimRight(string(f.data), "\x00")
}

func (f field) degrees(ref field) *float64 {
	d, m, s := f.rational(0), f.rational(1), f.rational(2)
	if d == nil || m == nil || s == nil {
		return nil
	}
	n := *d + *m/60 + *s/3600
	if ref.text() == "S" || ref.text() == "W" {
		n = -n
	}
	return &n
}

type tiffData struct {
	order binary.ByteOrder
	data  []byte
}

func (t tiffData) directory(offset int) map[uint16]field {
	fields := map[uint16]field{}
	if offset < 8 || offset > len(t.data)-2 {
		return fields
	}
	count := int(t.order.Uint16(t.data[offset:]))
	offset += 2
	if count > (len(t.data)-offset)/12 {
		return fields
	}
	for i := 0; i < count; i++ {
		entry := t.data[offset+i*12:]
		kind := t.order.Uint16(entry[2:])
		var width uint64
		switch kind {
		case 2:
			width = 1
		case 3:
			width = 2
		case 4:
			width = 4
		case 5, 12:
			width = 8
		default:
			continue
		}
		size := uint64(t.order.Uint32(entry[4:])) * width
		value := entry[8:12]
		if size > 4 {
			start := uint64(t.order.Uint32(value))
			if start+size > uint64(len(t.data)) {
				continue
			}
			value = t.data[start : start+size]
		}
		fields[t.order.Uint16(entry)] = field{t.order, kind, value[:size]}
	}
	return fields
}

func (t tiffData) subdir(f field) map[uint16]field {
	if offset := f.integer(); offset != nil {
		return t.directory(*offset)
	}
	return nil
}

func readTIFF(data []byte) (camera, error) {
	if len(data) < 8 {
		return camera{}, io.ErrUnexpectedEOF
	}
	t := tiffData{data: data}
	switch string(data[:4]) {
	case "II*\x00":
		t.order = binary.LittleEndian
	case "MM\x00*":
		t.order = binary.BigEndian
	default:
		return camera{}, errors.New("invalid TIFF header")
	}
	root := t.directory(int(t.order.Uint32(data[4:])))
	gps := t.subdir(root[0x8825])
	optics := root
	for tag, value := range t.subdir(root[0x8769]) {
		optics[tag] = value
	}
	c := camera{
		GPSLatitude: gps[2].degrees(gps[1]), GPSLongitude: gps[4].degrees(gps[3]),
		ModifyDate: root[0x132].text(), DateTimeOriginal: optics[0x9003].text(),
		ISO: optics[0x8827].integer(), FocalLengthIn35mmFormat: optics[0xa405].integer(),
		ExposureTime: optics[0x829a].rational(0), FNumber: optics[0x829d].rational(0),
	}
	if c.GPSLatitude == nil || c.GPSLongitude == nil || math.Abs(*c.GPSLatitude) > 90 || math.Abs(*c.GPSLongitude) > 180 {
		return camera{}, errors.New("image has no GPS coordinates")
	}
	return c, nil
}
