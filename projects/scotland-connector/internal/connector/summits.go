package connector

import "github.com/jomcgi/homelab/projects/scotland-connector/internal/summits"

// Sources are field-scoped. Existing providers keep their original reuse terms.
func ukvSources(items []CheckResultItem) []Source {
	fields := map[string]bool{}
	for _, item := range items {
		if item.Walk == nil || item.Walk.UKV == nil {
			continue
		}
		for _, p := range item.Walk.UKV.Points {
			for _, h := range p.Hours {
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
	return out
}
