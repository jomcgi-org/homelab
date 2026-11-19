package main

import (
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"net/url"
	"sort"
	"strconv"
	"time"
)

type location struct {
	ID   string  `json:"id"`
	Name *string `json:"name"`
	Lat  float64 `json:"lat"`
	Lon  float64 `json:"lon"`
}
type site struct {
	location
	Altitude int    `json:"altitude_m"`
	Zone     string `json:"lp_zone"`
}
type hour struct {
	Time  time.Time
	Cloud float64
	Sun   float64
}
type forecast struct {
	Fetched string
	Hours   []hour
}

// NOAA solar position as used by Astral, including the below-horizon
// refraction correction. Only elevations below the twilight floor are kept.
func elevation(t time.Time, lat, lon float64) float64 {
	const rad = math.Pi / 180
	c := (float64(t.Unix())/86400 + 2440587.5 - 2451545) / 36525
	l := math.Mod(280.46646+c*(36000.76983+c*.0003032), 360) * rad
	m := (357.52911 + c*(35999.05029-.0001537*c)) * rad
	ecc := .016708634 - c*(.000042037+.0000001267*c)
	centre := math.Sin(m)*(1.914602-c*(.004817+.000014*c)) + math.Sin(2*m)*(.019993-.000101*c) + math.Sin(3*m)*.000289
	omega := (125.04 - 1934.136*c) * rad
	lambda := l + (centre-.00569-.00478*math.Sin(omega))*rad
	ob := (23 + (26+(21.448-c*(46.815+c*(.00059-.001813*c)))/60)/60 + .00256*math.Cos(omega)) * rad
	dec := math.Asin(math.Sin(ob) * math.Sin(lambda))
	y := math.Pow(math.Tan(ob/2), 2)
	eq := 4 / rad * (y*math.Sin(2*l) - 2*ecc*math.Sin(m) + 4*ecc*y*math.Sin(m)*math.Cos(2*l) - .5*y*y*math.Sin(4*l) - 1.25*ecc*ecc*math.Sin(2*m))
	t = t.UTC()
	angle := (float64(t.Hour()*60+t.Minute()) + float64(t.Second())/60 + eq + 4*lon - 720) * rad / 4
	alt := math.Asin(math.Sin(lat*rad)*math.Sin(dec)+math.Cos(lat*rad)*math.Cos(dec)*math.Cos(angle)) / rad
	if alt < -0.575 {
		alt -= 20.772 / math.Tan(alt*rad) / 3600
	}
	return alt
}

type entry struct {
	Time string
	Data struct {
		Instant struct {
			Details struct {
				Cloud *float64 `json:"cloud_area_fraction"`
			}
		}
	}
}

func score(s site, series []entry) []hour {
	var hours []hour
	for _, e := range series {
		t, err := time.Parse(time.RFC3339, e.Time)
		if err != nil {
			continue
		}
		sun := elevation(t, s.Lat, s.Lon)
		if sun >= -10 {
			continue
		}
		cloud := 100.
		if e.Data.Instant.Details.Cloud != nil {
			cloud = *e.Data.Instant.Details.Cloud
		}
		hours = append(hours, hour{t, cloud, math.RoundToEven(sun*10) / 10})
	}
	sort.Slice(hours, func(i, j int) bool { return hours[i].Time.Before(hours[j].Time) })
	return hours
}

func fetch(client *http.Client, s site) ([]hour, error) {
	q := url.Values{"lat": {strconv.FormatFloat(s.Lat, 'f', -1, 64)}, "lon": {strconv.FormatFloat(s.Lon, 'f', -1, 64)}, "altitude": {strconv.Itoa(s.Altitude)}}
	req, err := http.NewRequest("GET", "https://api.met.no/weatherapi/locationforecast/2.0/complete?"+q.Encode(), nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("User-Agent", "jomcgi-homelab-stars/1.0 https://jomcgi.dev")
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("met.no: %s", resp.Status)
	}
	var data struct{ Properties struct{ Timeseries []entry } }
	if err = json.NewDecoder(resp.Body).Decode(&data); err != nil {
		return nil, err
	}
	if data.Properties.Timeseries == nil {
		return nil, fmt.Errorf("missing timeseries")
	}
	return score(s, data.Properties.Timeseries), nil
}
