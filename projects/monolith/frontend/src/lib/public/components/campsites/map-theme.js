// MapLibre cannot read CSS variables. Resolve the sheet's solid semantic roles
// at the canvas boundary, and update paint only: setStyle would discard the
// live GeoJSON, interaction listeners and layer state.
export function readPalette(element) {
  const style = getComputedStyle(element);
  const token = (name) => style.getPropertyValue(name).trim();
  return {
    sheet: token("--sheet"),
    paper: token("--paper"),
    ink: token("--ink"),
    muted: token("--ink-2"),
    water: token("--tone-cache"),
    clear: token("--camp-clear"),
    low: token("--camp-mixed"),
    medium: token("--camp-poor"),
    none: token("--camp-none"),
    hot: token("--tone-disk"),
  };
}

export const scorePaint = (p) => [
  "interpolate",
  ["linear"],
  ["get", "best_score"],
  0,
  p.none,
  40,
  p.low,
  70,
  p.medium,
  90,
  p.clear,
];

export const heatPaint = (p) => [
  "interpolate",
  ["linear"],
  ["heatmap-density"],
  0,
  "transparent",
  0.3,
  p.low,
  0.55,
  p.low,
  0.75,
  p.medium,
  0.9,
  p.hot,
  1,
  p.hot,
];

// MapLibre's interpolation produces colors in the library's own supported
// format, including for the softly tinted water and vegetation regions.
const tint = (paper, tone, amount) => [
  "interpolate",
  ["linear"],
  amount,
  0,
  paper,
  1,
  tone,
];

export function applyPalette(map, p) {
  for (const layer of map.getStyle().layers ?? []) {
    if (layer.id === "parks") {
      map.setPaintProperty(layer.id, "circle-color", scorePaint(p));
      map.setPaintProperty(layer.id, "circle-stroke-color", p.ink);
    } else if (layer.id === "weather-heat") {
      map.setPaintProperty(layer.id, "heatmap-color", heatPaint(p));
    } else if (layer.type === "background") {
      map.setPaintProperty(layer.id, "background-color", p.sheet);
    } else if (layer.type === "fill") {
      const kind = `${layer.id} ${layer["source-layer"] ?? ""}`;
      map.setPaintProperty(
        layer.id,
        "fill-color",
        /water/.test(kind)
          ? tint(p.paper, p.water, 0.18)
          : /park|landcover|landuse/.test(kind)
            ? tint(p.paper, p.clear, 0.08)
            : p.paper,
      );
      if (layer.paint?.["fill-outline-color"] !== undefined) {
        map.setPaintProperty(
          layer.id,
          "fill-outline-color",
          tint(p.paper, p.muted, 0.45),
        );
      }
    } else if (layer.type === "fill-extrusion") {
      map.setPaintProperty(layer.id, "fill-extrusion-color", p.paper);
    } else if (layer.type === "line") {
      map.setPaintProperty(
        layer.id,
        "line-color",
        tint(p.paper, p.muted, 0.45),
      );
    } else if (layer.type === "symbol" && layer.layout?.["text-field"]) {
      map.setPaintProperty(layer.id, "text-color", p.ink);
      map.setPaintProperty(layer.id, "text-halo-color", p.sheet);
    }
  }
}
