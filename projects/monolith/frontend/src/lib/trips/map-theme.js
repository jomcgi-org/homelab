// MapLibre consumes resolved colours, not CSS custom-property expressions.
// Read the sheet containing this map (the root still has legacy tier tokens).
export function mapPalette(container) {
  const style = getComputedStyle(container);
  const token = (name) => style.getPropertyValue(name).trim();
  return {
    ink: token("--ink"),
    muted: token("--ink-2"),
    sheet: token("--sheet"),
    paper: token("--paper"),
  };
}

export function themeBasemap(map, palette) {
  // Update paint in place. setStyle would discard route sources, selection,
  // photo-time hillshade and listeners; recreating the map loses the camera.
  for (const layer of map.getStyle().layers) {
    if (/^(route|trip-start|hillshade)/.test(layer.id)) continue;
    if (layer.type === "background")
      map.setPaintProperty(layer.id, "background-color", palette.sheet);
    if (layer.type === "fill")
      map.setPaintProperty(layer.id, "fill-color", palette.paper);
    if (layer.type === "line")
      map.setPaintProperty(layer.id, "line-color", palette.muted);
    if (layer.type === "symbol" && layer.layout?.["text-field"]) {
      map.setPaintProperty(layer.id, "text-color", palette.ink);
      map.setPaintProperty(layer.id, "text-halo-color", palette.sheet);
    }
  }
}

export function watchMapScheme(update) {
  const media = window.matchMedia("(prefers-color-scheme: dark)");
  const observer = new MutationObserver(update);
  observer.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["data-theme"],
  });
  media.addEventListener("change", update);
  update();
  return () => {
    observer.disconnect();
    media.removeEventListener("change", update);
  };
}
