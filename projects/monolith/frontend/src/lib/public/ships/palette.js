const VESSEL_KEYS = [
  "passenger",
  "cargo",
  "tanker",
  "hsc",
  "special",
  "unknown",
];
export const HEAT_STEPS = 6;

// MapLibre's color parser does not accept CSS variables or color-mix. Resolve
// them on the actual map surface, then let the browser rasterize CSS Color 4
// into sRGB bytes. The CSS legend and the GPU receive the same resolved color.
export function readPalette(surface) {
  const styles = getComputedStyle(surface);
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = 1;
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  const color = (token) => {
    ctx.clearRect(0, 0, 1, 1);
    ctx.fillStyle = styles.getPropertyValue(token).trim();
    ctx.fillRect(0, 0, 1, 1);
    const [r, g, b, a] = ctx.getImageData(0, 0, 1, 1).data;
    return `rgba(${r}, ${g}, ${b}, ${a / 255})`;
  };
  return {
    ...Object.fromEntries(
      VESSEL_KEYS.map((key) => [key, color(`--ships-${key}`)]),
    ),
    heat: Array.from({ length: HEAT_STEPS }, (_, i) =>
      color(`--ships-heat-${i + 1}`),
    ),
    ink: color("--ink"),
    muted: color("--ink-2"),
    sheet: color("--sheet"),
    paper: color("--paper"),
    water: color("--ships-water"),
  };
}

// Repaint the existing provider style in place. Never setStyle: its sources,
// attribution, sprites, camera and our live layers must retain their identity.
export function paintBasemap(map, palette) {
  for (const layer of map.getStyle().layers) {
    if (["heat-fill", "ship-track-line", "vessels"].includes(layer.id))
      continue;
    const set = (property, value) =>
      map.setPaintProperty(layer.id, property, value);
    if (layer.type === "background") set("background-color", palette.sheet);
    if (layer.type === "raster") set("raster-opacity", 0);
    if (layer.type === "fill") {
      set(
        "fill-color",
        layer.id.includes("water") ? palette.water : palette.paper,
      );
      if (layer.paint?.["fill-outline-color"])
        set("fill-outline-color", palette.muted);
    }
    if (layer.type === "line")
      set(
        "line-color",
        layer.id.includes("water") ? palette.water : palette.muted,
      );
    if (layer.type === "symbol" && layer.layout?.["text-field"]) {
      set("text-color", palette.muted);
      set("text-halo-color", palette.sheet);
    }
  }
}
