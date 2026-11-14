/**
 * House rules for this app, enforced on the source text. They keep the hard
 * parts (overlays, comboboxes, announcements, navigation, data access) inside
 * Astryx and the shared app modules, where they are tested once. A failure
 * names the file, the rule and what to use instead.
 */
import { readdirSync, readFileSync } from "node:fs";
import { join, relative } from "node:path";
import { describe, expect, it } from "vitest";

const root = join(import.meta.dirname, "..");

function files(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return files(path);
    return /\.(tsx?|css)$/.test(entry.name) ? [path] : [];
  });
}

const sources = [...files(join(root, "src")), ...files(join(root, "preview")), ...files(join(root, "factory"))]
  .map((path) => relative(root, path))
  .filter(
    (path) =>
      !/\.test\.tsx?$/.test(path) &&
      !path.startsWith("src/test/") &&
      !path.startsWith("src/theme/") &&
      path !== "src/api/schema.ts",
  );

interface Rule {
  name: string;
  pattern: RegExp;
  instead: string;
  /** Files where the pattern is the point. Keep this list short. */
  allow?: string[];
}

const rules: Rule[] = [
  {
    name: "no hand-built overlay or listbox roles",
    pattern:
      /role=["'](dialog|alertdialog|listbox|combobox|menu|menuitem|tooltip|tab)["']/,
    instead:
      "Dialog, AlertDialog, Drawer, Popover, Selector, Typeahead, DropdownMenu, Tooltip or TabList from Astryx",
  },
  {
    name: "no ad hoc live regions",
    pattern: /aria-live/,
    instead: "useToast() for outcomes, useAnnounce() for anything else",
  },
  {
    name: "no raw HTML injection",
    pattern: /dangerouslySetInnerHTML|\.innerHTML\s*=/,
    instead:
      "render text as text; src/handouts/HandoutText.tsx turns handout markup into elements without injecting any",
  },
  {
    name: "no raw interactive elements",
    pattern: /<(button|input|select|textarea|dialog|details|summary)[\s>]/,
    instead:
      "Button, TextInput, TextArea, Selector, CheckboxInput, Dialog or Collapsible from Astryx",
  },
  {
    name: "no raw anchors",
    pattern: /<a[\s>]/,
    instead:
      "Link or Button href from Astryx, with a path from src/app/paths.ts",
    allow: ["src/app/RouterLink.tsx"],
  },
  {
    name: "no fetch outside the API client",
    pattern: /\bfetch\(/,
    instead: "api() from src/api/client.ts, wrapped in a module under src/api",
    allow: ["src/api/client.ts"],
  },
  {
    name: "no hardcoded colours",
    pattern: /#[0-9a-fA-F]{3,8}\b|\brgba?\(/,
    instead: "a theme token such as var(--color-text-secondary)",
  },
  {
    name: "no inline styles",
    pattern: /\sstyle=\{\{/,
    instead: "component props (gap, padding, width) or a class in a .css file",
  },
  {
    name: "no browser storage outside the modules that own it",
    pattern: /\b(localStorage|sessionStorage)\b/,
    instead:
      "src/lib/session-expiry.js for the kept draft; read-aloud settings go through src/lib/read-aloud.js",
    // Passes window.localStorage to lib/read-aloud.js, which does the reading.
    allow: ["src/voices/ReadAloud.tsx"],
  },
];

describe("house rules", () => {
  it("has sources to check", () => {
    expect(sources.length).toBeGreaterThan(10);
  });

  for (const rule of rules) {
    it(rule.name, () => {
      const offenders = sources
        .filter((path) => !rule.allow?.includes(path))
        .flatMap((path) =>
          readFileSync(join(root, path), "utf8")
            .split("\n")
            .map((line, index) => ({ line, at: `${path}:${index + 1}` }))
            // Comments may describe a banned pattern.
            .filter(({ line }) => !/^\s*(\/\/|\*|\/\*)/.test(line))
            .filter(({ line }) => rule.pattern.test(line))
            .map(({ at }) => at),
        );
      expect(offenders, `Use ${rule.instead}.`).toEqual([]);
    });
  }
});
