package stars

import "math"

const earthRadiusKm = 6371.0088

// cellDeg is the grid cell edge. At Scottish latitudes 0.25 deg is roughly
// 28 km north-south and 15 km east-west, so a 150 km radius touches a few
// hundred cells holding a few thousand sites at most.
const cellDeg = 0.25

// DistanceKm is the haversine great-circle distance.
func DistanceKm(lat1, lon1, lat2, lon2 float64) float64 {
	p1, p2 := lat1*math.Pi/180, lat2*math.Pi/180
	dp := p2 - p1
	dl := (lon2 - lon1) * math.Pi / 180
	a := math.Sin(dp/2)*math.Sin(dp/2) + math.Cos(p1)*math.Cos(p2)*math.Sin(dl/2)*math.Sin(dl/2)
	return 2 * earthRadiusKm * math.Asin(math.Min(1, math.Sqrt(a)))
}

// kmPerDegLat is the mean meridian degree length.
const kmPerDegLat = 111.195

// FastDistanceKm is the equirectangular distance using the mean of the two
// latitude cosines, which needs no trig per pair. Within the 150 km query cap
// its error against haversine is far below the 0.1 km the API reports.
func FastDistanceKm(lat1, lon1, cos1, lat2, lon2, cos2 float64) float64 {
	dy := (lat2 - lat1) * kmPerDegLat
	dx := (lon2 - lon1) * kmPerDegLat * (cos1 + cos2) / 2
	return math.Sqrt(dx*dx + dy*dy)
}

type cellKey struct{ lat, lon int32 }

func keyFor(lat, lon float64) cellKey {
	return cellKey{int32(math.Floor(lat / cellDeg)), int32(math.Floor(lon / cellDeg))}
}

// grid buckets site indices by cell so a radius query scans only nearby cells.
type grid struct {
	cells map[cellKey][]int32
}

func newGrid(sites []Site) grid {
	g := grid{cells: make(map[cellKey][]int32)}
	for i := range sites {
		k := keyFor(sites[i].Lat, sites[i].Lon)
		g.cells[k] = append(g.cells[k], int32(i))
	}
	return g
}

// Candidate is a site within the query radius.
type Candidate struct {
	Site       *Site
	DistanceKm float64
}

// Within returns every site within radiusKm of (lat, lon), unordered.
func (d *Dataset) Within(lat, lon, radiusKm float64) []Candidate {
	var out []Candidate
	d.visit(lat, lon, radiusKm, func(s *Site, dist float64) {
		out = append(out, Candidate{Site: s, DistanceKm: dist})
	})
	return out
}

// visit calls fn for every site within radiusKm of (lat, lon) without
// allocating.
func (d *Dataset) visit(lat, lon, radiusKm float64, fn func(*Site, float64)) {
	dLat := radiusKm / 111.0
	// Widen the longitude span by the cosine at the pole-ward edge of the
	// box so the box always contains the circle.
	edge := math.Min(89.9, math.Abs(lat)+dLat)
	dLon := radiusKm / (111.320 * math.Cos(edge*math.Pi/180))
	lo, hi := keyFor(lat-dLat, lon-dLon), keyFor(lat+dLat, lon+dLon)
	cosQ := math.Cos(lat * math.Pi / 180)
	for la := lo.lat; la <= hi.lat; la++ {
		for lo2 := lo.lon; lo2 <= hi.lon; lo2++ {
			for _, i := range d.index.cells[cellKey{la, lo2}] {
				s := &d.Sites[i]
				if dist := FastDistanceKm(lat, lon, cosQ, s.Lat, s.Lon, s.cosLat); dist <= radiusKm {
					fn(s, dist)
				}
			}
		}
	}
}
