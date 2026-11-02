package connector

import "github.com/jomcgi/homelab/projects/scotland-connector/internal/summits"

// Sources are field-scoped. Existing providers keep their original reuse terms.
func ukvSources(items []CheckResultItem) []Source {
	fields := map[string]bool{}
	for _, item := range items {
		if item.Walk == nil || item.Walk.UKV == nil {
			continue
		}
		if item.Walk.UKV.RunAt != nil {
			fields["run_at"] = true
		}
		for _, p := range item.Walk.UKV.Points {
			fields["points[].sample_lat"] = true
			fields["points[].sample_lon"] = true
			fields["points[].cloud_base_reference"] = true
			if p.Orography != nil {
				fields["points[].orography_m_asl"] = true
			}
			for _, h := range p.Hours {
				fields["points[].hours[].valid_at"] = true
				if h.Wind != nil {
					fields["wind_kmh"] = true
				}
				if h.Gust != nil {
					fields["gust_kmh"] = true
				}
				if h.Cloud != nil {
					fields["cloud_base_m"] = true
				}
				if h.Freezing != nil {
					fields["freezing_level_m_asl"] = true
				}
			}
		}
	}
	var out []Source
	for _, field := range []string{"wind_kmh", "gust_kmh", "cloud_base_m", "freezing_level_m_asl"} {
		if !fields[field] {
			continue
		}
		out = append(out, Source{Name: "Met Office UKV", Attribution: "Contains Met Office data licensed under CC BY-SA 4.0", License: "CC BY-SA 4.0", URL: summits.Source, Use: "UKV-derived field only; metered access does not restrict reuse", Fields: []string{"data.items[].walk.ukv.points[].hours[]." + field}, LicenseURI: summits.License, Adaptation: summits.Adaptation})
	}
	// Attribute model metadata without changing the reuse terms of supplied
	// walk coordinates, point identities or explicit input elevations.
	for _, field := range []string{"run_at", "points[].sample_lat", "points[].sample_lon", "points[].cloud_base_reference", "points[].orography_m_asl", "points[].hours[].valid_at"} {
		if fields[field] {
			out = append(out, Source{Name: "Met Office UKV", Attribution: "Contains Met Office data licensed under CC BY-SA 4.0", License: "CC BY-SA 4.0", URL: summits.Source, Use: "UKV-derived field only; metered access does not restrict reuse", Fields: []string{"data.items[].walk.ukv." + field}, LicenseURI: summits.License, Adaptation: summits.Adaptation})
		}
	}
	return out
}
