package astro

import (
	"math"
	"time"
)

// sunAltitude is the sun's centre at rise and set: refraction (about 34')
// plus the disc's radius (about 16').
const sunAltitude = -0.833

// SunElevation uses the same solar coordinates as the stars moon calculation.
func SunElevation(t time.Time, lat, lon float64) float64 {
	c := sunCoords(days(t))
	h := rad*(280.16+360.9856235*days(t)+lon) - c.ra
	phi := rad * lat
	return math.Asin(math.Sin(phi)*math.Sin(c.dec)+math.Cos(phi)*math.Cos(c.dec)*math.Cos(h)) / rad
}

// SunTimes is sunrise and sunset on the UTC calendar day of day at lat, lon,
// by the NOAA sunrise equation, the same one the monolith's hikes job gates
// daylight hours with (projects/monolith/hikes/forecast.py sun_times), so the
// two agree to the second. ok is false in polar night or day, which Scotland
// never reaches.
func SunTimes(day time.Time, lat, lon float64) (rise, set time.Time, ok bool) {
	y, m, d := day.UTC().Date()
	noon := time.Date(y, m, d, 12, 0, 0, 0, time.UTC)
	julianDay := float64(noon.Unix())/86400 + 2440587.5
	n := julianDay - 2451545.0 + 0.0008

	meanNoon := n - lon/360
	anomaly := rad * math.Mod(357.5291+0.98560028*meanNoon, 360)
	centre := 1.9148*math.Sin(anomaly) + 0.0200*math.Sin(2*anomaly) + 0.0003*math.Sin(3*anomaly)
	eclipticLon := rad * math.Mod(anomaly/rad+centre+180+102.9372, 360)
	transit := 2451545.0 + meanNoon + 0.0053*math.Sin(anomaly) - 0.0069*math.Sin(2*eclipticLon)
	dec := math.Asin(math.Sin(eclipticLon) * math.Sin(obliquity))

	phi := rad * lat
	cosH := (math.Sin(rad*sunAltitude) - math.Sin(phi)*math.Sin(dec)) / (math.Cos(phi) * math.Cos(dec))
	if cosH >= 1 || cosH <= -1 {
		return time.Time{}, time.Time{}, false
	}
	h := math.Acos(cosH) / rad / 360
	at := func(jd float64) time.Time {
		return time.Unix(0, int64((jd-2440587.5)*86400*1e9)).UTC()
	}
	return at(transit - h), at(transit + h), true
}
