/**
 * The technical-drawing sheet as an Astryx theme: square corners, hairline
 * rules, no shadows, near-instant motion. Colour values are the `--ds-*` roles
 * in projects/design-system/tokens/technical-drawing.css, light and dark.
 *
 * This is the authoring source. `pnpm theme` compiles it to `sheet.css` and
 * `sheet.js`, which the app imports. Rebuild and commit both after an edit.
 */
import { defineTheme } from "@astryxdesign/core/theme";
import { neutralTheme } from "@astryxdesign/theme-neutral";

const sans = '"Avenir Next", "Segoe UI", system-ui, sans-serif';

export const sheetTheme = defineTheme({
  name: "sheet",
  extends: neutralTheme,
  typography: {
    scale: { base: 16, ratio: 1.2 },
    body: { family: "Schibsted Grotesk", fallbacks: sans },
    // Grimoire's display face: a system serif stack, so no font file ships.
    heading: {
      family: "Iowan Old Style",
      fallbacks: '"Palatino Linotype", Palatino, Georgia, serif',
      weight: "semibold",
    },
    code: {
      family: "ui-monospace",
      fallbacks: '"SF Mono", "Cascadia Mono", monospace',
    },
  },
  radius: { base: 4, multiplier: 0 },
  motion: { fast: 60, medium: 100, slow: 240, ratio: 0.75 },
  tokens: {
    "--color-accent": ["#1f4f9f", "#d3a45f"],
    "--color-on-accent": ["#ffffff", "#181a20"],
    "--color-text-accent": ["#1f4f9f", "#d3a45f"],
    "--color-icon-accent": ["#1f4f9f", "#d3a45f"],
    "--color-background-body": ["#ffffff", "#181a20"],
    "--color-background-surface": ["#ffffff", "#181a20"],
    "--color-background-card": ["#ffffff", "#181a20"],
    "--color-background-popover": ["#ffffff", "#1c1e26"],
    "--color-background-muted": ["#f5f6f8", "#1c1e26"],
    "--color-text-primary": ["#26231c", "#e8e5dc"],
    "--color-text-secondary": ["#625d50", "#a5a297"],
    "--color-icon-primary": ["#26231c", "#e8e5dc"],
    "--color-icon-secondary": ["#625d50", "#a5a297"],
    "--color-border": ["#d5d7dc", "#393b43"],
    "--color-border-emphasized": ["#77766f", "#82847e"],
    "--color-success": ["#0b7f47", "#6fbf8d"],
    "--color-text-success": ["#0b7f47", "#6fbf8d"],
    "--color-warning": ["#8a5700", "#d0a050"],
    "--color-text-warning": ["#8a5700", "#d0a050"],
    "--color-error": ["#b42328", "#ff8377"],
    "--color-text-error": ["#b42328", "#ff8377"],
    "--focus-outline-color": ["#1f4f9f", "#d3a45f"],
    "--focus-outline-width": "2px",
    "--shadow-low": "none",
    "--shadow-med": "none",
    "--shadow-high": "none",
  },
});
