const VESSEL_KEYS = [
  "passenger",
  "cargo",
  "tanker",
  "hsc",
  "special",
  "unknown",
];
export const HEAT_STEPS = 6;

// Resolve the same palette tokens as the legend before passing them to MapLibre.
export function readPalette(surface) {
  const probe = document.createElement("span");
  probe.hidden = true;
  surface.append(probe);
  const tone = (name) => {
    probe.className = "ships-tone-" + name;
    return getComputedStyle(probe).color;
  };
  try {
    return {
      ...Object.fromEntries(VESSEL_KEYS.map((key) => [key, tone(key)])),
      heat: Array.from({ length: HEAT_STEPS }, (_, i) =>
        tone("heat-" + (i + 1)),
      ),
      ink: tone("ink"),
      muted: tone("muted"),
      sheet: tone("sheet"),
      paper: tone("paper"),
      water: tone("water"),
    };
  } finally {
    probe.remove();
  }
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
