// Pure test helpers: no browser globals or module-load theme selection.
export const schemes = ["light", "dark"];
export const surfaces = ["--ds-surface", "--ds-surface-raised"];
export const textRoles = [
  "--ds-ink",
  "--ds-ink-muted",
  "--ds-ink-faint",
  "--ds-accent-ink",
  "--ds-ok",
  "--ds-warn",
  "--ds-err",
];
export const seriesRoles = Array.from(
  { length: 5 },
  (_, index) => `--ds-series-${index + 1}`,
);
export const graphicRoles = [
  "--ds-focus",
  "--ds-line-strong",
  "--ds-ok",
  "--ds-warn",
  "--ds-err",
  ...seriesRoles,
];
export const contrastRoles = [...new Set([...textRoles, ...graphicRoles])];

export function boundary(scheme) {
  return `[data-ds-theme="technical-drawing-${scheme}"][data-ds-theme]`;
}

// Accept only flat declaration blocks. Reject leftover syntax rather than
// silently missing an at-rule, nested selector or malformed declaration.
export function parseRules(css) {
  const source = css.replace(/\/\*[\s\S]*?\*\//g, "");
  const pattern = /([^{}]+)\{([^{}]*)\}/g;
  const rules = [];
  let end = 0;
  for (const match of source.matchAll(pattern)) {
    if (source.slice(end, match.index).trim()) {
      throw new Error("Unsupported stylesheet syntax");
    }
    const declarations = {};
    for (const declaration of match[2].split(";")) {
      if (!declaration.trim()) continue;
      const colon = declaration.indexOf(":");
      if (colon < 1) throw new Error("Invalid declaration");
      const name = declaration.slice(0, colon).trim();
      const value = declaration
        .slice(colon + 1)
        .trim()
        .replace(/\s+/g, " ");
      if (!value || Object.hasOwn(declarations, name)) {
        throw new Error(`Empty or duplicate declaration: ${name}`);
      }
      declarations[name] = value;
    }
    rules.push({ selector: match[1].trim(), declarations });
    end = match.index + match[0].length;
  }
  if (source.slice(end).trim()) {
    throw new Error("Unsupported stylesheet syntax");
  }
  return rules;
}

export function rolesFor(css, selector) {
  const rule = parseRules(css).find((entry) => entry.selector === selector);
  if (!rule) throw new Error(`Missing boundary: ${selector}`);
  return Object.fromEntries(
    Object.entries(rule.declarations).filter(([name]) =>
      name.startsWith("--ds-"),
    ),
  );
}

// WCAG 2.x sRGB relative luminance, using opaque six-digit colours only.
export function luminance(hex) {
  if (!/^#[0-9a-f]{6}$/i.test(hex)) {
    throw new Error(`Expected opaque #rrggbb colour: ${hex}`);
  }
  const linear = [1, 3, 5].map((offset) => {
    const channel = Number.parseInt(hex.slice(offset, offset + 2), 16) / 255;
    return channel <= 0.04045
      ? channel / 12.92
      : ((channel + 0.055) / 1.055) ** 2.4;
  });
  return linear[0] * 0.2126 + linear[1] * 0.7152 + linear[2] * 0.0722;
}

export function contrast(foreground, background) {
  const a = luminance(foreground);
  const b = luminance(background);
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}

// Exact README rows, light sheet / light raised / dark sheet / dark raised.
export function contrastRows(css) {
  const themes = schemes.map((scheme) => rolesFor(css, boundary(scheme)));
  const rows = contrastRoles.map((role) => {
    const ratios = themes.flatMap((theme) =>
      surfaces.map((surface) =>
        contrast(theme[role], theme[surface]).toFixed(2),
      ),
    );
    return `| \`${role}\` | ${ratios.join(" | ")} |`;
  });
  const accent = themes.map((theme) =>
    contrast(theme["--ds-on-accent"], theme["--ds-accent"]).toFixed(2),
  );
  rows.push(
    `| \`--ds-on-accent\` on \`--ds-accent\` | ${accent[0]} | ${accent[0]} | ${accent[1]} | ${accent[1]} |`,
  );
  return rows;
}
