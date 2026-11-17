import {
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

// Every colour on the site comes from these tokens, so checking token pairs
// catches contrast regressions before merge, with no browser. The Kargo dev
// stage still runs axe in a real browser for anything tokens cannot show.

type Mode = "light" | "dark";
type Colour = Record<Mode, string>;

const HEX = "#(?:[0-9a-f]{3}|[0-9a-f]{6})";

/** "#abc" → "#aabbcc"; "#aabbcc" unchanged. */
function longHex(hex: string): string {
  const digits = hex.slice(1).toLowerCase();
  return digits.length === 3
    ? `#${[...digits].map((d) => d + d).join("")}`
    : `#${digits}`;
}

/**
 * The light and dark values of a colour, or undefined when the value is in a
 * form this check cannot read (rgb(), a named colour, var(), and so on).
 */
export function colour(value: string): Colour | undefined {
  const pair = value.match(
    new RegExp(`^light-dark\\(\\s*(${HEX})\\s*,\\s*(${HEX})\\s*\\)$`, "i"),
  );
  if (pair) return { light: longHex(pair[1]!), dark: longHex(pair[2]!) };
  const single = value.match(new RegExp(`^(${HEX})$`, "i"));
  if (single) return { light: longHex(single[1]!), dark: longHex(single[1]!) };
  return undefined;
}

/**
 * Every custom property declared in a stylesheet, whatever its value. A
 * declaration ends at `;` or at the block's closing brace.
 */
export function declarations(css: string): [string, string][] {
  return [...css.matchAll(/(--[a-z0-9-]+)\s*:\s*([^;}]+)/gi)].map(
    ([, name, value]) => [name!, value!.trim()],
  );
}

export function customProperties(css: string): Map<string, string> {
  const found = new Map<string, string>();
  for (const [, name, value] of css.matchAll(
    /(--[a-z0-9-]+)\s*:\s*([^;}]+)/gi,
  )) {
    found.set(name!, value!.trim());
  }
  return found;
}

/** Every stylesheet under a directory, at any depth. */
export function stylesheets(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return stylesheets(path);
    return entry.name.endsWith(".css") ? [path] : [];
  });
}

// CSS named colour keywords (CSS Color 4), which a font or other token could
// otherwise carry.
const NAMED_COLOURS = new Set(
  `
  aliceblue antiquewhite aqua aquamarine azure beige bisque black
  blanchedalmond blue blueviolet brown burlywood cadetblue chartreuse
  chocolate coral cornflowerblue cornsilk crimson cyan darkblue darkcyan
  darkgoldenrod darkgray darkgreen darkgrey darkkhaki darkmagenta
  darkolivegreen darkorange darkorchid darkred darksalmon darkseagreen
  darkslateblue darkslategray darkslategrey darkturquoise darkviolet deeppink
  deepskyblue dimgray dimgrey dodgerblue firebrick floralwhite forestgreen
  fuchsia gainsboro ghostwhite gold goldenrod gray green greenyellow grey
  honeydew hotpink indianred indigo ivory khaki lavender lavenderblush
  lawngreen lemonchiffon lightblue lightcoral lightcyan lightgoldenrodyellow
  lightgray lightgreen lightgrey lightpink lightsalmon lightseagreen
  lightskyblue lightslategray lightslategrey lightsteelblue lightyellow lime
  limegreen linen magenta maroon mediumaquamarine mediumblue mediumorchid
  mediumpurple mediumseagreen mediumslateblue mediumspringgreen
  mediumturquoise mediumvioletred midnightblue mintcream mistyrose moccasin
  navajowhite navy oldlace olive olivedrab orange orangered orchid
  palegoldenrod palegreen paleturquoise palevioletred papayawhip peachpuff
  peru pink plum powderblue purple rebeccapurple red rosybrown royalblue
  saddlebrown salmon sandybrown seagreen seashell sienna silver skyblue
  slateblue slategray slategrey snow springgreen steelblue tan teal thistle
  tomato turquoise violet wheat white whitesmoke yellow yellowgreen
  transparent currentcolor
  `
    .trim()
    .split(/\s+/),
);

