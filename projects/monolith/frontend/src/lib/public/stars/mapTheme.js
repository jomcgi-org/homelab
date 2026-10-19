// MapLibre cannot consume CSS variables or color-mix(). Resolve the inherited
// sheet tokens through the browser, then convert to its supported RGB syntax.
export function readMapPalette(host) {
  const probe = document.createElement("span");
  probe.hidden = true;
  host.append(probe);
  const context = document.createElement("canvas").getContext("2d", {
    willReadFrequently: true,
  });
  try {
    return Object.fromEntries(
      [
        "sheet",
        "ink",
        "ink-2",
        "stroke",
        "band",
        "water",
        "low",
        "quarter",
        "mid",
        "upper",
        "high",
      ].map((role) => {
        probe.style.color = `var(--stars-${role})`;
        context.clearRect(0, 0, 1, 1);
        context.fillStyle = getComputedStyle(probe).color;
        context.fillRect(0, 0, 1, 1);
        const [r, g, b, a] = context.getImageData(0, 0, 1, 1).data;
        return [role, `rgba(${r}, ${g}, ${b}, ${a / 255})`];
      }),
    );
  } finally {
    probe.remove();
  }
}

export function cellColors(palette) {
  return [
    "interpolate",
    ["linear"],
    ["get", "score"],
    0,
    palette.low,
    35,
    palette.quarter,
    65,
    palette.mid,
    85,
    palette.upper,
    100,
    palette.high,
  ];
}

export function markerColors(palette) {
  return [
    "step",
    ["get", "score"],
    palette["ink-2"],
    35,
    palette.low,
    65,
    palette.quarter,
  ];
}

// Change paint only. setStyle would discard data sources, selected features and
// listeners, and could refit the camera. Symbol placement and all data stay put.
export function applyMapPalette(map, palette) {
  for (const layer of map.getStyle().layers) {
    if (layer.id === "sites" || layer.id === "sites-cells") continue;
    const water = /water/i.test(`${layer["source-layer"] ?? ""} ${layer.id}`);
    const paint = (name, value) => map.setPaintProperty(layer.id, name, value);
    if (layer.type === "background") paint("background-color", palette.sheet);
    if (layer.type === "fill")
      paint("fill-color", water ? palette.water : palette.band);
    if (layer.type === "fill-extrusion")
      paint("fill-extrusion-color", palette.band);
    if (layer.type === "line")
      paint("line-color", water ? palette.water : palette.stroke);
    if (layer.type === "symbol" && layer.layout?.["text-field"]) {
      paint("text-color", palette["ink-2"]);
      paint("text-halo-color", palette.sheet);
    }
  }
  map.setPaintProperty("sites-cells", "fill-color", cellColors(palette));
  map.setPaintProperty("sites", "circle-color", markerColors(palette));
  map.setPaintProperty("sites", "circle-stroke-color", palette.ink);
}
