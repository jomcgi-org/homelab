// SvelteKit's serializer preserves shared object references. Nearby sites often
// have identical forecast hours, so sharing those read-only records avoids
// repeating their fields in the inline HTML and navigation data. Keep every
// JSON field, including fields added by a future backend; this is not a projection
// or a forecast cache. The pool belongs to this one parsed response.
export function shareForecastHours(snapshot) {
  if (!Array.isArray(snapshot?.sites)) return snapshot;
  const hours = new Map();
  return {
    ...snapshot,
    sites: snapshot.sites.map((site) => {
      if (!Array.isArray(site.best_hours)) return site;
      return {
        ...site,
        best_hours: site.best_hours.map((hour) => {
          const key = JSON.stringify(hour);
          if (!hours.has(key)) hours.set(key, hour);
          return hours.get(key);
        }),
      };
    }),
  };
}
