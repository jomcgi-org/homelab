// Package astro computes the moon's position and phase for the dark-sky
// answers. It uses truncated lunar and solar series after Meeus
// (Astronomical Algorithms), good to well under a degree against a full
// ephemeris: plenty for "is the moon up, and how bright".
package astro

import (
	"math"
	"time"
)

const (
	rad = math.Pi / 180
	// j2000 is 2000-01-01T12:00:00Z as a Unix time.
	j2000        = 946728000
	obliquity    = 23.4397 * rad
	earthRadius  = 6378.14     // km
	sunDistance  = 149598000.0 // km
	secondsPerDy = 86400.0
)

func days(t time.Time) float64 {
	return float64(t.Unix()-j2000) / secondsPerDy
}

func rightAscension(l, b float64) float64 {
	return math.Atan2(math.Sin(l)*math.Cos(obliquity)-math.Tan(b)*math.Sin(obliquity), math.Cos(l))
}

func declination(l, b float64) float64 {
	return math.Asin(math.Sin(b)*math.Cos(obliquity) + math.Cos(b)*math.Sin(obliquity)*math.Sin(l))
}

type equatorial struct{ ra, dec, dist float64 }

// moonCoords keeps the largest periodic terms of the lunar theory: the
// equation of the centre, evection, variation and the annual equation in
// longitude, four terms in latitude and three in distance. With only the
// first term the error near the horizon passes a degree.
func moonCoords(d float64) equatorial {
	l0 := rad * (218.316 + 13.176396*d)  // mean longitude
	m := rad * (134.963 + 13.064993*d)   // moon's mean anomaly
	f := rad * (93.272 + 13.229350*d)    // argument of latitude
	e := rad * (297.850 + 12.190749*d)   // mean elongation from the sun
	ms := rad * (357.529 + 0.98560028*d) // sun's mean anomaly
	l := l0 + rad*(6.289*math.Sin(m)+1.274*math.Sin(2*e-m)+0.658*math.Sin(2*e)+
		0.214*math.Sin(2*m)-0.186*math.Sin(ms)-0.114*math.Sin(2*f))
	b := rad * (5.128*math.Sin(f) + 0.280*math.Sin(m+f) + 0.277*math.Sin(m-f) + 0.173*math.Sin(2*e-f))
	dist := 385001 - 20905*math.Cos(m) - 3699*math.Cos(2*e-m) - 2956*math.Cos(2*e)
	return equatorial{ra: rightAscension(l, b), dec: declination(l, b), dist: dist}
}

func sunCoords(d float64) equatorial {
	m := rad * (357.5291 + 0.98560028*d)
	c := rad * (1.9148*math.Sin(m) + 0.02*math.Sin(2*m) + 0.0003*math.Sin(3*m))
	l := m + c + rad*102.9372 + math.Pi
	return equatorial{ra: rightAscension(l, 0), dec: declination(l, 0), dist: sunDistance}
}

// Moon is the moon's geocentric position and phase at one instant. Computing
// it is most of the cost; Altitude for a given place is cheap, so callers
// that need many places at the same hour share one Moon.
type Moon struct {
	eq  equatorial
	d   float64
	Lit float64 // lit fraction of the disc, 0 to 1
}

// MoonAt is the moon at t.
func MoonAt(t time.Time) Moon {
	d := days(t)
	s, m := sunCoords(d), moonCoords(d)
	phi := math.Acos(math.Sin(s.dec)*math.Sin(m.dec) + math.Cos(s.dec)*math.Cos(m.dec)*math.Cos(s.ra-m.ra))
	inc := math.Atan2(s.dist*math.Sin(phi), m.dist-s.dist*math.Cos(phi))
	return Moon{eq: m, d: d, Lit: (1 + math.Cos(inc)) / 2}
}

// Altitude is the moon's topocentric altitude in degrees seen from lat, lon
// (degrees, east positive), without atmospheric refraction.
func (m Moon) Altitude(lat, lon float64) float64 {
	phi := rad * lat
	h := rad*(280.16+360.9856235*m.d) + rad*lon - m.eq.ra
	alt := math.Asin(math.Sin(phi)*math.Sin(m.eq.dec) + math.Cos(phi)*math.Cos(m.eq.dec)*math.Cos(h))
	// Parallax: from the surface the moon sits lower than from the centre.
	alt -= math.Asin(earthRadius / m.eq.dist * math.Cos(alt))
	return alt / rad
}

// MoonAltitude is MoonAt(t).Altitude(lat, lon).
func MoonAltitude(t time.Time, lat, lon float64) float64 { return MoonAt(t).Altitude(lat, lon) }

// MoonIllumination is MoonAt(t).Lit.
func MoonIllumination(t time.Time) float64 { return MoonAt(t).Lit }
