import { CLUSTER_COLORS, SHEET_CLUSTER_COLORS } from "./clusters.js";

// The private consumer keeps the original paint values, independent of the
// public scheme. These are the existing canvas colours, centralized here.
const LEGACY = {
  clusters: { ...CLUSTER_COLORS, __other: "#FFFFFF" },
  ink: "#141414",
  sheet: "#FFFFFF",
  selected: "#FF6B5B",
  touched: "#FFDE01",
};

export function resolveGraphPalette(stage, sheet = false) {
  if (!sheet) return LEGACY;
  // Reading a custom property alone can leave var() or color-mix() authored
  // text. Resolve a real colour property in the graph's inherited scope.
  const probe = document.createElement("span");
  probe.style.display = "none";
  stage.append(probe);
  const resolve = (value) => {
    probe.style.color = value;
    return getComputedStyle(probe).color;
  };
  try {
    return {
      clusters: Object.fromEntries(
        Object.entries({
          ...SHEET_CLUSTER_COLORS,
          __other: "var(--sheet)",
        }).map(([type, value]) => [type, resolve(value)]),
      ),
      ink: resolve("var(--ink)"),
      sheet: resolve("var(--sheet)"),
      selected: resolve("var(--accent-ink)"),
      touched: resolve("var(--accent)"),
    };
  } finally {
    probe.remove();
  }
}

export function watchGraphPalette(stage, update) {
  const refresh = () => update(resolveGraphPalette(stage, true));
  const observer = new MutationObserver(refresh);
  observer.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["data-theme"],
  });
  const media = window.matchMedia("(prefers-color-scheme: dark)");
  media.addEventListener("change", refresh);
  return () => {
    observer.disconnect();
    media.removeEventListener("change", refresh);
  };
}