function luminance(hex: string): number {
  const [r, g, b] = [1, 3, 5].map((i) => {
    const channel = parseInt(hex.slice(i, i + 2), 16) / 255;
    return channel <= 0.03928
      ? channel / 12.92
      : ((channel + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r! + 0.7152 * g! + 0.0722 * b!;
}

export function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi! + 0.05) / (lo! + 0.05);
}

const paletteCss = readFileSync("src/palette.css", "utf8");
const declared = customProperties(paletteCss);

// Every colour token has one role, and each role has its WCAG minimum:
// text 4.5:1 (1.4.3) and non-text UI 3:1 (1.4.11) on every background.
// `on-accent` text sits on the accent fill instead. Decorative borders, such
// as the dividers between list items, have no minimum under 1.4.11.
type ColourRole =
  "text" | "on-accent" | "non-text" | "background" | "decorative";
// Non-colour tokens have a role too, with a strict syntax that no colour
// matches, so a colour cannot hide as one.
type OtherRole = "font" | "radius" | "shadow";
type Role = ColourRole | OtherRole;

const SYNTAX: Record<OtherRole, RegExp> = {
  font: /^(?:"[^"#()]+"|[a-z][a-z0-9-]*)(?:\s*,\s*(?:"[^"#()]+"|[a-z][a-z0-9-]*))*$/i,
  radius: /^(?:0|\d+(?:\.\d+)?(?:px|rem|em|%))$/,
  shadow: /^none$/,
};
const isOther = (role: Role): role is OtherRole => role in SYNTAX;

/** A non-colour token's value matches its syntax and names no colour. */
function validOther(role: OtherRole, value: string): boolean {
  const words = value.toLowerCase().match(/[a-z][a-z0-9-]*/g) ?? [];
  return SYNTAX[role].test(value) && !words.some((w) => NAMED_COLOURS.has(w));
}

const ROLES: Record<string, Role> = {
  "--color-accent": "non-text",
  "--color-on-accent": "on-accent",
  "--color-text-accent": "text",
  "--color-icon-accent": "non-text",
  "--color-background-body": "background",
  "--color-background-surface": "background",
  "--color-background-card": "background",
  "--color-background-muted": "background",
  "--color-text-primary": "text",
  "--color-text-secondary": "text",
  "--color-border": "decorative",
  "--color-border-emphasized": "non-text",
  "--focus-outline-color": "non-text",
  "--font-family-body": "font",
  "--font-family-heading": "font",
  "--radius-inner": "radius",
  "--radius-element": "radius",
  "--radius-container": "radius",
  "--radius-page": "radius",
  "--shadow-low": "shadow",
  "--shadow-med": "shadow",
  "--shadow-high": "shadow",
};

const palette = new Map(
  [...declared]
    .filter(([name]) => !isOther(ROLES[name] ?? "text"))
    .map(([name, value]) => [name, colour(value)] as const),
);

const withRole = (role: ColourRole) =>
  Object.keys(ROLES).filter((token) => ROLES[token] === role);
const backgrounds = withRole("background");

const PAIRS: [string, string, number][] = [
  ...withRole("text").flatMap((token) =>
    backgrounds.map((bg): [string, string, number] => [token, bg, 4.5]),
  ),
  ...withRole("non-text").flatMap((token) =>
    backgrounds.map((bg): [string, string, number] => [token, bg, 3]),
  ),
  ...withRole("on-accent").map((token): [string, string, number] => [
    token,
    "--color-accent",
    4.5,
  ]),
];

describe("token contrast", () => {
  it("declares each custom property in palette.css once", () => {
    // A second declaration under another selector could carry an unchecked
    // value while the check sees only the last one.
    const names = declarations(paletteCss).map(([name]) => name);
    expect(names.filter((name, i) => names.indexOf(name) !== i)).toEqual([]);
  });

  it("gives every custom property in palette.css exactly one role", () => {
    expect([...declared.keys()].sort()).toEqual(Object.keys(ROLES).sort());
  });

  it("can read each colour token's light and dark value", () => {
    for (const [name, value] of declared) {
      const role = ROLES[name]!;
      if (isOther(role)) {
        expect(validOther(role, value), `${name} (${role}): ${value}`).toBe(
          true,
        );
      } else {
        expect(colour(value), `${name}: ${value}`).toBeDefined();
      }
    }
  });

  it("finds every custom property, so an unlisted colour fails the role check", () => {
    const css = `--warning: #fff; --brand: rgb(0 0 0); --radius-page: 0;
      :root { --last: #000 }`;
    expect([...customProperties(css).keys()]).toEqual([
      "--warning",
      "--brand",
      "--radius-page",
      "--last",
    ]);
    expect(validOther("font", '"Avenir Next", system-ui, sans-serif')).toBe(
      true,
    );
    expect(validOther("font", "red")).toBe(false);
    expect(validOther("font", '"Iowan", rebeccapurple')).toBe(false);
    expect(colour("#fff")).toEqual({ light: "#ffffff", dark: "#ffffff" });
    expect(colour("rgb(0 0 0)")).toBeUndefined();
    for (const role of ["font", "radius", "shadow"] as const) {
      expect("#fff").not.toMatch(SYNTAX[role]);
      expect("rgb(0 0 0)").not.toMatch(SYNTAX[role]);
    }
  });

  it("serves no stylesheets, scripts, SVG or HTML from public/", () => {
    // public/ is copied into the site unchecked, so anything that can carry
    // colour or code must live under src, where the checks run.
    const files = (dir: string): string[] =>
      readdirSync(dir, { withFileTypes: true }).flatMap((entry) =>
        entry.isDirectory()
          ? files(join(dir, entry.name))
          : [join(dir, entry.name)],
      );
    expect(
      files("public").filter((path) => /\.(?:css|m?js|svg|html?)$/i.test(path)),
    ).toEqual([]);
  });

  it("finds stylesheets at any depth", () => {
    const dir = mkdtempSync(join(tmpdir(), "sheets-"));
    mkdirSync(join(dir, "components"));
    writeFileSync(join(dir, "top.css"), "a {}");
    writeFileSync(join(dir, "components", "card.css"), "b {}");
    expect(stylesheets(dir).sort()).toEqual(
      [join(dir, "components", "card.css"), join(dir, "top.css")].sort(),
    );
  });

  it("keeps colours and inline styles out of the TypeScript sources", () => {
    const LITERAL =
      /#[0-9a-f]{3,8}\b|\b(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color|color-mix|light-dark)\(/i;
    const sources = (dir: string): string[] =>
      readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
        const path = join(dir, entry.name);
        if (entry.isDirectory()) return sources(path);
        return /\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)
          ? [path]
          : [];
      });
    for (const file of sources("src")) {
      const code = readFileSync(file, "utf8");
      expect(code, file).not.toMatch(LITERAL);
      expect(code, file).not.toMatch(/@stylexjs\/stylex|\bstyle\s*=\s*\{/);
    }
  });

  it("keeps colours out of every other stylesheet", () => {
    const LITERAL =
      /#[0-9a-f]{3,8}\b|\b(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color|color-mix|light-dark)\(/i;
    const COLOUR_PROPERTY =
      /(?:^|[;{\s])((?:background|border|outline|text-decoration|column-rule)(?:-[a-z]+)*|color|fill|stroke|caret-color|accent-color|box-shadow|text-shadow)\s*:\s*([^;}]+)/gi;
    const ALLOWED = new Set([
      "inherit",
      "initial",
      "unset",
      "currentcolor",
      "transparent",
      "none",
      "solid",
      "dashed",
      "dotted",
      "double",
    ]);
    const sheets = stylesheets("src").filter(
      (path) => path !== join("src", "palette.css"),
    );
    expect(sheets).toContain(join("src", "site.css"));
    for (const sheet of sheets) {
      const css = readFileSync(sheet, "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
      expect([...customProperties(css).keys()], sheet).toEqual([]);
      expect(css, sheet).not.toMatch(LITERAL);
      // A colour keyword can hide in any property, such as
      // filter: drop-shadow(0 0 2px red), not only in colour properties.
      for (const [, value] of css.matchAll(/[a-z-]+\s*:\s*([^;{}]+)/gi)) {
        const keywords = (
          value!.toLowerCase().match(/[a-z][a-z-]*/g) ?? []
        ).filter(
          (word) =>
            NAMED_COLOURS.has(word) &&
            word !== "transparent" &&
            word !== "currentcolor",
        );
        expect(keywords, `${sheet}: ${value!.trim()}`).toEqual([]);
      }
      for (const [, property, value] of css.matchAll(COLOUR_PROPERTY)) {
        // var(--token, fallback): the token must exist in palette.css, and the
        // fallback is checked like any other value, because the browser can
        // use it.
        let rest = value!;
        for (const [whole, token, fallback] of value!.matchAll(
          /var\(\s*(--[a-z0-9-]+)\s*(?:,([^()]*))?\)/gi,
        )) {
          expect(declared.has(token!), `${sheet}: ${property}: ${token}`).toBe(
            true,
          );
          rest = rest.replace(whole, ` ${fallback ?? ""} `);
        }
        const words = rest
          .split(/[\s,]+/)
          .filter((word) => word && !/^-?\d/.test(word));
        for (const word of words) {
          expect(
            ALLOWED.has(word.toLowerCase()),
            `${sheet}: ${property}: ${word}`,
          ).toBe(true);
        }
      }
    }
  });

  it("computes WCAG ratios", () => {
    expect(contrast("#000000", "#ffffff")).toBeCloseTo(21, 5);
    expect(contrast("#777777", "#ffffff")).toBeCloseTo(4.48, 2);
  });

  for (const mode of ["light", "dark"] as const) {
    it.each(PAIRS)(`${mode}: %s on %s is at least %d:1`, (fg, bg, min) => {
      const foreground = palette.get(fg)?.[mode];
      const background = palette.get(bg)?.[mode];
      expect(foreground, fg).toBeDefined();
      expect(background, bg).toBeDefined();
      expect(contrast(foreground!, background!)).toBeGreaterThanOrEqual(min);
    });
  }
});
