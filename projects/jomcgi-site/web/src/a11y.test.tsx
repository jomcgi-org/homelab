import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { render } from "@testing-library/react";
import axe from "axe-core";
import { describe, expect, it, vi } from "vitest";
import { Theme } from "@astryxdesign/core/theme";
import { neutralTheme } from "@astryxdesign/theme-neutral/built";
import { mount } from "./mount";
import { PAGES } from "./pages";

// Entry scripts call mount(); recording the call shows which page each mounts.
vi.mock("./mount", () => ({ mount: vi.fn() }));

/** Every HTML entry in the app, at any depth, outside build and package dirs. */
function htmlEntries(dir = ".", prefix = ""): string[] {
  return readdirSync(join(dir, prefix), { withFileTypes: true }).flatMap(
    (entry) => {
      const path = prefix ? `${prefix}/${entry.name}` : entry.name;
      if (entry.isDirectory()) {
        return ["dist", "node_modules"].includes(entry.name)
          ? []
          : htmlEntries(dir, path);
      }
      return entry.name.endsWith(".html") ? [path] : [];
    },
  );
}

describe("page registry", () => {
  it("registers every HTML entry, and nothing else", () => {
    // public/ is copied into the site as is, so a page there would skip both
    // the registry and axe. It may hold no HTML.
    expect(htmlEntries().sort()).toEqual(Object.keys(PAGES).sort());
  });

  it.each(Object.keys(PAGES))(
    "%s mounts its registered page",
    async (entry) => {
      const html = readFileSync(entry, "utf8");
      const scripts = [...html.matchAll(/<script\b[^>]*\ssrc="([^"]+)"/g)].map(
        (match) => match[1]!,
      );
      expect(scripts, `${entry} must have one entry script`).toHaveLength(1);
      vi.mocked(mount).mockClear();
      await import(/* @vite-ignore */ `../${scripts[0]!.replace(/^\.\//, "")}`);
      expect(vi.mocked(mount)).toHaveBeenCalledTimes(1);
      const element = vi.mocked(mount).mock.calls[0]![0] as { type: unknown };
      expect(element.type).toBe(PAGES[entry]);
    },
  );
});

// Structural accessibility rules on every registered page. happy-dom has no
// layout, so colour contrast is left to contrast.test.tsx and the browser
// check in the Kargo dev stage.
describe.each(Object.entries(PAGES))("%s", (_entry, Page) => {
  it("has no axe violations", async () => {
    const { container } = render(
      <Theme theme={neutralTheme}>
        <Page />
      </Theme>,
    );
    const results = await axe.run(container, {
      rules: { "color-contrast": { enabled: false } },
    });
    expect(
      results.violations.map((violation) => ({
        id: violation.id,
        nodes: violation.nodes.map((node) => node.target.join(" ")),
      })),
    ).toEqual([]);
  });
});
